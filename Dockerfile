FROM ubuntu:24.04 AS backend-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      python3-venv \
      python3-dev \
      build-essential \
      libpq-dev \
      gcc \
      unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container for the backend
WORKDIR /app/backend

# Copy the backend directory contents into the container at /app/backend
COPY ./backend /app/backend
RUN rm -f /app/backend/db/app.db

# Create and use a virtual environment for dependencies
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install any needed packages specified in backend/requirements_versioned.txt
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    python3 -m pip install --no-cache-dir --prefer-binary -r requirements_versioned.txt

# Pre-cache tiktoken encodings for airgapped environments
RUN TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache python3 -c \
    "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"

# Install Playwright browser (chromium only to save space)
RUN playwright install chromium --with-deps

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

# Set working directory for frontend
WORKDIR /app/frontend

# Install frontend dependencies and build the project
RUN yarn install --frozen-lockfile

# Download vendored JS libraries for airgapped artifact rendering
COPY ./scripts/download-vendor-libs.sh /app/scripts/download-vendor-libs.sh
RUN bash /app/scripts/download-vendor-libs.sh /app/frontend/public/libs

RUN yarn build

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Install Python runtime, Node.js 22 (runtime only), and minimal system libs
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git openssh-client python3 python3-venv tini libpq5  && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main" > /etc/apt/sources.list.d/microsoft-prod-24.04.list && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/22.04/prod jammy main" > /etc/apt/sources.list.d/microsoft-prod-22.04.list && \
    printf 'Package: *\nPin: origin packages.microsoft.com\nPin: release n=jammy\nPin-Priority: 100\n\nPackage: msodbcsql17\nPin: origin packages.microsoft.com\nPin: release n=jammy\nPin-Priority: 900\n' > /etc/apt/preferences.d/microsoft-odbc && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc tdsodbc freetds-dev && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 && \
    # For PPTX to PNG preview generation (slides mode)
    apt-get install -y --no-install-recommends libreoffice-impress poppler-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

RUN groupadd -r app \
    && useradd -r -g app -m -d /home/app -s /usr/sbin/nologin app \
    && mkdir -p /home/app /app/backend/db /app/frontend \
    && chown -R app:app /app /home/app

# Copy Python virtual environment and application code
COPY --from=backend-builder --chown=app:app /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=backend-builder --chown=app:app /app/backend /app/backend

# Copy pre-cached tiktoken encodings for airgapped environments
COPY --from=backend-builder --chown=app:app /opt/tiktoken_cache /opt/tiktoken_cache
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache

# Copy Playwright browser binaries from builder
COPY --from=backend-builder --chown=app:app /root/.cache/ms-playwright /home/app/.cache/ms-playwright

# Install Playwright system dependencies (runtime libs only, no browser download)
RUN playwright install-deps chromium

# Copy demo data sources (SQLite/DuckDB files for demo databases)
COPY --chown=app:app ./backend/demo-datasources /app/backend/demo-datasources

# Copy only the built Nuxt output to keep the image small
COPY --from=frontend-builder --chown=app:app /app/frontend/.output /app/frontend/.output

# Copy sandbox HTML for artifact validation (used by headless browser)
COPY --from=frontend-builder --chown=app:app /app/frontend/public/artifact-sandbox.html /app/frontend/public/artifact-sandbox.html

# Copy vendored JS libs for backend headless browser rendering (airgapped support)
COPY --from=frontend-builder --chown=app:app /app/frontend/public/libs /app/frontend/public/libs

# Copy runtime configs and scripts
COPY --chown=app:app ./backend/requirements_versioned.txt /app/backend/

# Download RDS/Aurora CA certificate bundle for IAM auth SSL verification
RUN mkdir -p /app/certs && \
    curl -sSL -o /app/certs/rds-combined-ca-bundle.pem \
      https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

# Create directories that the application needs to write to
RUN mkdir -p /app/backend/uploads/files /app/backend/uploads/branding /app/backend/logs && \
    chown -R app:app /app

WORKDIR /app

COPY --chown=app:app ./VERSION /app/VERSION
COPY --chown=app:app ./start.sh /app/start.sh
COPY --chown=app:app ./bow-config.yaml /app/bow-config.yaml

# Set executable permissions for start.sh
RUN chmod +x /app/start.sh

# Define environment variable for Node to run in production mode
ENV NODE_ENV=production
ENV ENVIRONMENT=production
ENV GIT_PYTHON_REFRESH=quiet

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=UTF-8
ENV HOME=/home/app

# Expose ports (documentational)
EXPOSE 3000

# Healthcheck against the Nuxt server; 
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:3000/ || exit 1

USER app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/bash", "start.sh"]
