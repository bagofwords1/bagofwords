"""Column-level security on accelerated relations.

The mirror of row-level security, one dimension over. The property under test is
not "the projection works" but "no column a user may not see ever reaches them"
— not in the rows, and not in the schema the agent is handed (or it writes SQL
against a column that isn't there). Every way a column policy can be wrong — a
restricted column that vanished, a grant for someone else, every column
restricted — has to land on *fewer* columns, never more.

Enforcement is asserted against a real encrypted DuckDB artifact, because the
interesting question is whether an agent can reach around the projection, and
only a real catalog can answer that.
"""

import pytest

from app.data_sources.fast import rls
from app.data_sources.fast.rls import Identity


def _identity(**kw) -> Identity:
    return Identity(user_id=kw.pop("user_id", "u1"), **kw)


COLUMNS = ["region", "amount", "status"]


def _policy(**kw):
    base = {"restricted_columns": ["amount"]}
    base.update(kw)
    return base


def _compile(policy, identity, **kw):
    kw.setdefault("available_columns", COLUMNS)
    return rls.compile_column_policy(policy, identity, **kw)


# --------------------------------------------------------------------------
# What each identity may see
# --------------------------------------------------------------------------

def test_nothing_restricted_means_every_column():
    assert _compile({"restricted_columns": []}, _identity()) is None
    assert _compile(None, _identity()) is None


def test_a_restricted_column_is_hidden_from_someone_with_no_grant():
    cols = _compile(_policy(), _identity())
    assert cols == ["region", "status"]
    assert "amount" not in cols


def test_a_wildcard_grant_unlocks_every_restricted_column():
    cols = _compile(
        _policy(grants=[{"principal_type": "role", "principal_id": "r-fin",
                         "columns": ["*"]}]),
        _identity(role_ids={"r-fin"}),
    )
    assert cols == COLUMNS


def test_a_grant_unlocks_only_the_columns_it_names():
    cols = _compile(
        _policy(restricted_columns=["amount", "status"],
                grants=[{"principal_type": "group", "principal_id": "g1",
                         "columns": ["amount"]}]),
        _identity(group_ids={"g1"}),
    )
    assert cols == ["region", "amount"]
    assert "status" not in cols


def test_the_projection_keeps_the_relations_column_order():
    """A stable projection order matters: the agent's `SELECT *` and the schema
    it was given must line up."""
    cols = _compile(_policy(restricted_columns=["region"]), _identity())
    assert cols == ["amount", "status"]


def test_groups_and_roles_match_by_name_as_well_as_id():
    for identity in (_identity(group_ids={"g-uuid"}), _identity(group_names={"finance"})):
        by_id = _compile(
            _policy(grants=[{"principal_type": "group", "principal_id": "g-uuid",
                             "columns": ["*"]}]), identity)
        by_name = _compile(
            _policy(grants=[{"principal_type": "group", "principal_id": "finance",
                             "columns": ["*"]}]), identity)
        assert by_id == COLUMNS or by_name == COLUMNS


# --------------------------------------------------------------------------
# Every wrong policy hides more, never less
# --------------------------------------------------------------------------

def test_a_restricted_column_not_in_the_relation_is_dropped_not_fatal():
    """The admin edited the query and the restricted column is gone. It cannot
    leak, so restricting it is a no-op — not a denial of the whole table."""
    assert _compile(_policy(restricted_columns=["ghost"]), _identity()) is None


def test_a_grant_naming_a_non_restricted_column_is_ignored():
    """Granting a column that was never restricted cannot *add* visibility —
    everyone already sees it — and must not widen the restricted set."""
    cols = _compile(
        _policy(grants=[{"principal_type": "group", "principal_id": "g1",
                         "columns": ["region"]}]),
        _identity(group_ids={"g1"}),
    )
    assert cols == ["region", "status"]  # amount still hidden


def test_a_grant_for_a_principal_the_user_is_not_does_not_apply():
    cols = _compile(
        _policy(grants=[{"principal_type": "group", "principal_id": "other",
                         "columns": ["*"]}]),
        _identity(group_ids={"mine"}),
    )
    assert "amount" not in cols


def test_a_malformed_grant_is_ignored_not_honoured():
    cols = _compile(
        _policy(grants=["nope", {"columns": ["*"]}, {"principal_type": "group"}]),
        _identity(group_ids={"g1"}),
    )
    assert "amount" not in cols


def test_an_anonymous_request_sees_no_restricted_columns():
    cols = _compile(_policy(), rls.ANONYMOUS)
    assert "amount" not in cols


def test_every_column_restricted_and_none_granted_returns_empty():
    """The caller maps an empty projection to a relation-level deny — a SELECT
    with no columns is not a query."""
    cols = _compile(_policy(restricted_columns=COLUMNS), _identity())
    assert cols == []


# --------------------------------------------------------------------------
# Save-time validation
# --------------------------------------------------------------------------

def test_validation_accepts_a_well_formed_policy():
    rls.validate_column_policy(
        _policy(grants=[{"principal_type": "role", "principal_id": "r1",
                         "columns": ["*"]}]),
        COLUMNS,
    )


@pytest.mark.parametrize("policy,fragment", [
    ({}, "required"),
    ({"restricted_columns": []}, "at least one column"),
    ({"restricted_columns": ["nope"]}, "not a column of this query"),
    ({"restricted_columns": ['a"; DROP TABLE x']}, "not a valid column name"),
    (_policy(grants=[{"principal_type": "alien", "principal_id": "x",
                      "columns": ["*"]}]), "must target a user, a group or a role"),
    (_policy(grants=[{"principal_type": "group", "principal_id": "",
                      "columns": ["*"]}]), "names no principal"),
    (_policy(grants=[{"principal_type": "group", "principal_id": "g1"}]),
     "names no columns"),
])
def test_validation_rejects_with_a_message_an_admin_can_act_on(policy, fragment):
    with pytest.raises(ValueError) as e:
        rls.validate_column_policy(policy, COLUMNS)
    assert fragment in str(e.value)


# --------------------------------------------------------------------------
# Enforcement against a real artifact
# --------------------------------------------------------------------------

@pytest.fixture
def artifact(tmp_path, monkeypatch):
    """A real encrypted DuckDB artifact: region, amount, status."""
    from app.data_sources.fast import artifacts

    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path, raising=False)
    key = artifacts.new_artifact_key()
    path = tmp_path / "probe.duckdb"
    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE sales (region VARCHAR, amount INTEGER, status VARCHAR)")
    con.executemany(
        "INSERT INTO sales VALUES (?, ?, ?)",
        [("EMEA", 10, "open"), ("EMEA", 20, "won"),
         ("APAC", 30, "open"), ("AMER", 40, "lost")],
    )
    con.close()
    return str(path), key


def _client(artifact, rls_filter):
    from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

    path, key = artifact
    return FastQueryClient([
        FastRelation(
            name="sales",
            artifact_path=path,
            artifact_key=key,
            columns=[{"name": "region"}, {"name": "amount"}, {"name": "status"}],
            rls_filter=rls_filter,
        )
    ])


def test_a_projected_relation_omits_the_restricted_column(artifact):
    f = rls.Filter(allowed_columns=["region", "status"])
    df = _client(artifact, f).execute_query("SELECT * FROM sales")
    assert "amount" not in df.columns
    assert set(df.columns) == {"region", "status"}
    assert len(df) == 4  # columns hidden, rows untouched


def test_the_restricted_column_is_not_reachable_around_the_projection(artifact):
    """The projection is the catalog copy, not a predicate on generated SQL.
    There is no object under any name that still carries the hidden column."""
    f = rls.Filter(allowed_columns=["region", "status"])
    client = _client(artifact, f)
    for attempt in (
        "SELECT amount FROM sales",
        "SELECT amount FROM bow_a0.sales",
        'SELECT "amount" FROM main.sales',
    ):
        with pytest.raises(Exception):
            client.execute_query(attempt)


def test_rows_and_columns_restrict_together(artifact):
    """A relation can filter rows AND project columns in one pass."""
    f = rls.Filter(column="region", allowed_values=["EMEA"],
                   allowed_columns=["region", "status"])
    df = _client(artifact, f).execute_query("SELECT * FROM sales")
    assert set(df.columns) == {"region", "status"}
    assert set(df["region"]) == {"EMEA"}
    assert len(df) == 2


def test_denying_every_column_serves_an_empty_relation_with_its_shape(artifact):
    """All columns restricted and none granted → deny_all, full shape, no rows.
    Not 'no such table'."""
    f = rls.Filter(deny_all=True, reason="no columns visible to this user")
    df = _client(artifact, f).execute_query("SELECT * FROM sales")
    assert len(df) == 0
    assert set(df.columns) == {"region", "amount", "status"}


def test_get_tables_hides_restricted_columns_from_the_schema(artifact):
    """The agent must not be *told* a column exists, or it writes SQL for it."""
    f = rls.Filter(allowed_columns=["region", "status"])
    tables = _client(artifact, f).get_tables()
    names = {c.name for c in tables[0].columns}
    assert names == {"region", "status"}


def test_the_description_tells_the_model_columns_are_restricted(artifact):
    f = rls.Filter(allowed_columns=["region", "status"])
    text = _client(artifact, f).description
    assert "Column-restricted" in text
    assert "amount" not in text  # the hidden column is not advertised either
    assert "Column-restricted" not in _client(artifact, None).description


# --------------------------------------------------------------------------
# Compilation through the service — row and column halves compose
# --------------------------------------------------------------------------

class _Row:
    def __init__(self, **kw):
        self.name = "sales"
        self.artifact_path = "/tmp/x"
        self.artifact_key_enc = "enc"
        self.columns = [{"name": "region"}, {"name": "amount"}, {"name": "status"}]
        self.no_rows = 4
        self.description = None
        self.last_refreshed_at = None
        self.rls_enabled = False
        self.rls_mode = rls.MODE_ATTRIBUTE
        self.rls_policy = None
        self.rls_default_deny = True
        self.cls_enabled = True
        self.cls_policy = {"restricted_columns": ["amount"]}
        for k, v in kw.items():
            setattr(self, k, v)


def test_column_only_policy_yields_a_filter_with_allowed_columns(monkeypatch):
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    client = CustomQueryService.build_fast_client([_Row()], identity=_identity())
    f = client.relations[0].rls_filter
    assert f is not None
    assert f.allowed_columns == ["region", "status"]
    assert f.allowed_values is None  # rows untouched


def test_neither_policy_enabled_gives_no_filter(monkeypatch):
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    client = CustomQueryService.build_fast_client(
        [_Row(cls_enabled=False, rls_enabled=False)], identity=_identity())
    assert client.relations[0].rls_filter is None


def test_a_denied_row_policy_short_circuits_before_columns(monkeypatch):
    """No rows means no columns matter — deny_all stands, and the projection is
    not computed on top of it."""
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    row = _Row(
        rls_enabled=True,
        rls_policy={"column": "region", "source": "profile.officeLocation", "op": "in"},
    )
    client = CustomQueryService.build_fast_client([row], identity=_identity())
    f = client.relations[0].rls_filter
    assert f.deny_all


def test_every_column_restricted_compiles_to_deny_all(monkeypatch):
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    row = _Row(cls_policy={"restricted_columns": ["region", "amount", "status"]})
    client = CustomQueryService.build_fast_client([row], identity=_identity())
    assert client.relations[0].rls_filter.deny_all
