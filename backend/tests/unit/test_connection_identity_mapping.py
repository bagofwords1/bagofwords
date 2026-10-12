"""Identity mapping: the source grammar, strict resolution, and how a pinned
identity reaches (and stays in) a Postgres session.

End-to-end resolution through the credential resolvers is covered in
tests/e2e/test_member_attributes_identity_mapping.py.
"""

from types import SimpleNamespace

import pytest

from app.services.connection_identity_mapping import (
    resolve_source_strict,
    resolve_with_context,
    source_error,
)
from app.services.mcp_context_injection import IdentityContext, identity_context_for


def _ctx(**attributes):
    return IdentityContext(email="dana@corp.example.com", name="Dana", user_id="u-1", role="member",
                           attributes=attributes)


# ---------------------------------------------------------------------------
# Source grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "user.email", "user.name", "user.id", "membership.role",
    "membership.attr:pg_role", "membership.attr:qlik.user-id",
    "static:CORP", "static:{membership.attr:pg_role}_ro", "static:corp\\{user.name}",
])
def test_whitelisted_sources_are_accepted(source):
    assert source_error(source) is None


@pytest.mark.parametrize("source", [
    "", "   ", None, 42, "user.password", "membership.attr:", "membership.attr:has space",
    "static:", "static:{user.password}", "static:{membership.attr:}", "x" * 300,
])
def test_anything_else_is_rejected(source):
    assert source_error(source) is not None


# ---------------------------------------------------------------------------
# Strict resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source,expected", [
    ("membership.attr:pg_role", "sales_team"),
    ("static:{membership.attr:pg_role}_ro", "sales_team_ro"),
    ("static:CORP", "CORP"),
    ("user.email", "dana@corp.example.com"),
])
def test_sources_resolve_from_the_members_context(source, expected):
    assert resolve_source_strict(source, _ctx(pg_role="sales_team")) == expected


@pytest.mark.parametrize("source", [
    "membership.attr:pg_role",
    "static:{membership.attr:pg_role}_ro",
    "static:   {membership.attr:pg_role}",
])
def test_an_empty_value_or_interpolated_atom_is_missing_not_blank(source):
    """'team_{attr}' with no attr must not become the role 'team_' — that is a
    different, possibly real, account."""
    assert resolve_source_strict(source, _ctx()) is None
    assert resolve_source_strict(source, _ctx(pg_role="  ")) is None


def test_every_empty_slot_is_reported():
    values, missing = resolve_with_context(
        {"user_directory": "static:CORP", "user_id": "membership.attr:qlik_user_id"}, _ctx()
    )
    assert values == {"user_directory": "CORP"}
    assert missing == ["user_id"]


def test_admin_attributes_win_over_idp_synced_ones():
    membership = SimpleNamespace(
        profile_attributes={"department": "Sales", "pg_role": "synced_role"},
        custom_attributes={"pg_role": "admin_role"},
        role="member",
    )
    ctx = identity_context_for(SimpleNamespace(email="a@x.com", name="A", id="1"), membership)
    assert resolve_source_strict("membership.attr:pg_role", ctx) == "admin_role"
    assert resolve_source_strict("membership.attr:department", ctx) == "Sales"


# ---------------------------------------------------------------------------
# Postgres: the pinned role
# ---------------------------------------------------------------------------


class _Stop(Exception):
    pass


def _engine_args(monkeypatch, **client_kwargs):
    """The (uri, connect_args) the client would open its pool with."""
    import app.data_sources.clients.postgresql_client as pg

    seen = {}

    def fake_get_engine(uri, connect_args=None, **kw):
        seen["uri"], seen["connect_args"] = uri, dict(connect_args or {})
        raise _Stop()

    monkeypatch.setattr(pg, "get_engine", fake_get_engine)
    client = pg.PostgresqlClient(host="h", port=5432, database="d", user="gateway", password="pw", **client_kwargs)
    with pytest.raises(RuntimeError):
        with client.connect():
            pass
    return seen


def test_a_mapped_role_is_set_at_session_start(monkeypatch):
    seen = _engine_args(monkeypatch, session_role="sales_team")
    assert seen["connect_args"]["options"] == "-c role=sales_team"


def test_two_roles_never_share_a_pool(monkeypatch):
    from app.data_sources.engine_pool import _key

    a = _engine_args(monkeypatch, session_role="sales_team")
    b = _engine_args(monkeypatch, session_role="finance_team")
    none = _engine_args(monkeypatch)
    keys = {_key(x["uri"], x["connect_args"], None) for x in (a, b, none)}
    assert len(keys) == 3
    assert "options" not in none["connect_args"]


@pytest.mark.parametrize("role", ["sales team", "x;drop", "a\\b", "r" * 64, "role'quote"])
def test_role_names_that_need_escaping_are_refused(role):
    from app.data_sources.clients.postgresql_client import PostgresqlClient

    with pytest.raises(ValueError):
        PostgresqlClient(host="h", port=5432, database="d", user="u", session_role=role)


@pytest.mark.parametrize("sql", [
    "SET ROLE finance_team",
    "select 1; set session role postgres",
    "RESET ROLE",
    "SET SESSION AUTHORIZATION postgres",
    "select set_config('role', 'postgres', false)",
    "DISCARD ALL",
])
def test_a_pinned_session_refuses_to_change_identity(monkeypatch, sql):
    import app.data_sources.clients.postgresql_client as pg

    def must_not_connect(*a, **k):
        raise AssertionError("the guard must refuse before connecting")

    monkeypatch.setattr(pg, "get_engine", must_not_connect)
    client = pg.PostgresqlClient(host="h", port=5432, database="d", user="u", session_role="sales_team")
    with pytest.raises(PermissionError):
        client.execute_query(sql)


def test_generated_code_cannot_switch_the_role_through_a_raw_connection():
    """Generated code can reach client.connect(); the code validator refuses
    role switches in its SQL strings for every connector."""
    from app.ai.code_execution.code_execution import UnsafePythonError, validate_python_code

    for code in (
        'with client.connect() as c:\n    c.execute(text("SET ROLE finance_team"))',
        "df = client.execute_query(\"select set_config('role', 'postgres', false)\")",
    ):
        with pytest.raises(UnsafePythonError):
            validate_python_code(code)
    # Ordinary prose and SQL are untouched.
    validate_python_code('df = client.execute_query("SELECT role, count(*) FROM staff GROUP BY role")')
