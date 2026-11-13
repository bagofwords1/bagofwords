"""S3 and S3-compatible object stores (AWS, MinIO, R2, Wasabi, Ceph, ...)."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from .base import ObjectStore, StorageError

logger = logging.getLogger(__name__)


def _client_error_code(exc) -> str:
    try:
        return str(exc.response.get("Error", {}).get("Code", ""))
    except Exception:
        return ""


def _not_found(exc) -> bool:
    return _client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}


def build_boto3_session(cfg):
    """A boto3 Session for the configured auth mode.

    'default' uses boto3's own chain (IRSA, Pod Identity, ECS/EC2 roles, env,
    profile). 'static' uses the configured keys. 'assume_role' assumes
    ``role_arn`` with auto-refreshing STS credentials on top of whichever of
    the two is available, so a long-running server never holds expired keys.
    """
    import boto3

    region = cfg.region or None
    if cfg.auth == "static":
        if not (cfg.access_key_id and cfg.secret_access_key):
            raise StorageError("storage.s3.auth=static needs access_key_id and secret_access_key")
        return boto3.Session(
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            aws_session_token=cfg.session_token or None,
            region_name=region,
        )

    if cfg.auth == "assume_role":
        if not cfg.role_arn:
            raise StorageError("storage.s3.auth=assume_role needs role_arn")
        from botocore.credentials import RefreshableCredentials
        from botocore.session import get_session

        if cfg.access_key_id and cfg.secret_access_key:
            base = boto3.Session(
                aws_access_key_id=cfg.access_key_id,
                aws_secret_access_key=cfg.secret_access_key,
                aws_session_token=cfg.session_token or None,
                region_name=region,
            )
        else:
            base = boto3.Session(region_name=region)
        sts = base.client("sts")

        def _refresh():
            kwargs = {"RoleArn": cfg.role_arn, "RoleSessionName": "bow-storage"}
            if cfg.external_id:
                kwargs["ExternalId"] = cfg.external_id
            c = sts.assume_role(**kwargs)["Credentials"]
            return {
                "access_key": c["AccessKeyId"],
                "secret_key": c["SecretAccessKey"],
                "token": c["SessionToken"],
                "expiry_time": c["Expiration"].isoformat(),
            }

        creds = RefreshableCredentials.create_from_metadata(
            metadata=_refresh(), refresh_using=_refresh, method="sts-assume-role"
        )
        botocore_session = get_session()
        botocore_session._credentials = creds  # documented pattern for refreshable creds
        if region:
            botocore_session.set_config_variable("region", region)
        return boto3.Session(botocore_session=botocore_session)

    return boto3.Session(region_name=region)


class S3Store(ObjectStore):
    name = "s3"

    def __init__(self, cfg, prefix: str = ""):
        super().__init__(prefix)
        if not cfg.bucket:
            raise StorageError("storage.s3.bucket is required when storage.backend=s3")
        from botocore.config import Config

        self.bucket = cfg.bucket
        self._sse = cfg.sse or ""
        self._kms_key_id = cfg.kms_key_id or ""
        session = build_boto3_session(cfg)
        s3_opts = {}
        if cfg.addressing_style in ("path", "virtual"):
            s3_opts["addressing_style"] = cfg.addressing_style
        self._client = session.client(
            "s3",
            endpoint_url=cfg.endpoint_url or None,
            config=Config(s3=s3_opts or None, retries={"max_attempts": 5, "mode": "standard"}),
        )
        self._endpoint = cfg.endpoint_url or "aws"

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix} ({self._endpoint})"

    def _extra(self, content_type: str | None) -> dict:
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        if self._sse:
            extra["ServerSideEncryption"] = self._sse
            if self._sse == "aws:kms" and self._kms_key_id:
                extra["SSEKMSKeyId"] = self._kms_key_id
        return extra

    def upload(self, key, local_path, content_type=None):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self._client.upload_file(local_path, self.bucket, self._k(key), ExtraArgs=self._extra(content_type))
            return self.etag(key)
        except (ClientError, BotoCoreError) as e:
            raise StorageError(f"S3 upload of {key!r} failed: {e}") from e

    def download(self, key, local_path):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=self._k(key))
        except ClientError as e:
            if _not_found(e):
                return None
            raise StorageError(f"S3 head of {key!r} failed: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"S3 head of {key!r} failed: {e}") from e
        try:
            self._client.download_file(self.bucket, self._k(key), local_path)
        except ClientError as e:
            if _not_found(e):
                return None
            raise StorageError(f"S3 download of {key!r} failed: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"S3 download of {key!r} failed: {e}") from e
        return head.get("ETag")

    def etag(self, key):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return self._client.head_object(Bucket=self.bucket, Key=self._k(key)).get("ETag")
        except ClientError as e:
            if _not_found(e):
                return None
            raise StorageError(f"S3 head of {key!r} failed: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"S3 head of {key!r} failed: {e}") from e

    def delete(self, key):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._k(key))
        except ClientError as e:
            if _not_found(e):
                return
            raise StorageError(f"S3 delete of {key!r} failed: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"S3 delete of {key!r} failed: {e}") from e

    def list(self, prefix) -> Iterator[str]:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self._k(prefix)
            )
            for page in pages:
                for obj in page.get("Contents", []) or []:
                    yield self._strip(obj["Key"])
        except (ClientError, BotoCoreError) as e:
            raise StorageError(f"S3 list of {prefix!r} failed: {e}") from e

    def list_detailed(self, prefix):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self._k(prefix)
            )
            for page in pages:
                for obj in page.get("Contents", []) or []:
                    modified = obj.get("LastModified")
                    yield self._strip(obj["Key"]), modified.timestamp() if modified else None
        except (ClientError, BotoCoreError) as e:
            raise StorageError(f"S3 list of {prefix!r} failed: {e}") from e

    def delete_prefix(self, prefix) -> int:
        from botocore.exceptions import BotoCoreError, ClientError
        keys = list(self.list(prefix))
        try:
            for i in range(0, len(keys), 1000):
                chunk = keys[i:i + 1000]
                self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": self._k(k)} for k in chunk], "Quiet": True},
                )
        except (ClientError, BotoCoreError):
            # Some S3-compatible stores lack multi-object delete; fall back.
            for k in keys:
                self.delete(k)
        return len(keys)
