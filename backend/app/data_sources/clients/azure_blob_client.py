"""Azure Blob Storage file-source client.

Backs the `azure_blob` data source type: a blob container (optionally scoped
to a prefix) exposed to the agent as a file catalog — the same primitives,
confinement and parsing as the S3 connector (all provider-neutral behavior is
in `_object_store_common.ObjectStoreClient`); this module owns only the Blob
I/O and auth. Data Lake Storage Gen2 accounts serve the same Blob API, so
hierarchical-namespace file systems work unchanged.

Auth: connection string, account key, SAS token, a service principal, or the
deployment's managed identity (DefaultAzureCredential).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.data_sources.clients._object_store_common import (
    ChunkedBody,
    ObjectNotFound,
    ObjectStoreClient,
)


class AzureBlobClient(ObjectStoreClient):
    """Azure Blob-backed file source (container + optional prefix)."""

    scheme = "az"
    metadata_key = "azure_blob"
    bucket_noun = "container"

    def __init__(
        self,
        container: str | None = None,
        account_name: str | None = None,
        prefix: str | None = None,
        account_url: str | None = None,
        include_globs: str | None = None,
        recursive: bool = True,
        max_file_mb: int = 100,
        index_mode: str | None = None,
        index_content: bool = True,
        max_catalog_objects: int = 5000,
        max_keywords: int = 50,
        allowed_extensions: str | None = None,
        # Credentials (one variant's worth is present at a time).
        connection_string: str | None = None,
        account_key: str | None = None,
        sas_token: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        **_ignored,
    ):
        super().__init__(
            bucket=container,
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
        self.account_name = (account_name or "").strip() or None
        if not self.account_name and connection_string:
            # Label the scope with the real account even when only a
            # connection string was given ("...;AccountName=acme;...").
            for part in str(connection_string).split(";"):
                k, _, v = part.partition("=")
                if k.strip().lower() == "accountname" and v.strip():
                    self.account_name = v.strip()
                    break
        self.account_url = (account_url or "").strip().rstrip("/") or None
        self.connection_string = connection_string
        self.account_key = account_key
        self.sas_token = (sas_token or "").strip().lstrip("?") or None
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._container_client = None

    @property
    def container(self) -> str:
        return self.bucket

    def _scope_uri(self) -> str:
        account = self.account_name or "account"
        return f"az://{account}/{self.bucket}/{self.prefix}"

    def _web_url(self, key: str) -> str:
        try:
            return f"{self._resolved_account_url()}/{self.bucket}/{key}"
        except ValueError:
            return f"az://{self.bucket}/{key}"

    # ---------------------------------------------------------------- client

    def _resolved_account_url(self) -> str:
        if self.account_url:
            return self.account_url
        if not self.account_name:
            raise ValueError("Storage account name (or an Account URL) is required.")
        return f"https://{self.account_name}.blob.core.windows.net"

    def _client(self):
        if self._container_client is not None:
            return self._container_client
        from azure.storage.blob import BlobServiceClient

        if self.connection_string:
            service = BlobServiceClient.from_connection_string(self.connection_string)
        else:
            url = self._resolved_account_url()
            if self.account_key:
                if not self.account_name:
                    raise ValueError("Storage account name is required with an account key.")
                credential: Any = {"account_name": self.account_name, "account_key": self.account_key}
            elif self.sas_token:
                credential = self.sas_token
            elif self.tenant_id and self.client_id and self.client_secret:
                from azure.identity import ClientSecretCredential

                credential = ClientSecretCredential(self.tenant_id, self.client_id, self.client_secret)
            else:
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            service = BlobServiceClient(account_url=url, credential=credential)
        self._container_client = service.get_container_client(self.bucket)
        return self._container_client

    # ---------------------------------------------------- storage primitives

    def _iter_list(self, list_prefix: str, recursive: bool) -> Iterator[dict[str, Any]]:
        container = self._client()
        if recursive:
            for b in container.list_blobs(name_starts_with=list_prefix or None):
                yield {"key": b.name, "size": b.size or 0, "modified": b.last_modified}
            return
        from azure.storage.blob import BlobPrefix

        for item in container.walk_blobs(name_starts_with=list_prefix or None, delimiter="/"):
            if isinstance(item, BlobPrefix):
                yield {"prefix": item.name}
            else:
                yield {"key": item.name, "size": item.size or 0, "modified": item.last_modified}

    def _head(self, key: str) -> dict[str, Any]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            props = self._client().get_blob_client(key).get_blob_properties()
        except ResourceNotFoundError as exc:
            raise ObjectNotFound(key) from exc
        return {
            "size": int(props.size or 0),
            "etag": props.etag,
            "modified": props.last_modified,
            "version": f"{props.etag}:{props.size}:{props.last_modified}",
        }

    def _download(self, key: str, start: int | None = None, end: int | None = None) -> tuple[bytes, int | None]:
        from azure.core.exceptions import HttpResponseError

        blob = self._client().get_blob_client(key)
        if start is None:
            downloader = blob.download_blob()
            data = downloader.readall()
            return data, len(data)
        try:
            length = None if end is None else end - start + 1
            downloader = blob.download_blob(offset=start, length=length)
        except HttpResponseError as exc:
            if getattr(exc, "status_code", None) == 416:  # range starts past EOF
                return b"", int(self._head(key)["size"])
            raise
        data = downloader.readall()
        # On a ranged download `properties.size` is the RANGE length; the
        # object's total size is the tail of Content-Range ("bytes 0-99/25071").
        total: int | None = None
        content_range = getattr(downloader.properties, "content_range", None) or ""
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                total = None
        if total is None:
            total = int(self._head(key)["size"])
        return data, total

    def _open_body(self, key: str, head: dict[str, Any], offset: int):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

        from app.data_sources.clients._streaming_grep import FileChanged

        blob = self._client().get_blob_client(key)
        try:
            downloader = blob.download_blob(
                offset=offset or None,
                etag=head.get("etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceModifiedError, ResourceNotFoundError) as exc:
            raise FileChanged() from exc
        return ChunkedBody(downloader.chunks())

    def _probe(self) -> None:
        for _ in self._client().list_blobs(name_starts_with=self.prefix or None, results_per_page=1):
            break

    def _is_not_found(self, exc: Exception) -> bool:
        try:
            from azure.core.exceptions import ResourceNotFoundError

            if isinstance(exc, ResourceNotFoundError):
                return True
        except ImportError:  # pragma: no cover
            pass
        return super()._is_not_found(exc)
