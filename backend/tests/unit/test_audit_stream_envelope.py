"""Envelope v1 (docs/design/audit-log-streams.md, Loop A1), additive contract
fields (docs/design/audit-event-contract.md): the public event shape every
stream and the export endpoint emit. Every key a v1 consumer parses keeps its
place and meaning; contract fields sit next to them."""
from datetime import datetime
from types import SimpleNamespace

from app.ee.audit.streams.envelope import build_envelope

V1_KEYS = {"id", "version", "action", "occurred_at", "organization", "actor", "targets", "context", "metadata"}


def _log(**kw):
    base = dict(
        id="6f1c2d3e-0000-4000-8000-000000000001", organization_id="org-1", user_id="user-1",
        action="report.updated", resource_type="report", resource_id="rep-1",
        details={"title": "Q3 Revenue"}, ip_address="203.0.113.4", user_agent="UA/1",
        created_at=datetime(2026, 10, 3, 12, 0, 0, 123456),
        occurred_at=None, outcome=None, failure_reason=None, request_id=None, session_id=None,
        actor_type=None, actor_snapshot=None, schema_version=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_legacy_row_keeps_the_v1_golden_shape_and_is_marked_legacy():
    env = build_envelope(_log(), user_email="a@acme.com", organization_name="Acme")
    # Reviewed by hand: the v1 keys are byte-for-byte what Loop A1 pinned.
    assert {k: env[k] for k in V1_KEYS} == {
        "id": "6f1c2d3e-0000-4000-8000-000000000001",
        "version": 1,
        "action": "report.updated",
        "occurred_at": "2026-10-03T12:00:00.123Z",
        "organization": {"id": "org-1", "name": "Acme"},
        "actor": {"type": "user", "id": "user-1", "email": "a@acme.com", "name": None},
        "targets": [{"type": "report", "id": "rep-1", "name": "Q3 Revenue"}],
        "context": {"ip_address": "203.0.113.4", "user_agent": "UA/1"},
        "metadata": {"title": "Q3 Revenue"},
    }
    assert env["legacy"] is True and env["schema_version"] == 1
    assert env["outcome"] == "success"  # inferred, never fabricated as a failure
    assert env["recorded_at"] == env["occurred_at"]
    assert env["correlation"] == {"request_id": None, "session_id": None, "parent_id": None, "execution_id": None}


def test_contract_row_exports_event_time_outcome_correlation_and_snapshot():
    env = build_envelope(_log(
        schema_version=2, occurred_at=datetime(2026, 10, 3, 11, 58, 0), outcome="failure", failure_reason="execution_failure",
        request_id="req-1", session_id="sid-1", actor_type="agent",
        actor_snapshot={"email": "old@acme.com", "name": "Old Name"},
        details={"title": "Q3", "agent_execution_id": "run-9"},
    ), user_email="renamed@acme.com", organization_name="Acme")
    assert env["legacy"] is False and env["schema_version"] == 2
    assert env["occurred_at"] == "2026-10-03T11:58:00.000Z"
    assert env["recorded_at"] == "2026-10-03T12:00:00.123Z"
    assert env["outcome"] == "failure" and env["failure_reason"] == "execution_failure"
    assert env["correlation"] == {"request_id": "req-1", "session_id": "sid-1", "parent_id": None, "execution_id": "run-9"}
    # identity at event time wins over the live join
    assert env["actor"] == {"type": "agent", "id": "user-1", "email": "old@acme.com", "name": "Old Name"}
    assert env["category"] == "data_access"


def test_deleted_user_keeps_identity_from_the_snapshot():
    env = build_envelope(_log(schema_version=2, user_id=None, actor_type="user", actor_snapshot={"email": "gone@acme.com"}))
    assert env["actor"]["type"] == "user" and env["actor"]["email"] == "gone@acme.com" and env["actor"]["id"] is None


def test_actor_type_follows_agent_execution_then_user_then_system():
    agent = build_envelope(_log(details={"agent_execution_id": "run-1", "queries": ["SELECT 1"]}))
    assert agent["actor"]["type"] == "agent"
    assert agent["metadata"]["queries"] == ["SELECT 1"]  # details pass through unchanged
    assert build_envelope(_log(user_id=None, details=None))["actor"]["type"] == "system"
    assert build_envelope(_log(details={}))["actor"]["type"] == "user"


def test_legacy_details_go_through_the_content_policy_on_export():
    env = build_envelope(_log(details={"api_key": "AKIAIOSFODNN7EXAMPLE", "title": "ok"}))
    assert env["metadata"]["api_key"] == "[REDACTED]" and env["metadata"]["title"] == "ok"


def test_no_resource_means_no_targets_and_timestamps_are_utc_z():
    env = build_envelope(_log(resource_type=None, resource_id=None, details=None))
    assert env["targets"] == []
    assert env["metadata"] == {}
    assert env["occurred_at"].endswith("Z") and env["recorded_at"].endswith("Z")
