"""Signed, short-lived capability tokens for serving embedded files.

Used so an artifact sandbox iframe (in-app) or a published report page (no
session) can load an image/PDF by file id without a bearer header, while still
being authorized and revocable-by-expiry. The token is a capability for ONE file
id and nothing else; it is minted only after the caller's access is checked
(org membership, or the file being embedded in a published report's artifact),
and it is NEVER persisted — the durable reference is always the file id, and the
token is minted fresh at render time.

Signed with a secret derived from the app's Fernet encryption key (a stable,
already-managed server secret), so no new key management is introduced.
"""
import hashlib
import time
from typing import Optional

import jwt

from app.settings.config import settings

_ALGO = "HS256"
DEFAULT_TTL_SECONDS = 3600  # 1 hour; minted fresh per render, re-mint on expiry.


def _secret() -> str:
    # Derive a dedicated signing secret from the Fernet key so rotating one
    # doesn't silently change the other's behavior, and the raw key is never
    # used directly as the JWT secret.
    raw = settings.bow_config.encryption_key
    if isinstance(raw, str):
        raw = raw.encode()
    return hashlib.sha256(b"bow-file-embed-token:" + raw).hexdigest()


def mint_file_token(file_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a signed capability token for a single file id."""
    now = int(time.time())
    payload = {"fid": str(file_id), "iat": now, "exp": now + int(ttl_seconds), "scope": "file-embed"}
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def verify_file_token(token: str, file_id: str) -> bool:
    """Return True iff the token is a valid, unexpired capability for file_id."""
    if not token:
        return False
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.PyJWTError:
        return False
    return payload.get("scope") == "file-embed" and str(payload.get("fid")) == str(file_id)


def file_embed_url(file_id: str, token: Optional[str] = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Build the relative embed URL (minting a token if one isn't supplied)."""
    tok = token or mint_file_token(file_id, ttl_seconds)
    return f"/api/files/{file_id}/embed?token={tok}"
