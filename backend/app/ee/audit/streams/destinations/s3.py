# Object-storage audit stream destinations: AWS S3 and Google Cloud Storage
# (through GCS's S3-compatible XML API with HMAC keys).
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import asyncio
import base64
import gzip
import hashlib
import json
from datetime import datetime
from typing import List

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
    "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken",
    "InvalidClientTokenId", "UnrecognizedClientException", "AllAccessDisabled", "AccountProblem",
}
_FATAL_CODES = {"NoSuchBucket", "InvalidBucketName", "InvalidArgument", "InvalidRequest", "MalformedXML"}


def object_key(prefix: str, events: List[dict], gz: bool) -> str:
    """Deterministic key: the batch's first event names it, so a retried batch
    overwrites the same object instead of creating a duplicate."""
    first = events[0]
    ts = first.get("occurred_at") or datetime.utcnow().isoformat()
    day = ts[:10]
    compact = ts.replace("-", "").replace(":", "").replace(".", "")
    prefix = (prefix or "").strip("/")
    name = f"{day}/{compact}_{first.get('id')}.json" + (".gz" if gz else "")
    return f"{prefix}/{name}" if prefix else name


class S3Destination(Destination):
    type = "s3"
    max_batch = 1000
    fields = [
        FieldSpec("bucket", "text", required=True),
        FieldSpec("region", "text", required=True, default="us-east-1"),
        FieldSpec("prefix", "text", default="bagofwords/audit"),
        FieldSpec("role_arn", "text"),
        FieldSpec("external_id", "text", advanced=True),
        FieldSpec("access_key_id", "text", secret=True),
        FieldSpec("secret_access_key", "text", secret=True),
        FieldSpec("gzip", "bool", default=False),
        FieldSpec("endpoint_url", "url", advanced=True),
        FieldSpec("sts_endpoint_url", "url", advanced=True),
    ]
    default_endpoint = None

    def _gzip(self) -> bool:
        v = self.cfg("gzip", False)
        return v is True or str(v).lower() in ("true", "1", "yes")

    def _client(self):
        import boto3
        from botocore.config import Config

        endpoint = self.cfg("endpoint_url") or self.default_endpoint
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
        # Non-AWS endpoints (GCS, MinIO) reject the CRC32 checksum headers newer
        # botocore sends by default; Content-MD5 is set explicitly instead.
        cfg = Config(
            retries={"total_max_attempts": 1, "mode": "standard"},  # the exporter owns retry/backoff
            connect_timeout=10,
            read_timeout=20,
            s3={"addressing_style": "path"} if endpoint else {},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        return boto3.client("s3", region_name=region, endpoint_url=endpoint, config=cfg, **creds)

    def _put(self, events: List[dict]) -> SendResult:
        from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError

        gz = self._gzip()
        body = ("\n".join(json.dumps(e, separators=(",", ":"), default=str) for e in events) + "\n").encode()
        if gz:
            body = gzip.compress(body, mtime=0)
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        key = object_key(self.cfg("prefix"), events, gz)
        try:
            client = self._client()
            extra = {"ContentEncoding": "gzip"} if gz else {}
            client.put_object(
                Bucket=self.cfg("bucket"), Key=key, Body=body, ContentMD5=md5,
                ContentType="application/x-ndjson", **extra,
            )
        except NoCredentialsError as e:
            return SendResult(INVALID, short_error(str(e)))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            msg = short_error(f"{code}: {e}")
            if code in _INVALID_CODES or status in (401, 403):
                return SendResult(INVALID, msg, status)
            if code in _FATAL_CODES or status == 404:
                return SendResult(FATAL, msg, status)
            return SendResult(RETRYABLE, msg, status)
        except (EndpointConnectionError, BotoCoreError) as e:
            return SendResult(RETRYABLE, short_error(f"{type(e).__name__}: {e}"))
        return SendResult.success(200)

    async def send(self, events: List[dict]) -> SendResult:
        return await asyncio.to_thread(self._put, events)


class GcsDestination(S3Destination):
    type = "gcs"
    fields = [
        FieldSpec("bucket", "text", required=True),
        FieldSpec("prefix", "text", default="bagofwords/audit"),
        FieldSpec("access_key_id", "text", required=True, secret=True),
        FieldSpec("secret_access_key", "text", required=True, secret=True),
        FieldSpec("gzip", "bool", default=False),
        FieldSpec("region", "text", default="auto", advanced=True),
        FieldSpec("endpoint_url", "url", default="https://storage.googleapis.com", advanced=True),
    ]
    default_endpoint = "https://storage.googleapis.com"
