"""CORS configuration.

Origins are read from the BOW_CORS_ALLOWED_ORIGINS env var (comma-separated).
If unset, only same-origin requests are allowed — this is the safe default for
single-host deployments where the bundled SPA serves from the same FastAPI
process. Set BOW_CORS_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com
in deployments where the frontend lives on a different origin.
"""

import os

from fastapi.middleware.cors import CORSMiddleware


def _parse_origins() -> list[str]:
    raw = os.environ.get("BOW_CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


def init_cors(app):
    origins = _parse_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # allow_credentials with wildcard origins is rejected by browsers anyway;
        # only enable when a concrete origin list is configured.
        allow_credentials=bool(origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Organization-Id"],
    )
