"""Object-storage backends for tests.

``object_store`` runs a test once per backend:

* ``s3-moto``        in-process fake S3 (always available)
* ``azure-azurite``  Microsoft's Azure Blob emulator, when one is listening
                     (``BOW_TEST_AZURITE_CONNECTION_STRING``, or the default
                     Azurite endpoint on 127.0.0.1:10000)
* ``s3-real``        a real bucket, when ``BOW_TEST_S3_BUCKET`` (+ AWS
                     credentials in the default chain) is set
* ``azure-real``     a real container, when ``BOW_TEST_AZURE_ACCOUNT_URL``,
                     ``BOW_TEST_AZURE_CONTAINER`` and ``BOW_TEST_AZURE_SAS_TOKEN``
                     are set

Each run gets its own key prefix and removes what it wrote. Secrets come from
the environment only — never commit them.
"""
from __future__ import annotations

import os
import socket
import uuid
from contextlib import contextmanager

import pytest

from app.services import storage
from app.settings.bow_config import AzureStorageConfig, S3StorageConfig

# Azurite's documented, public development-account key (not a secret).
_AZURITE_DEV_CS = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)

OBJECT_STORE_KINDS = ["s3-moto", "azure-azurite", "s3-real", "azure-real"]


def _azurite_connection_string() -> str | None:
    cs = os.environ.get("BOW_TEST_AZURITE_CONNECTION_STRING")
    if cs:
        return cs
    try:
        with socket.create_connection(("127.0.0.1", 10000), timeout=0.3):
            return _AZURITE_DEV_CS
    except OSError:
        return None


@contextmanager
def make_object_store(kind: str):
    """Yield a fresh, isolated ObjectStore of ``kind`` (or skip the test)."""
    prefix = f"bow-test-{uuid.uuid4().hex[:12]}/"

    if kind == "s3-moto":
        from moto import mock_aws
        from app.services.storage.s3 import S3Store

        with mock_aws():
            import boto3
            bucket = f"bow-test-{uuid.uuid4().hex[:12]}"
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
            yield S3Store(
                S3StorageConfig(bucket=bucket, region="us-east-1", auth="static",
                                access_key_id="testing", secret_access_key="testing"),
                prefix=prefix,
            )
        return

    if kind == "azure-azurite":
        cs = _azurite_connection_string()
        if not cs:
            pytest.skip("Azurite is not running (start azurite-blob or set BOW_TEST_AZURITE_CONNECTION_STRING)")
        from azure.storage.blob import BlobServiceClient
        from app.services.storage.azure_blob import AzureBlobStore

        container = f"bow-test-{uuid.uuid4().hex[:12]}"
        svc = BlobServiceClient.from_connection_string(cs)
        svc.create_container(container)
        try:
            yield AzureBlobStore(
                AzureStorageConfig(auth="connection_string", connection_string=cs, container=container),
                prefix=prefix,
            )
        finally:
            svc.delete_container(container)
        return

    if kind == "s3-real":
        bucket = os.environ.get("BOW_TEST_S3_BUCKET")
        if not bucket:
            pytest.skip("set BOW_TEST_S3_BUCKET (and AWS credentials) to test a real S3 bucket")
        from app.services.storage.s3 import S3Store

        store = S3Store(
            S3StorageConfig(bucket=bucket, region=os.environ.get("BOW_TEST_S3_REGION", ""),
                            endpoint_url=os.environ.get("BOW_TEST_S3_ENDPOINT_URL", ""), auth="default"),
            prefix=prefix,
        )
        try:
            yield store
        finally:
            store.delete_prefix("")
        return

    if kind == "azure-real":
        url = os.environ.get("BOW_TEST_AZURE_ACCOUNT_URL")
        container = os.environ.get("BOW_TEST_AZURE_CONTAINER")
        sas = os.environ.get("BOW_TEST_AZURE_SAS_TOKEN")
        if not (url and container and sas):
            pytest.skip("set BOW_TEST_AZURE_ACCOUNT_URL/_CONTAINER/_SAS_TOKEN to test a real Azure container")
        from app.services.storage.azure_blob import AzureBlobStore

        store = AzureBlobStore(
            AzureStorageConfig(auth="sas", account_url=url, container=container, sas_token=sas),
            prefix=prefix,
        )
        try:
            yield store
        finally:
            store.delete_prefix("")
        return

    raise ValueError(kind)


@pytest.fixture(params=OBJECT_STORE_KINDS)
def object_store(request):
    """Make the app use a fresh object store for the duration of one test."""
    with make_object_store(request.param) as store:
        storage.set_store(store)
        try:
            yield store
        finally:
            storage.reset_store()
