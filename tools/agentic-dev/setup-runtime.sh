git clone https://github.com/bagofwords1/bagofwords

cd bagofwords/backend && \
  UV_PROJECT_ENVIRONMENT=/opt/venv \
   uv sync --frozen --no-dev \
    --no-install-project \
    --extra kerberos \
    --extra dev

playwright install chromium --with-deps
playwright install-deps chromium
