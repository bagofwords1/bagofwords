"""Local audit trail (design C4/C5): the AuditLog unit + dispatch recording."""

from __future__ import annotations

import json

import pytest

from ..audit import AuditLog
from ..config import AgentConfig
from ..tunnel import EdgeAgentTunnel


# -- AuditLog unit -----------------------------------------------------------

def test_record_appends_to_ring_and_returns_entry():
    log = AuditLog(None)
    e = log.record(connection="pg", operation="execute_query", outcome="ok",
                   duration_ms=12, row_count=5, request_id="q1", sql="select 1")
    assert e["operation"] == "execute_query" and e["row_count"] == 5
    assert log.recent()[0] is not None
    assert len(log) == 1


def test_recent_is_newest_first_and_limited():
    log = AuditLog(None)
    for i in range(5):
        log.record(connection="pg", operation="test_connection", outcome="ok",
                   duration_ms=i)
    recent = log.recent(limit=2)
    assert [e["duration_ms"] for e in recent] == [4, 3]  # newest first


def test_ring_is_bounded_by_retain():
    log = AuditLog(None, retain=3)
    for i in range(10):
        log.record(connection="pg", operation="x", outcome="ok", duration_ms=i)
    assert len(log) == 3
    assert [e["duration_ms"] for e in log.recent()] == [9, 8, 7]


def test_persists_to_file_as_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(connection="pg", operation="execute_query", outcome="ok",
               duration_ms=7, row_count=3)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["operation"] == "execute_query" and row["row_count"] == 3
    assert row["ts"].endswith("+00:00") or "T" in row["ts"]


def test_file_tail_is_reloaded_into_ring_on_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(connection="pg", operation="get_schemas", outcome="ok",
                          duration_ms=100, row_count=8)
    # A fresh instance (a restart) sees the earlier entry.
    reopened = AuditLog(path)
    assert len(reopened) == 1
    assert reopened.recent()[0]["operation"] == "get_schemas"


def test_credentials_are_never_written(tmp_path):
    # The recorder has no credential parameter at all; assert a caller can't
    # smuggle one and that only the documented fields land on disk.
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(connection="pg", operation="execute_query", outcome="ok",
               duration_ms=1, sql="select * from t")
    row = json.loads(path.read_text().splitlines()[0])
    assert set(row) <= {"ts", "connection", "operation", "outcome",
                        "duration_ms", "row_count", "request_id", "error", "sql"}


def test_sql_is_truncated(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    e = log.record(connection="pg", operation="execute_query", outcome="ok",
                   duration_ms=1, sql="x" * 5000)
    assert len(e["sql"]) == 2000


def test_corrupt_file_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"ts":"t","operation":"ok"}\nnot json\n{"ts":"t2","operation":"two"}\n')
    log = AuditLog(path)
    assert len(log) == 2  # the junk line dropped, the two valid ones kept


# -- dispatch integration ----------------------------------------------------

class StubMsg:
    def __init__(self, data: bytes):
        self.data = data
        self.subject = "s"
        self.reply = "_INBOX.x"
        self.responses: list[bytes] = []

    async def respond(self, payload: bytes) -> None:
        self.responses.append(payload)


def _req(operation, **kwargs):
    return json.dumps({
        "jsonrpc": "2.0", "id": "q_1",
        "params": {"operation": operation, "kwargs": kwargs},
    }).encode()


@pytest.mark.asyncio
async def test_handle_request_records_an_audit_entry_on_unknown_connection():
    # No such connection -> _NotImplemented -> outcome "not_implemented",
    # and an audit line is still written (every terminal path records).
    audit = AuditLog(None)
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01", connections=[])
    t = EdgeAgentTunnel(cfg, audit=audit)
    msg = StubMsg(_req("execute_query", sql="select 1"))
    await t._handle_request(msg, "ghost")
    assert len(audit) == 1
    e = audit.recent()[0]
    assert e["operation"] == "execute_query"
    assert e["outcome"] == "not_implemented"
    assert e["request_id"] == "q_1"
    assert "duration_ms" in e


@pytest.mark.asyncio
async def test_malformed_request_is_audited_as_error():
    audit = AuditLog(None)
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01", connections=[])
    t = EdgeAgentTunnel(cfg, audit=audit)
    await t._handle_request(StubMsg(b"not json"), "pg")
    e = audit.recent()[0]
    assert e["outcome"] == "error" and e["error"] == "malformed request"
