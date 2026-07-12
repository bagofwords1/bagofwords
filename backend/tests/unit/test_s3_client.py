"""Unit tests for the S3Client (s3 / AWS S3 data source).

Exercises the S3 file primitives — list (with CommonPrefixes → folders + junk
filtering), structured read (csv → DataFrame), and the windowed byte-range read
(cursor: next_cursor / total_size / eof, with newline snapping) — plus the
security invariant (prefix confinement / traversal rejection) and the registry
wiring. The AWS boundary is stubbed with botocore's Stubber — no network, no
credentials, no moto dependency.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import boto3
import pandas as pd
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.data_sources.clients.base import Capability
from app.data_sources.clients.s3_client import S3Client


def _body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def _make_client(**overrides) -> S3Client:
    kwargs = dict(
        bucket="test-bucket",
        prefix="docs/",
        region="us-east-1",
        access_key="AKIATEST",
        secret_key="secret",
    )
    kwargs.update(overrides)
    client = S3Client(**kwargs)
    # Inject a real-but-stubbed boto3 s3 client so no network / creds are used.
    s3 = boto3.client(
        "s3", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="y",
    )
    client._s3 = s3
    return client, Stubber(s3)


# ---------------------------------------------------------------- registry


class TestRegistry:
    def test_registered_and_file_shaped(self):
        from app.schemas.data_source_registry import REGISTRY, resolve_client_class

        entry = REGISTRY["s3"]
        assert entry.is_connection is True
        assert entry.data_shape == "files"
        assert entry.catalog_ownership == "shared"
        assert entry.requires_license is None  # community / open tier
        assert resolve_client_class("s3") is S3Client

    def test_type_is_a_file_source(self):
        from app.ai.tools.implementations._file_tool_common import FILE_SOURCE_TYPES

        assert "s3" in FILE_SOURCE_TYPES

    def test_metadata_key_registered(self):
        from app.ai.tools.implementations.list_files import _FILE_METADATA_KEYS as lk
        from app.ai.tools.implementations.search_files import _FILE_METADATA_KEYS as sk

        assert "s3" in lk
        assert "s3" in sk


# ------------------------------------------------------------ capabilities


class TestCapabilities:
    def test_declares_list_and_read_only(self):
        assert Capability.LIST_FILES in S3Client.capabilities
        assert Capability.READ_FILE in S3Client.capabilities
        # Write and live search are intentionally out of scope for v1.
        assert Capability.WRITE_FILE not in S3Client.capabilities
        assert Capability.SEARCH_FILES not in S3Client.capabilities


# ------------------------------------------------------- prefix confinement


class TestConfinement:
    def test_relative_id_resolves_under_prefix(self):
        client, _ = _make_client()
        assert client._resolve_key("a/b.csv") == "docs/a/b.csv"

    def test_full_key_under_prefix_accepted(self):
        client, _ = _make_client()
        assert client._resolve_key("docs/a/b.csv") == "docs/a/b.csv"

    @pytest.mark.parametrize("bad", ["../secret.txt", "../../etc/passwd", "docs/../../x"])
    def test_traversal_rejected(self, bad):
        client, _ = _make_client()
        with pytest.raises(ValueError):
            client._resolve_key(bad)

    def test_empty_id_rejected(self):
        client, _ = _make_client()
        with pytest.raises(ValueError):
            client._resolve_key("  ")


# ----------------------------------------------------------------- listing


class TestListing:
    def test_list_maps_entries_and_filters_junk(self):
        client, stub = _make_client(recursive=True)
        mod = datetime(2025, 3, 15, 7, 36, 45, tzinfo=timezone.utc)
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": "docs/report.csv", "Size": 27221, "LastModified": mod},
                    {"Key": "docs/deck.pdf", "Size": 1000, "LastModified": mod},
                    # junk: folder marker + sidecar metadata → filtered out
                    {"Key": "docs/", "Size": 0, "LastModified": mod},
                    {"Key": "docs/report.csv.metadata", "Size": 81, "LastModified": mod},
                ],
                "KeyCount": 4,
                "IsTruncated": False,
            },
            {"Bucket": "test-bucket", "Prefix": "docs/"},
        )
        with stub:
            files = client.list_files()
        names = {f["name"] for f in files}
        assert names == {"report.csv", "deck.pdf"}
        report = next(f for f in files if f["name"] == "report.csv")
        assert report["id"] == "report.csv"          # relative to prefix
        assert report["size"] == 27221
        assert report["mime_type"] == "text/csv"
        assert report["is_folder"] is False
        assert report["web_url"] == "s3://test-bucket/docs/report.csv"
        assert report["modified_at"].startswith("2025-03-15T07:36:45")

    def test_non_recursive_lists_common_prefixes_as_folders(self):
        client, stub = _make_client(recursive=False)
        stub.add_response(
            "list_objects_v2",
            {
                "CommonPrefixes": [{"Prefix": "docs/2025/"}],
                "Contents": [{"Key": "docs/top.txt", "Size": 5,
                              "LastModified": datetime(2025, 1, 1, tzinfo=timezone.utc)}],
                "KeyCount": 2,
                "IsTruncated": False,
            },
            {"Bucket": "test-bucket", "Prefix": "docs/", "Delimiter": "/"},
        )
        with stub:
            files = client.list_files(recursive=False)
        folder = next(f for f in files if f["is_folder"])
        assert folder["id"] == "2025/"
        assert folder["name"] == "2025"

    def test_allowed_extensions_filter(self):
        client, stub = _make_client(recursive=True, allowed_extensions="csv")
        mod = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stub.add_response(
            "list_objects_v2",
            {"Contents": [
                {"Key": "docs/a.csv", "Size": 1, "LastModified": mod},
                {"Key": "docs/b.pdf", "Size": 1, "LastModified": mod},
            ], "KeyCount": 2, "IsTruncated": False},
            {"Bucket": "test-bucket", "Prefix": "docs/"},
        )
        with stub:
            files = client.list_files()
        assert [f["name"] for f in files] == ["a.csv"]


# ----------------------------------------------------------- structured read


class TestStructuredRead:
    def test_csv_returns_dataframe(self):
        client, stub = _make_client()
        csv = b"clause,penalty\npayment,100\nrenewal,50\n"
        stub.add_response("head_object", {"ContentLength": len(csv)},
                          {"Bucket": "test-bucket", "Key": "docs/a.csv"})
        stub.add_response("get_object", {"Body": _body(csv), "ContentLength": len(csv)},
                          {"Bucket": "test-bucket", "Key": "docs/a.csv"})
        with stub:
            df = client.read_file("a.csv")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["clause", "penalty"]
        assert len(df) == 2

    def test_oversize_rejected_before_download(self):
        client, stub = _make_client(max_file_mb=1)
        big = 5 * 1024 * 1024
        stub.add_response("head_object", {"ContentLength": big},
                          {"Bucket": "test-bucket", "Key": "docs/big.csv"})
        with stub:
            with pytest.raises(ValueError, match="exceeds"):
                client.read_file("big.csv")


# ------------------------------------------------------------- windowed read


class TestWindowedRead:
    def test_window_returns_cursor_fields(self):
        client, stub = _make_client()
        # 30 bytes total; read first 10.
        chunk = b"line1\nline"
        stub.add_response(
            "get_object",
            {"Body": _body(chunk), "ContentRange": "bytes 0-9/30", "ContentLength": 10},
            {"Bucket": "test-bucket", "Key": "docs/log.log", "Range": "bytes=0-9"},
        )
        with stub:
            out = client.read_file("log.log", offset=0, length=10)
        assert out["encoding"] == "text"
        assert out["total_size"] == 30
        assert out["eof"] is False
        # snapped to the last newline: content is "line1\n", cursor at byte 6
        assert out["content"] == "line1\n"
        assert out["next_cursor"] == 6

    def test_window_eof(self):
        client, stub = _make_client()
        chunk = b"tail-bytes"
        stub.add_response(
            "get_object",
            {"Body": _body(chunk), "ContentRange": "bytes 20-29/30", "ContentLength": 10},
            {"Bucket": "test-bucket", "Key": "docs/log.log", "Range": "bytes=20-29"},
        )
        with stub:
            out = client.read_file("log.log", offset=20, length=10)
        assert out["eof"] is True
        assert out["content"] == "tail-bytes"   # no snapping at eof
        assert out["next_cursor"] == 30

    def test_binary_window_base64(self):
        client, stub = _make_client()
        raw = b"\x89PNG\r\n\x1a\n\x00\x01"
        stub.add_response(
            "get_object",
            {"Body": _body(raw), "ContentRange": f"bytes 0-9/100", "ContentLength": len(raw)},
            {"Bucket": "test-bucket", "Key": "docs/img.png", "Range": "bytes=0-9"},
        )
        with stub:
            out = client.read_file("img.png", offset=0, length=10)
        import base64
        assert out["encoding"] == "base64"
        assert base64.b64decode(out["content"]) == raw
        assert out["total_size"] == 100


# ------------------------------------------------------------ test_connection


class TestConnection:
    def test_connection_success(self):
        client, stub = _make_client()
        stub.add_response(
            "list_objects_v2", {"KeyCount": 1, "IsTruncated": False},
            {"Bucket": "test-bucket", "Prefix": "docs/", "MaxKeys": 1},
        )
        with stub:
            res = client.test_connection()
        assert res["success"] is True

    def test_connection_requires_bucket(self):
        client = S3Client(bucket="")
        res = client.test_connection()
        assert res["success"] is False
        assert "bucket" in res["message"].lower()


# ------------------------------------------------------------------- auth


class _FakeSTS:
    def __init__(self, calls):
        self._calls = calls

    def assume_role(self, RoleArn, RoleSessionName):
        self._calls["assumed_role_arn"] = RoleArn
        return {
            "Credentials": {
                "AccessKeyId": "TMPAK",
                "SecretAccessKey": "TMPSK",
                "SessionToken": "TMPTOK",
            }
        }


class _FakeSession:
    def __init__(self, calls, **kwargs):
        self._calls = calls
        calls["session_kwargs"] = kwargs

    def client(self, service, **kwargs):
        self._calls[f"{service}_client_kwargs"] = kwargs
        return f"{service}-client"


def _patch_boto(monkeypatch, calls):
    import boto3

    def fake_client(service, **kwargs):
        calls.setdefault("sts_kwargs", kwargs)
        assert service == "sts"
        return _FakeSTS(calls)

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(boto3, "Session", lambda **kw: _FakeSession(calls, **kw))


class TestAuth:
    def test_role_variant_schema_makes_keys_optional(self):
        from app.schemas.data_sources.configs import S3RoleCredentials

        fields = S3RoleCredentials.model_fields
        assert fields["role_arn"].is_required()
        assert not fields["access_key"].is_required()
        assert not fields["secret_key"].is_required()
        # role_arn alone must validate (assume-role via instance profile / IRSA)
        S3RoleCredentials(role_arn="arn:aws:iam::123:role/reader")

    def test_assume_role_without_static_keys(self, monkeypatch):
        """role_arn but no keys → STS resolves via the ambient default chain
        (no keys passed), then the session uses the assumed temp creds."""
        calls: dict = {}
        _patch_boto(monkeypatch, calls)
        c = S3Client(
            bucket="b", prefix="", region="us-east-1",
            role_arn="arn:aws:iam::123:role/reader",
        )
        s3 = c._client()
        assert s3 == "s3-client"
        assert calls["assumed_role_arn"] == "arn:aws:iam::123:role/reader"
        # No static keys were handed to the STS client → default chain.
        assert "aws_access_key_id" not in calls["sts_kwargs"]
        # The working session is built from the assumed short-lived creds.
        assert calls["session_kwargs"]["aws_access_key_id"] == "TMPAK"
        assert calls["session_kwargs"]["aws_session_token"] == "TMPTOK"

    def test_assume_role_with_static_keys_passes_them_to_sts(self, monkeypatch):
        calls: dict = {}
        _patch_boto(monkeypatch, calls)
        c = S3Client(
            bucket="b", prefix="", region="us-east-1",
            role_arn="arn:aws:iam::123:role/reader",
            access_key="AKIA", secret_key="secret",
        )
        c._client()
        assert calls["sts_kwargs"]["aws_access_key_id"] == "AKIA"
        assert calls["sts_kwargs"]["aws_secret_access_key"] == "secret"

    def test_default_chain_no_role_no_keys(self, monkeypatch):
        """aws_default variant: no role, no keys → a plain region-only session
        that resolves via the instance profile / IRSA / env chain."""
        calls: dict = {}
        _patch_boto(monkeypatch, calls)
        c = S3Client(bucket="b", prefix="", region="us-east-1")
        c._client()
        assert calls["session_kwargs"] == {"region_name": "us-east-1"}
        assert "assumed_role_arn" not in calls  # nothing assumed
