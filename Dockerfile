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
    curl -sSL -o /tmp/packages-microsoft-prod.deb https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb && \
    dpkg -i /tmp/packages-microsoft-prod.deb && rm /tmp/packages-microsoft-prod.deb && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 mssql-tools18 unixodbc && \
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

# Copy demo data sources (SQLite/DuckDB files for demo databases)
COPY --chown=app:app ./backend/demo-datasources /app/backend/demo-datasources

# Copy only the built Nuxt output to keep the image small
COPY --from=frontend-builder --chown=app:app /app/frontend/.output /app/frontend/.output

# Copy runtime configs and scripts
COPY --chown=app:app ./backend/requirements_versioned.txt /app/backend/

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
