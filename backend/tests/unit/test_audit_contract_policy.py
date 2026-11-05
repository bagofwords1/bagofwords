"""Audit event contract primitives (docs/design/audit-event-contract.md):
content policy, bounded query evidence, correlation context and the
trusted-proxy address resolution. Pure logic, no database."""
import json
from types import SimpleNamespace

import pytest

from app.ee.audit import context as audit_context
from app.ee.audit.content_policy import REDACTED, bounded_queries, sanitize_details, with_queries
from app.ee.audit.contract import category_for, infer_outcome, normalize_outcome


# --- content policy ---------------------------------------------------------

@pytest.mark.parametrize("key", ["password", "api_key", "apiKey", "Authorization", "hmac_secret", "client_secret",
                                 "refresh_token", "cookie", "x-api-key", "private_key", "credentials"])
def test_secret_keys_are_redacted_at_any_depth(key):
    out = sanitize_details({"ok": "value", key: "s3cr3t-value", "nested": {"inner": {key: "deeper"}}, "list": [{key: "x"}]})
    assert out["ok"] == "value"
    assert out[key] == REDACTED
    assert out["nested"]["inner"][key] == REDACTED
    assert out["list"][0][key] == REDACTED


@pytest.mark.parametrize("value", [
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "Bearer abcdefghijklmnop",
    "gAAAAABmQ2xhdWRlLWF1ZGl0LWxvb3AtdGVzdC12YWx1ZQ==",
    "AKIAIOSFODNN7EXAMPLE",
    "sk-abcdefghijklmnopqrstuvwxyz",
])
def test_secret_shaped_values_are_redacted_whatever_the_key(value):
    out = sanitize_details({"note": value, "items": [value]})
    assert out["note"] == REDACTED and out["items"][0] == REDACTED


def test_ordinary_values_and_caller_object_are_untouched():
    src = {"title": "Q3 Revenue", "row_count": 12, "flags": [True, None], "sql": "SELECT 1"}
    out = sanitize_details(src)
    assert out == src and out is not src


def test_oversized_payload_becomes_an_explicit_marker_not_a_silent_cut():
    big = {"title": "t", "blob": "x" * 5000}
    out = sanitize_details(big, max_bytes=1000)
    assert out["_truncated"] is True
    assert out["_original_bytes"] > 1000
    assert "blob" in out["_dropped_keys"]
    assert out["title"] == "t"  # small scalars survive


# --- bounded query evidence --------------------------------------------------

def test_every_query_is_kept_regardless_of_count():
    qs = [f"SELECT {i}" for i in range(50)]
    out, trunc = bounded_queries(qs, max_bytes=100_000)
    assert out == qs and trunc is None


def test_over_budget_queries_are_shortened_with_a_marker_and_never_dropped():
    qs = ["SELECT " + ("a" * 3000), "SELECT 2", "SELECT " + ("b" * 3000)]
    out, trunc = bounded_queries(qs, max_bytes=2000)
    assert len(out) == 3
    assert out[1] == "SELECT 2"
    assert trunc["truncated"] is True and trunc["query_count"] == 3
    assert {s["index"] for s in trunc["shortened"]} == {0, 2}
    assert all(s["original_length"] == 3007 for s in trunc["shortened"])
    assert all(len(q) < 3007 for q in (out[0], out[2]))


def test_with_queries_attaches_marker_only_when_cut(monkeypatch):
    d = with_queries({"tool": "x"}, ["SELECT 1"])
    assert d["executed_queries"] == ["SELECT 1"] and "executed_queries_truncation" not in d


# --- contract -----------------------------------------------------------------

@pytest.mark.parametrize("action,outcome", [
    ("access.denied", "denied"), ("auth.login_failed", "failure"), ("tool.data_query_failed", "failure"),
    ("report.created", "success"), ("auth.login", "success"),
])
def test_outcome_is_inferred_for_legacy_producers_and_explicit_values_win(action, outcome):
    assert infer_outcome(action) == outcome
    assert normalize_outcome(None, action) == outcome
    assert normalize_outcome("denied", "report.created") == "denied"
    assert normalize_outcome("bogus", action) == outcome


def test_categories_cover_the_coverage_matrix_areas():
    assert category_for("auth.login") == "authentication"
    assert category_for("access.denied") == "authorization"
    assert category_for("member.invited") == "administration"
    assert category_for("query.executed") == "data_access"
    assert category_for("tool.data_queried") == "agent"


# --- correlation context ------------------------------------------------------

def _req(headers: dict, peer: str = "203.0.113.9"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer))


def test_request_id_is_validated_or_minted():
    ctx = audit_context.from_request(_req({"X-Request-ID": "abc-123-xyz-999"}))
    assert ctx.request_id == "abc-123-xyz-999"
    bad = audit_context.from_request(_req({"X-Request-ID": "<script>alert(1)</script>"}))
    assert bad.request_id and bad.request_id != "<script>alert(1)</script>" and len(bad.request_id) == 32


def test_session_id_is_the_opaque_sid_claim_never_the_token():
    import jwt
    token = jwt.encode({"sub": "u1", "sid": "0123456789abcdef0123456789abcdef"}, "irrelevant", algorithm="HS256")
    ctx = audit_context.from_request(_req({"Authorization": f"Bearer {token}"}))
    assert ctx.session_id == "0123456789abcdef0123456789abcdef"
    assert token not in json.dumps(ctx.__dict__)
    assert ctx.auth_method == "bearer"
    no_sid = jwt.encode({"sub": "u1"}, "irrelevant", algorithm="HS256")
    assert audit_context.from_request(_req({"Authorization": f"Bearer {no_sid}"})).session_id is None


@pytest.mark.parametrize("peer,xff,expected", [
    # untrusted public peer: header ignored, peer recorded
    ("203.0.113.9", "198.51.100.1", "203.0.113.9"),
    # loopback proxy (default trusted): rightmost untrusted hop is the client
    ("127.0.0.1", "198.51.100.1", "198.51.100.1"),
    ("127.0.0.1", "198.51.100.1, 10.0.0.5", "198.51.100.1"),
    ("127.0.0.1", "203.0.113.7, 198.51.100.1, 10.0.0.5", "198.51.100.1"),
    # docker/private proxy, malformed header: fall back to the peer
    ("172.18.0.3", "not-an-ip", "172.18.0.3"),
    ("127.0.0.1", None, "127.0.0.1"),
])
def test_client_ip_is_resolved_only_through_trusted_proxies(monkeypatch, peer, xff, expected):
    monkeypatch.setattr(audit_context, "_trusted_networks", lambda: [__import__("ipaddress").ip_network(n) for n in
                                                                   ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12")])
    assert audit_context.resolve_client_ip(peer, xff) == expected


def test_explicit_empty_trusted_list_records_the_peer_only(monkeypatch):
    monkeypatch.setattr(audit_context, "_trusted_networks", lambda: [])
    assert audit_context.resolve_client_ip("127.0.0.1", "198.51.100.1") == "127.0.0.1"


def test_scope_binds_and_restores_context():
    assert audit_context.current() is None
    with audit_context.scope(audit_context.for_job("x")) as ctx:
        assert audit_context.current() is ctx and ctx.source == "job"
    assert audit_context.current() is None
