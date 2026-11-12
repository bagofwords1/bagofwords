"""Object-store file-source client (AWS S3).

Backs the `s3` data source type: an S3 bucket (optionally scoped to a key
prefix) exposed to the agent as a file catalog, exactly like `network_dir` but
over S3 instead of a mounted filesystem. All provider-neutral behavior —
confinement, parsing, windowed reads, grep, catalog — lives in
`_object_store_common.ObjectStoreClient`; this module owns only the S3 I/O and
auth.

Auth mirrors the Athena client: static keys, keys + STS assume-role, or boto3's
default credential chain. `endpoint_url` points it at any S3-compatible store.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, Optional, Tuple

from app.data_sources.clients._object_store_common import (  # noqa: F401  (re-exports)
    DEFAULT_WINDOW_BYTES,
    GREPPABLE_EXTS,
    TABULAR_EXTS,
    TEXT_EXTS,
    ObjectStoreClient,
    _ext,
    _extract_pdf_pages_from_bytes,
)


class S3Client(ObjectStoreClient):
    """S3-backed file source (bucket + optional prefix)."""

    scheme = "s3"
    metadata_key = "s3"
    bucket_noun = "bucket"

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        allowed_extensions: Optional[str] = None,
        include_globs: Optional[str] = None,
        recursive: bool = True,
        max_file_mb: int = 100,
        index_content: bool = True,
        index_mode: Optional[str] = None,
        max_catalog_objects: int = 5000,
        max_keywords: int = 50,
        # Credentials (one variant's worth is present at a time).
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        session_token: Optional[str] = None,
        role_arn: Optional[str] = None,
        **_ignored,
    ):
        super().__init__(
            bucket=bucket,
            prefix=prefix,
            allowed_extensions=allowed_extensions,
            include_globs=include_globs,
            recursive=recursive,
            max_file_mb=max_file_mb,
            index_content=index_content,
            index_mode=index_mode,
            max_catalog_objects=max_catalog_objects,
            max_keywords=max_keywords,
        )
        self.region = (region or "").strip() or None
        self.endpoint_url = (endpoint_url or "").strip() or None

        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.role_arn = role_arn

        self._s3 = None  # lazily constructed boto3 client

    # ------------------------------------------------------------- s3 client

    def _client(self):
        if self._s3 is not None:
            return self._s3
        import boto3

        if self.role_arn:
            # Assume-role: mint short-lived creds via STS, then build a session.
            # Static keys are OPTIONAL here — when absent, the STS client itself
            # resolves via the default chain (instance profile / IRSA / env), so
            # a deployment role can assume the target role with no stored secrets.
            # Mirrors AWSAthenaClient._renew_session.
            sts_kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.access_key and self.secret_key:
                sts_kwargs["aws_access_key_id"] = self.access_key
                sts_kwargs["aws_secret_access_key"] = self.secret_key
            sts = boto3.client("sts", **sts_kwargs)
            resp = sts.assume_role(RoleArn=self.role_arn, RoleSessionName="bow-s3")
            c = resp["Credentials"]
            session = boto3.Session(
                aws_access_key_id=c["AccessKeyId"],
                aws_secret_access_key=c["SecretAccessKey"],
                aws_session_token=c["SessionToken"],
                region_name=self.region,
            )
        elif self.access_key and self.secret_key:
            session = boto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                aws_session_token=self.session_token,
                region_name=self.region,
            )
        else:
            # Default credential chain (env, shared config, instance profile, IRSA).
            session = boto3.Session(region_name=self.region)

        self._s3 = session.client("s3", endpoint_url=self.endpoint_url)
        return self._s3

    # ---------------------------------------------------- storage primitives

    def _iter_list(self, list_prefix: str, recursive: bool) -> Iterator[Dict[str, Any]]:
        paginator = self._client().get_paginator("list_objects_v2")
        kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": list_prefix}
        if not recursive:
            kwargs["Delimiter"] = "/"
        for page in paginator.paginate(**kwargs):
            # Sub-prefixes become folder entries when not recursing.
            for cp in page.get("CommonPrefixes", []) or []:
                yield {"prefix": cp.get("Prefix")}
            for obj in page.get("Contents", []) or []:
                yield {"key": obj["Key"], "size": obj.get("Size", 0), "modified": obj.get("LastModified")}

    def _head(self, key: str) -> Dict[str, Any]:
        head = self._client().head_object(Bucket=self.bucket, Key=key)
        return {
            "size": int(head.get("ContentLength", 0)),
            "etag": head.get("ETag"),
            "modified": head.get("LastModified"),
            "version": ":".join(
                str(head.get(k) or "") for k in ("VersionId", "ETag", "ContentLength", "LastModified")
            ),
            "raw": head,
        }

    def _download(self, key: str, start: Optional[int] = None, end: Optional[int] = None) -> Tuple[bytes, Optional[int]]:
        args: Dict[str, Any] = dict(Bucket=self.bucket, Key=key)
        if start is not None:
            args["Range"] = f"bytes={start}-{'' if end is None else end}"
        obj = self._client().get_object(**args)
        body = obj["Body"]
        try:
            if start is not None and end is not None:
                data = body.read(end - start + 1)
            else:
                data = body.read()
        finally:
            body.close()
        total: Optional[int] = None
        # total size from Content-Range ("bytes 0-199/27221").
        cr = obj.get("ContentRange") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[-1])
            except ValueError:
                total = None
        elif start is None:
            total = len(data)
        return data, total

    def _open_body(self, key: str, head: Dict[str, Any], offset: int):
        from app.data_sources.clients._streaming_grep import FileChanged
        raw = head.get("raw") or {}
        args: Dict[str, Any] = dict(Bucket=self.bucket, Key=key)
        if offset:
            args["Range"] = f"bytes={offset}-"
        if raw.get("VersionId") and raw["VersionId"] != "null":
            args["VersionId"] = raw["VersionId"]
        if raw.get("ETag"):
            args["IfMatch"] = raw["ETag"]
        try:
            response = self._client().get_object(**args)
        except Exception as exc:
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code")) in ("PreconditionFailed", "412", "NoSuchVersion"):
                raise FileChanged() from exc
            raise
        body = response["Body"]
        if offset:
            # Some S3-compatible endpoints ignore Range. Never label bytes
            # from the beginning of the object as a resumed scan.
            content_range = response.get("ContentRange", "")
            if not content_range.startswith(f"bytes {offset}-"):
                body.close()
                raise OSError("S3 did not honor the requested byte range")
        return body

    def _probe(self) -> None:
        self._client().list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)

    def _is_not_found(self, exc: Exception) -> bool:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey") or super()._is_not_found(exc)
