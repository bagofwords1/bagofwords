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


def mint_file_token(
    file_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    *,
    organization_id: str,
    report_id: Optional[str] = None,
    release_id: Optional[str] = None,
    via_public_link: bool = False,
) -> str:
    """Mint a signed capability token for a single file id.

    The token is bound to the organization the access check ran in (``org``)
    and, for published-report embeds, to that report (``rid``); /embed
    re-verifies both against the file, so a token can never serve a file
    from another org or outside the report it was minted for."""
    now = int(time.time())
    payload = {
        "fid": str(file_id),
        "org": str(organization_id),
        "iat": now,
        "exp": now + int(ttl_seconds),
        "scope": "file-embed",
    }
    if report_id:
        payload["rid"] = str(report_id)
    if release_id:
        # A published dashboard's embed: bound to its release, which outlives
        # the conversation it came from.
        payload["rel"] = str(release_id)
    if via_public_link and (report_id or release_id):
        # Minted for someone outside the org, so only the public link let them
        # in. /embed re-checks that grant on every request, so turning public
        # sharing off (or unsharing) revokes the token before it expires.
        payload["pub"] = True
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def decode_file_token(token: str, file_id: str) -> Optional[dict]:
    """The token's claims iff it is a valid, unexpired, org-bound capability
    for file_id; otherwise None."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.PyJWTError:
        return None
    if payload.get("scope") != "file-embed" or str(payload.get("fid")) != str(file_id):
        return None
    if not payload.get("org"):
        return None  # unbound (pre-fix) tokens are refused; they are minted fresh per render
    return payload


def verify_file_token(token: str, file_id: str) -> bool:
    """Return True iff the token is a valid, unexpired capability for file_id."""
    return decode_file_token(token, file_id) is not None


def file_embed_url(file_id: str, token: str) -> str:
    """Build the relative embed URL for an already-minted token."""
    return f"/api/files/{file_id}/embed?token={token}"
