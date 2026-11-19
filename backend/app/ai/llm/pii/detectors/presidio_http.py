"""Presidio analyzer over HTTP (``pii.presidio_url``).

Speaks the analyzer's REST API — ``POST {url}/analyze`` with
``{"text", "language", "entities"}`` returning
``[{"entity_type", "start", "end", "score"}]`` — so it works with the official
``mcr.microsoft.com/presidio-analyzer`` container and with any service that
exposes the same endpoint (e.g. one serving a GLiNER model).

Used from worker threads (the LLM chokepoint keeps detector work off the event
loop), so a plain blocking client with a firm timeout is the right tool. Any
failure — unreachable, timeout, non-200, malformed body — is a DetectorError:
the policy decides whether the request is refused or sent with pattern rules
only.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional, Sequence

import httpx

from .base import DetectedSpan, DetectorError
from .catalog import DETECTOR_ENTITIES

logger = logging.getLogger(__name__)


class PresidioHttpDetector:
    def __init__(self, url: str, *, timeout: float = 10.0, language: str = "en", transport: Optional[httpx.BaseTransport] = None):
        self.url = url.rstrip("/")
        self.language = language or "en"
        self.timeout = timeout
        self.name = f"presidio-http:{self.url}"
        self._client: Optional[httpx.Client] = None
        self._transport = transport
        self._lock = threading.Lock()

    def _http(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    # trust_env=False: an in-cluster service must not be sent
                    # through an outbound proxy.
                    self._client = httpx.Client(timeout=self.timeout, trust_env=False, transport=self._transport)
        return self._client

    def supported_entities(self) -> List[str]:
        return [e["id"] for e in DETECTOR_ENTITIES]

    def detect(self, text: str, entities: Sequence[str], language: Optional[str] = None) -> List[DetectedSpan]:
        try:
            resp = self._http().post(
                f"{self.url}/analyze",
                json={"text": text, "language": language or self.language, "entities": list(entities)},
            )
        except httpx.HTTPError as exc:
            raise DetectorError(f"PII detector service unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise DetectorError(f"PII detector service returned HTTP {resp.status_code}")
        try:
            body = resp.json()
            if not isinstance(body, list):
                raise ValueError("expected a list")
            wanted = set(entities)
            spans = []
            for r in body:
                # Every item must be well-formed: a response we only half
                # understand is not evidence that the text is clean.
                span = DetectedSpan(int(r["start"]), int(r["end"]), str(r["entity_type"]), float(r.get("score", 1.0)))
                if span.entity in wanted:
                    spans.append(span)
        except Exception as exc:
            raise DetectorError("PII detector service returned an unreadable response") from exc
        n = len(text)
        return [s for s in spans if 0 <= s.start < s.end <= n]
