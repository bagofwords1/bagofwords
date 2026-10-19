"""Translation of a query against a Priority subform table into the parent
`$expand` request.

The contract under test: every top-level `and` clause reaches the level whose
columns it names, verbatim apart from prefixed parent columns being renamed;
a clause naming two levels is refused; ordering and paging are applied to the
flattened rows, never inside `$expand` (where they would act per parent).

Requests are compared after decoding, as option dictionaries, so the tests pin
what is asked of Priority rather than one byte-exact URL spelling.
"""
from urllib.parse import unquote

import pandas as pd
import pytest

from app.data_sources.clients.priority_odata_query import (
    PostOps,
    QueryError,
    SubformRoute,
    apply_post,
    build_subform_request,
    expand_chain,
    flatten_expand,
    flatten_subform,
    parse_query_string,
    split_top_level,
)


def _route(levels=2):
    path = [
        {"form": "ORDERS", "keys": ["ORDNAME"]},
        {"form": "ORDERITEMS", "keys": ["KLINE"], "navigation": "ORDERITEMS_SUBFORM"},
        {"form": "ORDERITEMSTEXT", "keys": ["TEXTLINE"], "navigation": "ORDERITEMSTEXT_SUBFORM"},
    ][:levels]
    columns = {
        "KLINE": ["ORDERITEMS", "KLINE"], "PARTNAME": ["ORDERITEMS", "PARTNAME"],
        "PDES": ["ORDERITEMS", "PDES"], "PRICE": ["ORDERITEMS", "PRICE"],
        "TQUANT": ["ORDERITEMS", "TQUANT"], "STATDES": ["ORDERITEMS", "STATDES"],
        "ORDNAME": ["ORDERS", "ORDNAME"], "CURDATE": ["ORDERS", "CURDATE"],
        "CDES": ["ORDERS", "CDES"], "TOTPRICE": ["ORDERS", "TOTPRICE"],
        "ORDERS_STATDES": ["ORDERS", "STATDES"],
    }
    if levels == 3:
        columns.update({"TEXTLINE": ["ORDERITEMSTEXT", "TEXTLINE"], "TEXT": ["ORDERITEMSTEXT", "TEXT"]})
    name = path[-1]["form"]
    return SubformRoute.from_metadata(name, {"path": path, "columns": columns})


def _parse_expand_opts(inner: str) -> dict:
    out = {}
    for part in split_top_level(inner, ";"):
        k, _, v = part.partition("=")
        out[k] = v
    return out


def _request(route, qs):
    """Build the request and decode it into {form, outer opts, inner opts...}."""
    path, post = build_subform_request(route, qs)
    form, _, query = path.partition("?")
    outer = parse_query_string(query)
    levels = [outer]
    current = outer
    while "$expand" in current:
        nav, _, rest = current["$expand"].partition("(")
        inner = _parse_expand_opts(rest[:-1]) if rest else {}
        inner["_nav"] = nav
        levels.append(inner)
        current = inner
    return form, levels, post, unquote(query)


# ── routing $filter ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("expr,outer,inner", [
    ("PRICE gt 100", None, "PRICE gt 100"),
    ("CURDATE ge 2026-07-01T00:00:00Z", "CURDATE ge 2026-07-01T00:00:00Z", None),
    ("CURDATE ge 2026-07-01T00:00:00Z and PRICE gt 100 and CDES eq 'S-100'",
     "CURDATE ge 2026-07-01T00:00:00Z and CDES eq 'S-100'", "PRICE gt 100"),
    ("(PRICE gt 100 or TQUANT gt 50) and CURDATE ge 2026-07-01T00:00:00Z",
     "CURDATE ge 2026-07-01T00:00:00Z", "(PRICE gt 100 or TQUANT gt 50)"),
    ("(CURDATE ge 2026-07-01T00:00:00Z and PRICE gt 100)",
     "CURDATE ge 2026-07-01T00:00:00Z", "PRICE gt 100"),
    ("contains(PDES,'בורג') and not (TQUANT lt 5) and CDES eq 'O''Brien and Co'",
     "CDES eq 'O''Brien and Co'", "contains(PDES,'בורג') and not (TQUANT lt 5)"),
    ("PDES eq 'a and b' AND PRICE gt 3", None, "PDES eq 'a and b' and PRICE gt 3"),
    # `and` binds tighter than `or`: this is ONE expression, kept whole
    ("PARTNAME eq 'a' or PARTNAME eq 'b' and PRICE gt 1", None, "PARTNAME eq 'a' or PARTNAME eq 'b' and PRICE gt 1"),
    ("TQUANT eq duration'P1D' and CDES eq 'x'", "CDES eq 'x'", "TQUANT eq duration'P1D'"),
    ("((PRICE gt 1) or (PRICE lt 0)) and ((CDES eq 'x'))", "((CDES eq 'x'))", "((PRICE gt 1) or (PRICE lt 0))"),
])
def test_each_clause_reaches_the_level_whose_columns_it_names(expr, outer, inner):
    _, levels, _, _ = _request(_route(), f"$filter={expr}")
    assert levels[0].get("$filter") == outer
    assert levels[1].get("$filter") == inner


def test_prefixed_parent_column_is_renamed_for_priority():
    _, levels, _, _ = _request(_route(), "$filter=ORDERS_STATDES eq 'Final' and STATDES eq 'Open'")
    assert levels[0]["$filter"] == "STATDES eq 'Final'"
    assert levels[1]["$filter"] == "STATDES eq 'Open'"


def test_a_column_name_inside_a_string_is_not_a_reference():
    # 'CURDATE' here is data, not the parent column — the clause stays on the line
    _, levels, _, _ = _request(_route(), "$filter=PDES eq 'CURDATE'")
    assert levels[1]["$filter"] == "PDES eq 'CURDATE'"
    assert "$filter" not in levels[0]


@pytest.mark.parametrize("expr", [
    "CURDATE ge 2026-07-01T00:00:00Z or PRICE gt 100",
    # A or (B and C): splitting on the `and` would apply C to every row
    "CDES eq 'Globex' or ORDNAME eq 'A' and PRICE gt 1000",
    "PRICE gt TOTPRICE",
    "not (CDES eq 'x' and PRICE gt 1)",
    "contains(PDES, CDES)",
])
def test_a_clause_mixing_levels_is_refused(expr):
    with pytest.raises(QueryError) as exc:
        build_subform_request(_route(), f"$filter={expr}")
    assert "ORDERS" in str(exc.value) and "ORDERITEMS" in str(exc.value)


@pytest.mark.parametrize("expr", [
    "NOPE eq 1",
    "PRICE gt 1 and (CDES eq 'x'",
    "PRICE gt 1)",
    "PDES eq 'unterminated",
    "PRICE gt 1 and",
    "PRICE gt 1 ^ 2",
])
def test_unreadable_or_unknown_filters_are_refused_not_sent(expr):
    with pytest.raises(QueryError):
        build_subform_request(_route(), f"$filter={expr}")


def test_three_levels_route_each_clause_to_its_own_level():
    form, levels, _, _ = _request(
        _route(3), "$filter=CDES eq 'Globex' and PRICE lt 100 and contains(TEXT,'Galv')"
    )
    assert form == "ORDERS"
    assert levels[0]["$filter"] == "CDES eq 'Globex'"
    assert levels[1]["_nav"] == "ORDERITEMS_SUBFORM" and levels[1]["$filter"] == "PRICE lt 100"
    assert levels[2]["_nav"] == "ORDERITEMSTEXT_SUBFORM" and levels[2]["$filter"] == "contains(TEXT,'Galv')"


# ── $select, keys, and what is NOT sent ─────────────────────────────────────

def test_select_is_split_by_level_and_always_carries_parent_keys():
    _, levels, post, _ = _request(_route(), "$select=PARTNAME,CDES,PRICE")
    assert levels[0]["$select"].split(",") == ["ORDNAME", "CDES"]
    assert set(levels[1]["$select"].split(",")) == {"PARTNAME", "PRICE"}
    assert post.select == ["PARTNAME", "CDES", "PRICE"]


def test_selecting_only_parent_columns_still_returns_one_row_per_line():
    _, levels, _, _ = _request(_route(), "$select=CDES")
    assert levels[1]["$select"] == "KLINE"


def test_orderby_columns_are_fetched_even_when_not_selected():
    _, levels, _, _ = _request(_route(), "$select=PARTNAME&$orderby=CURDATE desc,PRICE")
    assert "CURDATE" in levels[0]["$select"].split(",")
    assert "PRICE" in levels[1]["$select"].split(",")


def test_no_select_asks_for_every_column():
    _, levels, _, _ = _request(_route(), "$filter=PRICE gt 1")
    assert "$select" not in levels[0] and "$select" not in levels[1]


def test_orderby_top_skip_never_reach_priority():
    """Inside $expand they would act per parent — "top 10 per order"."""
    _, levels, post, raw = _request(_route(), "$orderby=PRICE desc&$top=10&$skip=5")
    assert "$top" not in raw and "$skip" not in raw and "$orderby" not in raw
    assert post.top == 10 and post.skip == 5 and post.orderby == [("PRICE", True)]


def test_since_goes_to_the_top_level_form():
    _, levels, _, _ = _request(_route(), "$since=2026-01-01T00:00:00Z")
    assert levels[0]["$since"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("orderby,parent_orderby,ties", [
    ("CURDATE desc", [("CURDATE", True)], False),
    ("CURDATE desc,KLINE", [("CURDATE", True)], True),
    ("CURDATE desc,ORDNAME desc,KLINE", [("CURDATE", True), ("ORDNAME", True)], False),
    ("ORDERS_STATDES,PRICE desc", [("STATDES", False)], True),
    ("PRICE desc,CURDATE", None, False),
])
def test_parents_are_sorted_by_the_leading_parent_columns(orderby, parent_orderby, ties):
    """Priority can sort the parents by the columns the sort starts with; a
    line column after them only orders lines among parents tied on those
    columns, which cannot happen once the parent's key is among them."""
    _, _, post, _ = _request(_route(), f"$orderby={orderby}&$top=5")
    assert post.parent_orderby == parent_orderby
    assert post.parent_ties is ties


@pytest.mark.parametrize("qs", [
    "$expand=ORDERITEMSTEXT_SUBFORM",
    "$apply=groupby((PARTNAME))",
    "$top=ten",
    "$orderby=PRICE sideways",
    "$select=PRICE,NOPE",
    "$orderby=NOPE",
    "justtext",
    "$filter=PRICE gt 1&$filter=PRICE lt 5",
])
def test_unsupported_options_are_refused(qs):
    with pytest.raises(QueryError):
        build_subform_request(_route(), qs)


def test_literal_ampersand_and_hash_cannot_break_the_request():
    path, _ = build_subform_request(_route(), "$filter=CDES eq 'R&D #1 + co'")
    query = path.partition("?")[2]
    assert query.count("&") == 1  # only the separator before $expand
    assert "#" not in query and "+" not in query
    assert parse_query_string(query)["$filter"] == "CDES eq 'R&D #1 + co'"


def test_percent_encoded_input_is_decoded_first():
    _, levels, _, _ = _request(_route(), "$filter=PRICE%20gt%20100%20and%20CDES%20eq%20'x'")
    assert levels[0]["$filter"] == "CDES eq 'x'"
    assert levels[1]["$filter"] == "PRICE gt 100"


# ── flattening ──────────────────────────────────────────────────────────────

_RESPONSE = [
    {"ORDNAME": "A", "STATDES": "Open", "ORDERITEMS_SUBFORM": [
        {"KLINE": 1, "PRICE": 5.0, "STATDES": "Pending"},
        {"KLINE": 2, "PRICE": 7.0, "STATDES": "Done"},
    ]},
    {"ORDNAME": "B", "STATDES": "Final", "ORDERITEMS_SUBFORM": []},
    {"ORDNAME": "C", "STATDES": "Open", "ORDERITEMS_SUBFORM": [{"KLINE": 1, "PRICE": 1.0, "STATDES": "Done"}]},
]


def test_flatten_gives_one_row_per_line_with_parent_columns():
    rows = flatten_subform(_RESPONSE, _route())
    assert [(r["ORDNAME"], r["KLINE"]) for r in rows] == [("A", 1), ("A", 2), ("C", 1)]
    # the parent's STATDES lands under its prefixed name; the line keeps its own
    assert rows[0]["STATDES"] == "Pending" and rows[0]["ORDERS_STATDES"] == "Open"
    assert not any(isinstance(v, list) for r in rows for v in r.values())


def test_flatten_three_levels():
    resp = [{"ORDNAME": "A", "ORDERITEMS_SUBFORM": [
        {"KLINE": 1, "ORDERITEMSTEXT_SUBFORM": [{"TEXTLINE": 1, "TEXT": "x"}, {"TEXTLINE": 2, "TEXT": "y"}]},
        {"KLINE": 2, "ORDERITEMSTEXT_SUBFORM": []},
    ]}]
    rows = flatten_subform(resp, _route(3))
    assert [(r["ORDNAME"], r["KLINE"], r["TEXTLINE"]) for r in rows] == [("A", 1, 1), ("A", 1, 2)]


def test_explicit_expand_chain_and_sibling_refusal():
    assert expand_chain("ORDERITEMS_SUBFORM($select=PRICE;$expand=ORDERITEMSTEXT_SUBFORM($select=TEXT))") == [
        "ORDERITEMS_SUBFORM", "ORDERITEMSTEXT_SUBFORM"]
    assert expand_chain("ORDERITEMS_SUBFORM($filter=PDES eq 'a,b')") == ["ORDERITEMS_SUBFORM"]
    with pytest.raises(QueryError):
        expand_chain("ORDERITEMS_SUBFORM,ORDERSTEXT_SUBFORM")


def test_explicit_expand_flatten_prefixes_the_ancestor_on_collision():
    rows = flatten_expand(_RESPONSE, "ORDERS", ["ORDERITEMS_SUBFORM"])
    assert len(rows) == 4  # A×2, B without lines, C
    assert rows[0]["STATDES"] == "Pending" and rows[0]["ORDERS_STATDES"] == "Open"


# ── post-processing ─────────────────────────────────────────────────────────

def _frame():
    return pd.DataFrame(flatten_subform(_RESPONSE, _route()))


@pytest.mark.parametrize("top,expected", [(1, [7.0]), (2, [7.0, 5.0]), (5, [7.0, 5.0, 1.0])])
def test_order_happens_before_top(top, expected):
    out = apply_post(_frame(), PostOps(orderby=[("PRICE", True)], top=top))
    assert list(out["PRICE"]) == expected


def test_skip_then_top_then_max_rows_then_projection():
    post = PostOps(select=["KLINE", "ORDNAME"], orderby=[("PRICE", False)], skip=1, top=5)
    out = apply_post(_frame(), post, max_rows=1)
    assert list(out.columns) == ["KLINE", "ORDNAME"]
    assert out.to_dict("records") == [{"KLINE": 1, "ORDNAME": "A"}]


def test_multi_key_order_with_mixed_directions():
    out = apply_post(_frame(), PostOps(orderby=[("ORDNAME", True), ("PRICE", False)]))
    assert list(zip(out["ORDNAME"], out["PRICE"])) == [("C", 1.0), ("A", 5.0), ("A", 7.0)]


def test_post_on_an_empty_result_keeps_the_requested_columns():
    out = apply_post(pd.DataFrame(), PostOps(select=["PRICE", "CDES"], orderby=[("PRICE", True)], top=3))
    assert list(out.columns) == ["PRICE", "CDES"] and out.empty


def test_annotations_are_not_columns():
    resp = [{"ORDNAME": "A", "ORDERITEMS_SUBFORM@odata.count": 1, "ORDERITEMS_SUBFORM": [
        {"KLINE": 1, "@odata.etag": "W/1", "PRICE": 2.0}]}]
    rows = flatten_subform(resp, _route())
    assert rows == [{"ORDNAME": "A", "KLINE": 1, "PRICE": 2.0}]
    assert flatten_expand(resp, "ORDERS", ["ORDERITEMS_SUBFORM"]) == rows


@pytest.mark.parametrize("value,expected", [
    ({"TEXTLINE": 1, "TEXT": "x"}, [("A", 1, "x")]),   # single-entity subform: one object
    (None, []),                                        # ... or null when empty
])
def test_single_entity_navigation_is_one_row_or_none(value, expected):
    resp = [{"ORDNAME": "A", "ORDERITEMS_SUBFORM": [{"KLINE": 1, "ORDERITEMSTEXT_SUBFORM": value}]}]
    rows = flatten_subform(resp, _route(3))
    assert [(r["ORDNAME"], r["KLINE"], r["TEXT"]) for r in rows] == expected


def test_explicit_expand_keeps_a_parent_without_children():
    rows = flatten_expand(_RESPONSE, "ORDERS", ["ORDERITEMS_SUBFORM"])
    assert [r["ORDNAME"] for r in rows] == ["A", "A", "B", "C"]
    assert "KLINE" not in rows[2]


def test_explicit_expand_never_overwrites_a_value_on_name_collision():
    # the child itself owns a column called ORDERS_STATDES
    resp = [{"STATDES": "p", "ORDERITEMS_SUBFORM": [{"STATDES": "c", "ORDERS_STATDES": "own"}]}]
    row = flatten_expand(resp, "ORDERS", ["ORDERITEMS_SUBFORM"])[0]
    assert row["STATDES"] == "c" and row["ORDERS_STATDES"] == "own"
    assert sorted(row.values()) == ["c", "own", "p"]
    # a form expanding into itself: both levels' values survive
    row = flatten_expand([{"K": "g", "X_SUBFORM": [{"K": "p"}]}], "X", ["X_SUBFORM"])[0]
    assert sorted(row.values()) == ["g", "p"] and row["K"] == "p"


def test_explicit_expand_reports_which_form_each_column_came_from():
    """A bare name can come from either level: the parent keeps it when the
    child did not return the same column. Only the flattener knows which."""
    rows = [{"ORDNAME": "A", "STATDES": "Open", "ORDERITEMS_SUBFORM": [{"KLINE": 1}]}]
    origins = {}
    flatten_expand(rows, "ORDERS", ["ORDERITEMS_SUBFORM"], origins=origins)
    assert origins == {"KLINE": ("ORDERITEMS", "KLINE"), "ORDNAME": ("ORDERS", "ORDNAME"),
                       "STATDES": ("ORDERS", "STATDES")}
    origins = {}
    flatten_expand(_RESPONSE, "ORDERS", ["ORDERITEMS_SUBFORM"], origins=origins)
    assert origins["STATDES"] == ("ORDERITEMS", "STATDES")
    assert origins["ORDERS_STATDES"] == ("ORDERS", "STATDES")
