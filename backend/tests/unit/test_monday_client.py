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


def boards_responder(boards):
    def responder(query, variables):
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

    # board_relation -> FK to the linked board's table name
    assert len(main.fks) == 1
    assert main.fks[0].references_name == "Accounts"
    assert main.fks[0].column.name == "Account"


def test_get_schemas_scoping(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    tables = MondayClient(api_token="t", workspaces="Ops").get_schemas()
    assert [t.name for t in tables] == ["Tasks"]

    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    tables = MondayClient(api_token="t", boards="111").get_schemas()
    assert [t.name for t in tables] == ["Sales Pipeline"]


# ── execute_query ───────────────────────────────────────────────────────────

def test_execute_query_flattening_and_pagination(monkeypatch):
    fake_post(monkeypatch, boards_responder(ALL_BOARDS))
    df = MondayClient(api_token="t").execute_query(json.dumps({"board": "Sales Pipeline", "limit": 10}))
    assert list(df.columns) == [
        "item_id", "name", "group", "Status", "Amount", "Due Date", "Approved", "Priority", "Account",
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
