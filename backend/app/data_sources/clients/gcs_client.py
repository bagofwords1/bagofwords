"""Google Cloud Storage file-source client.

Backs the `gcs` data source type: a Cloud Storage bucket (optionally scoped to
a prefix) exposed to the agent as a file catalog — the same primitives,
confinement and parsing as the S3 connector (all provider-neutral behavior is
in `_object_store_common.ObjectStoreClient`); this module owns only the Cloud
Storage I/O and auth.

Auth: a service-account key, or Application Default Credentials (workload
identity / VM service account / GOOGLE_APPLICATION_CREDENTIALS). With no
credentials and a custom `endpoint_url`, it connects anonymously — for
emulators and public buckets.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from app.data_sources.clients._object_store_common import ObjectNotFound, ObjectStoreClient

_READ_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"


class GCSClient(ObjectStoreClient):
    """Cloud Storage-backed file source (bucket + optional prefix)."""

    scheme = "gs"
    metadata_key = "gcs"
    bucket_noun = "bucket"

    def __init__(
        self,
        bucket: str | None = None,
        prefix: str | None = None,
        project: str | None = None,
        endpoint_url: str | None = None,
        include_globs: str | None = None,
        recursive: bool = True,
        max_file_mb: int = 100,
        index_mode: str | None = None,
        index_content: bool = True,
        max_catalog_objects: int = 5000,
        max_keywords: int = 50,
        allowed_extensions: str | None = None,
        # Credentials (one variant's worth is present at a time).
        service_account_json: str | None = None,
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
        self.project = (project or "").strip() or None
        self.endpoint_url = (endpoint_url or "").strip().rstrip("/") or None
        self.service_account_json = service_account_json
        self._gcs = None  # lazily constructed google.cloud.storage.Client

    # ---------------------------------------------------------------- client

    def _client(self):
        if self._gcs is not None:
            return self._gcs
        from google.cloud import storage

        client_options = {"api_endpoint": self.endpoint_url} if self.endpoint_url else None
        if self.service_account_json:
            from google.oauth2 import service_account

            try:
                info = json.loads(self.service_account_json)
            except json.JSONDecodeError as e:
                raise ValueError(f"Service account JSON is not valid JSON: {e}") from e
            creds = service_account.Credentials.from_service_account_info(info, scopes=[_READ_SCOPE])
            self._gcs = storage.Client(
                project=self.project or info.get("project_id"),
                credentials=creds,
                client_options=client_options,
            )
        elif self.endpoint_url:
            # Emulator / public bucket: no Google auth round-trip.
            from google.auth.credentials import AnonymousCredentials

            self._gcs = storage.Client(
                project=self.project or "anonymous",
                credentials=AnonymousCredentials(),
                client_options=client_options,
            )
        else:
            import google.auth

            creds, default_project = google.auth.default(scopes=[_READ_SCOPE])
            self._gcs = storage.Client(
                project=self.project or default_project,
                credentials=creds,
                client_options=client_options,
            )
        return self._gcs

    def _bucket(self):
        return self._client().bucket(self.bucket)

    # ---------------------------------------------------- storage primitives

    def _iter_list(self, list_prefix: str, recursive: bool) -> Iterator[dict[str, Any]]:
        it = self._client().list_blobs(
            self.bucket,
            prefix=list_prefix or None,
            delimiter=None if recursive else "/",
        )
        for page in it.pages:
            for p in getattr(page, "prefixes", None) or ():
                yield {"prefix": p}
            for blob in page:
                yield {"key": blob.name, "size": blob.size or 0, "modified": blob.updated}

    def _get_blob(self, key: str):
        blob = self._bucket().get_blob(key)
        if blob is None:
            raise ObjectNotFound(key)
        return blob

    def _head(self, key: str) -> dict[str, Any]:
        blob = self._get_blob(key)
        return {
            "size": int(blob.size or 0),
            "etag": blob.etag,
            "modified": blob.updated,
            "generation": blob.generation,
            "version": f"{blob.generation}:{blob.etag}:{blob.size}",
        }

    def _download(self, key: str, start: int | None = None, end: int | None = None) -> tuple[bytes, int | None]:
        if start is None:
            blob = self._bucket().blob(key)
            data = blob.download_as_bytes()
            return data, len(data)
        # A ranged read needs the total size for the cursor, and must not ask
        # for a range past EOF (the API answers 416).
        blob = self._get_blob(key)
        size = int(blob.size or 0)
        if start >= size:
            return b"", size
        last = size - 1 if end is None else min(end, size - 1)
        data = blob.download_as_bytes(start=start, end=last, if_generation_match=blob.generation)
        return data, size

    def _open_body(self, key: str, head: dict[str, Any], offset: int):
        from google.api_core import exceptions as gexc

        from app.data_sources.clients._streaming_grep import FileChanged

        # Pin the read to the generation we saw: a rewrite between the
        # metadata call and the read fails instead of mixing two versions.
        blob = self._bucket().blob(key, generation=head.get("generation"))
        try:
            reader = blob.open("rb", chunk_size=1024 * 1024)
            if offset:
                reader.seek(offset)
        except gexc.NotFound as exc:
            raise FileChanged() from exc
        return reader

    def _probe(self) -> None:
        it = self._client().list_blobs(self.bucket, prefix=self.prefix or None, max_results=1)
        for _ in it:
            break

    def _is_not_found(self, exc: Exception) -> bool:
        try:
            from google.api_core import exceptions as gexc

            if isinstance(exc, gexc.NotFound):
                return True
        except ImportError:  # pragma: no cover - dependency always present
            pass
        return super()._is_not_found(exc)
