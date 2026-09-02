"""The ASGI app: `python -m uv run uvicorn api.main:app --reload --port 8000`.

**CORS is owned here, and it is not optional.** No other lane owns it: Lane D
tests through `TestClient`, which never issues a cross-origin request, and Lane
E tests against MSW, which never leaves the browser's own origin. The first time
MSW is switched off, Lane E's page at http://localhost:3000 fetches
http://localhost:8000 and the browser blocks it -- a live bug on the day of
wiring, with no owner. Mounting the middleware here is what prevents that, and
because no test in this repo can exercise a real preflight, it is verified once
by hand with curl (see `LANE-D-REPORT.md`).

The origins come from `RECON_CORS_ORIGINS` so a deployed origin can be added
without a code change, and never from a wildcard.

**Audit-on-read is owned here too, and for the same reason CORS is:** it is a
property of the boundary, not of any handler. A per-handler call would be
thirteen chances to forget and would leave the fourteenth endpoint silently
unlogged; a middleware logs the route that ran, whatever it was. It is also
where the clock is -- `core/` may not read one -- so the timestamp is stamped
here and handed down, exactly as `created_at` is.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.store.repo import AccessRecord, SubjectNotFound

from api import auth, settings
from api.auth import router as auth_router
from api.connections import router as connections_router
from api.connectors import router as connectors_router
from api.deps import get_repo
from api.jobs import utc_now
from api.routes import router

#: Paths that are NOT reads of financial data and are therefore not audited.
#: The two auth operations: a login is not a read of anybody's ledger, and
#: logging one under the org it just issued would be recording the session
#: rather than the access.
UNAUDITED_PREFIX = "/api/auth/"

#: The actor recorded for a read that carried no usable session. Not a
#: blank and not the single-user name: "somebody unauthenticated asked for
#: this" is a different fact from "the local operator asked for this", and
#: on an enabled deployment it is the more interesting one.
ANONYMOUS_SUBJECT = "anonymous"


def create_app() -> FastAPI:
    """Build the app. A factory so tests can rebuild it under a changed
    environment without reimporting the module."""
    app = FastAPI(
        title="Multi-Source Reconciliation API",
        version="0.1.0",
        description="Implements api/openapi.yaml.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        # Credentials are allowed **only** when there are credentials to
        # exchange. With `RECON_AUTH` disabled this stays `False`, which is
        # exactly today's behaviour and the reason the console's existing
        # preflights are unaffected; with auth enabled the session cookie has
        # to survive a cross-origin fetch from the console's dev origin, and a
        # cookie the browser refuses to send is a login that appears to work
        # and then 401s on the next request.
        #
        # This is safe here and would not be with a wildcard origin: the
        # browser refuses `Access-Control-Allow-Origin: *` together with
        # credentials, and `cors_origins()` already refuses a wildcard outright
        # -- so the two rules reinforce each other rather than depending on the
        # operator noticing.
        allow_credentials=settings.auth_enabled(),
        # Exactly what the operations need. OPTIONS is the preflight itself.
        allow_methods=["GET", "POST", "OPTIONS"],
        # `Content-Type` is what makes a JSON POST non-simple and triggers the
        # preflight in the first place.
        allow_headers=["Content-Type", "Accept"],
    )

    @app.middleware("http")
    async def _audit_reads(request: Request, call_next):
        """One `access_log` row per read of financial data.

        Four decisions worth stating, because each has a plausible opposite:

        * **Every GET under `/api/`, not a curated list.** A hand-maintained set
          of "the sensitive endpoints" is a set that a new endpoint is not in.

        * **Refused reads are logged too**, with their status. A 404 sweep
          across run ids is what enumeration looks like from the inside, and a
          log that recorded only successful reads would be blind to precisely
          the behaviour it exists to catch. That is also why the actor for an
          unauthenticated request is recorded as `anonymous` rather than the
          row being skipped.

        * **The resource is the route template, not the URL.** `/api/runs/{id}`
          groups; `/api/runs/run-9f3c.../` does not, and the identifier is in
          its own column anyway.

        * **It fails closed.** If the row cannot be written the request fails.
          A read that is served but not logged is exactly the outcome an
          audit-on-read control exists to prevent, and a control that degrades
          quietly under load is one nobody can testify to. The engine is
          configured with a 30-second busy timeout, so the realistic contention
          case waits rather than raising.
        """
        response = await call_next(request)
        if request.method != "GET" or not request.url.path.startswith("/api/"):
            return response
        if request.url.path.startswith(UNAUDITED_PREFIX):
            return response

        principal = _actor_for(request)
        route = request.scope.get("route")
        resource = getattr(route, "path", None) or request.url.path
        params = request.scope.get("path_params") or {}
        get_repo(principal).record_access(
            [
                AccessRecord(
                    actor=principal.subject,
                    resource=resource,
                    resource_id=params.get("id"),
                    action=request.method,
                    at=utc_now().isoformat(),
                    status=response.status_code,
                )
            ]
        )
        return response

    @app.exception_handler(SubjectNotFound)
    def _subject_not_found(request: Request, exc: SubjectNotFound) -> JSONResponse:
        """A subject that cannot be joined is a bug, and it says so.

        Not a 404: the exception exists. Not a null on the wire either -- the
        contract makes `subject` required. A 500 with the offending ids is the
        legible failure.
        """
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    # Auth first: its two operations are the ones that must be reachable
    # without a session, and they are on a router with no dependency.
    app.include_router(auth_router)
    app.include_router(connectors_router)
    app.include_router(connections_router)
    app.include_router(router)
    return app


def _actor_for(request: Request) -> auth.Principal:
    """Who to file this access under, for a request that may have been refused.

    `api/auth.current_principal` raises a 401 for a missing or bad cookie,
    which is right for a route and wrong here: the request has already been
    answered, and the access still happened. A refused read is recorded against
    `anonymous` in the default org, so an unauthenticated sweep leaves a trail
    instead of leaving nothing.
    """
    try:
        return auth.current_principal(request)
    except Exception:
        return auth.Principal(
            subject=ANONYMOUS_SUBJECT,
            org_id=auth.DEFAULT_ORG_ID,
            authenticated=False,
        )


app = create_app()
