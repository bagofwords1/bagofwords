# devex-mcp: MCP server exposed over streamable HTTP, runs in the sandbox cluster.
# Build from this directory:
#   docker build -t bagofwords/devex-mcp:latest .
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.10.9

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY devex_mcp ./devex_mcp
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --create-home mcp
USER mcp

EXPOSE 3400
ENTRYPOINT ["devex-mcp"]
# Public hostnames come from $MCP_ALLOWED_HOSTS (set in the Deployment).
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "3400"]
