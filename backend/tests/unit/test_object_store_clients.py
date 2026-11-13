"""Unit tests for the Google Cloud Storage and Azure Blob file sources.

The storage wire behavior (listing, ranges, grep, confinement) is covered
end-to-end against the real emulators in
tests/integrations/test_object_store_live.py. These tests pin what needs no
server: registry + file-tool wiring, auth-variant → SDK credential selection
(the SDK constructors are the stubbed boundary), and the provider-neutral
chunked body used for streamed grep.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.data_sources.clients._object_store_common import ChunkedBody, ObjectStoreClient
from app.data_sources.clients.azure_blob_client import AzureBlobClient
from app.data_sources.clients.base import Capability
from app.data_sources.clients.gcs_client import GCSClient

TYPES = {"gcs": GCSClient, "azure_blob": AzureBlobClient}


# ---------------------------------------------------------------- registry


@pytest.mark.parametrize("type_name,cls", TYPES.items())
def test_registered_as_a_shared_file_source(type_name, cls):
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    entry = REGISTRY[type_name]
    assert entry.data_shape == "files"
    assert entry.category == "files"
    assert entry.catalog_ownership == "shared"
    assert entry.requires_license is None
    assert resolve_client_class(type_name) is cls
    assert issubclass(cls, ObjectStoreClient)


@pytest.mark.parametrize("type_name", TYPES)
def test_every_file_tool_recognises_the_type(type_name):
    from app.ai.tools.implementations._file_catalog import FILE_METADATA_KEYS
    from app.ai.tools.implementations._file_tool_common import FILE_SOURCE_TYPES
    from app.ai.tools.implementations.connection_catalog_common import FILE_SOURCE_TYPES as catalog_types
    from app.ai.tools.implementations.search_files import _FILE_METADATA_KEYS as search_keys

    assert type_name in FILE_SOURCE_TYPES
    assert type_name in catalog_types
    assert TYPES[type_name].metadata_key in FILE_METADATA_KEYS
    assert TYPES[type_name].metadata_key in search_keys


@pytest.mark.parametrize("cls", TYPES.values())
def test_read_only_file_capabilities(cls):
    assert {Capability.LIST_FILES, Capability.READ_FILE, Capability.GREP_FILES} <= cls.capabilities
    assert Capability.WRITE_FILE not in cls.capabilities
    assert Capability.QUERY not in cls.capabilities


@pytest.mark.parametrize("type_name", TYPES)
def test_every_auth_variant_builds_a_client(type_name):
    """Each credentials schema's fields are accepted by the client constructor
    (the form → kwargs contract)."""
    from app.schemas.data_source_registry import REGISTRY

    entry = REGISTRY[type_name]
    config_fields = set(entry.config_schema.model_fields)
    for variant in entry.credentials_auth.by_auth.values():
        kwargs = dict.fromkeys(variant.schema.model_fields, "x")
        kwargs.update({f: "x" for f in config_fields if f not in {"recursive", "max_file_mb", "max_catalog_objects"}})
        TYPES[type_name](**kwargs)  # must not raise on unknown/missing kwargs


@pytest.mark.parametrize("type_name,field", [("gcs", "bucket"), ("azure_blob", "container")])
def test_missing_bucket_fails_the_connection_test(type_name, field):
    res = TYPES[type_name](**{field: ""}).test_connection()
    assert res["success"] is False
    assert TYPES[type_name].bucket_noun in res["message"]


# -------------------------------------------------------------- GCS auth


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return object()


def test_gcs_service_account_key_is_used_read_only(monkeypatch):
    from google.cloud import storage
    from google.oauth2 import service_account

    rec, seen = _Recorder(), {}
    monkeypatch.setattr(storage, "Client", rec)

    def fake_from_info(info, scopes=None):
        seen.update(info=info, scopes=scopes)
        return "sa-creds"

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", staticmethod(fake_from_info))
    key = {"type": "service_account", "project_id": "acme-prod", "client_email": "r@acme.iam"}
    GCSClient(bucket="b", service_account_json=json.dumps(key))._client()
    assert seen["info"] == key
    assert seen["scopes"] == ["https://www.googleapis.com/auth/devstorage.read_only"]
    assert rec.calls[0]["credentials"] == "sa-creds"
    assert rec.calls[0]["project"] == "acme-prod"  # taken from the key when blank


def test_gcs_invalid_key_is_a_clear_error():
    res = GCSClient(bucket="b", service_account_json="{not json").test_connection()
    assert res["success"] is False
    assert "JSON" in res["message"]


def test_gcs_application_default_credentials(monkeypatch):
    import google.auth
    from google.cloud import storage

    rec = _Recorder()
    monkeypatch.setattr(storage, "Client", rec)
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: ("adc-creds", "adc-project"))
    GCSClient(bucket="b")._client()
    assert rec.calls[0]["credentials"] == "adc-creds"
    assert rec.calls[0]["project"] == "adc-project"


def test_gcs_custom_endpoint_without_key_is_anonymous(monkeypatch):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    rec = _Recorder()
    monkeypatch.setattr(storage, "Client", rec)
    GCSClient(bucket="b", endpoint_url="http://emulator:4443/")._client()
    assert isinstance(rec.calls[0]["credentials"], AnonymousCredentials)
    assert rec.calls[0]["client_options"] == {"api_endpoint": "http://emulator:4443"}


# ------------------------------------------------------------ Azure auth


class _FakeService:
    def __init__(self, sink, **kwargs):
        sink.append(kwargs)

    def get_container_client(self, name):
        return ("container", name)


@pytest.fixture
def azure_calls(monkeypatch):
    import azure.storage.blob as blob

    calls: list = []
    monkeypatch.setattr(blob, "BlobServiceClient", type("BSC", (), {
        "__init__": lambda self, **kw: _FakeService.__init__(self, calls, **kw),
        "get_container_client": _FakeService.get_container_client,
        "from_connection_string": classmethod(lambda cls, s: (calls.append({"connection_string": s}), cls(account_url="from-conn"))[1]),
    }))
    return calls


def test_azure_connection_string(azure_calls):
    conn = "DefaultEndpointsProtocol=https;AccountName=acme;AccountKey=k;EndpointSuffix=core.windows.net"
    c = AzureBlobClient(container="reports", connection_string=conn)
    assert c._client() == ("container", "reports")
    assert azure_calls[0] == {"connection_string": conn}
    assert c.account_name == "acme"  # labelled from the connection string
    assert "az://acme/reports/" in c.description


def test_azure_account_key_targets_the_account_endpoint(azure_calls):
    AzureBlobClient(container="r", account_name="acme", account_key="k")._client()
    assert azure_calls[0]["account_url"] == "https://acme.blob.core.windows.net"
    assert azure_calls[0]["credential"] == {"account_name": "acme", "account_key": "k"}


def test_azure_sas_token_strips_the_question_mark(azure_calls):
    AzureBlobClient(container="r", account_name="acme", sas_token="?sv=2024&sig=abc")._client()
    assert azure_calls[0]["credential"] == "sv=2024&sig=abc"


def test_azure_account_url_override(azure_calls):
    AzureBlobClient(container="r", account_url="https://acme.blob.core.usgovcloudapi.net/", sas_token="sig")._client()
    assert azure_calls[0]["account_url"] == "https://acme.blob.core.usgovcloudapi.net"


def test_azure_service_principal(azure_calls, monkeypatch):
    import azure.identity

    monkeypatch.setattr(azure.identity, "ClientSecretCredential", lambda t, c, s: ("sp", t, c, s))
    AzureBlobClient(container="r", account_name="acme", tenant_id="t", client_id="c", client_secret="s")._client()
    assert azure_calls[0]["credential"] == ("sp", "t", "c", "s")


def test_azure_managed_identity_when_no_secret(azure_calls, monkeypatch):
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda **kw: "default-cred")
    AzureBlobClient(container="r", account_name="acme")._client()
    assert azure_calls[0]["credential"] == "default-cred"


def test_azure_needs_an_account_without_a_connection_string():
    res = AzureBlobClient(container="r", sas_token="sig").test_connection()
    assert res["success"] is False
    assert "account" in res["message"].lower()


# ----------------------------------------------------------- chunked body


@pytest.mark.parametrize("sizes", [[1, 1, 1], [7], [3, 100, 2], [5, 5, 5, 5]])
def test_chunked_body_reads_are_lossless_and_bounded(sizes):
    payload = bytes(range(256)) * 3
    chunks, i = [], 0
    for n in [64, 1, 300, 17, 1000]:
        chunks.append(payload[i:i + n])
        i += n
    body = ChunkedBody(iter(chunks))
    out = b""
    k = 0
    while True:
        part = body.read(sizes[k % len(sizes)])
        k += 1
        assert len(part) <= sizes[(k - 1) % len(sizes)]
        if not part:
            break
        out += part
    assert out == payload
    body.close()
    assert body.closed


# ------------------------------------------- index tiers are provider-neutral


class _MemoryStore(ObjectStoreClient):
    """A store that keeps its objects in a dict and counts downloads."""

    scheme = "mem"
    metadata_key = "mem"
    bucket_noun = "bucket"

    def __init__(self, objects, **kwargs):
        super().__init__(bucket="b", **kwargs)
        self.objects = dict(objects)
        self.downloads = []

    def _iter_list(self, list_prefix, recursive):
        for key, data in sorted(self.objects.items()):
            yield {"key": key, "size": len(data), "modified": datetime(2026, 1, 1, tzinfo=UTC)}

    def _download(self, key, start=None, end=None):
        self.downloads.append(key)
        data = self.objects[key]
        return (data if start is None else data[start:(end + 1 if end is not None else None)]), len(data)


class _Sink:
    def __init__(self):
        self.docs = {}
        self.walk_truncated = None

    def is_current(self, file_id, version):
        return file_id in self.docs and self.docs[file_id][0] == version

    def prior_meta(self, file_id):
        return self.docs[file_id][2]

    def prior_status(self, file_id):
        return self.docs[file_id][1].status

    def write(self, file_id, path, version, doc, meta=None):
        self.docs[file_id] = (version, doc, meta)

    def flush(self):
        pass


def test_any_object_store_feeds_the_full_text_index_and_skips_unchanged_objects():
    body = ("filler about budgets\n" * 50 + "Reference PO-48213 was approved\n").encode()
    sink = _Sink()
    store = _MemoryStore({"orders/batch.txt": body}, index_mode="fulltext")
    row = store.get_schemas(section_sink=sink)[0].metadata_json["mem"]

    _version, doc, _meta = sink.docs["orders/batch.txt"]
    assert row["fulltext"] == doc.status == "indexed"
    assert "48213" in {t.base for s in doc.sections for t in s.terms}
    assert sink.walk_truncated is False

    again = _MemoryStore({"orders/batch.txt": body}, index_mode="fulltext")
    again_row = again.get_schemas(section_sink=sink)[0].metadata_json["mem"]
    assert again.downloads == []
    assert again_row["keywords"] == row["keywords"]


def test_any_object_store_reuses_keywords_for_unchanged_objects():
    objects = {"a.txt": b"renewal clause for the warehouse lease\n"}
    first = _MemoryStore(objects, index_mode="content")
    tables = first.get_schemas()
    assert "warehouse" in tables[0].metadata_json["mem"]["keywords"]

    second = _MemoryStore(objects, index_mode="content")
    reused = second.get_schemas(prior_catalog={t.name: t.metadata_json for t in tables})
    assert second.downloads == []
    assert reused[0].metadata_json["mem"]["keywords"] == tables[0].metadata_json["mem"]["keywords"]
