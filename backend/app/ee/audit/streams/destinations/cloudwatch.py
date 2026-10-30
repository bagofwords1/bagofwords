# Amazon CloudWatch Logs audit stream destination.
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import asyncio
import json
from datetime import UTC, datetime

from app.core.egress import Policy, check_url
from app.core.url_safety import UnsafeURLError
from app.ee.audit.streams.destinations.base import (
    FATAL,
    INVALID,
    RETRYABLE,
    Destination,
    FieldSpec,
    SendResult,
    short_error,
)

_INVALID_CODES = {
    "AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException",
    "ExpiredTokenException", "InvalidClientTokenId", "AccessDenied", "InvalidAccessKeyId",
    "SignatureDoesNotMatch", "ExpiredToken", "AuthFailure",
}
_FATAL_CODES = {"InvalidParameterException", "DataAlreadyAcceptedException", "MalformedQueryException"}
_RETRYABLE_CODES = {
    "ThrottlingException", "ServiceUnavailableException", "InternalFailure", "LimitExceededException",
    "OperationAbortedException", "RequestExpired",
}

# PutLogEvents limits: 10,000 events / 1,048,576 bytes per call, each event
# counted as its UTF-8 size plus 26 bytes, and no event older than 14 days or
# more than 2 hours in the future (those are silently dropped by the service).
_MAX_BYTES = 1_048_576
_EVENT_OVERHEAD = 26
_MAX_EVENT_BYTES = 256 * 1024
_MAX_PAST = 14 * 24 * 3600 * 1000
_MAX_FUTURE = 2 * 3600 * 1000


def _epoch_ms(iso: str | None, now_ms: int) -> int:
    """Envelope ``occurred_at`` (ISO-8601, UTC) → epoch ms, clamped into the
    window CloudWatch accepts so an old backfill is not silently discarded."""
    ts = now_ms
    if iso:
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ts = int(dt.timestamp() * 1000)
        except ValueError:
            ts = now_ms
    return max(now_ms - _MAX_PAST + 60_000, min(ts, now_ms + _MAX_FUTURE - 60_000))


def chunk_log_events(events: list[dict], now_ms: int | None = None) -> list[list[dict]]:
    """Encode envelopes as CloudWatch ``{timestamp, message}`` entries, sorted
    by timestamp as the API requires, split into calls under the 1 MB limit."""
    now_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    entries = []
    for e in events:
        msg = json.dumps(e, separators=(",", ":"), default=str)
        if len(msg.encode()) > _MAX_EVENT_BYTES:
            # Keep the event rather than drop it: trim the only unbounded field.
            slim = dict(e)
            slim["metadata"] = {"truncated": True}
            msg = json.dumps(slim, separators=(",", ":"), default=str)
        entries.append({"timestamp": _epoch_ms(e.get("occurred_at"), now_ms), "message": msg})
    entries.sort(key=lambda x: x["timestamp"])
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for en in entries:
        n = len(en["message"].encode()) + _EVENT_OVERHEAD
        if cur and (size + n > _MAX_BYTES or len(cur) >= 10_000):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(en)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


class CloudWatchDestination(Destination):
    type = "cloudwatch"
    max_batch = 1000
    fields = [
        FieldSpec("log_group", "text", required=True, default="/bagofwords/audit"),
        FieldSpec("log_stream", "text", required=True, default="audit"),
        FieldSpec("region", "text", required=True, default="us-east-1"),
        FieldSpec("role_arn", "text"),
        FieldSpec("external_id", "text", advanced=True),
        FieldSpec("access_key_id", "text", secret=True),
        FieldSpec("secret_access_key", "text", secret=True),
        FieldSpec("create_log_group", "bool", default=True, advanced=True),
        FieldSpec("endpoint_url", "url", advanced=True),
        FieldSpec("sts_endpoint_url", "url", advanced=True),
    ]

    def _flag(self, key: str, default: bool) -> bool:
        v = self.cfg(key, default)
        return v is True or str(v).lower() in ("true", "1", "yes")

    def _check_endpoints(self) -> None:
        for key in ("endpoint_url", "sts_endpoint_url"):
            url = self.cfg(key)
            if url:
                check_url(str(url), Policy.ADMIN)

    def _client(self):
        import boto3
        from botocore.config import Config

        self._check_endpoints()
        region = self.cfg("region")
        creds = {}
        if self.secrets.get("access_key_id") and self.secrets.get("secret_access_key"):
            creds = {
                "aws_access_key_id": self.secrets["access_key_id"],
                "aws_secret_access_key": self.secrets["secret_access_key"],
            }
        if self.cfg("role_arn"):
            sts = boto3.client("sts", region_name=region, endpoint_url=self.cfg("sts_endpoint_url") or None, **creds)
            params = {"RoleArn": self.cfg("role_arn"), "RoleSessionName": "bagofwords-audit-stream"}
            if self.cfg("external_id"):
                params["ExternalId"] = self.cfg("external_id")
            c = sts.assume_role(**params)["Credentials"]
            creds = {
                "aws_access_key_id": c["AccessKeyId"],
                "aws_secret_access_key": c["SecretAccessKey"],
                "aws_session_token": c["SessionToken"],
            }
        cfg = Config(
            retries={"total_max_attempts": 1, "mode": "standard"},  # the exporter owns retry/backoff
            connect_timeout=10,
            read_timeout=20,
        )
        return boto3.client("logs", region_name=region, endpoint_url=self.cfg("endpoint_url") or None, config=cfg, **creds)

    def _ensure_stream(self, client) -> None:
        """Create the log group (opt-in, default on) and stream when missing.
        Idempotent: ResourceAlreadyExists from a concurrent creator is fine."""
        from botocore.exceptions import ClientError

        group, stream = self.cfg("log_group"), self.cfg("log_stream")
        try:
            client.create_log_stream(logGroupName=group, logStreamName=stream)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceAlreadyExistsException":
                return
            if code != "ResourceNotFoundException" or not self._flag("create_log_group", True):
                raise
            try:
                client.create_log_group(logGroupName=group)
            except ClientError as e2:
                if e2.response.get("Error", {}).get("Code", "") != "ResourceAlreadyExistsException":
                    raise
            try:
                client.create_log_stream(logGroupName=group, logStreamName=stream)
            except ClientError as e3:
                if e3.response.get("Error", {}).get("Code", "") != "ResourceAlreadyExistsException":
                    raise

    def _put(self, events: list[dict]) -> SendResult:
        from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError

        group, stream = self.cfg("log_group"), self.cfg("log_stream")
        try:
            client = self._client()
            ensured = False
            for chunk in chunk_log_events(events):
                for attempt in (0, 1):
                    try:
                        resp = client.put_log_events(logGroupName=group, logStreamName=stream, logEvents=chunk)
                        break
                    except ClientError as e:
                        code = e.response.get("Error", {}).get("Code", "")
                        if code == "ResourceNotFoundException" and attempt == 0 and not ensured:
                            self._ensure_stream(client)
                            ensured = True
                            continue
                        raise
                rejected = resp.get("rejectedLogEventsInfo") or {}
                if rejected:
                    # Only reachable if clamping failed; the cursor still moves
                    # (the service accepted the call) but the operator sees it.
                    return SendResult(FATAL, short_error(f"CloudWatch rejected events: {rejected}"))
        except UnsafeURLError as e:
            return SendResult(INVALID, short_error(f"destination refused: {e}"))
        except NoCredentialsError as e:
            return SendResult(INVALID, short_error(str(e)))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            msg = short_error(f"{code}: {e}")
            if code in _INVALID_CODES or status in (401, 403):
                return SendResult(INVALID, msg, status)
            if code in _RETRYABLE_CODES or (status or 0) >= 500 or status == 429:
                return SendResult(RETRYABLE, msg, status)
            if code in _FATAL_CODES or code == "ResourceNotFoundException" or status == 404:
                return SendResult(FATAL, msg, status)
            return SendResult(RETRYABLE, msg, status)
        except (EndpointConnectionError, BotoCoreError) as e:
            return SendResult(RETRYABLE, short_error(f"{type(e).__name__}: {e}"))
        return SendResult.success(200)

    async def send(self, events: list[dict]) -> SendResult:
        return await asyncio.to_thread(self._put, events)
