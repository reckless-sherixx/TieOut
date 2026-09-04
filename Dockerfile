# syntax=docker/dockerfile:1
#
# The reconciliation API. `uvicorn api.main:app`, one process, SQLite on a
# mounted volume.
#
#   docker build -t recon-api .
#   docker run --rm -p 8000:8000 -v recon-data:/data recon-api
#   curl -s localhost:8000/api/runs
#
# The console (`web/`) is NOT in this image. It is a Next.js app deployed by
# Vercel from the repository; see `deploy/vercel.md`. Building it here would put
# a second runtime, a second dependency tree and a second failure mode into an
# image whose only job is to answer HTTP.
#
# --- the single-instance constraint, stated up front --------------------------
#
# The store is SQLite (`core/store/repo.py`), on a file. **Run exactly one
# instance of this image.** Two containers pointed at one volume are two
# writers on one SQLite file across a network filesystem, which is the
# configuration SQLite's own documentation warns about; two containers on two
# volumes are two disjoint databases that answer differently depending on which
# one the load balancer picked. Neither failure is loud. For a demo, one
# instance is entirely sufficient -- a 500-record run completes in seconds --
# but it is a property of the deployment and not something to discover later.
# Set the platform's replica count to 1 explicitly rather than relying on a
# default; `deploy/railway.md` says where.


# =============================================================================
# Stage 1 -- dependencies
# =============================================================================
# The uv image, rather than pip, because `uv.lock` is the lockfile this project
# actually maintains and `--frozen` makes the image's dependency set verifiable
# against it. No new dependency is introduced: uv is a build-time tool and does
# not appear in the runtime stage.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# UV_COMPILE_BYTECODE_TIMEOUT is not decoration. The default is 60 seconds per
# file, and `google/genai/models.py` exceeds it on a cold builder -- the build
# then dies with a message about bytecode rather than about the package, having
# already spent five minutes downloading. It is intermittent, which is worse
# than reproducible: it passes on the retry and fails on the reviewer's machine.
#
# The compilation is worth keeping rather than switching off. The runtime stage
# sets PYTHONDONTWRITEBYTECODE and runs as a user with no write access to /app,
# so anything not compiled here is re-parsed from source on every start.
ENV UV_COMPILE_BYTECODE=1 \
    UV_COMPILE_BYTECODE_TIMEOUT=600 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the two files that determine the dependency set, so this layer stays
# cached across every source edit. Copying the source first would rebuild the
# whole dependency tree on a one-line docstring change.
COPY pyproject.toml uv.lock ./

# --frozen           fail rather than silently re-resolve. The image gets the
#                    dependency set `uv.lock` names, or the build stops.
# --no-install-project  the project itself is copied as source in stage 2.
#                    `[tool.hatch.build.targets.wheel]` packages only `core/`,
#                    so an installed wheel would carry the engine and not
#                    `api/` -- half an image, failing at import.
# --no-dev           pytest, hypothesis and httpx are test dependencies. They
#                    are not in the runtime image and the tests are not either.
RUN uv sync --frozen --no-install-project --no-dev


# =============================================================================
# Stage 2 -- runtime
# =============================================================================
# `python:3.12-slim-bookworm`, not the uv image: uv has done its work and does
# not need to ship. `pyproject.toml` requires >=3.12; 3.12 is pinned rather than
# floated so a new minor release does not change the runtime under a deployment
# nobody is watching.
FROM python:3.12-slim-bookworm AS runtime

# PYTHONDONTWRITEBYTECODE -- the .pyc files are already compiled in stage 1, and
#   a non-root user cannot write new ones into /app anyway.
# PYTHONUNBUFFERED -- otherwise uvicorn's startup lines sit in a pipe buffer and
#   the platform's log view looks like a container that never booted.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

# --- the writable state, and where it lives ----------------------------------
# Every default in `api/settings.py` is a path under `out/`, relative to the
# process's working directory. In a container that would be a path inside the
# image's writable layer: it survives a restart and is destroyed by a redeploy,
# which is the worst of both. These three point it at /data instead, which is
# where the volume mounts.
ENV RECON_DB_PATH=/data/recon.db \
    RECON_DATASETS_DIR=/data/datasets \
    RECON_UPLOADS_DIR=/data/uploads

# --- one font, and why an image that answers HTTP needs one -------------------
#
# `report/build.py` renders every amount through `core/money.fmt_inr`, which
# emits U+20B9, the rupee sign. **No base-14 PDF font encodes it**, so reportlab
# falls back to Helvetica and each amount renders as a black box -- U+25A0 --
# in a document whose entire subject is money. `report/fonts.py` looks for a
# system TTF and verifies the glyph against the font's own cmap, but a
# `-slim` image ships no fonts at all, so there is nothing for it to find.
#
# Verified on 2026-09-04: without this the containerised report contained no
# U+20B9 and did contain U+25A0. The host, which has Arial, was fine -- which is
# exactly why this had to be checked in the container rather than reasoned about.
#
# DejaVu rather than Noto: ~1.5 MB against ~25 MB, and it has carried U+20B9
# since 2.34. `fonts-dejavu-core` is the smallest package that contains it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends fonts-dejavu-core; \
    rm -rf /var/lib/apt/lists/*; \
    test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf

ENV RECON_REPORT_FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    RECON_REPORT_FONT_BOLD=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

# A non-root user. The API writes to exactly one place -- /data -- so /app can
# be owned by root and read-only to the process, which means a compromised
# request handler cannot rewrite the code it is running.
RUN set -eux; \
    groupadd --system --gid 10001 recon; \
    useradd --system --uid 10001 --gid recon --home-dir /app --shell /usr/sbin/nologin recon; \
    mkdir -p /data; \
    chown recon:recon /data

WORKDIR /app

# The virtualenv from stage 1. Owned by root, readable by all: the runtime user
# needs to import from it, not to write to it.
COPY --from=builder --chown=root:root /app/.venv /app/.venv

# The source. Named explicitly rather than `COPY . .` -- an explicit list
# cannot quietly start shipping a new top-level directory, and `.dockerignore`
# is then the second line of defence rather than the only one.
#
#   api/      the ASGI app, the routes, the settings, the contract
#   core/     the engine: matcher, generator, adapters, store, llm, itc, drift
#   scorer/   the metrics, imported by `api/jobs.py`
#   report/   the PDF renderer, imported by `GET /api/runs/{id}/report.pdf`
#
# The explicit list is what caught `report/` going missing: it was added to the
# repository after this file was written, `uv sync` installed reportlab so the
# import looked satisfiable, and the route answered 500 with
# `ModuleNotFoundError: No module named 'report'` -- in the container only, on a
# path no test exercises. `COPY . .` would have hidden that by shipping
# everything, including whatever lands in the repository next.
COPY --chown=root:root api/ /app/api/
COPY --chown=root:root core/ /app/core/
COPY --chown=root:root scorer/ /app/scorer/
COPY --chown=root:root report/ /app/report/
COPY --chown=root:root pyproject.toml uv.lock /app/

USER recon

# The volume. Declared so that `docker run` without `-v` still gets an
# anonymous volume rather than writing the database into the container's
# writable layer -- and so `docker inspect` names the path a deployer has to
# provision.
VOLUME ["/data"]

EXPOSE 8000

# --- health -------------------------------------------------------------------
# **`/docs` is used because this API has no health endpoint.** Every route in
# `api/routes.py` and `api/auth.py` is under `/api/`; there is no `/health`,
# `/healthz` or `/livez`. Adding one is a source change and this is not the lane
# that owns source, so the check uses FastAPI's own `/docs`, which:
#
#   * is served by the same ASGI app, so a 200 means the app imported, the
#     settings resolved and the event loop is running;
#   * is NOT under `/api/`, so it does not trip the audit-on-read middleware in
#     `api/main.py`. That matters more than it looks: the middleware writes an
#     `access_log` row for every GET under `/api/` and *fails closed* if the
#     write fails, so a health check pointed at `/api/runs` would write a row to
#     the database every 30 seconds forever and would report unhealthy on a
#     full disk rather than on a broken app.
#
# What it therefore does NOT prove: that the database is reachable or writable.
# A real health endpoint would touch the store. This one is a liveness check,
# and calling it a readiness check would be overclaiming.
#
# Shell form on purpose -- ${PORT:-8000} has to expand, and exec form does not
# run a shell. `urllib` rather than curl, so the runtime image needs no extra
# package installed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/docs', timeout=4).read(1)" || exit 1

# --- start --------------------------------------------------------------------
# `${PORT:-8000}`: Railway injects `PORT` and expects the process to bind it;
# Fly.io does not, and its `internal_port` is set in `fly.toml` instead. The
# default covers `docker run` and Fly; the expansion covers Railway. `exec` so
# uvicorn is PID 1 and receives the platform's SIGTERM directly -- without it
# the shell holds PID 1, swallows the signal, and every deploy ends in a 10 s
# timeout followed by SIGKILL.
#
# One worker, deliberately. See the SQLite note at the top of this file: extra
# workers are extra writers on one file, and `--workers 2` here would be the
# same bug as two replicas.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
