"""FastAPI dependencies.

Separate from `api/main.py` so `api/routes.py` can depend on the repository
without importing the app that includes it.

**This module is the only place an org id crosses from the request into the
store.** It reads one off the session principal and hands the route a `Repo`
already bound to it; the route never sees the value, no request body carries
one, and `core/store/repo.py` has no method that takes one. That is the whole
of the multi-tenancy enforcement path, and it is three lines long on purpose --
a tenancy check spread across thirteen handlers is a tenancy check with
thirteen chances to be forgotten.
"""

from __future__ import annotations

from functools import lru_cache

from core.store.repo import Repo

from api import settings
from api.auth import PrincipalDep


@lru_cache(maxsize=None)
def _repo_for(path: str) -> Repo:
    """One `Repo` -- and one SQLAlchemy engine -- per database file.

    Cached because the engine owns a connection pool: building a new one per
    request would open a new SQLite connection per request, and the 500 ms
    status poll would pay for it.

    The instance this returns is bound to the default org and is never handed
    to a route directly; `get_repo` narrows it per request with `scoped`, which
    shares this engine rather than opening a second one.
    """
    return Repo(path)


def get_repo(principal: PrincipalDep) -> Repo:
    """The repository dependency, scoped to the requesting principal's org.

    With `RECON_AUTH` disabled the principal is the single local operator and
    the org is `DEFAULT_ORG_ID`, so this resolves to exactly the repository
    every existing test and the console already talk to -- the scoping is on
    the same code path in both configurations, which is what keeps it exercised.

    Overridable in tests via `app.dependency_overrides[get_repo]`.
    """
    return _repo_for(str(settings.db_path())).scoped(principal.org_id)
