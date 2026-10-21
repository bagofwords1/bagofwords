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
    def _text(value) -> str:
        """Readable one-line text for any JSON value.

        Systems like ServiceNow send reference fields as objects
        ({"value": "<sys_id>", "display_value": "IIS 10 Application"}); a bare
        f-string would paste the Python dict repr into the summary.
        """
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, (str, int, float)):
            return str(value).strip()
        if isinstance(value, dict):
            for key in ("display_value", "name", "title", "label", "value"):
                text = WebhookAdapter._text(value.get(key))
                if text:
                    return text
            return ""
        if isinstance(value, (list, tuple)):
            return ", ".join(t for t in (WebhookAdapter._text(v) for v in value) if t)
        return str(value).strip()

    @staticmethod
    def _ct_eq(a: str, b: str) -> bool:
        return hmac.compare_digest(a or "", b or "")

    @staticmethod
    def _hexsig(secret: str, body: bytes) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
