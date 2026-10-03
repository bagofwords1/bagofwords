# HTTP-family audit stream destinations: generic HTTPS, Datadog, Splunk HEC,
# Microsoft Sentinel (Logs Ingestion API).
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import hashlib
import hmac
import json
import socket
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from app.ee.audit.streams.destinations.base import (
    INVALID,
    RETRYABLE,
    Destination,
    FieldSpec,
    SendResult,
    classify_http,
    short_error,
)

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_USER_AGENT = "BagOfWords-AuditStream/1"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "bagofwords"


class _HttpDestination(Destination):
    def _verify(self) -> bool:
        v = self.cfg("verify_tls", True)
        return not (v is False or str(v).lower() in ("false", "0", "no"))

    async def _post(self, url: str, *, content: bytes, headers: Dict[str, str]) -> SendResult:
        headers = {"User-Agent": _USER_AGENT, **headers}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, verify=self._verify()) as client:
                r = await client.post(url, content=content, headers=headers)
        except httpx.TimeoutException as e:
            return SendResult(RETRYABLE, short_error(f"timeout: {e}"))
        except httpx.HTTPError as e:
            # Connection refused/reset, DNS, TLS handshake: transient from our
            # side. A TLS *verification* failure is a config problem.
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                return SendResult(INVALID, short_error(f"TLS verification failed: {e}"))
            return SendResult(RETRYABLE, short_error(f"{type(e).__name__}: {e}"))
        return self._classify(r)

    def _classify(self, r: httpx.Response) -> SendResult:
        return classify_http(r.status_code, r.text[:300])


class HttpsDestination(_HttpDestination):
    """POST a JSON array of envelopes; optional HMAC signature and auth header."""

    type = "https"
    max_batch = 500
    fields = [
        FieldSpec("url", "url", required=True),
        FieldSpec("auth_header_name", "text", default="Authorization", advanced=True),
        FieldSpec("auth_header_value", "text", secret=True),
        FieldSpec("hmac_secret", "text", secret=True),
        FieldSpec("headers", "headers", advanced=True),
        FieldSpec("verify_tls", "bool", default=True, advanced=True),
    ]

    @staticmethod
    def signature(secret: str, timestamp: str, body: bytes) -> str:
        mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
        return "v1=" + mac.hexdigest()

    async def send(self, events: List[dict]) -> SendResult:
        body = json.dumps(events, separators=(",", ":"), default=str).encode()
        headers = {"Content-Type": "application/json"}
        for k, v in (self.cfg("headers") or {}).items():
            headers[str(k)] = str(v)
        if self.secrets.get("auth_header_value"):
            headers[self.cfg("auth_header_name")] = self.secrets["auth_header_value"]
        if self.secrets.get("hmac_secret"):
            ts = str(int(time.time()))
            headers["X-BOW-Timestamp"] = ts
            headers["X-BOW-Signature"] = self.signature(self.secrets["hmac_secret"], ts, body)
        return await self._post(self.cfg("url"), content=body, headers=headers)


class DatadogDestination(_HttpDestination):
    """Datadog Logs intake v2: array of log entries, envelope JSON as message."""

    type = "datadog"
    max_batch = 500  # intake limits: 1000 entries / 5MB per request
    fields = [
        FieldSpec("site", "select", required=True, default="datadoghq.com",
                  options=["datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu", "ap1.datadoghq.com", "ddog-gov.com"]),
        FieldSpec("api_key", "text", required=True, secret=True),
        FieldSpec("service", "text", default="bagofwords"),
        FieldSpec("tags", "text", advanced=True),
        FieldSpec("intake_url", "url", advanced=True),
    ]

    def url(self) -> str:
        return self.cfg("intake_url") or f"https://http-intake.logs.{self.cfg('site')}/api/v2/logs"

    async def send(self, events: List[dict]) -> SendResult:
        host = _hostname()
        entries = []
        for e in events:
            entry = {
                "ddsource": "bagofwords",
                "service": self.cfg("service"),
                "hostname": host,
                "message": json.dumps(e, separators=(",", ":"), default=str),
            }
            if self.cfg("tags"):
                entry["ddtags"] = self.cfg("tags")
            entries.append(entry)
        body = json.dumps(entries, separators=(",", ":")).encode()
        return await self._post(
            self.url(), content=body,
            headers={"Content-Type": "application/json", "DD-API-KEY": self.secrets.get("api_key", "")},
        )


class SplunkDestination(_HttpDestination):
    """Splunk HTTP Event Collector: newline-delimited events."""

    type = "splunk"
    max_batch = 500
    fields = [
        FieldSpec("hec_url", "url", required=True),
        FieldSpec("token", "text", required=True, secret=True),
        FieldSpec("index", "text"),
        FieldSpec("sourcetype", "text", default="bagofwords:audit", advanced=True),
        FieldSpec("verify_tls", "bool", default=True, advanced=True),
    ]

    def url(self) -> str:
        base = self.cfg("hec_url").rstrip("/")
        return base if base.endswith("/services/collector/event") else base + "/services/collector/event"

    async def send(self, events: List[dict]) -> SendResult:
        host = _hostname()
        lines = []
        for e in events:
            item = {
                "time": _epoch(e.get("occurred_at")),
                "host": host,
                "source": "bagofwords",
                "sourcetype": self.cfg("sourcetype"),
                "event": e,
            }
            if self.cfg("index"):
                item["index"] = self.cfg("index")
            lines.append(json.dumps(item, separators=(",", ":"), default=str))
        return await self._post(
            self.url(), content="\n".join(lines).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Splunk {self.secrets.get('token', '')}"},
        )

    def _classify(self, r: httpx.Response) -> SendResult:
        # HEC answers 503 "server is busy" for back-pressure and 400 for bad
        # payloads or a disabled token (code 1/4 → auth).
        if r.status_code == 400:
            try:
                code = r.json().get("code")
            except Exception:
                code = None
            if code in (1, 2, 3, 4):
                return SendResult(INVALID, short_error(f"HTTP 400: {r.text[:300]}"), 400)
        return classify_http(r.status_code, r.text[:300])


class SentinelDestination(_HttpDestination):
    """Azure Monitor Logs Ingestion API (custom table behind a DCR) — what
    Microsoft Sentinel reads. Authenticates with Entra client credentials."""

    type = "sentinel"
    max_batch = 500  # 1MB per call limit; envelopes are small
    fields = [
        FieldSpec("tenant_id", "text", required=True),
        FieldSpec("client_id", "text", required=True),
        FieldSpec("client_secret", "text", required=True, secret=True),
        FieldSpec("dce_url", "url", required=True),
        FieldSpec("dcr_immutable_id", "text", required=True),
        FieldSpec("stream_name", "text", required=True, default="Custom-BagOfWordsAudit_CL"),
        FieldSpec("authority_url", "url", default="https://login.microsoftonline.com", advanced=True),
    ]
    API_VERSION = "2023-01-01"
    SCOPE = "https://monitor.azure.com/.default"

    def __init__(self, config, secrets):
        super().__init__(config, secrets)
        self._token: Optional[str] = None
        self._token_exp = 0.0

    async def _get_token(self) -> SendResult:
        if self._token and time.time() < self._token_exp - 60:
            return SendResult.success()
        url = f"{self.cfg('authority_url').rstrip('/')}/{self.cfg('tenant_id')}/oauth2/v2.0/token"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(url, data={
                    "grant_type": "client_credentials",
                    "client_id": self.cfg("client_id"),
                    "client_secret": self.secrets.get("client_secret", ""),
                    "scope": self.SCOPE,
                })
        except httpx.HTTPError as e:
            return SendResult(RETRYABLE, short_error(f"token endpoint: {e}"))
        if r.status_code in (400, 401, 403):
            return SendResult(INVALID, short_error(f"token endpoint HTTP {r.status_code}: {r.text[:300]}"), r.status_code)
        if r.status_code != 200:
            return classify_http(r.status_code, r.text[:300])
        data = r.json()
        self._token = data.get("access_token")
        self._token_exp = time.time() + float(data.get("expires_in", 3600))
        return SendResult.success()

    def url(self) -> str:
        return (
            f"{self.cfg('dce_url').rstrip('/')}/dataCollectionRules/{self.cfg('dcr_immutable_id')}"
            f"/streams/{self.cfg('stream_name')}?api-version={self.API_VERSION}"
        )

    async def send(self, events: List[dict]) -> SendResult:
        tok = await self._get_token()
        if not tok.ok:
            return tok
        records = [{"TimeGenerated": e.get("occurred_at"), **e} for e in events]
        res = await self._post(
            self.url(), content=json.dumps(records, separators=(",", ":"), default=str).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
        )
        if res.status == 401:
            self._token = None  # expired/revoked: refetch on the next attempt
        return res


def _epoch(iso: Optional[str]) -> float:
    if not iso:
        return time.time()
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return time.time()
