import hashlib
import hmac
import json
import time
from typing import Optional


class WebhookAdapter:
    """Base adapter. Subclasses implement source-specific HMAC verification,
    handshake detection, dedup-id extraction, and payload normalization.

    Verification for ``token`` / ``url_token`` auth modes is handled centrally in
    the receiver (it's source-agnostic); adapters only implement HMAC.
    """

    source = "generic"
    # Max clock skew (seconds) for timestamped (generic) signatures.
    TIMESTAMP_TOLERANCE = 300

    def is_handshake(self, headers: dict, payload: dict) -> bool:
        """True for protocol handshakes (e.g. GitHub ping) that get a 200 no-op."""
        return False

    def verify_hmac(self, secret: str, raw_body: bytes, headers: dict) -> bool:
        """Verify the request's HMAC signature. Override per source."""
        raise NotImplementedError

    def event_id(self, headers: dict, payload: dict) -> Optional[str]:
        """A unique per-delivery id used for idempotency/dedup."""
        return None

    def normalize(self, headers: dict, payload: dict) -> dict:
        """Return {summary, details, raw, event_key}.

        - summary: one-line for the chat timeline
        - details: curated, readable multi-line "full event" for the agent
        - raw: the untouched payload (TraceModal only)
        - event_key: best-effort event type label
        """
        raise NotImplementedError

    # ---- shared helpers ----

    @staticmethod
    def _ct_eq(a: str, b: str) -> bool:
        return hmac.compare_digest(a or "", b or "")

    @staticmethod
    def _hexsig(secret: str, body: bytes) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
