# Bow Data Edge Agent — production image.
#
# Build from the REPO ROOT (the build context is the repo, not data_plane/),
# because the image has to carry two trees:
#   * data_plane/ — the agent itself, and
#   * backend/app/ — the real data-source clients the agent reuses over the
#     tunnel (design B1 Step 5 / Step 9). They are put on PYTHONPATH rather than
#     re-implemented; the check the design calls for is that importing a client
#     never drags in app.models / app.settings, which the agent's registry
#     guarantees by importing client modules directly.
#
#   docker build -f data_plane/Dockerfile -t bow/data-edge-agent .
#
# The image ships drivers for the SQL sources whose deps are in the data_plane
# lock (postgres, mysql/mariadb). Enabling another on-prem type is a matter of
# adding its driver to data_plane/pyproject.toml and rebuilding — a missing
# driver surfaces on the admin UI's Test button, which is the intended gating.

# ── builder: resolve the locked venv ────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

# uv, pinned to the same release the backend image uses.
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

# Manifests first, so the (slow) dependency layer caches independently of source.
COPY data_plane/pyproject.toml data_plane/uv.lock ./
# --no-install-project: the agent runs from source (WORKDIR), not as an installed
# wheel, so we only need its locked dependencies here. --no-dev keeps pytest out.
RUN uv sync --frozen --no-dev --no-install-project

# ── runtime ─────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# libpq for psycopg2 at runtime; tini as a real init so signals reach the agent
# and zombies are reaped. curl is deliberately NOT installed — the healthcheck
# uses the venv's own Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root. A fixed uid/gid so a mounted data volume can be pre-owned by the host.
RUN groupadd --gid 10001 bow \
    && useradd --uid 10001 --gid 10001 --home-dir /home/bow --create-home bow

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # `data_plane` root so `python -m data_edge_agent` resolves regardless of the
    # working directory (a `-w` override or a bind mount must not break it), and
    # `backend` so the reused data-source clients import (design Step 5/9).
    PYTHONPATH=/opt/bow/data_plane:/opt/bow/backend \
    # Where the credential store, its key, and the audit trail live. Mount a
    # volume here so admin-UI edits and the audit log survive a container
    # restart; the entrypoint points store/audit at it unless overridden.
    BOW_EDGE_AGENT_DATA_DIR=/data

COPY --from=builder /opt/venv /opt/venv

# Agent source, then the reused backend clients. Copied as two layers so a
# backend-client change does not bust the agent-source layer and vice versa.
COPY data_plane/data_edge_agent /opt/bow/data_plane/data_edge_agent
COPY data_plane/pyproject.toml /opt/bow/data_plane/pyproject.toml
COPY backend/app /opt/bow/backend/app

COPY data_plane/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown -R bow:bow /data /opt/bow

WORKDIR /opt/bow/data_plane
USER bow

# The admin UI (design C4). Loopback-bound inside the container; publish it only
# to the operator (e.g. `-p 127.0.0.1:9191:9191`), never to a public interface.
EXPOSE 9191

# Liveness: the admin server answers as soon as the process is up — before and
# during NATS connect — so this proves the agent is alive without conflating it
# with broker connectivity (which the agent heals on its own by reconnecting).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/status' % os.environ.get('BOW_EDGE_AGENT_ADMIN_PORT','9191'), timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
