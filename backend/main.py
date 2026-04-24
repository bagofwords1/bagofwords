import os
import json
import uvicorn
import argparse
import uuid
import time
from datetime import datetime

from tenacity import retry, stop_after_attempt, wait_exponential
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Disable Nagle on uvicorn's accepted sockets so SSE/WebSocket streaming
# isn't coalesced into jumpy bursts. Must run before uvicorn imports the
# protocol classes it will instantiate.
from app.core.tcp_nodelay import enable_tcp_nodelay
enable_tcp_nodelay()

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
from app.core.scheduler import scheduler, try_acquire_scheduler_leader
from app.core.spa import mount_spa
from app.models.user import User
from app.services.maintenance_service import purge_step_payloads_keep_latest_per_query
from app.data_sources.clients.qvd_client import warm_all_qvd_caches
from app.data_sources.clients.powerbi_report_server_client import warm_all_pbirs_caches
from app.core.otel import setup_telemetry, instrument_app

from app.routes import (
    report,
    test,
    widget,
    query,
    visualization,
    entity,
    completion,
    completion_feedback,
    file,
    organization,
    data_source,
    demo_data_source,
    text_widget,
    user_profile,
    llm,
    git,
    organization_settings,
    branding,
    metadata_resource,
    bow_settings,
    external_platform,
    external_user_mapping,
    slack_webhook,
    teams_webhook,
    whatsapp_webhook,
    step,
    instruction,
    onboarding,
    console,
    agent_execution,
    auth as auth_routes,
    user_data_source_credentials,
    mentions,
    api_key,
    mcp,
    build,
    connection,
    connection_oauth,
    artifact,
    oauth_server,
    rbac,
    scheduled_prompt,
    excel,
)
from app.routes.oidc_auth import router as oidc_auth_router
from app.ee.routes import router as enterprise_router
from app.ee.license import get_license_info, has_feature

# Initialize logging
loggers = setup_logging()
logger = get_logger(__name__)
# Initialize OpenTelemetry if enabled (before app creation)
setup_telemetry(settings.bow_config.otel)
# Read configuration
enable_google_oauth = settings.bow_config.google_oauth.enabled
google_client_id = settings.bow_config.google_oauth.client_id
google_client_secret = settings.bow_config.google_oauth.client_secret

# Initialize FastAPI app
swagger_enabled = settings.bow_config.swagger.enabled
app = FastAPI(
    title=settings.PROJECT_NAME,
    debug=settings.DEBUG,
    docs_url="/swagger" if swagger_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if swagger_enabled else None,
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

# Instrument FastAPI with OpenTelemetry
instrument_app(app, settings.bow_config.otel)
init_cors(app)

# Register typed-error handlers so AppError instances become localized responses.
from app.errors import register_exception_handlers  # noqa: E402
register_exception_handlers(app)

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

@app.get("/health", include_in_schema=False)
async def health():
    """Liveness probe — used by k8s, docker healthcheck, and CI wait loops."""
    return {"status": "ok"}


app.include_router(demo_data_source.router, prefix="/api")  # Must be before data_source for /data_sources/demos to match
app.include_router(data_source.router, prefix="/api")
app.include_router(report.router, prefix="/api")
app.include_router(scheduled_prompt.router, prefix="/api")
app.include_router(test.router, prefix="/api")
app.include_router(widget.router, prefix="/api")
app.include_router(query.router, prefix="/api")
app.include_router(visualization.router, prefix="/api")
app.include_router(entity.router, prefix="/api")
app.include_router(completion.router)
app.include_router(completion_feedback.router, prefix="/api")
app.include_router(file.router, prefix="/api")
app.include_router(organization.router, prefix="/api")
app.include_router(rbac.router, prefix="/api")
app.include_router(text_widget.router, prefix="/api")
app.include_router(llm.router, prefix="/api")
app.include_router(git.router, prefix="/api")
app.include_router(organization_settings.router, prefix="/api")
app.include_router(branding.router, prefix="/api")
app.include_router(metadata_resource.router, prefix="/api")
app.include_router(bow_settings.router, prefix="/api")
app.include_router(external_platform.router, prefix="/api")
app.include_router(external_user_mapping.router, prefix="/api")
app.include_router(slack_webhook.router)
app.include_router(teams_webhook.router)
app.include_router(whatsapp_webhook.router)
app.include_router(step.router, prefix="/api")
app.include_router(instruction.router, prefix="/api")
app.include_router(build.router, prefix="/api")
app.include_router(console.router, prefix="/api")
app.include_router(agent_execution.router, prefix="/api")
app.include_router(onboarding.router, prefix="/api")
app.include_router(user_data_source_credentials.router, prefix="/api")
app.include_router(mentions.router, prefix="/api")
app.include_router(api_key.router, prefix="/api")
app.include_router(mcp.router, prefix="/api")
app.include_router(oauth_server.well_known_router)  # /.well-known/* at root
app.include_router(oauth_server.router, prefix="/api")  # /api/oauth/*
app.include_router(connection.router, prefix="/api")
app.include_router(connection_oauth.router, prefix="/api")
app.include_router(artifact.router, prefix="/api")
app.include_router(excel.router, prefix="/api")
app.include_router(enterprise_router, prefix="/api")

# External-facing aliases: MCP clients and the Excel add-in connect to
# /mcp and /excel directly (these paths were previously provided by the
# Nuxt reverse-proxy rewrites /mcp→/api/mcp, /excel→/api/excel).
app.include_router(mcp.router)
app.include_router(excel.router)

# SCIM 2.0 provisioning endpoints (mounted at /scim/v2, not under /api)
from app.ee.scim.routes import scim_router
app.include_router(scim_router)

# SPA: serve generated Nuxt output at / when SERVE_FRONTEND=1.
# Must be the last route registration so it only catches unmatched paths.
mount_spa(app)

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

    # Only one uvicorn worker should register & run scheduled jobs. Otherwise
    # N-workers × every scheduled tick becomes an N-way resource storm
    # (customer log showed warm_all_qvd_caches firing 5–6× simultaneously).
    is_scheduler_leader = try_acquire_scheduler_leader()
    if not is_scheduler_leader:
        logger.info("Scheduler leader lock not acquired — skipping job registration in this worker")

    # Register daily maintenance jobs
    if is_scheduler_leader:
        try:
            scheduler.add_job(
                purge_step_payloads_keep_latest_per_query,
                trigger="cron",
                hour=3,
                minute=0,
                id="purge_step_payloads_daily",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
                kwargs={"null_fields": ("data", "data_model", "view")},
            )
            logger.info("Scheduled job: purge_step_payloads_keep_latest_per_query @ 03:00 daily")
        except Exception as e:
            logger.error(f"Failed to schedule purge job: {e}")

    # Background warmup of QVD Parquet caches so the first create_data/inspect_data
    # on a 1-5GB QVD doesn't block the UI for minutes.
    if is_scheduler_leader:
        try:
            scheduler.add_job(
                warm_all_qvd_caches,
                trigger="interval",
                hours=1,
                id="qvd_warmup",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
                next_run_time=datetime.now(),
            )
            logger.info("Scheduled job: qvd_warmup every 1h (runs once at startup)")
        except Exception as e:
            logger.error(f"Failed to schedule QVD warmup job: {e}")

    # Background warmup of PBIRS pbix Parquet caches so first queries against a
    # Power BI report don't pay the pbixray parse cost (~10-30s on ~50MB pbix).
    if is_scheduler_leader:
        try:
            scheduler.add_job(
                warm_all_pbirs_caches,
                trigger="interval",
                hours=1,
                id="pbirs_warmup",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=600,
                next_run_time=datetime.now(),
            )
            logger.info("Scheduled job: pbirs_warmup every 1h (runs once at startup)")
        except Exception as e:
            logger.error(f"Failed to schedule PBIRS warmup job: {e}")

    # Register LDAP group sync job if configured AND licensed (sync is enterprise-only)
    if is_scheduler_leader and settings.bow_config.ldap.enabled and has_feature("ldap"):
        try:
            from app.ee.ldap.jobs import ldap_sync_all_organizations
            scheduler.add_job(
                ldap_sync_all_organizations,
                trigger="interval",
                minutes=settings.bow_config.ldap.sync_interval_minutes,
                id="ldap_group_sync",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )
            logger.info(f"Scheduled job: ldap_group_sync every {settings.bow_config.ldap.sync_interval_minutes}m")
        except Exception as e:
            logger.error(f"Failed to schedule LDAP sync job: {e}")

    # All workers must start their scheduler (route handlers in this worker
    # call scheduler.add_job to persist user-scheduled prompts/reports to the
    # shared jobstore). Only the leader registers the global warmup jobs
    # above — those are the ones that fanned out to N workers.
    scheduler.start()

    if is_scheduler_leader:
        # Re-register scheduled prompt jobs (only the leader flushes these to
        # the jobstore; non-leaders would duplicate the set).
        from app.services.scheduled_prompt_service import scheduled_prompt_service
        await scheduled_prompt_service.register_all_jobs()

    # Validate license at startup
    license_info = get_license_info()
    license_status = f"Enterprise ({license_info.org_name})" if license_info.licensed else "Community"

    print(f"""
   ____                       __                         _
 |  _ \\                     / _|                       | |
 | |_) | __ _  __ _    ___ | |_  __      _____  _ __ __| |___
 |  _ < / _` |/ _` |  / _ \\|  _| \\ \\ /\\ / / _ \\| '__/ _` / __|
 | |_) | (_| | (_| | | (_) | |    \\ V  V / (_) | | | (_| \\__ \\
 |____/ \\__,_|\\__, |  \\___/|_|     \\_/\\_/ \\___/|_|  \\__,_|___/
               __/ |
              |___/

Starting server with configuration:
    - Environment: {settings.ENVIRONMENT}
    - Debug Mode: {settings.DEBUG}
    - Google OAuth: {'Enabled' if enable_google_oauth else 'Disabled'}
    - Email Verification: {'Enabled' if settings.bow_config.features.verify_emails else 'Disabled'}
    - Deployment Type: {settings.bow_config.deployment.type}
    - License: {license_status}
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
