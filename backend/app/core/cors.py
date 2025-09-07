from fastapi.middleware.cors import CORSMiddleware
from app.settings.config import settings

def init_cors(app):
    # Get allowed origins from config or use secure defaults
    allowed_origins = []
    
    if settings.ENVIRONMENT == "development":
        allowed_origins = [
            "http://localhost:3000",
            "https://localhost:3000",
            "http://127.0.0.1:3000",
            "https://127.0.0.1:3000",
        ]
    else:
        # Production: only allow configured base URL
        if settings.bow_config.base_url:
            allowed_origins = [settings.bow_config.base_url]
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Accept-Language",
            "Content-Language",
            "Content-Type",
            "Authorization",
            "X-Organization-Id",
            "X-Requested-With",
        ],
        expose_headers=["X-Total-Count"],
    )
