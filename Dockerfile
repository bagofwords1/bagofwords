FROM ubuntu:24.04 AS backend-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      python3-dev \
      build-essential \
      libpq-dev \
      gcc \
      unixodbc-dev \
      libkrb5-dev \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container for the backend
WORKDIR /app/backend

# Copy only the dependency manifests first so the uv sync layer is cached
# independently of application source changes.
COPY ./backend/pyproject.toml ./backend/uv.lock ./

# Create and use a virtual environment for dependencies
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

# Install locked main deps into the venv; dev group excluded from image.
# The kerberos extra (python-gssapi) enables per-user constrained delegation
# (S4U) for on-prem SQL Server SSO; it builds against libkrb5-dev above.
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-install-project --extra kerberos

# Copy the full backend source after deps are installed
COPY ./backend /app/backend
RUN rm -f /app/backend/db/app.db

# Pre-cache tiktoken encodings for airgapped environments
RUN TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache python3 -c \
    "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"

# Bake the local embedding model (semantic file/instruction search) for
# airgapped environments — same pattern as tiktoken. Override the model with
# BOW_EMBEDDINGS_MODEL, or skip with BOW_EMBEDDINGS_ENABLED=false.
RUN BOW_EMBEDDINGS_CACHE_DIR=/opt/bow-models python3 scripts/download_embedding_model.py

# Install Playwright browser (chromium only to save space)
RUN playwright install chromium --with-deps

FROM rust:1-slim-bookworm AS qvd2parquet-builder

WORKDIR /build/qvd2parquet
COPY ./tools/qvd2parquet/Cargo.toml ./tools/qvd2parquet/Cargo.lock ./
# Pre-build dependencies against a stub main so cargo caches the dep graph.
RUN mkdir src && echo 'fn main() {}' > src/main.rs && \
    cargo build --release --locked && \
    rm -rf src target/release/qvd2parquet target/release/qvd2parquet.d \
           target/release/deps/qvd2parquet-* 2>/dev/null || true
COPY ./tools/qvd2parquet/src ./src
RUN cargo build --release --locked && \
    strip target/release/qvd2parquet

FROM ubuntu:24.04 AS frontend-builder

ENV DEBIAN_FRONTEND=noninteractive

# Install Node.js 22 and prepare environment
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs git && \
    npm install --global yarn@1.22.22 && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container for the frontend
WORKDIR /app

# Copy the VERSION and config file first so they can be used by Nuxt
COPY ./VERSION /app/VERSION
COPY ./bow-config.yaml /app/bow-config.yaml

# Copy the frontend directory contents
COPY ./frontend /app/frontend

# `frontend/plugins/i18n.ts` imports `../../locales/*.json` at build time,
# so the repo-root `locales/` dir must be present for Rollup to resolve them.
COPY ./locales /app/locales

# Set working directory for frontend
WORKDIR /app/frontend

# Install frontend dependencies and build the project
RUN yarn install --frozen-lockfile

# Download vendored JS libraries for airgapped artifact rendering
COPY ./scripts/download-vendor-libs.sh /app/scripts/download-vendor-libs.sh
RUN bash /app/scripts/download-vendor-libs.sh /app/frontend/public/libs

# `nuxt generate` produces a fully static SPA under .output/public, which
# FastAPI serves directly in production (see backend/app/core/spa.py).
# This replaces the previous `yarn build` + Node runtime pattern.
RUN yarn generate

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Install Python runtime and minimal system libs. Node.js is no longer
# needed at runtime: the frontend is pre-generated as static files by the
# frontend-builder stage and served directly by FastAPI.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git openssh-client python3 python3-venv tini libpq5 vim-tiny && \
    # Kerberos runtime for Windows Integrated auth to SQL Server: GSSAPI libs
    # for the ODBC driver / python-gssapi, plus kinit/klist for keytab ops.
    # Mount /etc/krb5.conf and a keytab (see docs/sql-server-kerberos.md).
    apt-get install -y --no-install-recommends krb5-user libgssapi-krb5-2 && \
    curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    ARCH="$(dpkg --print-architecture)" && \
    echo "deb [arch=${ARCH} signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main" > /etc/apt/sources.list.d/microsoft-prod-24.04.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends unixodbc tdsodbc freetds-dev && \
    (ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 || echo "WARN: msodbcsql18 not available for ${ARCH}") && \
    if [ "${ARCH}" = "amd64" ]; then \
      echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/22.04/prod jammy main" > /etc/apt/sources.list.d/microsoft-prod-22.04.list && \
      printf 'Package: *\nPin: origin packages.microsoft.com\nPin: release n=jammy\nPin-Priority: 100\n\nPackage: msodbcsql17\nPin: origin packages.microsoft.com\nPin: release n=jammy\nPin-Priority: 900\n' > /etc/apt/preferences.d/microsoft-odbc && \
      apt-get update && \
      (ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 || echo "WARN: msodbcsql17 install failed"); \
    fi && \
    # For PPTX to PNG preview generation (slides mode)
    apt-get install -y --no-install-recommends libreoffice-impress poppler-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Oracle Instant Client (Basic Light) lets python-oracledb run in "thick"
# mode, which the backend enables at startup whenever these libraries are
# present (see init_thick_mode_if_available in oracledb_client.py). Thin mode
# cannot reach Oracle servers older than 12.1, accounts with 10G-only
# password verifiers (DPY-3015), or Native Network Encryption (DPY-4011
# "connection reset by peer"); thick mode handles all of these and is a
# superset of thin. Pinned to 19c rather than 23c because the 19c client
# connects to servers 11.2+ while 23c requires 19+. Install failure is
# non-fatal so airgapped builds still succeed (the driver stays thin).
RUN ARCH="$(dpkg --print-architecture)" && \
    case "${ARCH}" in \
      amd64) IC_ARCH="linux.x64"; GNU_TRIPLET="x86_64-linux-gnu" ;; \
      arm64) IC_ARCH="linux.arm64"; GNU_TRIPLET="aarch64-linux-gnu" ;; \
      *) IC_ARCH="" ;; \
    esac && \
    if [ -n "${IC_ARCH}" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends libaio1t64 unzip && \
      ln -sf "/usr/lib/${GNU_TRIPLET}/libaio.so.1t64" "/usr/lib/${GNU_TRIPLET}/libaio.so.1" && \
      (curl -fsSL -o /tmp/instantclient.zip \
         "https://download.oracle.com/otn_software/linux/instantclient/1928000/instantclient-basiclite-${IC_ARCH}-19.28.0.0.0dbru.zip" && \
       mkdir -p /opt/oracle && \
       unzip -q /tmp/instantclient.zip -d /opt/oracle && \
       ln -s /opt/oracle/instantclient_19_28 /opt/oracle/instantclient && \
       echo /opt/oracle/instantclient > /etc/ld.so.conf.d/oracle-instantclient.conf && \
       ldconfig \
       || echo "WARN: Oracle Instant Client install failed; python-oracledb stays in thin mode") && \
      rm -f /tmp/instantclient.zip && \
      apt-get clean && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "WARN: no Oracle Instant Client build for ${ARCH}; python-oracledb stays in thin mode"; \
    fi

RUN groupadd -r app \
    && useradd -r -g app -m -d /home/app -s /usr/sbin/nologin app \
    && mkdir -p /home/app /app/backend/db /app/frontend \
    && chown -R app:app /app /home/app

# Copy Python virtual environment and application code
COPY --from=backend-builder --chown=app:app /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=backend-builder --chown=app:app /app/backend /app/backend

# Streaming QVD → Parquet converter (bounded RAM; replaces in-process qvdrs wheel)
COPY --from=qvd2parquet-builder /build/qvd2parquet/target/release/qvd2parquet /usr/local/bin/qvd2parquet

# Copy pre-cached tiktoken encodings for airgapped environments
COPY --from=backend-builder --chown=app:app /opt/tiktoken_cache /opt/tiktoken_cache
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache

# Copy pre-baked embedding model for airgapped environments
COPY --from=backend-builder --chown=app:app /opt/bow-models /opt/bow-models
ENV BOW_EMBEDDINGS_CACHE_DIR=/opt/bow-models

# Copy Playwright browser binaries from builder
COPY --from=backend-builder --chown=app:app /root/.cache/ms-playwright /home/app/.cache/ms-playwright

# Install Playwright system dependencies (runtime libs only, no browser download)
RUN playwright install-deps chromium

# Copy demo data sources (SQLite/DuckDB files for demo databases)
COPY --chown=app:app ./backend/demo-datasources /app/backend/demo-datasources

# Copy the generated static SPA (nuxt generate output includes all public/
# assets — libs, artifact-sandbox.html, icons, etc. — copied automatically).
COPY --from=frontend-builder --chown=app:app /app/frontend/.output/public /app/frontend/dist

# Keep the legacy public paths available for backend headless browser
# rendering code that reads files from disk (not over HTTP).
COPY --from=frontend-builder --chown=app:app /app/frontend/public/artifact-sandbox.html /app/frontend/public/artifact-sandbox.html
COPY --from=frontend-builder --chown=app:app /app/frontend/public/libs /app/frontend/public/libs

# Download RDS/Aurora CA certificate bundle for IAM auth SSL verification
RUN mkdir -p /app/certs && \
    curl -sSL -o /app/certs/rds-combined-ca-bundle.pem \
      https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

# Create directories that the application needs to write to
# These paths match volume mounts in docker-compose.yaml; they must exist with
# app-user ownership so Docker seeds named volumes with writable perms on first run.
RUN mkdir -p /app/backend/uploads/files /app/backend/uploads/branding \
             /app/backend/branding_uploads /app/backend/logs && \
    chown -R app:app /app

WORKDIR /app

COPY --chown=app:app ./VERSION /app/VERSION
COPY --chown=app:app ./start.sh /app/start.sh
COPY --chown=app:app ./bow-config.yaml /app/bow-config.yaml
# Release notes served by /api/changelog (backend runs from /app/backend, so
# repo root maps to /app). Keep this so the "What's New" menu works in the image.
COPY --chown=app:app ./CHANGELOG.md /app/CHANGELOG.md

# Set executable permissions for start.sh
RUN chmod +x /app/start.sh

ENV ENVIRONMENT=production
ENV GIT_PYTHON_REFRESH=quiet

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=UTF-8
ENV HOME=/home/app

# Tell FastAPI to serve the generated SPA from disk.
ENV SERVE_FRONTEND=1
ENV FRONTEND_DIST_DIR=/app/frontend/dist

# Expose the uvicorn port (documentational).
EXPOSE 3000

# Healthcheck against /health so failures reflect backend readiness, not
# just the static SPA index (which would always 200 even if uvicorn was wedged).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:3000/health || exit 1

USER app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/bash", "start.sh"]
