"""Unit tests for MondayClient.

Covers the client contract:
- test_connection: success and auth failure
- get_schemas: boards -> tables, subitem-board filtering, duplicate-name
  disambiguation, status labels in column descriptions, board_relation ->
  foreign keys, workspace/board config scoping
- execute_query: JSON spec parsing, column selection by title or id,
  status-label -> index translation in rules, cursor pagination, row cap,
  typed cell values (numbers -> float, checkbox -> bool, rating -> int,
  empty -> None), deterministic empty-result columns

HTTP is faked at the `requests.post` boundary with payloads shaped like real
monday.com GraphQL responses (verified live against api.monday.com)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.data_sources.clients.monday_client import MondayClient


STATUS_SETTINGS = json.dumps({
    "labels": {"0": "Backlog", "1": "In Progress", "2": "Done"},
})

BOARD_MAIN = {
    "id": "111",
    "name": "Sales Pipeline",
    "description": "Q3 deals",
    "board_kind": "public",
    "items_count": 3,
    "workspace": {"id": "9", "name": "Sales"},
    "columns": [
        {"id": "name", "title": "Name", "type": "name", "settings_str": "{}"},
        {"id": "color_1", "title": "Status", "type": "status", "settings_str": STATUS_SETTINGS},
        {"id": "numbers_1", "title": "Amount", "type": "numbers", "settings_str": "{}"},
        {"id": "date_1", "title": "Due Date", "type": "date", "settings_str": "{}"},
        {"id": "check_1", "title": "Approved", "type": "checkbox", "settings_str": "{}"},
        {"id": "rating_1", "title": "Priority", "type": "rating", "settings_str": "{}"},
        {"id": "relation_1", "title": "Account", "type": "board_relation",
         "settings_str": json.dumps({"boardIds": [222]})},
    ],
}

BOARD_LINKED = {
    "id": "222",
    "name": "Accounts",
    "description": None,
    "board_kind": "public",
    "items_count": 1,
    "workspace": {"id": "9", "name": "Sales"},
    "columns": [
        {"id": "name", "title": "Name", "type": "name", "settings_str": "{}"},
        {"id": "text_1", "title": "Industry", "type": "text", "settings_str": "{}"},
    ],
}

BOARD_SUBITEMS = {
    "id": "333",
    "name": "Subitems of Sales Pipeline",
    "description": None,
    "board_kind": "public",
    "items_count": 0,
    "workspace": {"id": "9", "name": "Sales"},
    "columns": [{"id": "name", "title": "Name", "type": "name", "settings_str": "{}"}],
}

BOARD_DUPE_A = {**BOARD_LINKED, "id": "444", "name": "Tasks", "workspace": {"id": "9", "name": "Sales"}}
BOARD_DUPE_B = {**BOARD_LINKED, "id": "555", "name": "Tasks", "workspace": {"id": "10", "name": "Ops"}}
BOARD_MULTI_LEVEL = {
    **BOARD_LINKED,
    "id": "666",
    "name": "Portfolio Projects",
    "hierarchy_type": "multi_level",
}


def item(item_id, name, values):
    return {"id": str(item_id), "name": name, "group": {"title": "Main"}, "column_values": values}


ITEMS_PAGE_1 = {
    "cursor": "CURSOR-2",
    "items": [
        item(1, "Deal A", [
            {"id": "color_1", "text": "Done", "value": None, "type": "status"},
            {"id": "numbers_1", "text": "1200.5", "value": '"1200.5"', "type": "numbers"},
            {"id": "date_1", "text": "2026-01-15", "value": None, "type": "date"},
            {"id": "check_1", "text": "v", "value": '{"checked":"true"}', "type": "checkbox"},
            {"id": "rating_1", "text": "4", "value": '{"rating":4}', "type": "rating"},
        ]),
        item(2, "Deal B", [
            {"id": "color_1", "text": "Backlog", "value": None, "type": "status"},
            {"id": "numbers_1", "text": "", "value": None, "type": "numbers"},
            {"id": "date_1", "text": "", "value": None, "type": "date"},
            {"id": "check_1", "text": "", "value": None, "type": "checkbox"},
            {"id": "rating_1", "text": "", "value": None, "type": "rating"},
        ]),
    ],
}

ITEMS_PAGE_2 = {
    "cursor": None,
    "items": [
        item(3, "Deal C", [
            {"id": "color_1", "text": "In Progress", "value": None, "type": "status"},
            {"id": "numbers_1", "text": "99", "value": '"99"', "type": "numbers"},
            {"id": "date_1", "text": "2026-02-01", "value": None, "type": "date"},
            {"id": "check_1", "text": "", "value": None, "type": "checkbox"},
            {"id": "rating_1", "text": "2", "value": '{"rating":2}', "type": "rating"},
        ]),
    ],
}


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def fake_post(monkeypatch, responder):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return responder(json["query"], json.get("variables") or {})

    monkeypatch.setattr("app.data_sources.clients.monday_client.requests.post", post)
    return calls


def boards_responder(boards, workspaces=None):
    """Stub the discovery surface: global crawl, workspace listing, per-
    workspace id sweep, by-id recovery. With no workspaces the sweep is a
    no-op and discovery equals the global crawl."""
    def responder(query, variables):
        if "workspaces (limit: $limit, page: $page" in query:
            page = variables.get("page", 1)
            return FakeResponse({"data": {"workspaces": (workspaces or []) if page == 1 else []}})
        if "workspace_ids: $ws" in query:
            page = variables.get("page", 1)
            ws = (variables.get("ws") or [None])[0]
            in_ws = [b for b in boards if str((b.get("workspace") or {}).get("id")) == str(ws)]
            return FakeResponse({"data": {"boards": [{"id": b["id"]} for b in in_ws] if page == 1 else []}})
        if "boards (ids: $ids" in query:
            wanted = set(variables.get("ids") or [])
            return FakeResponse({"data": {"boards": [b for b in boards if str(b["id"]) in wanted]}})
        if "boards (limit: $limit, page: $page" in query:
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": boards if page == 1 else []}})
        if "next_items_page" in query:
            return FakeResponse({"data": {"next_items_page": ITEMS_PAGE_2}})
        if "items_page" in query:
            return FakeResponse({"data": {"boards": [{"items_page": ITEMS_PAGE_1}]}})
        if "me {" in query or "me {" in query.replace("  ", " "):
            return FakeResponse({"data": {
                "me": {"name": "Test User", "email": "t@example.com"},
                "account": {"name": "Acme"}, "boards": [{"id": "111"}],
            }})
        raise AssertionError(f"unexpected query: {query}")
    return responder


ALL_BOARDS = [BOARD_MAIN, BOARD_LINKED, BOARD_SUBITEMS, BOARD_DUPE_A, BOARD_DUPE_B]


# ── test_connection ─────────────────────────────────────────────────────────

def test_connection_success(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    result = MondayClient(api_token="t").test_connection()
    assert result["success"] is True
    assert "Test User" in result["message"] and "Acme" in result["message"]


def test_connection_degrades_without_me_scope(monkeypatch):
    """A delegated OAuth token without me:read fails only the Query.me probe —
    test_connection must fall back to account-level fields, not fail."""
    def responder(query, variables):
        if "me {" in query:
            return FakeResponse({"errors": [{"message":
                "Unauthorized to load field 'Query.me', Reason: missing required scopes"}]})
        return FakeResponse({"data": {"account": {"name": "Acme"}, "boards": [{"id": "111"}]}})

    fake_post(monkeypatch, responder)
    result = MondayClient(access_token="delegated").test_connection()
    assert result["success"] is True
    assert "Acme" in result["message"]


def test_connection_auth_failure(monkeypatch):
    fake_post(monkeypatch, lambda q, v: FakeResponse("<html>denied</html>", status_code=401))
    result = MondayClient(api_token="bad").test_connection()
    assert result["success"] is False
    assert "401" in result["message"]


def test_no_credentials():
    result = MondayClient().test_connection()
    assert result["success"] is False
    assert "no credentials" in result["message"]


# ── get_schemas ─────────────────────────────────────────────────────────────

def test_get_schemas_shape(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    tables = MondayClient(api_token="t").get_schemas()
    names = {t.name for t in tables}
    # subitem board dropped; duplicate names disambiguated with the board id
    assert names == {"Sales Pipeline", "Accounts", "Tasks [444]", "Tasks [555]"}

    main = next(t for t in tables if t.name == "Sales Pipeline")
    columns = {c.name: c for c in main.columns}
    # base columns + board columns named by title; "name"-type column not duplicated
    assert list(columns)[:3] == ["item_id", "name", "group"]
    assert columns["Amount"].dtype == "float"
    assert columns["Approved"].dtype == "bool"
    assert columns["Priority"].dtype == "int"
    assert columns["Due Date"].dtype == "date"
    # status labels surface in the column description for the planner
    assert "Done" in columns["Status"].description
    assert main.pks[0].name == "item_id"
    assert main.metadata_json["board_id"] == "111"
    assert main.metadata_json["hierarchy_type"] == "classic"

    # board_relation -> companion ids column + FK from it to the linked table
    assert columns["Account (item_ids)"].dtype == "list[int]"
    assert len(main.fks) == 1
    assert main.fks[0].references_name == "Accounts"
    assert main.fks[0].column.name == "Account (item_ids)"
    assert main.fks[0].references_column.name == "item_id"


def test_get_schemas_scoping(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    tables = MondayClient(api_token="t", workspaces="Ops").get_schemas()
    assert [t.name for t in tables] == ["Tasks"]

    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    tables = MondayClient(api_token="t", boards="111").get_schemas()
    assert [t.name for t in tables] == ["Sales Pipeline"]


def test_get_schemas_discovers_classic_and_multi_level_boards(monkeypatch):
    """Every active board visible to the token is part of the catalog,
    regardless of whether monday stores it as a classic or multi-level board."""
    def post(url, json=None, headers=None, timeout=None):
        query = json["query"]
        variables = json.get("variables") or {}
        if "workspaces (limit" in query:
            return FakeResponse({"data": {"workspaces": []}})
        if "boards (" not in query or "page" not in variables:
            raise AssertionError(f"unexpected query: {query}")

        includes_all_hierarchies = "hierarchy_types: [classic, multi_level]" in query
        supports_multi_level = (headers or {}).get("API-Version", "") >= "2025-10"
        boards = [BOARD_MAIN]
        if includes_all_hierarchies and supports_multi_level:
            boards.append(BOARD_MULTI_LEVEL)
        return FakeResponse({"data": {"boards": boards if variables["page"] == 1 else []}})

    monkeypatch.setattr("app.data_sources.clients.monday_client.requests.post", post)

    tables = MondayClient(api_token="t").get_schemas()

    assert {table.name for table in tables} == {"Sales Pipeline", "Portfolio Projects"}
    multi_level = next(table for table in tables if table.name == "Portfolio Projects")
    assert multi_level.metadata_json["hierarchy_type"] == "multi_level"


def test_get_schemas_survives_short_non_final_pages(monkeypatch):
    """monday does not guarantee non-final pages contain `limit` boards. A
    short page mid-crawl must not truncate discovery — pagination stops only
    on an EMPTY page."""
    pages = {1: [BOARD_MAIN], 2: [BOARD_LINKED], 3: [BOARD_DUPE_A], 4: []}

    def responder(query, variables):
        if "workspaces (limit" in query:
            return FakeResponse({"data": {"workspaces": []}})
        if "boards (limit: $limit, page: $page" in query:
            return FakeResponse({"data": {"boards": pages[variables["page"]]}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    tables = MondayClient(api_token="t").get_schemas()
    assert {t.name for t in tables} == {"Sales Pipeline", "Accounts", "Tasks"}


def test_get_schemas_recovers_boards_hidden_from_global_listing(monkeypatch):
    """The global boards listing can omit boards that workspace-scoped queries
    return (observed live with shareable boards / cross-product workspaces).
    The per-workspace sweep recovers them via boards(ids:)."""
    def responder(query, variables):
        if "workspaces (limit" in query:
            page = variables.get("page", 1)
            ws = [{"id": "9", "name": "Sales", "kind": "open"},
                  {"id": "10", "name": "Ops", "kind": "open"}]
            return FakeResponse({"data": {"workspaces": ws if page == 1 else []}})
        if "workspace_ids: $ws" in query:
            page = variables.get("page", 1)
            ws = (variables.get("ws") or [None])[0]
            by_ws = {"9": [{"id": "111"}], "10": [{"id": "555"}]}
            return FakeResponse({"data": {"boards": by_ws.get(str(ws), []) if page == 1 else []}})
        if "boards (ids: $ids" in query:
            assert variables["ids"] == ["555"]
            return FakeResponse({"data": {"boards": [BOARD_DUPE_B]}})
        if "boards (limit: $limit, page: $page" in query:
            # the global crawl never returns board 555
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": [BOARD_MAIN] if page == 1 else []}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    tables = MondayClient(api_token="t").get_schemas()
    assert {t.name for t in tables} == {"Sales Pipeline", "Tasks"}


def test_get_schemas_filters_subitem_boards_by_type_not_name(monkeypatch):
    """A REAL board that happens to be named 'Subitems of …' (monday's
    'expand subitems into a board' creates these) must be kept; the shadow
    board is excluded by its API type. Boards without a type field fall back
    to the name heuristic."""
    real_named_like_subitems = {**BOARD_LINKED, "id": "777",
                                "name": "Subitems of Important Board", "type": "board"}
    shadow_renamed = {**BOARD_LINKED, "id": "888", "name": "Roadmap children",
                      "type": "sub_items_board"}
    legacy_no_type = BOARD_SUBITEMS  # no "type" key, "Subitems of " name

    fake_post(monkeypatch, boards_responder(
        [BOARD_MAIN, real_named_like_subitems, shadow_renamed, legacy_no_type]))
    tables = MondayClient(api_token="t").get_schemas()
    assert {t.name for t in tables} == {"Sales Pipeline", "Subitems of Important Board"}


def test_get_schemas_workspace_scoping_matches_main_workspace(monkeypatch):
    """Boards in monday's legacy Main workspace return workspace: null — the
    'Main workspace' alias must select them; any other filter set must not
    silently include them."""
    main_ws_board = {**BOARD_LINKED, "id": "999", "name": "Legacy Board", "workspace": None}
    fake_post(monkeypatch, boards_responder([BOARD_MAIN, main_ws_board]))
    tables = MondayClient(api_token="t", workspaces="Main workspace").get_schemas()
    assert [t.name for t in tables] == ["Legacy Board"]

    fake_post(monkeypatch, boards_responder([BOARD_MAIN, main_ws_board]))
    tables = MondayClient(api_token="t", workspaces="Sales").get_schemas()
    assert [t.name for t in tables] == ["Sales Pipeline"]


def test_resolve_board_escalates_to_deep_discovery(monkeypatch):
    """Query-time board resolution starts shallow (no workspace sweep). A
    board the global listing omits must still resolve — via one escalation
    to deep discovery — instead of failing with 'not found'."""
    def responder(query, variables):
        if "workspaces (limit" in query:
            page = variables.get("page", 1)
            ws = [{"id": "10", "name": "Ops", "kind": "open"}]
            return FakeResponse({"data": {"workspaces": ws if page == 1 else []}})
        if "workspace_ids: $ws" in query:
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": [{"id": "555"}] if page == 1 else []}})
        if "boards (ids: $ids" in query:
            return FakeResponse({"data": {"boards": [BOARD_DUPE_B]}})
        if "boards (limit: $limit, page: $page" in query:
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": [BOARD_MAIN] if page == 1 else []}})
        if "items_page" in query:
            return FakeResponse({"data": {"boards": [{"items_page": {"cursor": None, "items": []}}]}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    df = MondayClient(api_token="t").execute_query(json.dumps({"board": "Tasks", "limit": 5}))
    assert df.empty  # resolved via escalation; empty board, not a lookup error


def test_get_schemas_survives_workspace_listing_failure(monkeypatch):
    """A token that cannot list workspaces (missing workspaces:read) must not
    break discovery — the global crawl still produces the catalog."""
    def responder(query, variables):
        if "workspaces (limit" in query:
            return FakeResponse({"errors": [{"message":
                "Unauthorized to load field 'Query.workspaces', Reason: missing required scopes"}]})
        if "boards (limit: $limit, page: $page" in query:
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": [BOARD_MAIN] if page == 1 else []}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    tables = MondayClient(api_token="t").get_schemas()
    assert [t.name for t in tables] == ["Sales Pipeline"]


# ── execute_query ───────────────────────────────────────────────────────────

def test_execute_query_flattening_and_pagination(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    df = MondayClient(api_token="t").execute_query(json.dumps({"board": "Sales Pipeline", "limit": 10}))
    assert list(df.columns) == [
        "item_id", "name", "group", "Status", "Amount", "Due Date", "Approved", "Priority",
        "Account", "Account (item_ids)",
    ]
    # both pages fetched through the cursor
    assert len(df) == 3
    row = df.iloc[0]
    assert row["item_id"] == 1
    assert row["Amount"] == 1200.5
    assert row["Approved"] is True or row["Approved"] == True  # noqa: E712
    assert row["Priority"] == 4
    assert row["Status"] == "Done"
    # empty cells -> None/NaN, not ""
    assert pd.isna(df.iloc[1]["Amount"])
    assert df.iloc[1]["Approved"] == False  # noqa: E712


def test_execute_query_row_cap(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    df = MondayClient(api_token="t").execute_query(json.dumps({"board": "111", "limit": 2}))
    assert len(df) == 2  # stopped at the cap, no second page beyond it


def test_execute_query_column_selection_and_label_translation(monkeypatch):
    calls = fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    client = MondayClient(api_token="t")
    df = client.execute_query(json.dumps({
        "board": "Sales Pipeline",
        "columns": ["Status", "numbers_1"],          # title AND raw id both resolve
        "rules": [{"column_id": "Status", "compare_value": ["Done", "In Progress"], "operator": "any_of"}],
        "order_by": {"column_id": "Due Date", "direction": "desc"},
        "limit": 3,
    }))
    assert "Status" in df.columns and "Amount" in df.columns
    items_call = next(c for c in calls if c.get("variables", {}).get("qp"))
    qp = items_call["variables"]["qp"]
    # label text translated to label indices; column titles resolved to ids
    assert qp["rules"][0]["column_id"] == "color_1"
    assert qp["rules"][0]["compare_value"] == [2, 1]
    assert qp["order_by"][0]["column_id"] == "date_1"
    assert items_call["variables"]["cols"] == ["color_1", "numbers_1"]


def test_execute_query_accepts_base_columns_in_selection(monkeypatch):
    """The published schema lists item_id/name/group on every table, so specs
    legitimately request them — they must be accepted (and skipped), not fail
    as unknown columns."""
    calls = fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    df = MondayClient(api_token="t").execute_query(json.dumps({
        "board": "Sales Pipeline",
        "columns": ["item_id", "name", "group", "Status"],
        "limit": 3,
    }))
    assert list(df.columns) == ["item_id", "name", "group", "Status"]
    items_call = next(c for c in calls if "items_page" in c["query"])
    assert items_call["variables"]["cols"] == ["color_1"]  # only the board column is fetched


def test_execute_query_relation_columns_use_display_value(monkeypatch):
    """Mirror / connect-boards / dependency / subitems column values return an
    EMPTY `text` — their display string is on the type-specific
    `display_value` field. The items query must request the fragments and the
    cell parser must backfill text from display_value."""
    board = {
        **BOARD_LINKED,
        "id": "777",
        "name": "Linked Board",
        "columns": [
            {"id": "name", "title": "Name", "type": "name", "settings_str": "{}"},
            {"id": "rel_1", "title": "Account", "type": "board_relation", "settings_str": "{}"},
            {"id": "mirror_1", "title": "Account Status", "type": "mirror", "settings_str": "{}"},
        ],
    }
    page = {"cursor": None, "items": [item(1, "Deal", [
        {"id": "rel_1", "text": None, "value": "{}", "type": "board_relation",
         "display_value": "Acme Corp, Globex", "linked_item_ids": ["901", "902"]},
        {"id": "mirror_1", "text": "", "value": None, "type": "mirror",
         "display_value": "Active"},
    ])]}

    calls = []

    def responder(query, variables):
        calls.append(query)
        if "workspaces (limit" in query:
            return FakeResponse({"data": {"workspaces": []}})
        if "boards (limit: $limit, page: $page" in query:
            return FakeResponse({"data": {"boards": [board] if variables.get("page", 1) == 1 else []}})
        if "items_page" in query:
            return FakeResponse({"data": {"boards": [{"items_page": page}]}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    df = MondayClient(api_token="t").execute_query(json.dumps({"board": "Linked Board", "limit": 5}))
    assert df.iloc[0]["Account"] == "Acme Corp, Globex"
    assert df.iloc[0]["Account (item_ids)"] == [901, 902]
    assert df.iloc[0]["Account Status"] == "Active"
    items_query = next(q for q in calls if "items_page" in q)
    assert "... on MirrorValue { display_value }" in items_query
    assert "... on SubtasksValue { display_value }" in items_query
    for fragment in ["BoardRelationValue", "DependencyValue"]:
        assert f"... on {fragment} {{ display_value linked_item_ids }}" in items_query


def test_execute_query_item_ids_companion_selectable_and_joinable(monkeypatch):
    """Selecting the '<Column> (item_ids)' companion published in the schema
    must select the parent column, and the companion must support the
    documented explode-and-merge join on the linked board's item_id."""
    board = {
        **BOARD_LINKED,
        "id": "777",
        "name": "Linked Board",
        "columns": [
            {"id": "name", "title": "Name", "type": "name", "settings_str": "{}"},
            {"id": "rel_1", "title": "Account", "type": "board_relation",
             "settings_str": json.dumps({"boardIds": [222]})},
        ],
    }
    page = {"cursor": None, "items": [
        item(1, "Deal A", [{"id": "rel_1", "text": None, "value": "{}", "type": "board_relation",
                            "display_value": "Acme", "linked_item_ids": ["10"]}]),
        item(2, "Deal B", [{"id": "rel_1", "text": None, "value": "{}", "type": "board_relation",
                            "display_value": "Acme, Globex", "linked_item_ids": ["10", "11"]}]),
    ]}

    def responder(query, variables):
        if "workspaces (limit" in query:
            return FakeResponse({"data": {"workspaces": []}})
        if "boards (limit: $limit, page: $page" in query:
            return FakeResponse({"data": {"boards": [board] if variables.get("page", 1) == 1 else []}})
        if "items_page" in query:
            return FakeResponse({"data": {"boards": [{"items_page": page}]}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    df = MondayClient(api_token="t").execute_query(json.dumps(
        {"board": "Linked Board", "columns": ["Account (item_ids)"], "limit": 5}))
    assert list(df.columns) == ["item_id", "name", "group", "Account", "Account (item_ids)"]

    accounts = pd.DataFrame({"item_id": [10, 11], "name": ["Acme", "Globex"]})
    joined = (df.explode("Account (item_ids)")
                .merge(accounts, left_on="Account (item_ids)", right_on="item_id",
                       how="left", suffixes=("", " (Account)")))
    assert len(joined) == 3  # Deal A -> Acme; Deal B -> Acme + Globex
    assert sorted(joined["name (Account)"]) == ["Acme", "Acme", "Globex"]


def test_execute_query_bad_specs(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    client = MondayClient(api_token="t")
    with pytest.raises(ValueError, match="JSON object"):
        client.execute_query("SELECT * FROM boards")
    with pytest.raises(ValueError, match='"board" key'):
        client.execute_query(json.dumps({"limit": 5}))
    with pytest.raises(ValueError, match="not found"):
        client.execute_query(json.dumps({"board": "No Such Board"}))
    with pytest.raises(ValueError, match="ambiguous"):
        client.execute_query(json.dumps({"board": "Tasks"}))
    with pytest.raises(ValueError, match="Unknown column"):
        client.execute_query(json.dumps({"board": "Sales Pipeline", "columns": ["Nope"]}))


def test_execute_query_empty_result_columns(monkeypatch):
    def responder(query, variables):
        if "workspaces (limit" in query:
            return FakeResponse({"data": {"workspaces": []}})
        if "boards (limit: $limit, page: $page" in query:
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": [BOARD_MAIN] if page == 1 else []}})
        if "items_page" in query:
            return FakeResponse({"data": {"boards": [{"items_page": {"cursor": None, "items": []}}]}})
        raise AssertionError(query)

    fake_post(monkeypatch, responder)
    df = MondayClient(api_token="t").execute_query(
        json.dumps({"board": "Sales Pipeline", "columns": ["Amount"], "limit": 5})
    )
    assert df.empty
    assert list(df.columns) == ["item_id", "name", "group", "Amount"]


# ── throttling ──────────────────────────────────────────────────────────────

def test_retry_on_429(monkeypatch):
    attempts = {"n": 0}

    def responder(query, variables):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse("<html>rate limited</html>", status_code=429, headers={"Retry-After": "0"})
        return FakeResponse({"data": {
            "me": {"name": "U", "email": "u@e.com"}, "account": {"name": "A"}, "boards": [],
        }})

    fake_post(monkeypatch, responder)
    monkeypatch.setattr("app.data_sources.clients.monday_client.time.sleep", lambda s: None)
    result = MondayClient(api_token="t").test_connection()
    assert result["success"] is True
    assert attempts["n"] == 2


# ── regression: shareable boards in a closed workspace ──────────────────────

BOARD_SHARE_A = {
    **BOARD_LINKED,
    "id": "777",
    "name": "Shared Tasks",
    "board_kind": "share",
    "workspace": {"id": "77", "name": "Closed WS"},
}
BOARD_SHARE_B = {
    **BOARD_LINKED,
    "id": "888",
    "name": "Shared Portfolio",
    "board_kind": "share",
    "workspace": {"id": "77", "name": "Closed WS"},
}


def test_get_schemas_recovers_shareable_boards_in_a_closed_workspace(monkeypatch):
    """Boards with board_kind 'share' living in a CLOSED workspace that the
    indexing identity is not a member of.

    Both existing discovery passes are blind to them:
      - the unqualified global crawl omits shareable boards, and
      - `workspaces (membership_kind: all)` never lists a closed workspace the
        identity is not a member of, so the per-workspace sweep has no id to
        sweep and the by-id recovery is never reached.

    Board-level sharing does not imply workspace membership, so only a crawl
    scoped explicitly to each board_kind finds them.
    """
    def responder(query, variables):
        if "workspaces (limit: $limit, page: $page" in query:
            page = variables.get("page", 1)
            # The closed workspace 77 is NOT listed: the identity reaches its
            # boards as a board subscriber, not as a workspace member.
            ws = [{"id": "9", "name": "Sales", "kind": "open"}]
            return FakeResponse({"data": {"workspaces": ws if page == 1 else []}})
        if "workspace_ids: $ws" in query:
            page = variables.get("page", 1)
            ws = (variables.get("ws") or [None])[0]
            in_ws = [b for b in (BOARD_MAIN,) if str((b.get("workspace") or {}).get("id")) == str(ws)]
            return FakeResponse({"data": {"boards": [{"id": b["id"]} for b in in_ws] if page == 1 else []}})
        if "boards (ids: $ids" in query:
            wanted = set(variables.get("ids") or [])
            pool = [BOARD_MAIN, BOARD_SHARE_A, BOARD_SHARE_B]
            return FakeResponse({"data": {"boards": [b for b in pool if str(b["id"]) in wanted]}})
        if "board_kind: $kind" in query:
            page = variables.get("page", 1)
            by_kind = {
                "public": [BOARD_MAIN],
                "private": [],
                "share": [BOARD_SHARE_A, BOARD_SHARE_B],
            }
            batch = by_kind.get(variables.get("kind"), [])
            return FakeResponse({"data": {"boards": batch if page == 1 else []}})
        if "boards (limit: $limit, page: $page" in query:
            # The unqualified account-wide listing omits the shareable boards.
            page = variables.get("page", 1)
            return FakeResponse({"data": {"boards": [BOARD_MAIN] if page == 1 else []}})
        raise AssertionError(f"unexpected query: {query}")

    fake_post(monkeypatch, responder)
    tables = MondayClient(api_token="t").get_schemas()
    assert {t.name for t in tables} == {
        "Sales Pipeline", "Shared Tasks", "Shared Portfolio",
    }


def test_board_kind_crawl_failure_does_not_break_discovery(monkeypatch):
    """A token or API version that rejects the board_kind argument must not
    fail the whole index — the unqualified crawl and workspace sweep still
    stand on their own."""
    def responder(query, variables):
        if "board_kind: $kind" in query:
            return FakeResponse({"errors": [{"message": "Argument 'board_kind' is not defined"}]})
        return boards_responder([BOARD_MAIN, BOARD_LINKED])(query, variables)

    fake_post(monkeypatch, responder)
    tables = MondayClient(api_token="t").get_schemas()
    assert {t.name for t in tables} == {"Sales Pipeline", "Accounts"}
