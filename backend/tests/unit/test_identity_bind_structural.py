"""Identity params must be applied as a real row filter, not merely mentioned.

Contract: `verify_identity_binds_in_queries` returns None only when every
executed query restricts its result rows to the viewer's identity value;
otherwise an error. Text that merely contains the value after WHERE
(tautologies, OR-widening, negations, unscoped subqueries) is refused for SQL,
Salesforce SOQL and Splunk SPL. Shapes the structural check can't parse fall
back to the legacy text heuristic unchanged (and log a warning).
"""

import logging

import pytest

from app.ai.code_execution.identity_bind import CapturedQuery
from app.ai.code_execution.query_params import verify_identity_binds_in_queries
from app.schemas.param_schema import ParamSpec

SPEC = [ParamSpec.model_validate({"name": "email", "type": "string", "source": "identity"})]


def _check(queries, value, client=None, specs=SPEC, name="email"):
    caps = [CapturedQuery(q.format(v=value), client) if client else q.format(v=value) for q in queries]
    return verify_identity_binds_in_queries(caps, {name: value}, specs)


EMAILS = ["sarah@example.com", "ops.lead@corp.io"]
SQL_CLIENTS = [None, "PostgresqlClient", "SqliteClient", "MysqlClient", "SnowflakeClient"]

SQL_BYPASSES = [
    "SELECT * FROM orders WHERE '{v}' = '{v}'",
    "SELECT * FROM orders WHERE region = 'EU' OR owner = '{v}'",
    "SELECT * FROM orders WHERE note <> '{v}'",
    "SELECT * FROM orders WHERE note != '{v}'",
    "SELECT * FROM orders WHERE NOT (owner = '{v}')",
    "SELECT * FROM orders WHERE owner NOT IN ('{v}')",
    "SELECT * FROM orders WHERE owner LIKE '%' AND note NOT LIKE '{v}'",
    "SELECT * FROM orders WHERE owner IN ('{v}', 'someone.else@corp.io')",
    "SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM users WHERE email = '{v}')",
    "SELECT * FROM orders WHERE 1=1 AND (owner = '{v}' OR 1=1)",
    # value only constrains a subquery / CTE the outer query doesn't read from
    "WITH me AS (SELECT * FROM users WHERE email = '{v}') SELECT * FROM orders WHERE 1=1",
    "SELECT * FROM orders WHERE id > (SELECT count(*) FROM users WHERE email = '{v}')",
    # every UNION branch must be scoped
    "SELECT * FROM orders WHERE owner = '{v}' UNION ALL SELECT * FROM orders",
    # LEFT JOIN ON doesn't drop driving rows
    "SELECT * FROM orders o LEFT JOIN users u ON u.email = '{v}' AND u.id = o.uid",
    # a scoped source on the outer side of a RIGHT join doesn't scope the result
    "SELECT * FROM (SELECT * FROM users WHERE email = '{v}') u RIGHT JOIN orders o ON o.uid = u.id",
]

SQL_LEGIT = [
    "SELECT * FROM orders WHERE owner = '{v}'",
    "SELECT * FROM orders WHERE '{v}' = owner",
    "SELECT * FROM orders o WHERE o.owner = '{v}' AND o.status = 'open'",
    "SELECT * FROM orders WHERE owner IN ('{v}')",
    "SELECT * FROM orders WHERE LOWER(owner) = LOWER('{v}')",
    "SELECT * FROM orders WHERE CAST(owner AS VARCHAR) = '{v}' AND (a = 1 OR b = 2)",
    "SELECT o.* FROM orders AS o JOIN users AS u ON u.id = o.uid WHERE u.email = '{v}'",
    "SELECT * FROM orders o JOIN users u ON u.email = '{v}' AND u.id = o.uid",
    "SELECT region, count(*) FROM orders GROUP BY region, owner HAVING owner = '{v}'",
    "WITH mine AS (SELECT * FROM orders WHERE owner = '{v}') SELECT * FROM mine WHERE 1=1",
    "WITH mine AS (SELECT * FROM orders WHERE owner = '{v}') SELECT m.*, r.name FROM mine m LEFT JOIN regions r ON r.id = m.rid",
    "SELECT * FROM (SELECT * FROM orders WHERE owner = '{v}') t",
    "SELECT * FROM orders WHERE owner_id IN (SELECT id FROM users WHERE email = '{v}')",
    "SELECT * FROM orders WHERE owner_id = (SELECT id FROM users WHERE email = '{v}')",
    "SELECT * FROM orders WHERE owner = '{v}' UNION SELECT * FROM archive WHERE owner = '{v}'",
    "SELECT * FROM orders -- scoped below\nWHERE owner = '{v}'",
]


@pytest.mark.parametrize("client", SQL_CLIENTS)
@pytest.mark.parametrize("value", EMAILS)
@pytest.mark.parametrize("query", SQL_BYPASSES)
def test_sql_widening_predicates_are_refused(query, value, client):
    assert _check([query], value, client) is not None


@pytest.mark.parametrize("client", SQL_CLIENTS)
@pytest.mark.parametrize("value", EMAILS)
@pytest.mark.parametrize("query", SQL_LEGIT)
def test_sql_scoped_reads_are_accepted(query, value, client):
    assert _check([query], value, client) is None


def test_quoted_value_with_apostrophe_is_matched():
    # rendered SQL escapes the quote; the identity value itself does not carry it
    sql = "SELECT * FROM orders WHERE owner = 'o''neil@corp.io'"
    assert verify_identity_binds_in_queries(
        [CapturedQuery(sql, "PostgresqlClient")], {"email": "o'neil@corp.io"}, SPEC) is None
    taut = "SELECT * FROM orders WHERE 'o''neil@corp.io' = 'o''neil@corp.io'"
    assert verify_identity_binds_in_queries(
        [CapturedQuery(taut, "PostgresqlClient")], {"email": "o'neil@corp.io"}, SPEC) is not None


def test_no_table_read_passes_and_every_query_must_be_scoped():
    v = "sarah@example.com"
    assert _check(["SELECT 1", "SELECT * FROM t WHERE owner = '{v}'"], v, "PostgresqlClient") is None
    assert _check(["SELECT * FROM t WHERE owner = '{v}'", "SELECT * FROM t WHERE '{v}'='{v}'"],
                  v, "PostgresqlClient") is not None


def test_multiple_identity_params_each_must_bind():
    specs = [ParamSpec.model_validate(s) for s in (
        {"name": "email", "type": "string", "source": "identity"},
        {"name": "dept", "type": "string", "source": "identity"},
    )]
    vals = {"email": "sarah@example.com", "dept": "Sales"}
    ok = CapturedQuery("SELECT * FROM t WHERE owner = 'sarah@example.com' AND dept = 'Sales'", "PostgresqlClient")
    bad = CapturedQuery("SELECT * FROM t WHERE owner = 'sarah@example.com' OR dept = 'Sales'", "PostgresqlClient")
    assert verify_identity_binds_in_queries([ok], vals, specs) is None
    assert verify_identity_binds_in_queries([bad], vals, specs) is not None


def test_list_identity_value_accepts_in_over_own_values_only():
    specs = [ParamSpec.model_validate({"name": "groups", "type": "string", "source": "identity"})]
    vals = {"groups": ["emea", "managers"]}
    ok = CapturedQuery("SELECT * FROM t WHERE grp IN ('emea', 'managers')", "PostgresqlClient")
    widened = CapturedQuery("SELECT * FROM t WHERE grp IN ('emea', 'apac')", "PostgresqlClient")
    assert verify_identity_binds_in_queries([ok], vals, specs) is None
    assert verify_identity_binds_in_queries([widened], vals, specs) is not None


# ------------------------------------------------------------------ SOQL

@pytest.mark.parametrize("query,ok", [
    ("SELECT Id, Name FROM Opportunity WHERE Owner.Email = '{v}'", True),
    ("SELECT Id FROM Opportunity WHERE Owner.Email = '{v}' AND StageName = 'Closed Won'", True),
    ("SELECT Id FROM Opportunity WHERE StageName = 'Closed Won' OR Owner.Email = '{v}'", False),
    ("SELECT Id FROM Opportunity WHERE '{v}' = '{v}'", False),
    ("SELECT Id FROM Opportunity WHERE Owner.Email != '{v}'", False),
])
def test_soql_uses_structural_rule(query, ok):
    res = _check([query], "sarah@example.com", "SalesforceClient")
    assert (res is None) is ok


# ------------------------------------------------------------------- SPL

@pytest.mark.parametrize("query,ok", [
    ('search index=crm owner="{v}" | stats count by status', True),
    ("index=crm owner={v} status=open", True),
    ('index=crm (owner="{v}" status=open) | table _time owner', True),
    ('{{"spl": "search index=crm owner=\\"{v}\\" | head 10", "earliest": "-24h"}}', True),
    ('index=crm sourcetype=deals | search owner="{v}" | stats count', True),
    ('index=crm | search owner="{v}" OR status=open', False),
    ('index=crm owner="{v}" OR status=open', False),
    ('index=crm (owner="{v}" OR status=open)', False),
    ('index=crm NOT owner="{v}"', False),
    ('index=crm owner!="{v}"', False),
    ('index=crm "{v}"', False),
])
def test_spl_base_search_must_bind_field(query, ok):
    res = _check([query], "sarah@example.com", "SplunkClient")
    assert (res is None) is ok


# -------------------------------------------------------------- fallback

def _legacy(capture, needles):
    """The pre-structural heuristic, verbatim, as the fallback oracle."""
    from app.ai.code_execution.query_params import _heuristic_binds_value
    return _heuristic_binds_value(capture, needles)


@pytest.mark.parametrize("client,query", [
    # sqlglot can't parse these -> legacy heuristic decides
    ("PostgresqlClient", "SELECT * FROM t WHERE owner = '{v}' AND ((("),
    ("SalesforceClient", "SELECT Id FROM Account WHERE Owner.Email = '{v}' AND CreatedDate = LAST_N_DAYS:30"),
    ("SalesforceClient", "SELECT FIELDS(ALL) FROM Account WHERE CreatedDate = LAST_N_DAYS:30"),
    # SPL shapes we don't model
    ("SplunkClient", 'index=crm owner="{v}" | append [search index=other]'),
    ("SplunkClient", '| inputlookup users.csv | search owner="{v}"'),
    ("SplunkClient", 'index=crm | where owner="{v}"'),
])
def test_unparseable_falls_back_to_legacy_and_warns(client, query, caplog):
    v = "sarah@example.com"
    text = query.format(v=v)
    with caplog.at_level(logging.WARNING, logger="app.ai.code_execution.identity_bind"):
        res = verify_identity_binds_in_queries([CapturedQuery(text, client)], {"email": v}, SPEC)
    assert (res is None) is _legacy(text, [v])
    assert any(getattr(r, "event", None) == "identity_bind_check_fallback" for r in caplog.records)


@pytest.mark.parametrize("client,capture,ok", [
    ("MongoClient", '{{"collection": "orders", "filter": {{"owner": "{v}"}}}}', True),
    ("MongoClient", '{{"collection": "orders", "filter": {{}}}}', False),
    ("QlikClient", 'execute_query(app="x", filters={{"owner": "{v}"}})', True),
])
def test_non_sql_connectors_keep_containment_semantics(client, capture, ok):
    res = _check([capture], "sarah@example.com", client)
    assert (res is None) is ok


def test_verify_has_no_dead_code_after_final_return():
    import ast
    import inspect
    import textwrap
    from app.ai.code_execution import query_params

    fn = ast.parse(textwrap.dedent(inspect.getsource(query_params.verify_identity_binds_in_queries))).body[0]
    returns = [i for i, st in enumerate(fn.body) if isinstance(st, ast.Return)]
    assert returns and returns[0] == len(fn.body) - 1  # nothing unreachable after it
