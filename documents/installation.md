# Docker Installation Details

## Quick Start
```bash
# Build the Docker image
docker build -t bow .

# Run the container
docker run -p 3000:3000 bow 
```
## Dockerfile Overview

This multi-stage Dockerfile builds a full-stack application:

### 1. Backend Stage
- Base: Ubuntu 24.04
- Installs Python 3, build tools, and unixODBC dev headers
- Creates a Python virtual environment (`/opt/venv`)
- Copies backend and installs `backend/requirements_versioned.txt`

### 2. Frontend Stage  
- Base: Ubuntu 24.04
- Installs Node.js 22 and Yarn
- Builds the Nuxt frontend (`frontend/.output`)

### 3. Final Stage
- Base: Ubuntu 24.04
- Installs Python runtime, Node.js 22 (runtime), and ODBC components
  - Microsoft ODBC Driver 18 for SQL Server (`msodbcsql18`)
  - SQL Server tools (`mssql-tools18`)
  - `unixodbc`
- Copies Python venv and backend app code
- Copies built Nuxt output to serve the frontend
- Sets environment variables and uses `tini` as entrypoint
- Exposes port 3000
- Runs via `start.sh`

## Requirements
- Docker installed on your system
- Source code with:
  - ./bow-config.yaml
  - ./backend/
  - ./frontend/
  - ./VERSION
  - ./start.sh

Optional verifications inside the running container:
```bash
python -c "import pyodbc; print(pyodbc.version)"
odbcinst -q -d -n "ODBC Driver 18 for SQL Server"
```

```yaml
  ## Bow Config:
  # Deployment Configuration
deployment:
  type: "saas"  # Options: "saas" or "self_hosted"

base_url: http://0.0.0.0:3000
  
# Feature Flags
features:
  allow_uninvited_signups: true
  allow_multiple_organizations: true # If true, there could be more than 1 organization in the system
  verify_emails: false
  enable_google_oauth: true

google_oauth:
  # Enable Google OAuth and enable Google People API
  client_id: "YOUR_CLIENT_ID"
  client_secret: "YOUR_CLIENT_SECRET"

default_llm:
  - provider_type: "bow"
    provider_name: "Bow"
    api_key: "YOUR_API_KEY"
    models:
      - model_id: "gpt-4o-mini"
        model_name: "bow-small"
        is_default: true
        is_enabled: true
#  - provider_type: "anthropic"
#    provider_name: "Anthropic"
#    api_key: "YOUR_API_KEY"
#    models:
#      - model_id: "claude-3-5-sonnet"
#        model_name: "claude-3-5-sonnet"

smtp_settings:
  host: "smtp.gmail.com"
  port: 587
  username: "YOUR_EMAIL"
  password: "YOUR_PASSWORD"

encryption_key: "YOUR_ENCRYPTION_KEY"

# Example self-hosted configuration:
# deployment:
#   type: "self_hosted"
# features:
#   allow_signups: false
#   allow_multiple_organizations: false
#   show_billing_page: false
#   show_license_page: true
```
