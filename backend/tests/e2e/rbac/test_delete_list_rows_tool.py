"""delete_list_rows — the chat tool that removes rows from an Agent List.

Contracts:
- Offered in chat and training, never to sub-agents.
- Same gate as the REST delete: only a user who can MANAGE the owning agent
  may delete; anyone else is refused before any card is shown.
- Rows are matched first (by _row_id, by key value ignoring case/spaces, or
  all). A selection that matches nothing, or an invalid selection, shows no
  card.
- Nothing is deleted until the user confirms on the card; a cancel deletes
  nothing. Approval deletes exactly the matched rows and nothing else; a
  clear keeps the list and its schema.
"""
import uuid

import pytest

from tests.e2e.rbac.test_list_schema_tools import _create_input, _h, drive, lists_of, world  # noqa: F401

pytestmark = pytest.mark.e2e

COMPANIES = ["Acme Corp", "Globex", "Initech", "Umbrella"]


def _rec(name, value):
    env = lambda v: {"value": v, "status": "found", "evidence": [], "note": None}  # noqa: E731
    return {"row_id": None, "fields": {"counterparty": env(name), "annual_value": env(value)}}


@pytest.fixture
def filled(test_client, world):
    """A keyed list with one row per company, written through submit_list."""
    out, _, _ = drive(world, "create_list", _create_input(name=f"Contracts {uuid.uuid4().hex[:4]}", require_evidence=False),
                      decision={"approved": True})
    assert out["status"] == "created", out
    sub, _, _ = drive(world, "submit_list", {"list_id": out["list_id"],
                                             "records": [_rec(c, 1000 + i) for i, c in enumerate(COMPANIES)]})
    assert sub["inserted"] == len(COMPANIES), sub
    return out


def rows_of(test_client, world, lst, actor="admin"):
    r = test_client.get(f"/api/data_sources/{world['agent']['id']}/lists/{lst['list_id']}/rows?limit=500",
                        headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    return {row["id"]: row for row in rows}


def names(rows):
    return sorted(next((env.get("value") for env in r["values"].values() if isinstance(env.get("value"), str)), None)
                  for r in rows.values())


def cards(events):
    return [e for e in events if e.type == "tool.confirmation"]


# ── catalog ────────────────────────────────────────────────────────────────

def test_offered_in_chat_and_training_but_not_to_sub_agents():
    from app.ai.registry import ToolRegistry
    from app.ai.sub_agent_profiles import BLOCKED_IN_SUB_AGENTS
    r = ToolRegistry()
    for mode in ("chat", "training"):
        names_ = set()
        for pt in ("action", "research"):
            names_ |= {t["name"] for t in r.get_catalog_for_plan_type(pt, mode=mode)}
        assert "delete_list_rows" in names_, mode
    assert "delete_list_rows" in BLOCKED_IN_SUB_AGENTS


# ── gate ───────────────────────────────────────────────────────────────────

def test_viewer_cannot_delete_and_sees_no_card(test_client, world, filled):
    before = rows_of(test_client, world, filled)
    out, events, _ = drive(world, "delete_list_rows", {"list_id": filled["list_id"], "all": True}, actor="viewer")
    assert out["success"] is False and out["status"] == "permission_denied"
    assert not cards(events)
    assert rows_of(test_client, world, filled).keys() == before.keys()


@pytest.mark.parametrize("bad", [
    {},                                              # no selector
    {"all": True, "keys": ["Globex"]},               # two selectors
    {"row_ids": []},                                 # empty selector
])
def test_invalid_selection_shows_no_card(test_client, world, filled, bad):
    out, events, _ = drive(world, "delete_list_rows", {"list_id": filled["list_id"], **bad})
    assert out["success"] is False and out["status"] == "invalid" and out["errors"]
    assert not cards(events)
    assert len(rows_of(test_client, world, filled)) == len(COMPANIES)


def test_selection_matching_nothing_shows_no_card(test_client, world, filled):
    out, events, _ = drive(world, "delete_list_rows",
                           {"list_id": filled["list_id"], "keys": ["Nobody Inc"], "reason": "x"})
    assert out["success"] is False and out["status"] == "no_match"
    assert out["not_found"] == ["Nobody Inc"]
    assert not cards(events)
    assert len(rows_of(test_client, world, filled)) == len(COMPANIES)


def test_unknown_list_is_reported(world, filled):
    out, events, _ = drive(world, "delete_list_rows", {"list_id": "no such list", "all": True})
    assert out["status"] == "list_not_found" and filled["list_name"] in out["message"]
    assert not cards(events)


# ── confirm ────────────────────────────────────────────────────────────────

def test_cancel_deletes_nothing_and_returns_the_note(test_client, world, filled):
    out, events, _ = drive(world, "delete_list_rows", {"list_id": filled["list_id"], "all": True},
                           decision={"approved": False, "response": {"feedback": "keep Acme"}})
    assert len(cards(events)) == 1
    assert out["success"] is False and out["status"] == "rejected" and out["feedback"] == "keep Acme"
    assert out["deleted"] == 0
    assert len(rows_of(test_client, world, filled)) == len(COMPANIES)


@pytest.mark.parametrize("actor", ["admin", "manager"])
def test_approved_delete_by_key_removes_only_those_rows(test_client, world, filled, actor):
    # Keys match like submissions do: case and extra spaces don't matter.
    out, events, _ = drive(world, "delete_list_rows",
                           {"list_id": filled["list_name"], "keys": ["  acme   CORP ", "initech", "Missing Co"],
                            "reason": "User asked to drop them."},
                           actor=actor, decision={"approved": True})
    card = cards(events)[0].payload
    assert card["count"] == 2 and card["total"] == len(COMPANIES) and len(card["rows"]) == 2
    assert out["success"] is True and out["status"] == "deleted"
    assert out["deleted"] == 2 and out["remaining"] == len(COMPANIES) - 2 and out["total"] == len(COMPANIES)
    assert out["not_found"] == ["Missing Co"]
    assert names(rows_of(test_client, world, filled)) == ["Globex", "Umbrella"]


def test_approved_delete_by_row_id(test_client, world, filled):
    rows = rows_of(test_client, world, filled)
    target = sorted(rows)[:3]
    out, events, _ = drive(world, "delete_list_rows", {"list_id": filled["list_id"], "row_ids": target},
                           decision={"approved": True})
    assert cards(events)[0].payload["count"] == 3
    assert out["deleted"] == 3
    left = rows_of(test_client, world, filled)
    assert set(left) == set(rows) - set(target)


def test_clear_all_empties_the_list_but_keeps_its_schema(test_client, world, filled):
    out, events, _ = drive(world, "delete_list_rows", {"list_id": filled["list_id"], "all": True},
                           decision={"approved": True})
    assert cards(events)[0].payload["clear_all"] is True
    assert out["deleted"] == len(COMPANIES) and out["remaining"] == 0
    assert rows_of(test_client, world, filled) == {}
    lst = next(l for l in lists_of(test_client, world) if l["id"] == filled["list_id"])
    assert [f["name"] for f in lst["fields"]] == [f["name"] for f in filled["fields"]]


def test_keys_on_a_list_without_a_key_field_are_refused(test_client, world):
    out, _, _ = drive(world, "create_list", _create_input(name="Unkeyed", key_field=None, require_evidence=False),
                      decision={"approved": True})
    assert out["status"] == "created" and not out["key_field"]
    res, events, _ = drive(world, "delete_list_rows", {"list_id": out["list_id"], "keys": ["Acme Corp"]})
    assert res["status"] == "invalid" and res["errors"]
    assert not cards(events)
