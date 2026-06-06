import json
import time
from typing import Optional

from .base import WebhookAdapter


class GenericAdapter(WebhookAdapter):
    """Generic source (cron / curl / Zapier / custom service).

    HMAC scheme: X-BOW-Signature-256 = sha256=hmac(secret, "{timestamp}.{body}")
    with X-BOW-Timestamp checked for skew (replay guard). Dedup via X-BOW-Delivery.
    """

    source = "generic"

    def is_handshake(self, headers: dict, payload: dict) -> bool:
        return False

    def verify_hmac(self, secret: str, raw_body: bytes, headers: dict) -> bool:
        sig = headers.get("x-bow-signature-256") or ""
        ts = headers.get("x-bow-timestamp") or ""
        if not sig or not ts:
            return False
        # Replay guard: reject stale timestamps.
        try:
            if abs(time.time() - float(ts)) > self.TIMESTAMP_TOLERANCE:
                return False
        except (TypeError, ValueError):
            return False
        signed = f"{ts}.".encode() + raw_body
        expected = "sha256=" + self._hexsig(secret, signed)
        return self._ct_eq(sig, expected)

    def event_id(self, headers: dict, payload: dict) -> Optional[str]:
        return headers.get("x-bow-delivery") or headers.get("x-webhook-id")

    def normalize(self, headers: dict, payload: dict) -> dict:
        event_key = (
            payload.get("type")
            or payload.get("event")
            or payload.get("action")
            or "event"
        )
        title = payload.get("title") or payload.get("name") or payload.get("message") or ""
        summary = f"{event_key}: {title}".strip(": ").strip() or f"Webhook event: {event_key}"
        try:
            details = json.dumps(payload, indent=1, default=str)[:2000]
        except Exception:
            details = str(payload)[:2000]
        return {
            "summary": summary,
            "details": f"event: {event_key}\n{details}",
            "raw": payload,
            "event_key": str(event_key),
        }
