"""GCSClient and AzureBlobClient against real storage APIs (the official
emulators: fake-gcs-server and Azurite). Needs Docker.

The same provider-neutral contract the S3 unit tests pin, exercised over the
real wire protocol: listing (recursive + folders, junk filtered), structured
reads, windowed byte-range reads with a correct total size, resumable grep,
not-found and changed-mid-scan reporting, versions, the content catalog, and
prefix + include-glob confinement.

Run:
    cd backend
    uv run pytest tests/integrations/test_object_store_live.py -v
"""
from __future__ import annotations

import time

import pandas as pd
import pytest

tc_core = pytest.importorskip("testcontainers.core.container")

from app.data_sources.clients._file_source_common import GlobScopeError, ScopeEscapeError  # noqa: E402
from app.data_sources.clients.azure_blob_client import AzureBlobClient  # noqa: E402
from app.data_sources.clients.gcs_client import GCSClient  # noqa: E402

BUCKET = "bow-live"
LOG = ("line\n" * 5000 + "ERROR café exploded\n" + "line\n" * 10).encode()
FILES = {
    "sales/2025/q1.csv": b"region,revenue\nEMEA,120\nAMER,340\nAPAC,99\n",
    "sales/2025/notes.txt": b"Quarterly notes\nHIT: churn rose in APAC\nend\n",
    "logs/app.log": LOG,
    "readme.md": b"# Bucket readme\nproduct catalogue\n",
    ".DS_Store": b"junk",
    "folder/": b"",
}

AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


def _wait_http(url: str, timeout: float = 60.0) -> None:
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, timeout=2)
            return
        except Exception:
            time.sleep(1)
    raise TimeoutError(f"{url} never answered")


class _Store:
    """One running emulator: makes clients and can rewrite an object."""

    def __init__(self, kind, make, put):
        self.kind, self._make, self.put = kind, make, put

    def client(self, **kw):
        return self._make(**kw)


# Containers started for the current backend; stopped on teardown even when
# seeding fails half-way (otherwise a failed setup leaks a running server).
_STARTED: list = []


def _stop_started() -> None:
    while _STARTED:
        try:
            _STARTED.pop().stop()
        except Exception:
            pass


def _start(image, port, command):
    c = tc_core.DockerContainer(image).with_exposed_ports(port).with_command(command)
    try:
        c.start()
        _STARTED.append(c)
    except Exception as e:  # no Docker here
        pytest.skip(f"Docker / {image} unavailable: {e}")
    return c


@pytest.fixture(scope="module", params=["gcs", "azure_blob"])
def store(request):
    try:
        yield from _store(request.param)
    finally:
        _stop_started()


def _store(kind):
    if kind == "gcs":
        # fake-gcs-server builds media (download) URLs from -public-host, so
        # it must be told the host port it is reachable on.
        import socket

        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            host_port = s.getsockname()[1]
        c = tc_core.DockerContainer("fsouza/fake-gcs-server:1.52.2").with_bind_ports(4443, host_port).with_command(
            f"-scheme http -port 4443 -public-host 127.0.0.1:{host_port} -external-url http://127.0.0.1:{host_port}")
        try:
            c.start()
            _STARTED.append(c)
        except Exception as e:  # no Docker here
            pytest.skip(f"Docker / fake-gcs-server unavailable: {e}")
        endpoint = f"http://127.0.0.1:{host_port}"
        _wait_http(f"{endpoint}/storage/v1/b")
        admin = storage.Client(project="test", credentials=AnonymousCredentials(),
                               client_options={"api_endpoint": endpoint})
        admin.create_bucket(BUCKET)

        def put(key, data):
            admin.bucket(BUCKET).blob(key).upload_from_string(data)

        def make(**kw):
            return GCSClient(**{"bucket": BUCKET, "endpoint_url": endpoint, "project": "test", **kw})
    else:
        from azure.storage.blob import BlobServiceClient

        c = _start("mcr.microsoft.com/azure-storage/azurite:3.34.0", 10000,
                   "azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck --loose")
        host, port = c.get_container_host_ip(), c.get_exposed_port(10000)
        conn = (f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={AZURITE_KEY};"
                f"BlobEndpoint=http://{host}:{port}/devstoreaccount1;")
        _wait_http(f"http://{host}:{port}/devstoreaccount1?comp=list")
        admin = BlobServiceClient.from_connection_string(conn)
        admin.create_container(BUCKET)

        def put(key, data):
            admin.get_container_client(BUCKET).upload_blob(key, data, overwrite=True)

        def make(**kw):
            return AzureBlobClient(**{"container": BUCKET, "connection_string": conn, **kw})

    for key, data in FILES.items():
        put(key, data)
    yield _Store(kind, make, put)


def test_connection_names_the_scope(store):
    res = store.client().test_connection()
    assert res["success"], res["message"]
    assert BUCKET in res["message"]


def test_listing_hides_junk_and_folder_markers(store):
    ids = {f["id"] for f in store.client().list_files()}
    assert ids == {"sales/2025/q1.csv", "sales/2025/notes.txt", "logs/app.log", "readme.md"}


def test_non_recursive_listing_returns_folders(store):
    entries = store.client().list_files(recursive=False)
    folders = {e["id"] for e in entries if e["is_folder"]}
    files = {e["id"] for e in entries if not e["is_folder"]}
    assert {"sales/", "logs/"} <= folders
    assert "readme.md" in files and "sales/2025/q1.csv" not in files


def test_structured_read_parses_csv(store):
    df = store.client().read_file("sales/2025/q1.csv")
    assert isinstance(df, pd.DataFrame)
    assert int(df["revenue"].sum()) == 559


@pytest.mark.parametrize("offset,length", [(0, 100), (777, 1000), (len(LOG) - 50, 4096)])
def test_windowed_read_reports_the_object_total(store, offset, length):
    w = store.client().read_file("logs/app.log", offset=offset, length=length)
    assert w["total_size"] == len(LOG)
    assert w["eof"] == (offset + length >= len(LOG))


def test_windows_concatenate_to_the_whole_object(store):
    client, out, cursor = store.client(), [], 0
    for _ in range(100):
        w = client.read_file("logs/app.log", offset=cursor, length=4000)
        out.append(w["content"])
        cursor = w["next_cursor"]
        if w["eof"]:
            break
    assert "".join(out).encode() == LOG


def test_window_past_eof_is_empty(store):
    w = store.client().read_file("logs/app.log", offset=len(LOG) + 10, length=10)
    assert w["eof"]


def test_resumable_grep_finds_each_match_once(store):
    client = store.client()
    res = client.grep_files("ERROR", file_ids=["logs/app.log"], max_bytes_per_file=4096)
    found = list(res["matches"])
    for _ in range(50):
        if not res.get("next_cursor"):
            break
        res = client.grep_files("ERROR", file_ids=["logs/app.log"], max_bytes_per_file=4096,
                                cursor=res["next_cursor"])
        found += res["matches"]
    assert len(found) == 1


def test_grep_by_name_pattern(store):
    res = store.client().grep_files("churn", name_pattern="*.txt")
    assert [m["file_id"] for m in res["matches"]] == ["sales/2025/notes.txt"]


def test_grep_reports_missing_objects(store):
    res = store.client().grep_files("x", file_ids=["does/not/exist.txt"])
    assert res["skipped_files"] == [{"file_id": "does/not/exist.txt", "reason": "not_found"}]


def test_grep_reports_an_object_rewritten_mid_scan(store):
    client = store.client()
    store.put("logs/rotating.log", b"line\n" * 4000)
    first = client.grep_files("NEVER", file_ids=["logs/rotating.log"], max_bytes_per_file=1024)
    assert first["next_cursor"]
    store.put("logs/rotating.log", b"rotated\n" * 4000)
    second = client.grep_files("NEVER", file_ids=["logs/rotating.log"], max_bytes_per_file=1024,
                               cursor=first["next_cursor"])
    assert {"file_id": "logs/rotating.log", "reason": "file_changed"} in second["skipped_files"]


def test_file_version_changes_with_content(store):
    client = store.client()
    store.put("v.txt", b"one")
    v1 = client.file_version("v.txt")
    store.put("v.txt", b"two")
    v2 = client.file_version("v.txt")
    assert v1 and v2 and v1 != v2


def test_raw_bytes_are_the_original_object(store):
    data, name, mime = store.client().read_raw_bytes("readme.md")
    assert data == FILES["readme.md"]
    assert name == "readme.md" and mime == "text/markdown"


def test_catalog_indexes_content_keywords(store):
    tables = {t.name: t for t in store.client().get_schemas()}
    meta = tables["sales/2025/notes.txt"].metadata_json[store.client().metadata_key]
    assert "churn" in meta["keywords"]
    assert meta["web_url"]


def test_prefix_confines_listing_and_reads(store):
    client = store.client(prefix="sales")
    assert {f["id"] for f in client.list_files()} == {"2025/q1.csv", "2025/notes.txt"}
    for escape in ("../readme.md", "2025/../../logs/app.log"):
        with pytest.raises(ScopeEscapeError):
            client.read_file(escape)


def test_include_globs_deny_off_pattern_objects(store):
    client = store.client(prefix="sales/", include_globs="**/*.csv")
    assert {f["id"] for f in client.list_files()} == {"2025/q1.csv"}
    with pytest.raises(GlobScopeError):
        client.read_file("2025/notes.txt")


def test_wrong_bucket_fails_the_connection_test(store):
    kw = {"bucket": "missing-bucket"} if store.kind == "gcs" else {"container": "missing-container"}
    assert store.client(**kw).test_connection()["success"] is False
