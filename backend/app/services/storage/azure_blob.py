"""Azure Blob Storage."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from .base import ObjectStore, StorageError

logger = logging.getLogger(__name__)


def build_container_client(cfg):
    """A ContainerClient for the configured auth mode (see AzureStorageConfig)."""
    try:
        from azure.storage.blob import ContainerClient
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise StorageError("storage.backend=azure needs the azure-storage-blob package") from e

    if not cfg.container:
        raise StorageError("storage.azure.container is required when storage.backend=azure")

    if cfg.auth == "connection_string":
        if not cfg.connection_string:
            raise StorageError("storage.azure.auth=connection_string needs connection_string")
        return ContainerClient.from_connection_string(cfg.connection_string, container_name=cfg.container)

    if not cfg.account_url:
        raise StorageError("storage.azure.account_url is required (https://<account>.blob.core.windows.net)")

    if cfg.auth == "sas":
        if not cfg.sas_token:
            raise StorageError("storage.azure.auth=sas needs sas_token")
        token = cfg.sas_token.lstrip("?")
        return ContainerClient(account_url=cfg.account_url, container_name=cfg.container, credential=token)

    if cfg.auth == "service_principal":
        if not (cfg.tenant_id and cfg.client_id and cfg.client_secret):
            raise StorageError("storage.azure.auth=service_principal needs tenant_id, client_id and client_secret")
        from azure.identity import ClientSecretCredential
        cred = ClientSecretCredential(cfg.tenant_id, cfg.client_id, cfg.client_secret)
        return ContainerClient(account_url=cfg.account_url, container_name=cfg.container, credential=cred)

    from azure.identity import DefaultAzureCredential
    kwargs = {"managed_identity_client_id": cfg.client_id} if cfg.client_id else {}
    cred = DefaultAzureCredential(**kwargs)
    return ContainerClient(account_url=cfg.account_url, container_name=cfg.container, credential=cred)


class AzureBlobStore(ObjectStore):
    name = "azure"

    def __init__(self, cfg, prefix: str = ""):
        super().__init__(prefix)
        self._cc = build_container_client(cfg)
        self._where = f"{cfg.account_url or 'connection-string'}/{cfg.container}"

    def describe(self) -> str:
        return f"azure://{self._where}/{self.prefix}"

    @staticmethod
    def _errors():
        from azure.core.exceptions import AzureError, ResourceNotFoundError
        return AzureError, ResourceNotFoundError

    def upload(self, key, local_path, content_type=None):
        from azure.storage.blob import ContentSettings
        AzureError, _ = self._errors()
        try:
            with open(local_path, "rb") as fh:
                # BlobClient.upload_blob returns the new properties (etag);
                # ContainerClient.upload_blob returns a client instead.
                res = self._cc.get_blob_client(self._k(key)).upload_blob(
                    fh, overwrite=True,
                    content_settings=ContentSettings(content_type=content_type) if content_type else None,
                    max_concurrency=4,
                )
            return (res or {}).get("etag")
        except AzureError as e:
            raise StorageError(f"Azure upload of {key!r} failed: {e}") from e

    def download(self, key, local_path):
        AzureError, NotFound = self._errors()
        try:
            stream = self._cc.download_blob(self._k(key), max_concurrency=4)
            with open(local_path, "wb") as fh:
                stream.readinto(fh)
            return stream.properties.etag
        except NotFound:
            return None
        except AzureError as e:
            raise StorageError(f"Azure download of {key!r} failed: {e}") from e

    def etag(self, key):
        AzureError, NotFound = self._errors()
        try:
            return self._cc.get_blob_client(self._k(key)).get_blob_properties().etag
        except NotFound:
            return None
        except AzureError as e:
            raise StorageError(f"Azure head of {key!r} failed: {e}") from e

    def delete(self, key):
        AzureError, NotFound = self._errors()
        try:
            self._cc.delete_blob(self._k(key))
        except NotFound:
            return
        except AzureError as e:
            raise StorageError(f"Azure delete of {key!r} failed: {e}") from e

    def list(self, prefix) -> Iterator[str]:
        AzureError, _ = self._errors()
        try:
            for blob in self._cc.list_blobs(name_starts_with=self._k(prefix)):
                yield self._strip(blob.name)
        except AzureError as e:
            raise StorageError(f"Azure list of {prefix!r} failed: {e}") from e

    def list_detailed(self, prefix):
        AzureError, _ = self._errors()
        try:
            for blob in self._cc.list_blobs(name_starts_with=self._k(prefix)):
                modified = blob.last_modified
                yield self._strip(blob.name), modified.timestamp() if modified else None
        except AzureError as e:
            raise StorageError(f"Azure list of {prefix!r} failed: {e}") from e
