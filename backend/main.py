import os
import json
import uvicorn
import argparse
import uuid
import time

from tenacity import retry, stop_after_attempt, wait_exponential
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Add this before app initialization
parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, help='Path to custom config file')
args, _ = parser.parse_known_args()

# Set environment variable for config path if specified
if args.config:
    os.environ['BOW_CONFIG_PATH'] = args.config

from fastapi import FastAPI, Request, Depends
from fastapi.responses import RedirectResponse
from httpx_oauth.clients.google import GoogleOAuth2
from httpx_oauth.clients.openid import OpenID

from fastapi.openapi.utils import get_openapi

from app.core.auth import get_user_manager, auth_backend, create_fastapi_users, SECRET
from app.dependencies import get_db, async_session_maker
from app.schemas.user_schema import UserCreate, UserRead, UserUpdate
from app.settings.config import settings
from app.settings.logging_config import setup_logging, get_logger
from app.core.cors import init_cors
from app.core.scheduler import scheduler
from app.models.user import User

from app.routes import (
    report,
    widget,
    query,
    visualization,
    entity,
    completion,
    completion_feedback,
    file,
    organization,
    data_source,
    memory,
    text_widget,
    user_profile,
    llm,
    git_repository,
    organization_settings,
    branding,
    metadata_resource,
    bow_settings,
    external_platform,
    external_user_mapping,
    slack_webhook,
    step,
    instruction,
    onboarding,
    console,
    agent_execution,
    auth as auth_routes,
    user_data_source_credentials,
    mentions
)
from app.routes.oidc_auth import router as oidc_auth_router

# Initialize logging
loggers = setup_logging()
logger = get_logger(__name__)

# Read configuration
enable_google_oauth = settings.bow_config.google_oauth.enabled
google_client_id = settings.bow_config.google_oauth.client_id
google_client_secret = settings.bow_config.google_oauth.client_secret

# Initialize FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME, 
    debug=settings.DEBUG,
    openapi_tags=[
        {"name": "auth", "description": "Authentication operations"},
        {"name": "reports", "description": "Report management"},
        {"name": "widgets", "description": "Widget operations"},
        {"name": "data_sources", "description": "Data source management"},
        {"name": "organizations", "description": "Organization management"},
        {"name": "users", "description": "User management"},
        {"name": "files", "description": "File operations"},
        {"name": "completions", "description": "AI completions"},
        {"name": "llm", "description": "LLM and their providers settings"},
        {"name": "memories", "description": "Memory management"},
        {"name": "git", "description": "Git repository and data source integration"},
        {"name": "settings", "description": "Settings management"},
    ],
    swagger_ui_oauth2_redirect_url="/api/auth/jwt/login"
)

init_cors(app)

oauth_providers = []
google_oauth_client = None

"""
OIDC (with PKCE) is mounted via app.routes.oidc_auth. We keep main.py free of flow details.
"""

fastapi_users = create_fastapi_users(get_user_manager, auth_backend, oauth_providers)
current_user = fastapi_users.current_user(active=True)

app.include_router(user_profile.router, prefix="/api")

# Determine auth mode
auth_mode = getattr(settings.bow_config, 'auth').mode if hasattr(settings.bow_config, 'auth') else 'hybrid'
enable_local = auth_mode in ("hybrid", "local_only")
enable_sso = auth_mode in ("hybrid", "sso_only")

# New unified auth provider routes (Google + OIDC)
if enable_sso:
    app.include_router(auth_routes.router, prefix="/api", tags=["auth"])

# Local auth routes
if enable_local:
    app.include_router(
        fastapi_users.get_auth_router(auth_backend),
        prefix="/api/auth/jwt",
        tags=["auth"]
    )

    app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix="/api/auth",
        tags=["auth"]
    )

    app.include_router(
        fastapi_users.get_reset_password_router(),
        prefix="/api/auth",
        tags=["auth"],
    )

    if settings.bow_config.features.verify_emails:
        app.include_router(
            fastapi_users.get_verify_router(UserRead),
            prefix="/api/auth",
            tags=["auth"],
        )

app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/api/users",
    tags=["users"],
)

# Google OAuth is handled by custom OIDC router for uniform behavior


app.include_router(data_source.router, prefix="/api")
app.include_router(report.router, prefix="/api")
app.include_router(widget.router, prefix="/api")
app.include_router(query.router, prefix="/api")
app.include_router(visualization.router, prefix="/api")
app.include_router(entity.router, prefix="/api")
app.include_router(completion.router)
app.include_router(completion_feedback.router, prefix="/api")
app.include_router(file.router, prefix="/api")
app.include_router(organization.router, prefix="/api")
app.include_router(text_widget.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(llm.router, prefix="/api")
app.include_router(git_repository.router, prefix="/api")
app.include_router(organization_settings.router, prefix="/api")
app.include_router(branding.router, prefix="/api")
app.include_router(metadata_resource.router, prefix="/api")
app.include_router(bow_settings.router, prefix="/api")
app.include_router(external_platform.router, prefix="/api")
app.include_router(external_user_mapping.router, prefix="/api")
app.include_router(slack_webhook.router)
app.include_router(step.router, prefix="/api")
app.include_router(instruction.router, prefix="/api")
app.include_router(console.router, prefix="/api")
app.include_router(agent_execution.router, prefix="/api")
app.include_router(onboarding.router, prefix="/api")
app.include_router(user_data_source_credentials.router, prefix="/api")
app.include_router(mentions.router, prefix="/api")

# Remove the direct assignment of app.openapi_schema and replace with this function
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=settings.PROJECT_NAME,
        version=settings.PROJECT_VERSION,
        description="Bag of Words API",
        routes=app.routes,
    )

    # Add security schemes
    openapi_schema["components"]["securitySchemes"] = {
        "OAuth2PasswordBearer": {
            "type": "oauth2",
            "flows": {
                "password": {
                    "tokenUrl": "/api/auth/jwt/login",
                    "scopes": {}
                }
            }
        },
        "X-Organization-ID": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Organization-ID",
            "description": "Organization ID header"
        }
    }

    # Add global security requirements
    openapi_schema["security"] = [
        {
            "OAuth2PasswordBearer": [],
            "X-Organization-ID": []
        }
    ]

    # Make sure the security requirement is applied to all paths
    for path in openapi_schema["paths"].values():
        for operation in path.values():
            operation["security"] = [
                {
                    "OAuth2PasswordBearer": [],
                    "X-Organization-ID": []
                }
            ]

    app.openapi_schema = openapi_schema
    return app.openapi_schema

# Assign the custom function to app.openapi
app.openapi = custom_openapi

# Add this function before the startup_event
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
async def check_db_connection():
    """Verify database connection with retries"""
    try:
        async with async_session_maker() as session:
            # Try a simple query to verify the connection
            await session.execute(text("SELECT 1"))
            await session.commit()
            logger.info("✅ Database connection successful")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {str(e)}")
        raise

@app.on_event("startup")
async def startup_event():
    try:
        # Check database connection first with retries
        await check_db_connection()
    except Exception as e:
        logger.error(f"Failed to connect to database after 3 retries: {str(e)}")
        exit(1)
    logger.info(
        "Application starting",
        extra={
            "environment": settings.ENVIRONMENT,
            "debug_mode": settings.DEBUG,
            "google_oauth": enable_google_oauth,
            "email_verification": settings.bow_config.features.verify_emails,
            "deployment_type": settings.bow_config.deployment.type,
            "version": settings.PROJECT_VERSION
        }
    )
    
    scheduler.start()
    print(f"""
   ____                       __                         _     
 |  _ \\                     / _|                       | |    
 | |_) | __ _  __ _    ___ | |_  __      _____  _ __ __| |___ 
 |  _ < / _` |/ _` |  / _ \\|  _| \\ \\ /\\ / / _ \\| '__/ _` / __|
 | |_) | (_| | (_| | | (_) | |    \\ V  V / (_) | | | (_| \\__ \\
 |____/ \\__,_|\\__, |  \\___/|_|     \\_/\\_/ \\___/|_|  \\__,_|___/
               __/ |                                          
              |___/                                                                       

🚀 Starting server with configuration:
    - Environment: {settings.ENVIRONMENT}
    - Debug Mode: {settings.DEBUG}
    - Google OAuth: {'Enabled' if enable_google_oauth else 'Disabled'}
    - Email Verification: {'Enabled' if settings.bow_config.features.verify_emails else 'Disabled'}
    - Deployment Type: {settings.bow_config.deployment.type}
    - Version: {settings.PROJECT_VERSION}
    
    You can now start using the app at {settings.bow_config.base_url}
    """)

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        workers=20
    )
