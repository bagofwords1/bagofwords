"""aggregate_list_submissions: one item per list, distinct-row counts."""
from app.services.report_summary_lists import aggregate_list_submissions


def _ok(list_id, rows, name="L"):
    return {"success": True, "list_id": list_id, "list_name": name, "data_source_id": "ds",
            "rows": [{"row_id": r, "action": a} for r, a in rows]}


def test_groups_by_list_in_first_write_order():
    items = aggregate_list_submissions([
        ("t1", "c1", _ok("B", [("b1", "inserted")])),
        ("t2", "c2", _ok("A", [("a1", "inserted")])),
        ("t3", "c3", _ok("B", [("b2", "inserted")])),
    ])
    assert [i.list_id for i in items] == ["B", "A"]
    assert [s.message_id for s in items[0].submissions] == ["c1", "c3"]
    assert items[0].inserted == 2


def test_a_row_counts_once_and_added_wins_over_later_updates():
    [item] = aggregate_list_submissions([
        ("t1", "c1", _ok("L", [("r1", "inserted"), ("r2", "updated")])),
        ("t2", "c2", _ok("L", [("r1", "updated"), ("r2", "updated"), ("r3", "unchanged")])),
    ])
    assert (item.inserted, item.updated) == (1, 1)


def test_rejected_and_malformed_results_are_ignored():
    assert aggregate_list_submissions([
        ("t1", "c1", {"success": False, "list_id": "L", "errors": ["x"]}),
        ("t2", "c2", None),
        ("t3", "c3", {"success": True}),
    ]) == []
