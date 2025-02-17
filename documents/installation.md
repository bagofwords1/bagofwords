# Docker Installation Guide

## Quick Start
```bash
# Build the Docker image
docker build -t bow .

# Run the container
docker run -p 80:80 bow 
```
## Dockerfile Overview

This multi-stage Dockerfile builds a full-stack application:

### 1. Backend Stage
- Base: Python 3.12.2-slim
- Creates Python virtual environment
- Installs PostgreSQL dependencies
- Copies and installs backend requirements

### 2. Frontend Stage  
- Base: Node 22
- Uses Yarn for package management
- Builds frontend application

### 3. Final Stage
- Combines backend and frontend
- Installs Nginx web server
- Sets up environment variables
- Exposes port 80
- Runs via start.sh script

## Requirements
- Docker installed on your system
- Source code with:
  - ./bow-config.yaml
  - ./backend/
  - ./frontend/
  - ./nginx.conf
  - ./VERSION
  - ./start.sh

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
