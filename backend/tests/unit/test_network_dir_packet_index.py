"""Mixed-share query contract, exercised with real capture files and real decoders."""

import shutil
from pathlib import Path

import pytest

from app.data_sources.clients.network_dir_client import NetworkDirClient
from tests.fixtures.packet_captures import ARP, IP, write_capture


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
    assert "dns.a" in {c.name for c in catalog["second.pcapng"].columns}
    assert client.read_file("notes.txt") == "incident timeline"
    assert "photo.jpg" in catalog
    reader = NetworkDirClient(root_path=str(share))
    rows = reader.execute_query('SELECT capture_id, frame_number, "dns.a" AS answers FROM packets')
    assert rows.groupby("capture_id").size().to_dict() == {"first.pcap": 2, "second.pcapng": 1}
    assert rows.loc[rows.capture_id == "first.pcap", "answers"].isna().all()
    dns = rows[rows.capture_id == "second.pcapng"].iloc[0]
    assert dns.frame_number == 1 and list(dns.answers) == ["192.0.2.1", "192.0.2.2"]

    write_capture(share / "first.pcap", [ARP])
    (share / "second.pcapng").unlink()
    status = reader.execute_query("SELECT file_id, status FROM indexed_files")
    assert dict(status.itertuples(index=False, name=None)) == {"first.pcap": "pending"}
    client.get_schemas()
    assert reader.execute_query("SELECT count(*) AS n FROM packets").iloc[0].n == 1


@pytest.mark.skipif(not shutil.which("tshark"), reason="requires tshark")
def test_cache_is_scoped_and_failed_files_are_visible(share, monkeypatch):
    import duckdb

    (share / "broken.pcap").write_bytes(b"not a capture")
    (share / "alias.pcap").symlink_to(share / "second.pcapng")
    client = NetworkDirClient(root_path=str(share), include_globs="*.pcap")
    client.get_schemas()
    rows = client.execute_query("SELECT file_id, status FROM indexed_files")
    assert dict(rows.itertuples(index=False, name=None)) == {"first.pcap": "ready", "broken.pcap": "failed"}
    assert client.execute_query("SELECT DISTINCT capture_id FROM packets").capture_id.tolist() == ["first.pcap"]
    with pytest.raises(duckdb.PermissionException):
        client.execute_query("SELECT * FROM read_csv_auto('notes.txt')")

    # Rebuild on a host with no decoder: unchanged packets still come from cache.
    monkeypatch.setenv("PATH", "")
    client.get_schemas()
    assert client.execute_query("SELECT count(*) AS n FROM packets").iloc[0].n == 2


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
    catalog = {t.name: t for t in client.get_schemas()}
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
    client.get_schemas()
    result = client.execute_query('SELECT count(*) AS frames, count("dns.a") AS dns FROM packets')
    assert tuple(result.iloc[0]) == (1025, 1)
    assert set(client.execute_query("SELECT status FROM indexed_files").status) == {"ready"}
