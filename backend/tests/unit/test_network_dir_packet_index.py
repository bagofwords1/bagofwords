"""Mixed-share query contract, exercised with real capture files and real decoders."""

import shutil
from pathlib import Path

import pytest

from app.data_sources.clients.network_dir_client import NetworkDirClient
from tests.fixtures.packet_captures import ARP, IP, write_capture


def bind_selection(client, catalog):
    from app.data_sources.clients._network_dir_tables import attach_catalog

    selected = [
        (t.name, [{"name": c.name} for c in t.columns], t.metadata_json)
        for t in catalog.values()
        if t.metadata_json.get("network_dir_relation")
    ]
    ids = [
        (t.name, t.metadata_json["network_dir"]["cache_version"])
        for t in catalog.values()
        if (t.metadata_json.get("network_dir") or {}).get("sql_table")
    ]
    attach_catalog(client, selected, ids)


def index_and_bind(client):
    catalog = {t.name: t for t in client.get_schemas()}
    bind_selection(client, catalog)
    return catalog


@pytest.fixture
def share(tmp_path, monkeypatch):
    from app.services import storage

    monkeypatch.setattr(storage, "UPLOADS_ROOT", tmp_path / "cache")
    root = tmp_path / "share"
    root.mkdir()
    (root / "notes.txt").write_text("incident timeline")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    write_capture(root / "first.pcap", [ARP, ARP])
    write_capture(root / "second.pcapng", [IP])
    return root


@pytest.mark.skipif(not shutil.which("tshark"), reason="install tshark to run the real decode")
def test_mixed_capture_schemas_and_refresh(share):
    client = NetworkDirClient(root_path=str(share))
    catalog = {t.name: t for t in client.get_schemas()}
    assert "dns.a" in {c.name for c in catalog.get("packets", catalog["second.pcapng"]).columns}
    bind_selection(client, catalog)
    assert not catalog["second.pcapng"].columns
    assert client.read_file("notes.txt") == "incident timeline"
    assert "photo.jpg" in catalog
    reader = NetworkDirClient(root_path=str(share))
    with pytest.raises(PermissionError):
        reader.execute_query("SELECT * FROM packets")
    bind_selection(reader, catalog)
    rows = reader.execute_query('SELECT capture_id, frame_number, "dns.a" AS answers FROM packets')
    assert rows.groupby("capture_id").size().to_dict() == {"first.pcap": 2, "second.pcapng": 1}
    assert rows.loc[rows.capture_id == "first.pcap", "answers"].isna().all()
    dns = rows[rows.capture_id == "second.pcapng"].iloc[0]
    assert dns.frame_number == 1 and list(dns.answers) == ["192.0.2.1", "192.0.2.2"]

    write_capture(share / "first.pcap", [ARP])
    (share / "second.pcapng").unlink()
    status = reader.execute_query("SELECT file_id, status FROM indexed_files")
    assert dict(status.itertuples(index=False, name=None)) == {"first.pcap": "pending"}
    bind_selection(reader, index_and_bind(client))
    assert reader.execute_query("SELECT count(*) AS n FROM packets").iloc[0].n == 1


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires tshark")
def test_cache_is_scoped_and_failed_files_are_visible(share, monkeypatch):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    (share / "broken.pcap").write_bytes(b"not a capture")
    (share / "alias.pcap").symlink_to(share / "second.pcapng")
    client = NetworkDirClient(root_path=str(share), include_globs="*.pcap")
    index_and_bind(client)
    rows = client.execute_query("SELECT file_id, status FROM indexed_files")
    assert dict(rows.itertuples(index=False, name=None)) == {"first.pcap": "ready", "broken.pcap": "failed"}
    assert client.execute_query("SELECT DISTINCT capture_id FROM packets").capture_id.tolist() == ["first.pcap"]
    with pytest.raises(ReadOnlyViolation):
        client.execute_query("SELECT * FROM read_csv_auto('notes.txt')")

    # Rebuild on a host with no decoder: unchanged packets still come from cache.
    monkeypatch.setenv("PATH", "")
    index_and_bind(client)
    assert client.execute_query("SELECT count(*) AS n FROM packets").iloc[0].n == 2


def _statuses(client):
    rows = client.execute_query("SELECT file_id, status, reason FROM indexed_files")
    return {r.file_id: (r.status, r.reason) for r in rows.itertuples(index=False)}


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires tshark")
def test_permanent_decode_failure_is_kept_until_the_file_changes(share, monkeypatch, tmp_path):
    client = NetworkDirClient(root_path=str(share), allowed_extensions="pcap")
    (share / "broken.pcap").write_bytes(b"\xd4\xc3\xb2\xa1 truncated capture header")
    index_and_bind(client)
    status, reason = _statuses(client)["broken.pcap"]
    assert status == "failed" and reason

    # An unchanged file is not decoded again: with no decoder on the host a
    # retry would replace the recorded reason, so the reason must survive.
    monkeypatch.setenv("PATH", str(tmp_path / "no-decoder"))
    index_and_bind(client)
    assert _statuses(client)["broken.pcap"] == (status, reason)

    # A changed file is a new version and is decoded.
    monkeypatch.undo()
    write_capture(share / "broken.pcap", [ARP, ARP, ARP])
    index_and_bind(client)
    assert _statuses(client)["broken.pcap"][0] == "ready"
    assert client.execute_query(
        "SELECT count(*) AS n FROM packets WHERE capture_id = 'broken.pcap'"
    ).iloc[0].n == 3


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires tshark")
@pytest.mark.parametrize("decoder", ["missing", "unlaunchable"])
def test_host_side_decoder_failures_are_retried(share, monkeypatch, tmp_path, decoder):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if decoder == "unlaunchable":
        # On PATH but cannot start inside the sandbox (a script needs /bin/sh,
        # which the sandbox does not allow to execute; on hosts without the
        # sandbox the launcher refuses outright).
        fake = bin_dir / "tshark"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
    real_path = __import__("os").environ["PATH"]
    monkeypatch.setenv("PATH", str(bin_dir))
    client = NetworkDirClient(root_path=str(share), allowed_extensions="pcap,pcapng")
    index_and_bind(client)
    assert {s for s, _ in _statuses(client).values()} == {"failed"}

    # Fixing the host is enough: the same unchanged files decode on the next refresh.
    monkeypatch.setenv("PATH", real_path)
    index_and_bind(client)
    assert {s for s, _ in _statuses(client).values()} == {"ready"}


@pytest.mark.parametrize("mode", ["none", "metadata"])
def test_listing_only_modes_do_not_create_packet_relations(share, mode):
    client = NetworkDirClient(root_path=str(share), index_mode=mode)
    assert "packets" not in {t.name for t in client.get_schemas()}
    assert any(f["id"].endswith(".pcap") for f in client.list_files())


def test_mixed_qvd_and_document_keep_their_own_readers(share, monkeypatch):
    import os
    from scripts.gen_network_dir_fixtures import _make_docx
    from app.data_sources.clients import qvd_client

    native = os.environ.get("QVD2PARQUET_BIN") or shutil.which("qvd2parquet")
    if not native:
        pytest.skip("requires qvd2parquet")
    monkeypatch.setattr(qvd_client, "_QVD2PARQUET_BIN", native)
    src = Path(__file__).resolve().parents[1] / "config" / "test_source.qvd"
    shutil.copyfile(src, share / "orders.qvd")
    _make_docx(share / "incident.docx", ["incident timeline"])
    client = NetworkDirClient(root_path=str(share))
    catalog = index_and_bind(client)
    relation = catalog["orders.qvd"].metadata_json["network_dir"]["sql_table"]
    first = client.execute_query(f'SELECT "OrderNumber" FROM "{relation}"')
    assert len(first) > 0
    assert "OrderNumber" not in {c.name for c in catalog["packets"].columns}
    assert "incident timeline" in client.read_file("incident.docx")
    assert client.read_file("photo.jpg") == (share / "photo.jpg").read_bytes()


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires tshark")
def test_empty_capture_and_field_first_seen_in_last_batch(share):
    write_capture(share / "first.pcap", [ARP] * 1024 + [IP])
    write_capture(share / "second.pcapng", [])
    client = NetworkDirClient(root_path=str(share))
    index_and_bind(client)
    result = client.execute_query('SELECT count(*) AS frames, count("dns.a") AS dns FROM packets')
    assert tuple(result.iloc[0]) == (1025, 1)
    assert set(client.execute_query("SELECT status FROM indexed_files").status) == {"ready"}


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires tshark")
def test_every_decoded_field_and_payload_is_available_only_through_encrypted_artifacts(share, monkeypatch):
    import subprocess
    from lxml import etree
    import pyarrow as pa
    import pyarrow.parquet as pq
    from app.data_sources.clients._network_dir_tables import Source

    frame = bytes.fromhex("01020304050600112233445588b5") + b"offline packet fixture"
    path = share / "payload.pcap"
    write_capture(path, [frame])
    xml = subprocess.check_output(["tshark", "-n", "-r", str(path), "-T", "pdml"])
    fields = {f.get("name") for f in etree.fromstring(xml).iter("field") if f.get("name")}
    client = NetworkDirClient(root_path=str(share))
    catalog = index_and_bind(client)
    assert "data.data" in fields
    assert fields <= {col.name for col in catalog["packets"].columns}
    artifact = Source.resolve(client, path.name).artifact
    with pytest.raises((pa.ArrowException, OSError)):
        pq.read_table(artifact)
    monkeypatch.setattr(client, "list_files", lambda **kw: pytest.fail("SQL walked the share"))
    result = client.execute_query("""-- decoded bytes
        FROM packets SELECT "data.data" AS payload WHERE capture_id='payload.pcap'""")
    assert bytes.fromhex(result.payload.iloc[0][0].replace(":", "")) == b"offline packet fixture"
    (share / "Show notes.txt").write_text("from the file")
    assert client.execute_query("Show notes.txt") == "from the file"
    discovered = client.execute_query("SELECT column_name FROM duckdb_columns() WHERE table_name='packets'")
    assert fields <= set(discovered.column_name)


def test_catalog_limit_and_capabilities(share):
    from app.data_sources.clients.base import Capability
    from app.services.custom_query_service import is_accelerable_type

    assert not is_accelerable_type("network_dir")
    for mode in ("none", "metadata", "content", "fulltext"):
        config = {"index_mode": mode}
        client = NetworkDirClient(root_path=str(share), **config)
        assert (Capability.QUERY in client.capabilities_for_config(config)) == (Capability.QUERY in client.capabilities)
    client = NetworkDirClient(root_path=str(share), allowed_extensions="pcap,pcapng", max_catalog_objects=1)
    index_and_bind(client)
    assert "truncated" in set(client.execute_query("SELECT status FROM indexed_files").status)
