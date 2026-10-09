export WORKDIR=/app/workspace
export BACKEND_DIR=${WORKDIR}/bagofwords/backend
export FRONTEND_DIR=${WORKDIR}/bagofwords/frontend
export DEBIAN_FRONTEND=noninteractive
# clone sources
cd ${WORKDIR} && \
 git clone https://github.com/bagofwords1/bagofwords
# install backend dependencies
cd ${BACKEND_DIR} && \
  UV_PROJECT_ENVIRONMENT=/opt/venv \
   uv sync --frozen --no-dev \
    --no-install-project \
    --extra kerberos \
    --extra dev
# install frontend dependencies
cd ${FRONTEND_DIR} && \
  yarn install --frozen-lockfile
# install playwright dependencies
playwright install chromium --with-deps
playwright install-deps chromium

