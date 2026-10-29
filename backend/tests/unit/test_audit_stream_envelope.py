"""Envelope v1 (docs/design/audit-log-streams.md, Loop A1): the public event
shape every stream and the export endpoint emit."""
from datetime import datetime
from types import SimpleNamespace

from app.ee.audit.streams.envelope import build_envelope


def _log(**kw):
    base = dict(
        id="6f1c2d3e-0000-4000-8000-000000000001", organization_id="org-1", user_id="user-1",
        action="report.updated", resource_type="report", resource_id="rep-1",
        details={"title": "Q3 Revenue"}, ip_address="203.0.113.4", user_agent="UA/1",
        created_at=datetime(2026, 10, 3, 12, 0, 0, 123456),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_user_event_golden():
    # Reviewed by hand against the plan's envelope definition.
    assert build_envelope(_log(), user_email="a@acme.com", organization_name="Acme") == {
        "id": "6f1c2d3e-0000-4000-8000-000000000001",
        "version": 1,
        "action": "report.updated",
        "occurred_at": "2026-10-03T12:00:00.123Z",
        "organization": {"id": "org-1", "name": "Acme"},
        "actor": {"type": "user", "id": "user-1", "email": "a@acme.com"},
        "targets": [{"type": "report", "id": "rep-1", "name": "Q3 Revenue"}],
        "context": {"ip_address": "203.0.113.4", "user_agent": "UA/1"},
        "metadata": {"title": "Q3 Revenue"},
    }


def test_actor_type_follows_agent_execution_then_user_then_system():
    agent = build_envelope(_log(details={"agent_execution_id": "run-1", "queries": ["SELECT 1"]}))
    assert agent["actor"]["type"] == "agent"
    assert agent["metadata"]["queries"] == ["SELECT 1"]  # details pass through unchanged
    assert build_envelope(_log(user_id=None, details=None))["actor"]["type"] == "system"
    assert build_envelope(_log(details={}))["actor"]["type"] == "user"


def test_no_resource_means_no_targets_and_timestamps_are_utc_z():
    env = build_envelope(_log(resource_type=None, resource_id=None, details=None))
    assert env["targets"] == []
    assert env["metadata"] == {}
    assert env["occurred_at"].endswith("Z")
