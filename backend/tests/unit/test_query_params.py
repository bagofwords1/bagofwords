"""Parameterized queries — core contract tests.

Covers the safety-critical pieces: safe SQL rendering (injection, types,
dialects, IN expansion), value resolution (identity locking, unknown
rejection, defaults, optional='All'), fingerprint stability, the AST guards,
and declaration/code consistency.
"""

import pytest

from app.schemas.param_schema import ParamSpec, parse_param_specs
from app.ai.code_execution.query_params import (
    ParamError,
    check_declarations_vs_code,
    check_params_not_formatted,
    code_accepts_params,
    extract_code_param_names,
    params_fingerprint,
    render_sql_with_params,
    resolve_param_values,
    verify_identity_binds_in_queries,
)


class FakeIdentity:
    user_id = "u-1"
    email = "sarah@example.com"
    profile_attributes = {"department": "Sales"}
    group_names = {"emea", "managers"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_string_quoted_and_escaped():
    out = render_sql_with_params("SELECT * FROM t WHERE a = :v", {"v": "x' OR '1'='1"})
    assert out == "SELECT * FROM t WHERE a = 'x'' OR ''1''=''1'"


def test_render_backslash_dialect():
    out = render_sql_with_params(
        "SELECT * FROM t WHERE a = :v", {"v": "a\\'b"}, client_type_name="MysqlClient"
    )
    assert out == "SELECT * FROM t WHERE a = 'a\\\\''b'"


def test_render_mssql_non_ascii_gets_nvarchar_prefix():
    # SQL_Latin1_General_* databases turn a plain 'מטה' into '???' at parse
    # time; only N'…' survives. Reproduces the "Run with a value returns 0
    # rows" report on an MSSQL source.
    out = render_sql_with_params(
        "WHERE cat = :c", {"c": "מטה"}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat = N'מטה'"
    out = render_sql_with_params(
        "WHERE cat = :c", {"c": "מטה"}, client_type_name="MsFabricClient"
    )
    assert out == "WHERE cat = N'מטה'"


def test_render_mssql_ascii_stays_plain():
    out = render_sql_with_params(
        "WHERE cat = :c AND n = :n", {"c": "Admin", "n": 3}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat = 'Admin' AND n = 3"


def test_render_mssql_list_prefixes_each_non_ascii_item():
    out = render_sql_with_params(
        "WHERE cat IN :c", {"c": ["מטה", "Admin", "יצרן"]}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat IN (N'מטה', 'Admin', N'יצרן')"


def test_render_mssql_non_ascii_still_escapes_quotes():
    out = render_sql_with_params(
        "WHERE cat = :c", {"c": "מט'ה"}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat = N'מט''ה'"


def test_render_non_tsql_clients_never_prefix():
    for name in ("SqliteClient", "PostgresqlClient", "MysqlClient", ""):
        out = render_sql_with_params("WHERE cat = :c", {"c": "מטה"}, client_type_name=name)
        assert out == "WHERE cat = 'מטה'", name


def test_render_number_and_null_and_bool():
    out = render_sql_with_params(
        "WHERE n = :n AND m = :m AND b = :b", {"n": 5, "m": None, "b": True}
    )
    assert out == "WHERE n = 5 AND m = NULL AND b = TRUE"


def test_render_list_in_expansion():
    out = render_sql_with_params("WHERE g IN :g", {"g": ["a", "b"]})
    assert out == "WHERE g IN ('a', 'b')"


def test_render_empty_list_never_matches():
    out = render_sql_with_params("WHERE g IN :g", {"g": []})
    assert out == "WHERE g IN (NULL)"


def test_render_missing_value_raises():
    with pytest.raises(ParamError):
        render_sql_with_params("WHERE a = :a AND b = :b", {"a": 1})


def test_render_ignores_double_colon_casts():
    out = render_sql_with_params("SELECT x::text FROM t WHERE a = :a", {"a": 1})
    assert out == "SELECT x::text FROM t WHERE a = 1"


def test_render_extra_values_ok():
    out = render_sql_with_params("WHERE a = :a", {"a": 1, "unused": 2})
    assert out == "WHERE a = 1"


def test_render_non_finite_rejected():
    with pytest.raises(ParamError):
        render_sql_with_params("WHERE a = :a", {"a": float("inf")})


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _specs(*specs):
    return [ParamSpec.model_validate(s) for s in specs]


def test_resolve_input_uses_request_then_default():
    specs = _specs({"name": "region", "type": "string", "default": "EMEA"})
    assert resolve_param_values(specs, {"region": "APAC"})["region"] == "APAC"
    assert resolve_param_values(specs, None)["region"] == "EMEA"


def test_resolve_unknown_param_rejected():
    specs = _specs({"name": "region", "type": "string"})
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"nope": 1})


def test_resolve_identity_locked_rejects_client_value():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"email": "attacker@example.com"}, FakeIdentity())


def test_resolve_identity_binds_email():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    out = resolve_param_values(specs, None, FakeIdentity())
    assert out["email"] == "sarah@example.com"


def test_resolve_identity_profile_attribute():
    specs = _specs({
        "name": "dept", "type": "string", "source": "identity",
        "identity_binding": "viewer.profile_attributes.department",
    })
    assert resolve_param_values(specs, None, FakeIdentity())["dept"] == "Sales"


def test_resolve_identity_groups_sorted():
    specs = _specs({
        "name": "grps", "type": "list", "source": "identity",
        "identity_binding": "viewer.groups",
    })
    assert resolve_param_values(specs, None, FakeIdentity())["grps"] == ["emea", "managers"]


def test_resolve_identity_default_overridable():
    specs = _specs({
        "name": "member", "type": "string", "source": "input_identity_default",
    })
    out = resolve_param_values(specs, {"member": "bob@example.com"}, FakeIdentity())
    assert out["member"] == "bob@example.com"
    out2 = resolve_param_values(specs, None, FakeIdentity())
    assert out2["member"] == "sarah@example.com"


def test_resolve_optional_none_means_all():
    specs = _specs({"name": "status", "type": "string", "required": False})
    assert resolve_param_values(specs, None)["status"] is None


def test_resolve_required_missing_raises():
    specs = _specs({"name": "member_id", "type": "id", "required": True})
    with pytest.raises(ParamError):
        resolve_param_values(specs, None)


def test_resolve_type_validation():
    specs = _specs({"name": "n", "type": "number"})
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"n": "not-a-number"})
    assert resolve_param_values(specs, {"n": "42"})["n"] == 42


def test_resolve_strict_options():
    specs = _specs({
        "name": "r", "type": "string", "options": ["a", "b"], "strict_options": True,
    })
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"r": "c"})
    assert resolve_param_values(specs, {"r": "a"})["r"] == "a"


def test_no_specs_but_values_rejected_via_unknown():
    with pytest.raises(ParamError):
        resolve_param_values([], {"x": 1})


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_stable_and_order_independent():
    a = params_fingerprint({"x": 1, "y": "z"})
    b = params_fingerprint({"y": "z", "x": 1})
    assert a == b and len(a) == 64


def test_fingerprint_empty_is_blank():
    assert params_fingerprint({}) == ""
    assert params_fingerprint(None) == ""


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------

CODE_OK = """
def generate_df(ds_clients, excel_files, params):
    sql = "SELECT * FROM tasks WHERE assignee = :email"
    return ds_clients["a:b"].execute_query(sql, params={"email": params["email"]})
"""

CODE_FSTRING = """
def generate_df(ds_clients, excel_files, params):
    sql = f"SELECT * FROM tasks WHERE assignee = '{params['email']}'"
    return ds_clients["a:b"].execute_query(sql)
"""


def test_extract_and_accepts():
    assert code_accepts_params(CODE_OK)
    assert extract_code_param_names(CODE_OK) == {"email"}
    assert not code_accepts_params("def generate_df(ds_clients, excel_files):\n    return None")


def test_fstring_guard():
    assert check_params_not_formatted(CODE_OK) is None
    assert check_params_not_formatted(CODE_FSTRING) is not None


def test_declarations_vs_code():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    assert check_declarations_vs_code(CODE_OK, specs) == []
    extra = _specs(
        {"name": "email", "type": "string"},
        {"name": "dead", "type": "string"},
    )
    errs = check_declarations_vs_code(CODE_OK, extra)
    assert any("dead" in e for e in errs)
    errs2 = check_declarations_vs_code(
        "def generate_df(ds_clients, excel_files):\n    return None", specs
    )
    assert errs2


# ---------------------------------------------------------------------------
# Identity verification against executed SQL
# ---------------------------------------------------------------------------

def test_identity_verification_pass_and_fail():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    values = {"email": "sarah@example.com"}
    ok = verify_identity_binds_in_queries(
        ["SELECT * FROM t WHERE assignee = 'sarah@example.com'"], values, specs
    )
    assert ok is None
    bad = verify_identity_binds_in_queries(
        ["SELECT * FROM t"], values, specs
    )
    assert bad is not None


def test_parse_param_specs_skips_garbage():
    specs = parse_param_specs([
        {"name": "ok", "type": "string"},
        {"type": "missing-name"},
        "not-a-dict",
    ])
    assert [s.name for s in specs] == ["ok"]
