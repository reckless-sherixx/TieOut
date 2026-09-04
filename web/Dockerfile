# The console. Next.js 16, standalone output, one node process.
#
# Built from the repository root so the build context is `web/`:
#
#   docker build -f web/Dockerfile -t tieout-web web/
#
# but `docker compose up` is the supported path -- see `compose.yaml`, which
# passes the two build arguments below. Building this image without them
# produces a console that looks correct and answers from mocks.
#
# --- the build-time variable problem, stated first ----------------------------
#
# `NEXT_PUBLIC_*` is INLINED INTO THE BUNDLE AT BUILD TIME. It is not read at
# runtime, so setting it in `environment:` does nothing at all -- the value is
# already compiled into the JavaScript the browser downloads.
#
# Two of them matter, and both default to the wrong thing for a container:
#
#   NEXT_PUBLIC_API_MOCKING  defaults to "enabled" (web/lib/api.ts). A build
#     made without `disabled` serves a bundle that answers every request from
#     MSW while the API container sits idle beside it, healthy and unused. The
#     console looks perfect and every number in it is fiction. This is the one
#     failure worth reading the file for.
#
#   NEXT_PUBLIC_API_BASE  is the URL THE BROWSER uses, not the URL this
#     container uses. The browser runs on the host, outside the compose
#     network, so `http://api:8000` is unreachable from it -- that name only
#     resolves between containers. It must be the host-mapped address,
#     http://localhost:8000 by default.

# =============================================================================
# Stage 1 -- dependencies
# =============================================================================
# `npm ci`, not `npm install`: ci installs exactly `package-lock.json` and fails
# if the lockfile and manifest disagree, which is the property that makes an
# image reproducible.
FROM node:22-bookworm-slim AS deps

WORKDIR /app

# Only the two files that determine the dependency set, so this layer survives
# every source edit.
COPY package.json package-lock.json ./
RUN npm ci


# =============================================================================
# Stage 2 -- build
# =============================================================================
FROM node:22-bookworm-slim AS builder

WORKDIR /app

# Declared with the safe value as the default rather than left unset: an unset
# ARG here would silently fall through to the runtime default in
# `web/lib/api.ts`, which is "enabled", which is mocks.
ARG NEXT_PUBLIC_API_MOCKING=disabled
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_PUBLIC_API_MOCKING=${NEXT_PUBLIC_API_MOCKING} \
    NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE} \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

RUN npm run build

# Fail the BUILD rather than the demo. `next build` inlines the literal string
# into a chunk, so its presence is checkable right here -- and if it is absent,
# every number the console renders will be a mock. Discovering that during a
# recording is the expensive version of this check.
RUN set -eux; \
    if ! grep -rqF '"API_MOCKING_ENABLED",0,!1' .next/static/chunks/ 2>/dev/null \
       && ! grep -rqF 'API_MOCKING_ENABLED' .next/static/chunks/ 2>/dev/null; then \
      echo "BUILD ABORTED: could not confirm the mocking flag was inlined." >&2; \
      exit 1; \
    fi; \
    if grep -rqF '"enabled"' .next/static/chunks/*.js 2>/dev/null \
       && [ "${NEXT_PUBLIC_API_MOCKING}" != "disabled" ]; then \
      echo "BUILD ABORTED: NEXT_PUBLIC_API_MOCKING is not 'disabled'." >&2; \
      echo "This image would serve mocks. Build via 'docker compose up'." >&2; \
      exit 1; \
    fi


# =============================================================================
# Stage 3 -- runtime
# =============================================================================
FROM node:22-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# A non-root user. This process writes nothing: it serves a prebuilt bundle and
# proxies nothing, so /app can be root-owned and read-only to it.
RUN set -eux; \
    groupadd --system --gid 10002 tieout; \
    useradd --system --uid 10002 --gid tieout --home-dir /app --shell /usr/sbin/nologin tieout

WORKDIR /app

# The three pieces standalone output produces. `server.js` and its traced
# node_modules come from `standalone/`; the static assets and `public/` are NOT
# included in it and have to be copied separately, which is the step everyone
# misses and which shows up as a page with no CSS.
COPY --from=builder --chown=root:root /app/.next/standalone ./
COPY --from=builder --chown=root:root /app/.next/static ./.next/static
COPY --from=builder --chown=root:root /app/public ./public

USER tieout

EXPOSE 3000

# Liveness only. A 200 here means the node process bound the port and can
# render; it says nothing about whether the API is reachable, because the
# browser reaches the API directly and this container never calls it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "require('http').get({host:'127.0.0.1',port:process.env.PORT||3000,path:'/'},r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
