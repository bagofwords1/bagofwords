"""Unit tests for line-level grep (grep_files capability + engine + tool wiring).

Covers the shared sweep engine (matching, context, caps, cursor resume,
binary/oversize skips), the NetworkDirClient implementation against a temp
tree (including the include-globs access boundary), the S3Client
implementation against a stubbed boto3 client, and the tool/capability
registration. No DB, no network.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.data_sources.clients._file_source_common import GlobScopeError
from app.data_sources.clients._grep_common import (
    compile_pattern,
    make_cursor,
    parse_cursor,
    render_matches_details,
    run_grep_sweep,
)
from app.data_sources.clients.base import Capability
from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.data_sources.clients.s3_client import S3Client


@pytest.fixture()
def logs(tmp_path: Path) -> Path:
    """A small log corpus: text logs, csv, ndjson, a binary, an oversized file."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "web.log").write_text(
        "2026-07-01 10:00:01 INFO boot ok\n"
        "2026-07-01 10:00:02 ERROR ERR_TIMEOUT_504 upstream\n"
        "2026-07-01 10:00:03 INFO retrying\n"
        "2026-07-01 10:00:04 ERROR ERR_TIMEOUT_504 upstream again\n"
        "2026-07-01 10:00:05 INFO recovered\n"
    )
    (tmp_path / "app" / "worker.log").write_text(
        "job=1 status=ok\n"
        "job=2 status=error code=ERR_TIMEOUT_504\n"
        "job=3 status=ok\n"
    )
    (tmp_path / "events.ndjson").write_text(
        '{"evt":"login","user":"a"}\n'
        '{"evt":"error","code":"ERR_TIMEOUT_504"}\n'
    )
    (tmp_path / "rows.csv").write_text("id,msg\n1,fine\n2,ERR_TIMEOUT_504 in row\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02ERR_TIMEOUT_504\x00")
    return tmp_path


def _client(root: Path, **overrides) -> NetworkDirClient:
    return NetworkDirClient(root_path=str(root), **overrides)


# ---------------------------------------------------------------- registry


class TestRegistration:
    def test_clients_declare_capability(self, logs):
        assert Capability.GREP_FILES in NetworkDirClient.capabilities
        assert Capability.GREP_FILES in S3Client.capabilities
        assert Capability.GREP_FILES in _client(logs).capabilities

    def test_tool_registered_and_gated(self):
        from app.ai.registry import ToolRegistry

        reg = ToolRegistry()
        meta = reg.get_metadata("grep_files")
        assert meta is not None
        assert meta.requires_capability == "grep_files"
        assert meta.category == "research"
        assert meta.idempotent is True
        # Gated OUT for a source set without the capability…
        names = {t["name"] for t in reg.get_catalog_for_plan_type(
            "research", available_capabilities={"search_files", "read_file"},
        )}
        assert "grep_files" not in names
        # …and IN when a scannable source is attached.
        names = {t["name"] for t in reg.get_catalog_for_plan_type(
            "research", available_capabilities={"grep_files"},
        )}
        assert "grep_files" in names

    def test_base_client_raises_not_implemented(self):
        from app.data_sources.clients.graph_drive_client import GraphDriveClient

        assert Capability.GREP_FILES not in GraphDriveClient.capabilities


# ---------------------------------------------------------------- engine


class TestEngine:
    def _sweep(self, files: dict, **kw):
        candidates = [{"id": k, "path": k, "size": len(v)} for k, v in files.items()]
        return run_grep_sweep(
            candidates=candidates,
            read_bytes=lambda e: files[e["id"]],
            scope_key="t",
            **kw,
        )

    def test_literal_vs_regex(self):
        files = {"a.txt": b"price is $5.00\nno match\n"}
        # Literal: the $ and . are escaped.
        r = self._sweep(files, pattern="$5.00", is_regex=False)
        assert r["total_matches"] == 1 and r["matches"][0]["line_no"] == 1
        # Same string as a regex matches nothing ($ = end anchor mid-pattern).
        r = self._sweep(files, pattern="$5.00", is_regex=True)
        assert r["total_matches"] == 0
        assert r["stop_reason"] == "complete" and r["next_cursor"] is None

    def test_ignore_case(self):
        files = {"a.txt": b"Error here\nerror there\nERROR everywhere\n"}
        r = self._sweep(files, pattern="^error", ignore_case=True)
        assert r["total_matches"] == 3
        r = self._sweep(files, pattern="^error", ignore_case=False)
        assert r["total_matches"] == 1

    def test_context_lines(self):
        files = {"a.txt": b"l1\nl2\nHIT\nl4\nl5\n"}
        r = self._sweep(files, pattern="HIT", before=2, after=2)
        m = r["matches"][0]
        assert m["before"] == ["l1", "l2"]
        assert m["after"] == ["l4", "l5"]
        assert m["line_no"] == 3

    def test_binary_and_oversize_skips(self):
        files = {"bin.dat": b"\x00\x01HIT", "big.log": b"HIT\n" * 10, "ok.log": b"HIT\n"}
        candidates = [
            {"id": "bin.dat", "size": 4},
            {"id": "big.log", "size": 40},
            {"id": "ok.log", "size": 4},
        ]
        r = run_grep_sweep(
            candidates=candidates, read_bytes=lambda e: files[e["id"]],
            pattern="HIT", max_bytes_per_file=10, scope_key="t",
        )
        reasons = {s["file_id"]: s["reason"] for s in r["skipped_files"]}
        assert reasons == {"bin.dat": "binary", "big.log": "too_large"}
        assert r["files_scanned"] == 1 and r["total_matches"] == 1

    def test_unreadable_and_scope_skips(self):
        def read(entry):
            if entry["id"] == "denied.txt":
                raise GlobScopeError("off scope")
            raise OSError("io fail")

        r = run_grep_sweep(
            candidates=[{"id": "denied.txt"}, {"id": "broken.txt"}],
            read_bytes=read, pattern="x", scope_key="t",
        )
        reasons = {s["file_id"]: s["reason"] for s in r["skipped_files"]}
        assert reasons == {"denied.txt": "access_denied", "broken.txt": "unreadable"}
        assert r["stop_reason"] == "complete"

    def test_per_file_cap_flags_truncated_and_moves_on(self):
        files = {"noisy.log": b"HIT\n" * 100, "z.log": b"HIT\n"}
        r = self._sweep(files, pattern="HIT", max_matches_per_file=3, max_matches=100)
        assert r["truncated"] is True
        # noisy emitted 3, z emitted 1 — the noisy file didn't eat the budget.
        by_file = {}
        for m in r["matches"]:
            by_file.setdefault(m["file_id"], 0)
            by_file[m["file_id"]] += 1
        assert by_file == {"noisy.log": 3, "z.log": 1}
        assert r["files_with_matches"] == 2

    def test_max_matches_stops_with_cursor_and_resume_is_lossless(self):
        files = {"a.log": b"HIT 1\nmiss\nHIT 2\nHIT 3\n", "b.log": b"HIT 4\nHIT 5\n"}

        def page(cursor):
            return self._sweep(files, pattern="HIT", max_matches=2, cursor=cursor)

        seen = []
        cursor = None
        for _ in range(10):
            r = page(cursor)
            seen.extend((m["file_id"], m["line_no"]) for m in r["matches"])
            cursor = r["next_cursor"]
            if r["stop_reason"] == "complete":
                break
        assert r["stop_reason"] == "complete"
        assert seen == [("a.log", 1), ("a.log", 3), ("a.log", 4), ("b.log", 1), ("b.log", 2)]

    def test_max_files_stops_with_cursor(self):
        files = {f"f{i}.log": b"HIT\n" for i in range(5)}
        r = self._sweep(files, pattern="HIT", max_files=2)
        assert r["stop_reason"] == "max_files"
        assert r["files_scanned"] == 2
        assert r["next_cursor"]
        # Resuming finishes the sweep.
        r2 = self._sweep(files, pattern="HIT", max_files=10, cursor=r["next_cursor"])
        assert r2["stop_reason"] == "complete"
        assert r["total_matches"] + r2["total_matches"] == 5

    def test_cursor_rejects_different_scope(self):
        cur = make_cursor("a.log", 3, "scope-A")
        assert parse_cursor(cur, "scope-A") == ("a.log", 3)
        with pytest.raises(ValueError):
            parse_cursor(cur, "scope-B")
        with pytest.raises(ValueError):
            parse_cursor("not-a-cursor", "scope-A")

    def test_invalid_or_empty_pattern_raises(self):
        import re as _re
        with pytest.raises(_re.error):
            compile_pattern("([unclosed", is_regex=True)
        with pytest.raises(ValueError):
            compile_pattern("  ", is_regex=True)

    def test_long_line_clipped_and_flagged(self):
        files = {"a.log": b"x" * 1000 + b" HIT\n"}
        r = self._sweep(files, pattern="HIT")
        m = r["matches"][0]
        assert m["line_truncated"] is True and len(m["line"]) == 500


class TestRenderMatchesDetails:
    """The model-facing rendering: the planner consumes observation.details,
    so the matched LINE CONTENT must be in it — counts alone are useless."""

    def _match(self, path="app/web.log", line_no=2, line="ERROR boom", before=None, after=None):
        return {"file_id": path, "path": path, "line_no": line_no, "line": line,
                "before": before or [], "after": after or []}

    def test_contains_line_content_and_location(self):
        text = render_matches_details([
            self._match(line="ERROR ERR_TIMEOUT_504 upstream",
                        before=["INFO boot ok"], after=["INFO retrying"]),
        ])
        assert "ERR_TIMEOUT_504 upstream" in text
        assert "app/web.log:2:" in text          # matched line, grep ':' marker
        assert "app/web.log:1- INFO boot ok" in text   # context, grep '-' marker
        assert "app/web.log:3- INFO retrying" in text

    def test_caps_and_note_says_found_not_missing(self):
        many = [self._match(line_no=i + 1, line="E " + "x" * 200) for i in range(100)]
        text = render_matches_details(many, max_chars=1000)
        assert len(text) < 1400  # cap + note
        # The note must say the rest WERE found (not "go fetch more") and must
        # not point at a cursor that doesn't exist.
        assert "all 100 were found" in text
        assert "cursor" not in text
        assert "do NOT re-run the same sweep" in text

    def test_note_mentions_cursor_only_when_pages_remain(self):
        many = [self._match(line_no=i + 1, line="E " + "x" * 200) for i in range(100)]
        text = render_matches_details(many, max_chars=1000, has_more_pages=True)
        assert "Continue with the cursor" in text

    def test_adaptive_budget_renders_modest_sets_in_full(self):
        # The incident case: ~a dozen long JSON log lines must ALL render —
        # no note, no clipping — under the default (adaptive) budget.
        matches = [
            self._match(line_no=i + 1, line='{"ts":"2026-07-12"} ' + "x" * 480)
            for i in range(11)
        ]
        text = render_matches_details(matches)
        assert all(f":{i + 1}:" in text for i in range(11))
        assert "showing" not in text

    def test_empty(self):
        assert render_matches_details([]) == ""


# ------------------------------------------------------------- network_dir


class TestNetworkDirGrep:
    def test_sweep_matches_across_text_formats(self, logs):
        c = _client(logs)
        r = c.grep_files("ERR_TIMEOUT_504", is_regex=False)
        hit_files = {m["file_id"] for m in r["matches"]}
        # Content-sniffed: log, ndjson AND csv all grep; binary is skipped.
        assert hit_files == {
            "app/web.log", "app/worker.log", "events.ndjson", "rows.csv",
        }
        assert r["total_matches"] == 5
        assert {s["file_id"] for s in r["skipped_files"]} == {"blob.bin"}
        assert r["skipped_files"][0]["reason"] == "binary"
        assert r["stop_reason"] == "complete"

    def test_line_numbers_and_context(self, logs):
        c = _client(logs)
        r = c.grep_files("ERR_TIMEOUT_504", file_ids=["app/web.log"], before=1, after=1)
        assert [m["line_no"] for m in r["matches"]] == [2, 4]
        first = r["matches"][0]
        assert first["before"] == ["2026-07-01 10:00:01 INFO boot ok"]
        assert first["after"] == ["2026-07-01 10:00:03 INFO retrying"]

    def test_name_pattern_scopes_sweep(self, logs):
        c = _client(logs)
        r = c.grep_files("ERR_TIMEOUT_504", name_pattern="*.log")
        assert {m["file_id"] for m in r["matches"]} == {"app/web.log", "app/worker.log"}

    def test_folder_scope(self, logs):
        c = _client(logs)
        r = c.grep_files("ERR_TIMEOUT_504", folder_id="app")
        assert {m["file_id"] for m in r["matches"]} == {"app/web.log", "app/worker.log"}

    def test_explicit_off_glob_id_reported_denied(self, logs):
        c = _client(logs, include_globs="app/*.log")
        r = c.grep_files("ERR_TIMEOUT_504", file_ids=["app/web.log", "rows.csv"])
        assert {m["file_id"] for m in r["matches"]} == {"app/web.log"}
        assert {(s["file_id"], s["reason"]) for s in r["skipped_files"]} == {
            ("rows.csv", "access_denied"),
        }

    def test_sweep_respects_include_globs(self, logs):
        c = _client(logs, include_globs="app/*.log")
        r = c.grep_files("ERR_TIMEOUT_504")
        assert {m["file_id"] for m in r["matches"]} == {"app/web.log", "app/worker.log"}

    def test_traversal_rejected_as_not_found(self, logs):
        c = _client(logs)
        r = c.grep_files("x", file_ids=["../outside.txt"])
        assert r["skipped_files"] == [{"file_id": "../outside.txt", "reason": "not_found"}]

    def test_cursor_pages_through_corpus(self, logs):
        c = _client(logs)
        r1 = c.grep_files("ERR_TIMEOUT_504", is_regex=False, max_matches=2)
        assert r1["stop_reason"] == "max_matches" and r1["next_cursor"]
        r2 = c.grep_files(
            "ERR_TIMEOUT_504", is_regex=False, max_matches=100, cursor=r1["next_cursor"]
        )
        assert r2["stop_reason"] == "complete"
        assert len(r1["matches"]) + len(r2["matches"]) == 5

    def test_agrep_files_wrapper(self, logs):
        import asyncio

        c = _client(logs)
        r = asyncio.run(c.agrep_files("ERR_TIMEOUT_504", name_pattern="*.ndjson"))
        assert [m["file_id"] for m in r["matches"]] == ["events.ndjson"]


class TestGrepFilesTool:
    """Tool-level contract: the observation must carry the matched lines
    (details), not just counts — the planner never sees the raw output."""

    @pytest.mark.asyncio
    async def test_observation_details_carry_matched_lines(self, logs):
        from unittest.mock import AsyncMock, patch
        from app.ai.tools.implementations.grep_files import GrepFilesTool

        client = _client(logs)
        with patch(
            "app.ai.tools.implementations.grep_files.resolve_file_client",
            new=AsyncMock(return_value=(client, None)),
        ):
            tool = GrepFilesTool()
            events = [e async for e in tool.run_stream(
                {"connection_id": "C1", "pattern": "ERR_TIMEOUT_504",
                 "is_regex": False, "before": 1, "after": 1},
                {},
            )]
        payload = events[-1].payload
        assert payload["output"]["success"] is True
        details = payload["observation"].get("details") or ""
        # Line content + location reach the model, with context.
        assert "ERR_TIMEOUT_504 upstream" in details
        assert "app/web.log:2:" in details
        assert "app/web.log:1- " in details
        # And the summary still carries the accounting.
        assert "5 match(es)" in payload["observation"]["summary"]

    @pytest.mark.asyncio
    async def test_bad_regex_is_clean_error(self, logs):
        from unittest.mock import AsyncMock, patch
        from app.ai.tools.implementations.grep_files import GrepFilesTool

        with patch(
            "app.ai.tools.implementations.grep_files.resolve_file_client",
            new=AsyncMock(return_value=(_client(logs), None)),
        ):
            tool = GrepFilesTool()
            events = [e async for e in tool.run_stream(
                {"connection_id": "C1", "pattern": "([unclosed"}, {},
            )]
        payload = events[-1].payload
        assert payload["output"]["success"] is False
        assert "Invalid pattern" in payload["output"]["error"]


# ---------------------------------------------------------------------- s3


class TestS3Grep:
    def _stubbed(self):
        import boto3
        from botocore.stub import Stubber

        client = S3Client(
            bucket="b", prefix="logs/", region="us-east-1",
            access_key="k", secret_key="s",
        )
        s3 = boto3.client(
            "s3", region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="y",
        )
        client._s3 = s3
        return client, Stubber(s3)

    @staticmethod
    def _body(data: bytes):
        from botocore.response import StreamingBody
        return StreamingBody(io.BytesIO(data), len(data))

    def test_sweep_lists_then_gets_matching_objects(self):
        client, stub = self._stubbed()
        listing = {
            "Contents": [
                {"Key": "logs/a.log", "Size": 20},
                {"Key": "logs/huge.log", "Size": 99_999_999},
            ],
        }
        stub.add_response("list_objects_v2", listing, {"Bucket": "b", "Prefix": "logs/"})
        stub.add_response(
            "get_object", {"Body": self._body(b"ok\nERR here\n")},
            {"Bucket": "b", "Key": "logs/a.log"},
        )
        with stub:
            r = client.grep_files("ERR", max_bytes_per_file=1000)
        assert [(m["file_id"], m["line_no"]) for m in r["matches"]] == [("a.log", 2)]
        assert r["skipped_files"] == [{"file_id": "huge.log", "reason": "too_large"}]
        assert r["stop_reason"] == "complete"

    def test_explicit_id_off_glob_denied_without_get(self):
        client, stub = self._stubbed()
        client.include_globs = ["*.log"]
        # No stubbed HEAD/GET for the denied id — confinement happens first.
        stub.add_response(
            "head_object", {"ContentLength": 8},
            {"Bucket": "b", "Key": "logs/a.log"},
        )
        stub.add_response(
            "get_object", {"Body": self._body(b"ERR one\n")},
            {"Bucket": "b", "Key": "logs/a.log"},
        )
        with stub:
            r = client.grep_files("ERR", file_ids=["a.log", "secret.csv"])
        assert [m["file_id"] for m in r["matches"]] == ["a.log"]
        assert {(s["file_id"], s["reason"]) for s in r["skipped_files"]} == {
            ("secret.csv", "access_denied"),
        }
