"""compute_schema_diff: the structured before/after diff a schema-change
notification carries.

The contract is "an admin can tell exactly what changed": which tables were
added or removed, and, for a table that stayed, which columns were added or
removed and which changed type. The lists are bounded so a huge catalog can't
bloat the notification row, and ``truncated`` says when that happened.
"""
import pytest

from app.services.schema_diff import compute_schema_diff, MAX_TABLES, MAX_COLUMNS


def cols(*pairs):
    return [{"name": n, "dtype": t} for n, t in pairs]


def test_no_change_is_empty():
    pre = {"orders": cols(("id", "int"), ("total", "numeric"))}
    # Column order is not a change.
    post = {"orders": cols(("total", "numeric"), ("id", "int"))}
    diff = compute_schema_diff(pre, post)
    assert diff["total"] == 0
    assert diff["added_tables"] == [] and diff["removed_tables"] == [] and diff["changed"] == []
    assert diff["truncated"] is False


@pytest.mark.parametrize("added,removed", [(["refunds"], []), ([], ["legacy"]), (["a", "b"], ["c"])])
def test_added_and_removed_tables(added, removed):
    pre = {"keep": cols(("id", "int")), **{t: cols(("id", "int")) for t in removed}}
    post = {"keep": cols(("id", "int")), **{t: cols(("id", "int")) for t in added}}
    diff = compute_schema_diff(pre, post)
    assert diff["added_tables"] == sorted(added)
    assert diff["removed_tables"] == sorted(removed)
    assert diff["changed"] == []
    assert diff["total"] == len(added) + len(removed)


def test_column_added_removed_and_type_change_on_a_kept_table():
    pre = {"orders": cols(("id", "int"), ("coupon", "text"), ("region", "varchar"))}
    post = {"orders": cols(("id", "int"), ("discount", "numeric"), ("region", "text"))}
    diff = compute_schema_diff(pre, post)
    assert diff["total"] == 1
    [ch] = diff["changed"]
    assert ch["table"] == "orders"
    assert ch["added_cols"] == [{"name": "discount", "type": "numeric"}]
    assert ch["removed_cols"] == ["coupon"]
    assert ch["type_changes"] == [{"col": "region", "from": "varchar", "to": "text"}]


def test_accepts_column_shapes_seen_in_the_catalog():
    # Columns may come as dicts keyed "dtype" or "type", or as bare names.
    pre = {"t": [{"name": "a", "type": "int"}, "b"]}
    post = {"t": [{"name": "a", "dtype": "bigint"}, "b", "c"]}
    [ch] = compute_schema_diff(pre, post)["changed"]
    assert ch["type_changes"] == [{"col": "a", "from": "int", "to": "bigint"}]
    assert [c["name"] for c in ch["added_cols"]] == ["c"]


def test_tolerates_missing_column_lists():
    diff = compute_schema_diff({"t": None}, {"t": cols(("id", "int"))})
    assert diff["total"] == 1
    assert diff["changed"][0]["added_cols"] == [{"name": "id", "type": "int"}]


def test_large_diffs_are_bounded_and_flagged():
    n = MAX_TABLES + 7
    pre = {"wide": cols(*[(f"c{i}", "int") for i in range(MAX_COLUMNS + 5)])}
    post = {f"t{i:03d}": cols(("id", "int")) for i in range(n)}
    post["wide"] = []
    diff = compute_schema_diff(pre, post)
    # The real count survives truncation — the UI says "and N more".
    assert diff["total"] == n + 1
    assert len(diff["added_tables"]) == MAX_TABLES
    assert len(diff["changed"][0]["removed_cols"]) == MAX_COLUMNS
    assert diff["truncated"] is True


def test_new_inactive_tables_are_reported_separately():
    pre = {"orders": cols(("id", "int"))}
    post = {"orders": cols(("id", "int"), ("x", "int"))}
    diff = compute_schema_diff(pre, post, new_inactive_tables=["audit_b", "audit_a"])
    assert diff["new_inactive_tables"] == ["audit_a", "audit_b"]
    # Not counted as an agent-visible change.
    assert diff["total"] == 1
