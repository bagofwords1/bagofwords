"""Row- and column-level security on a report query's results.

The property under test is not "the filter works" but "nothing returns rows or
columns it shouldn't". Every way a policy can be wrong — a column the query
stopped returning, a typo in the identity source, a request with no user, a
result that isn't a table — has to land on *less* data, never more. A leak here
is silent: the dashboard renders, the numbers are just somebody else's.

Enforcement is asserted through `QueryCapturingClientWrapper`, the real wrapper
every execution path builds, rather than by calling the filter directly. The
interesting question is whether generated code can reach an unfiltered frame,
and only the real call path can answer that.
"""

import pandas as pd
import pytest

from app.data_sources.fast import rls
from app.data_sources.fast.rls import Identity
from app.services import query_access_policy as qap
from app.services.query_access_policy import (
    ColumnMask,
    QueryAccessPolicy,
    compile_column_policy,
    compile_for_query,
    validate_column_policy,
)


def _identity(**kw) -> Identity:
    return Identity(user_id=kw.pop("user_id", "u1"), **kw)


def _frame():
    return pd.DataFrame(
        {
            "region": ["EMEA", "EMEA", "APAC", "AMER"],
            "amount": [10, 20, 30, 40],
            "salary": [100, 200, 300, 400],
        }
    )


class _Query:
    """Stand-in for the Query row, with policy columns only."""

    def __init__(self, **kw):
        self.id = "q1"
        self.rls_enabled = False
        self.rls_mode = rls.MODE_ATTRIBUTE
        self.rls_policy = None
        self.rls_default_deny = True
        self.cls_enabled = False
        self.cls_policy = None
        for k, v in kw.items():
            setattr(self, k, v)


def _row_policy(**kw):
    base = {"column": "region", "source": "profile.officeLocation", "op": "in"}
    base.update(kw)
    return base


def _col_policy(name="salary", grants=None):
    return {"columns": [{"name": name, "grants": grants or []}]}


# --------------------------------------------------------------------------
# Enforcement runs through the real client wrapper
# --------------------------------------------------------------------------

def _wrapped(policy, frame=None):
    """A client wrapped exactly as every execution path wraps it."""
    from app.ai.code_execution.code_execution import wrap_clients_for_capture

    df = _frame() if frame is None else frame

    class FakeClient:
        def execute_query(self, sql, *a, **k):
            return df.copy()

        def query(self, sql, *a, **k):
            # The real base client aliases query -> execute_query. Returning
            # the raw frame here is deliberate: if the wrapper ever delegates
            # .query() straight through instead of routing it back through its
            # own execute_query, this unfiltered frame is what leaks.
            return df.copy()

    return wrap_clients_for_capture(
        {"c": FakeClient()}, [], [], access_policy=policy
    )["c"]


def test_a_viewer_sees_only_the_rows_matching_their_own_attribute():
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM sales")
    assert set(df["region"]) == {"EMEA"}
    assert len(df) == 2


def test_the_query_alias_is_filtered_too():
    """Model-generated code reaches for `.query(...)` constantly. If that alias
    delegated to the raw client the policy would be a no-op for most real
    code — the single highest-value hole in this design."""
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    df = _wrapped(policy).query("SELECT * FROM sales")
    assert set(df["region"]) == {"EMEA"}


def test_every_execute_query_call_is_filtered_independently():
    """One step routinely fetches several frames. A policy applied once, or
    applied to the last call only, would serve the others unfiltered."""
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    client = _wrapped(policy)
    for _ in range(3):
        assert set(client.execute_query("SELECT * FROM sales")["region"]) == {"EMEA"}


def test_a_restricted_column_is_masked_but_still_present():
    """Dropping the column would raise KeyError inside generated chart code
    keyed on it; nulling degrades to a blank series instead of a crash."""
    policy = compile_for_query(
        _Query(cls_enabled=True, cls_policy=_col_policy("salary")),
        _identity(),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM staff")
    assert "salary" in df.columns
    assert df["salary"].isna().all()
    # Everything else is untouched.
    assert list(df["amount"]) == [10, 20, 30, 40]


def test_a_grant_reveals_the_restricted_column():
    policy = compile_for_query(
        _Query(
            cls_enabled=True,
            cls_policy=_col_policy(
                "salary",
                [{"principal_type": "role", "principal_id": "finance", "values": ["*"]}],
            ),
        ),
        _identity(role_names={"finance"}),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM staff")
    assert list(df["salary"]) == [100, 200, 300, 400]


def test_rows_are_filtered_before_columns_are_masked():
    """The row filter binds to a column that may itself be restricted. Masking
    first would leave nothing to filter on."""
    policy = compile_for_query(
        _Query(
            rls_enabled=True,
            rls_policy=_row_policy(),
            cls_enabled=True,
            cls_policy=_col_policy("region"),
        ),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM sales")
    assert len(df) == 2                     # the row filter still worked...
    assert df["region"].isna().all()        # ...and the column is still masked


def test_unrestricted_columns_and_rows_pass_through_untouched():
    policy = compile_for_query(_Query(), _identity())
    assert policy is None


# --------------------------------------------------------------------------
# Every wrong policy denies
# --------------------------------------------------------------------------

def test_an_identity_with_no_value_for_the_attribute_gets_no_rows():
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()),
        _identity(profile_attributes={}),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM sales")
    assert len(df) == 0
    # The relation still has its shape — an empty widget, not a broken one.
    assert list(df.columns) == ["region", "amount", "salary"]


def test_an_anonymous_request_gets_no_rows():
    """No user is not a bypass. Every background path that forgot to thread an
    identity would otherwise be a full-result leak."""
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()), rls.ANONYMOUS
    )
    assert len(_wrapped(policy).execute_query("SELECT 1")) == 0


def test_a_column_the_query_no_longer_returns_denies():
    """The creator edited the query and dropped the column the policy filters
    on. We cannot filter on what is gone, so nobody sees anything."""
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy(column="territory")),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    assert len(_wrapped(policy).execute_query("SELECT * FROM sales")) == 0


def test_a_typo_in_the_identity_source_denies():
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy(source="profil.офис")),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    assert len(_wrapped(policy).execute_query("SELECT * FROM sales")) == 0


def test_rls_enabled_with_no_policy_denies():
    policy = compile_for_query(_Query(rls_enabled=True, rls_policy=None), _identity())
    assert len(_wrapped(policy).execute_query("SELECT * FROM sales")) == 0


def test_sql_mode_denies_until_it_is_implemented():
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy(), rls_mode=rls.MODE_SQL),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    assert len(_wrapped(policy).execute_query("SELECT * FROM sales")) == 0


def test_a_grant_for_a_principal_the_viewer_is_not_does_not_reveal_the_column():
    policy = compile_for_query(
        _Query(
            cls_enabled=True,
            cls_policy=_col_policy(
                "salary",
                [{"principal_type": "group", "principal_id": "other", "values": ["*"]}],
            ),
        ),
        _identity(group_ids={"mine"}),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM staff")
    assert df["salary"].isna().all()


def test_a_malformed_grant_is_ignored_not_honoured():
    policy = compile_for_query(
        _Query(
            cls_enabled=True,
            cls_policy={
                "columns": [
                    {"name": "salary", "grants": ["nope", {"values": ["*"]}]}
                ]
            },
        ),
        _identity(group_ids={"g1"}),
    )
    df = _wrapped(policy).execute_query("SELECT * FROM staff")
    assert df["salary"].isna().all()


def test_a_non_tabular_result_under_a_policy_refuses_rather_than_passing_through():
    """We could not filter it, so we must not serve it."""
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    client = _wrapped(policy, frame=_frame())

    class Weird:
        def execute_query(self, sql, *a, **k):
            return {"not": "a frame"}

    from app.ai.code_execution.code_execution import wrap_clients_for_capture

    wrapped = wrap_clients_for_capture({"c": Weird()}, [], [], access_policy=policy)["c"]
    with pytest.raises(RuntimeError) as e:
        wrapped.execute_query("SELECT 1")
    assert "Refusing to serve" in str(e.value)


def test_values_are_compared_as_strings_so_a_numeric_column_still_matches():
    """Identity values are always strings (an IdP claim, a group name). A
    numeric column would never match under a strict-typed comparison."""
    frame = pd.DataFrame({"dept_id": [1, 2, 3], "amount": [10, 20, 30]})
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy(column="dept_id",
                                                        source="profile.deptId")),
        _identity(profile_attributes={"deptId": 2}),
    )
    df = _wrapped(policy, frame=frame).execute_query("SELECT * FROM d")
    assert list(df["dept_id"]) == [2]


def test_null_cells_fall_out_of_a_filtered_column():
    """A NULL region belongs to nobody's slice. Fail closed."""
    frame = pd.DataFrame({"region": ["EMEA", None], "amount": [1, 2]})
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy()),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    df = _wrapped(policy, frame=frame).execute_query("SELECT * FROM sales")
    assert len(df) == 1


def test_a_case_differing_column_name_still_matches():
    """Oracle and Snowflake upper-case unquoted identifiers. Failing to match
    would silently un-restrict the column."""
    frame = pd.DataFrame({"REGION": ["EMEA", "APAC"], "amount": [1, 2]})
    policy = compile_for_query(
        _Query(rls_enabled=True, rls_policy=_row_policy(column="region")),
        _identity(profile_attributes={"officeLocation": "EMEA"}),
    )
    df = _wrapped(policy, frame=frame).execute_query("SELECT * FROM sales")
    assert len(df) == 1


# --------------------------------------------------------------------------
# The column compiler on its own
# --------------------------------------------------------------------------

def test_columns_not_named_by_the_policy_stay_visible():
    """An allowlist of RESTRICTIONS. A creator protecting one column should not
    have to enumerate the other forty."""
    mask = compile_column_policy(_col_policy("salary"), _identity())
    assert mask.masked == ["salary"]


def test_an_empty_column_policy_masks_nothing():
    assert compile_column_policy(None, _identity()).unrestricted
    assert compile_column_policy({}, _identity()).unrestricted


@pytest.mark.parametrize("identity,expected", [
    (_identity(group_ids={"g-uuid"}), True),
    (_identity(group_names={"finance"}), True),
    (_identity(group_ids={"other"}), False),
])
def test_column_grants_match_groups_by_id_or_name(identity, expected):
    mask = compile_column_policy(
        _col_policy("salary", [
            {"principal_type": "group", "principal_id": "g-uuid"},
            {"principal_type": "group", "principal_id": "finance"},
        ]),
        identity,
    )
    assert mask.unrestricted is expected


# --------------------------------------------------------------------------
# Save-time validation
# --------------------------------------------------------------------------

COLUMNS = ["region", "amount", "salary"]


def test_validation_accepts_a_well_formed_column_policy():
    validate_column_policy(_col_policy("salary"), COLUMNS)


@pytest.mark.parametrize("policy,fragment", [
    ({}, "required"),
    ({"columns": []}, "at least one column"),
    ({"columns": [{"name": ""}]}, "has no name"),
    ({"columns": [{"name": "nope"}]}, "not a column of this query"),
    ({"columns": [{"name": "salary"}, {"name": "SALARY"}]}, "listed twice"),
    ({"columns": [{"name": "salary", "grants": [{"principal_type": "alien",
                                                 "principal_id": "x"}]}]},
     "must target a user, a group or a role"),
    ({"columns": [{"name": "salary", "grants": [{"principal_type": "group",
                                                 "principal_id": ""}]}]},
     "names no principal"),
])
def test_validation_rejects_with_a_message_a_creator_can_act_on(policy, fragment):
    with pytest.raises(ValueError) as e:
        validate_column_policy(policy, COLUMNS)
    assert fragment in str(e.value)


# --------------------------------------------------------------------------
# The mask value itself
# --------------------------------------------------------------------------

def test_masking_uses_null_not_a_sentinel_string():
    """A sentinel like '***' is a value: it changes the column's dtype, breaks
    numeric aggregation, and renders as literal text on a chart axis."""
    assert qap.MASK_VALUE is None


def test_masking_does_not_mutate_the_callers_frame():
    """The wrapper hands back a filtered copy; the source frame a client cached
    internally must not come back masked on the next call."""
    original = _frame()
    policy = compile_for_query(
        _Query(cls_enabled=True, cls_policy=_col_policy("salary")), _identity()
    )
    QueryAccessPolicy(
        row_filter=policy.row_filter, column_mask=policy.column_mask
    ).apply(original)
    assert list(original["salary"]) == [100, 200, 300, 400]
