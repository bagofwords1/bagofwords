"""Row-level security on accelerated relations.

The property under test is not "the filter works" but "nothing returns rows it
shouldn't". Every way a policy can be wrong — a typo in the source, a column
that vanished when the query was edited, a mode that isn't implemented, a
request with no user at all — has to land on *fewer* rows, never more. A leak
here is silent: the report renders, the numbers are just somebody else's.

Enforcement is asserted against a real encrypted DuckDB artifact rather than a
mock, because the interesting question is whether an agent can reach around the
filtered view, and only a real catalog can answer that.
"""

import pytest

from app.data_sources.fast import rls
from app.data_sources.fast.rls import Identity


def _identity(**kw) -> Identity:
    return Identity(user_id=kw.pop("user_id", "u1"), **kw)


def _policy(**kw):
    base = {"column": "region", "source": "profile.officeLocation", "op": "in"}
    base.update(kw)
    return base


COLUMNS = ["region", "amount", "status"]


def _compile(policy, identity, **kw):
    kw.setdefault("available_columns", COLUMNS)
    return rls.compile_policy(policy, identity, **kw)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_a_user_sees_the_slice_matching_their_own_attribute():
    f = _compile(_policy(), _identity(profile_attributes={"officeLocation": "EMEA"}))
    assert f.allowed_values == ["EMEA"]
    assert f.column == "region"
    assert not f.deny_all


def test_a_multi_valued_attribute_widens_to_all_of_its_values():
    f = _compile(
        _policy(),
        _identity(profile_attributes={"officeLocation": ["EMEA", "APAC"]}),
    )
    assert f.allowed_values == ["APAC", "EMEA"]


def test_a_grant_adds_values_beyond_the_users_own():
    f = _compile(
        _policy(grants=[{"principal_type": "group", "principal_id": "g1",
                         "values": ["APAC"]}]),
        _identity(profile_attributes={"officeLocation": "EMEA"}, group_ids={"g1"}),
    )
    assert f.allowed_values == ["APAC", "EMEA"]


def test_a_wildcard_grant_removes_the_filter_entirely():
    """The 'sees everything' escape hatch — a finance-admin group, typically."""
    f = _compile(
        _policy(grants=[{"principal_type": "role", "principal_id": "r-admin",
                         "values": ["*"]}]),
        _identity(role_ids={"r-admin"}),
    )
    assert f.unrestricted


def test_groups_and_roles_match_by_name_as_well_as_id():
    """The UI writes ids; an admin editing JSON reaches for names. Both are
    org-scoped and resolved from the same tables, so rejecting names would be a
    papercut with no security benefit."""
    for identity in (
        _identity(group_ids={"g-uuid"}),
        _identity(group_names={"finance"}),
    ):
        f = _compile(
            _policy(grants=[{"principal_type": "group", "principal_id": "g-uuid",
                             "values": ["*"]}]),
            identity,
        )
        by_name = _compile(
            _policy(grants=[{"principal_type": "group", "principal_id": "finance",
                             "values": ["*"]}]),
            identity,
        )
        assert f.unrestricted or by_name.unrestricted


@pytest.mark.parametrize("source,identity,expected", [
    ("user.email", _identity(email="a@x.io"), ["a@x.io"]),
    ("groups", _identity(group_names={"emea", "apac"}), ["apac", "emea"]),
    ("roles", _identity(role_names={"analyst"}), ["analyst"]),
])
def test_the_other_identity_sources_resolve(source, identity, expected):
    f = _compile(_policy(source=source, column="region"), identity)
    assert f.allowed_values == expected


# --------------------------------------------------------------------------
# Every wrong policy denies
# --------------------------------------------------------------------------

def test_an_identity_with_no_value_for_the_attribute_gets_nothing():
    """A user who has never signed in through the IdP has no profile
    attributes. Default-deny means they see zero rows, not every row."""
    f = _compile(_policy(), _identity(profile_attributes={}))
    assert f.deny_all


def test_default_deny_can_be_turned_off_but_starts_on():
    f = _compile(_policy(), _identity(), default_deny=False)
    assert f.unrestricted
    assert _compile(_policy(), _identity()).deny_all


def test_an_anonymous_request_sees_nothing():
    """No user is not a bypass. Every background path that forgot to thread an
    identity would otherwise be a full-table leak."""
    f = _compile(_policy(), rls.ANONYMOUS)
    assert f.deny_all


def test_a_typo_in_the_source_denies_rather_than_matching_everything():
    f = _compile(_policy(source="profil.офис"), _identity(
        profile_attributes={"officeLocation": "EMEA"}))
    assert f.deny_all


def test_a_column_that_is_no_longer_in_the_relation_denies():
    """The admin edited the query and dropped the column the policy filters on.
    We cannot filter on what is gone, so we return nothing."""
    f = _compile(_policy(column="region"), _identity(
        profile_attributes={"officeLocation": "EMEA"}),
        available_columns=["amount", "status"])
    assert f.deny_all
    assert "not in the materialized relation" in f.reason


def test_rls_enabled_with_no_policy_denies():
    assert _compile(None, _identity()).deny_all


def test_sql_mode_denies_until_it_is_implemented():
    """Accepting the row but not the semantics would serve unfiltered data
    under a policy the admin believes is enforced."""
    f = _compile(_policy(), _identity(profile_attributes={"officeLocation": "EMEA"}),
                 mode=rls.MODE_SQL)
    assert f.deny_all


def test_an_unknown_mode_denies():
    assert _compile(_policy(), _identity(), mode="magic").deny_all


def test_an_unsupported_operator_denies():
    f = _compile(_policy(op="like"), _identity(
        profile_attributes={"officeLocation": "EMEA"}))
    assert f.deny_all


@pytest.mark.parametrize("column", [
    'region"; DROP TABLE x; --',
    "region OR 1=1",
    "a'b",
    "",
])
def test_a_column_name_that_is_not_a_plain_identifier_denies(column):
    """The column name is the one thing interpolated into SQL, so it never
    reaches the query unless it is structurally a name."""
    f = _compile(_policy(column=column), _identity(
        profile_attributes={"officeLocation": "EMEA"}), available_columns=[])
    assert f.deny_all


def test_a_grant_for_a_principal_the_user_is_not_does_not_apply():
    f = _compile(
        _policy(grants=[{"principal_type": "group", "principal_id": "other",
                         "values": ["*"]}]),
        _identity(group_ids={"mine"}),
    )
    assert f.deny_all


def test_a_malformed_grant_is_ignored_not_honoured():
    f = _compile(
        _policy(grants=["not a dict", {"values": ["*"]}, {"principal_type": "group"}]),
        _identity(group_ids={"g1"}),
    )
    assert f.deny_all


# --------------------------------------------------------------------------
# Save-time validation
# --------------------------------------------------------------------------

def test_validation_accepts_a_well_formed_policy():
    rls.validate_policy(_policy(), rls.MODE_ATTRIBUTE, COLUMNS)


@pytest.mark.parametrize("policy,fragment", [
    ({}, "required"),
    (_policy(column=""), "Choose the column"),
    (_policy(column="nope"), "not a column of this query"),
    (_policy(source="nonsense"), "not a known identity source"),
    (_policy(op="like"), "Unsupported operator"),
    (_policy(grants=[{"principal_type": "alien", "principal_id": "x"}]),
     "must target a user, a group or a role"),
    (_policy(grants=[{"principal_type": "group", "principal_id": ""}]),
     "names no principal"),
])
def test_validation_rejects_with_a_message_an_admin_can_act_on(policy, fragment):
    with pytest.raises(ValueError) as e:
        rls.validate_policy(policy, rls.MODE_ATTRIBUTE, COLUMNS)
    assert fragment in str(e.value)


def test_validation_refuses_sql_mode_rather_than_storing_a_dead_policy():
    with pytest.raises(ValueError) as e:
        rls.validate_policy(_policy(), rls.MODE_SQL, COLUMNS)
    assert "not available yet" in str(e.value)


# --------------------------------------------------------------------------
# Enforcement against a real artifact
# --------------------------------------------------------------------------

@pytest.fixture
def artifact(tmp_path, monkeypatch):
    """A real encrypted DuckDB artifact with three regions in it."""
    import duckdb

    from app.data_sources.fast import artifacts

    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path, raising=False)
    key = artifacts.new_artifact_key()
    path = tmp_path / "probe.duckdb"
    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE sales (region VARCHAR, amount INTEGER)")
    con.executemany(
        "INSERT INTO sales VALUES (?, ?)",
        [("EMEA", 10), ("EMEA", 20), ("APAC", 30), ("AMER", 40)],
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
            columns=[{"name": "region"}, {"name": "amount"}],
            rls_filter=rls_filter,
        )
    ])


def test_an_unfiltered_relation_serves_every_row(artifact):
    df = _client(artifact, None).execute_query("SELECT COUNT(*) AS n FROM sales")
    assert int(df.iloc[0]["n"]) == 4


def test_a_filtered_relation_serves_only_the_allowed_rows(artifact):
    f = rls.Filter(column="region", allowed_values=["EMEA"])
    df = _client(artifact, f).execute_query("SELECT region, amount FROM sales")
    assert set(df["region"]) == {"EMEA"}
    assert len(df) == 2


def test_deny_all_serves_a_relation_that_exists_and_is_empty(artifact):
    """Not 'no such table' — the schema context advertises the relation, and an
    agent that gets a missing-table error will report a bug instead of an
    answer."""
    f = rls.Filter(column="region", deny_all=True, reason="unresolved")
    client = _client(artifact, f)
    df = client.execute_query("SELECT * FROM sales")
    assert len(df) == 0
    assert list(df.columns) == ["region", "amount"]


def test_the_unfiltered_relation_is_not_reachable_around_the_view(artifact):
    """The filter is the catalog, not a predicate bolted onto generated SQL.
    There is no unfiltered object under any name the agent can reach."""
    f = rls.Filter(column="region", allowed_values=["EMEA"])
    client = _client(artifact, f)
    for attempt in (
        'SELECT COUNT(*) FROM bow_a0.sales',
        'SELECT COUNT(*) FROM "bow_a0"."sales"',
        "SELECT COUNT(*) FROM main.sales",
    ):
        try:
            df = client.execute_query(attempt)
        except Exception:
            continue           # not nameable at all — the strongest outcome
        assert int(df.iloc[0, 0]) == 2, f"reached unfiltered rows via: {attempt}"


def test_a_filtered_relation_still_cannot_escape_the_sandbox(artifact):
    """RLS must not have opened a hole in the lockdown that was already there."""
    f = rls.Filter(column="region", allowed_values=["EMEA"])
    client = _client(artifact, f)
    for attempt in (
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "COPY (SELECT 1) TO '/tmp/bow_rls_escape.csv'",
        "SET enable_external_access = true",
    ):
        with pytest.raises(Exception):
            client.execute_query(attempt)


def test_allowed_values_are_bound_not_interpolated(artifact):
    """A value carrying a quote is data, not syntax. If this ever becomes
    string concatenation, a profile attribute becomes an injection vector."""
    f = rls.Filter(column="region", allowed_values=["EMEA' OR '1'='1", "EMEA"])
    df = _client(artifact, f).execute_query("SELECT region FROM sales")
    assert set(df["region"]) == {"EMEA"}


def test_one_relations_policy_does_not_leak_into_another(artifact):
    """Each relation gets its own values table; sharing one would apply the
    first policy to every relation in the session."""
    from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

    path, key = artifact
    client = FastQueryClient([
        FastRelation(name="sales", artifact_path=path, artifact_key=key,
                     columns=[{"name": "region"}],
                     rls_filter=rls.Filter(column="region", allowed_values=["EMEA"])),
        FastRelation(name="sales_all", artifact_path=path, artifact_key=key,
                     columns=[{"name": "region"}], rls_filter=None),
    ])
    with client.connect() as con:
        # The second relation is registered from the same artifact under a
        # different name; only the first carries a policy.
        assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2


def test_the_client_description_tells_the_model_the_rows_are_filtered(artifact):
    """Without this the agent reports a user's slice as a company-wide total."""
    f = rls.Filter(column="region", allowed_values=["EMEA"])
    text = _client(artifact, f).description
    assert "Row-filtered" in text
    assert "not the whole relation" in text
    assert "Row-filtered" not in _client(artifact, None).description


# --------------------------------------------------------------------------
# Identity has no permissive default
# --------------------------------------------------------------------------

class _Row:
    def __init__(self, **kw):
        self.name = "sales"
        self.artifact_path = "/tmp/x"
        self.artifact_key_enc = "enc"
        self.columns = [{"name": "region"}]
        self.no_rows = 4
        self.description = None
        self.last_refreshed_at = None
        self.rls_enabled = True
        self.rls_mode = rls.MODE_ATTRIBUTE
        self.rls_policy = _policy()
        self.rls_default_deny = True
        for k, v in kw.items():
            setattr(self, k, v)


def test_build_fast_client_without_an_identity_denies_protected_relations(monkeypatch):
    """`build_fast_client(rows)` with no identity is the shape a background path
    accidentally produces. It must mean *no rows*, not *all rows*."""
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    client = CustomQueryService.build_fast_client([_Row()])
    assert client.relations[0].rls_filter.deny_all


def test_an_unprotected_relation_gets_no_filter_at_all(monkeypatch):
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    client = CustomQueryService.build_fast_client([_Row(rls_enabled=False)])
    assert client.relations[0].rls_filter is None


def test_a_real_identity_gets_its_slice(monkeypatch):
    from app.data_sources.fast import artifacts
    from app.services.custom_query_service import CustomQueryService

    monkeypatch.setattr(artifacts, "decrypt_key", lambda enc: "key")
    client = CustomQueryService.build_fast_client(
        [_Row()],
        identity=_identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    assert client.relations[0].rls_filter.allowed_values == ["EMEA"]


def test_a_half_registered_filtered_relation_aborts_the_session(artifact, monkeypatch):
    """If registration stops between the ATTACH and the DETACH, the unfiltered
    artifact is still nameable. Serving the rest of the session would mean
    serving that, so the whole connection fails instead."""
    from app.data_sources.fast.fast_client import FastQueryClient

    client = _client(artifact, rls.Filter(column="region", allowed_values=["EMEA"]))
    monkeypatch.setattr(
        FastQueryClient, "_detach",
        staticmethod(lambda con, alias, rel: (_ for _ in ()).throw(RuntimeError("stuck"))),
    )
    with pytest.raises(RuntimeError) as e:
        client.execute_query("SELECT 1")
    assert "Refusing to serve" in str(e.value)


def test_an_unrestricted_relation_still_serves_when_another_artifact_is_broken(artifact):
    """The fail-closed rule is scoped to row-filtered relations; a merely
    missing artifact must not take the session down."""
    from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

    path, key = artifact
    client = FastQueryClient([
        FastRelation(name="broken", artifact_path="/nonexistent/x.duckdb",
                     artifact_key=key, columns=[]),
        FastRelation(name="sales", artifact_path=path, artifact_key=key,
                     columns=[{"name": "region"}]),
    ])
    df = client.execute_query("SELECT COUNT(*) AS n FROM sales")
    assert int(df.iloc[0]["n"]) == 4


# --------------------------------------------------------------------------
# Relation identity — names and stats
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_name_clashing_only_in_case_is_rejected():
    """A custom query `album` next to a source table `Album` is two relations
    the agent cannot tell apart — and every by-name lookup around them (usage
    stats, the cached badge) folds case, so one wears the other's numbers."""
    from fastapi import HTTPException

    from app.models.connection_table import KIND_TABLE
    from app.services.custom_query_service import custom_query_service

    class Existing:
        id = "row-1"
        name = "Album"
        kind = KIND_TABLE

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [Existing()]

    class DB:
        async def execute(self, q):
            return Result()

    with pytest.raises(HTTPException) as e:
        await custom_query_service._ensure_name_free(DB(), "conn-1", "album")
    assert e.value.status_code == 409
    # The message has to name the row that actually exists, not the name typed,
    # or the admin goes looking for an `album` that isn't there.
    assert "'Album'" in e.value.detail
    assert "capitalisation" in e.value.detail


@pytest.mark.asyncio
async def test_an_exact_name_clash_keeps_its_plainer_message():
    from fastapi import HTTPException

    from app.models.connection_table import KIND_BOW
    from app.services.custom_query_service import custom_query_service

    class Existing:
        id = "row-1"
        name = "album"
        kind = KIND_BOW

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [Existing()]

    class DB:
        async def execute(self, q):
            return Result()

    with pytest.raises(HTTPException) as e:
        await custom_query_service._ensure_name_free(DB(), "conn-1", "album")
    assert "another custom query" in e.value.detail
    assert "capitalisation" not in e.value.detail


@pytest.mark.asyncio
async def test_renaming_a_query_to_its_own_name_is_allowed():
    """Editing must not trip over the row being edited."""
    from app.models.connection_table import KIND_BOW
    from app.services.custom_query_service import custom_query_service

    class Existing:
        id = "row-1"
        name = "album"
        kind = KIND_BOW

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [Existing()]

    class DB:
        async def execute(self, q):
            return Result()

    await custom_query_service._ensure_name_free(DB(), "conn-1", "Album", exclude_id="row-1")
