"""Audit log filters, search and export (docs/design/audit-log-streams.md, Loop A6)."""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.audit.conftest import h

pytestmark = pytest.mark.e2e

BASE = "/api/enterprise/audit"


def _list(client, who, **params):
    items, page = [], 1
    while True:
        r = client.get(BASE, params={"page": page, "page_size": 100, **params}, headers=h(who["token"], who["org_id"]))
        assert r.status_code == 200, r.text
        body = r.json()
        items += body["items"]
        if page >= max(body["total_pages"], 1):
            return items, body["total"]
        page += 1


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org):
    """Two users in one org acting on two resource types, plus a foreign org."""
    admin = bootstrap_admin()
    second = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"], role="admin")
    second["org_id"] = admin["org_id"]
    title = f"Revenue {uuid.uuid4().hex[:6]}"
    for who in (admin, second):
        r = test_client.post("/api/api_keys", json={"name": "k"}, headers=h(who["token"], admin["org_id"]))
        assert r.status_code == 200, r.text
    rep = test_client.post("/api/reports", json={"title": title}, headers=h(admin["token"], admin["org_id"]))
    assert rep.status_code == 200, rep.text
    other = bootstrap_admin("other")
    test_client.post("/api/api_keys", json={"name": "foreign"}, headers=h(other["token"], other["org_id"]))
    return {"admin": admin, "second": second, "other": other, "title": title}


def test_user_filter_returns_exactly_that_users_rows(test_client, cast):
    admin, second = cast["admin"], cast["second"]
    every, _ = _list(test_client, admin)
    for who in (admin, second):
        rows, total = _list(test_client, admin, user_id=who["user_id"])
        assert total == len(rows) == len([i for i in every if i["user_id"] == who["user_id"]]) > 0
        assert all(i["user_id"] == who["user_id"] for i in rows)


def test_resource_type_filter_supports_one_or_many(test_client, cast):
    admin = cast["admin"]
    every, _ = _list(test_client, admin)
    keys, _ = _list(test_client, admin, resource_type="api_key")
    assert keys and all(i["resource_type"] == "api_key" for i in keys)
    both, total = _list(test_client, admin, resource_type="api_key,report")
    assert total == len([i for i in every if i["resource_type"] in ("api_key", "report")])
    assert {i["resource_type"] for i in both} == {"api_key", "report"}


def test_date_range_filter_bounds_rows(test_client, cast):
    admin = cast["admin"]
    every, _ = _list(test_client, admin)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert _list(test_client, admin, start_date=future)[1] == 0
    assert _list(test_client, admin, start_date=past)[1] == len(every)
    assert _list(test_client, admin, end_date=past)[1] == 0


def test_filters_intersect(test_client, cast):
    admin, second = cast["admin"], cast["second"]
    rows, total = _list(test_client, admin, user_id=second["user_id"], resource_type="api_key")
    assert total >= 1 and all(i["user_id"] == second["user_id"] and i["resource_type"] == "api_key" for i in rows)


def test_search_matches_actor_email_and_details_title(test_client, cast):
    admin, second = cast["admin"], cast["second"]
    local = second["email"].split("@")[0]
    rows, total = _list(test_client, admin, search=local)
    assert total > 0 and all(i["user_email"] == second["email"] for i in rows)
    word = cast["title"].split(" ")[1]
    rows, total = _list(test_client, admin, search=word.upper())  # case-insensitive
    assert total > 0 and all(cast["title"] in str(i["details"]) for i in rows)


def test_resource_types_are_distinct_and_org_scoped(test_client, cast):
    admin = cast["admin"]
    r = test_client.get(f"{BASE}/resource-types", headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200
    types = r.json()
    every, _ = _list(test_client, admin)
    assert sorted(types) == sorted({i["resource_type"] for i in every if i["resource_type"]})
    assert len(types) == len(set(types))


def _export(client, who, fmt="json", **params):
    r = client.get(f"{BASE}/export", params={"format": fmt, **params}, headers=h(who["token"], who["org_id"]))
    assert r.status_code == 200, r.text
    return r


def test_json_export_is_envelope_v1_and_matches_the_list(test_client, cast):
    admin, other = cast["admin"], cast["other"]
    r = _export(test_client, admin, resource_type="api_key")
    assert "attachment" in r.headers["content-disposition"]
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    listed, _ = _list(test_client, admin, resource_type="api_key")
    assert {e["id"] for e in events} == {i["id"] for i in listed}
    assert all(e["version"] == 1 and e["organization"]["id"] == admin["org_id"] for e in events)
    assert [e["occurred_at"] for e in events] == sorted(e["occurred_at"] for e in events)  # oldest first
    foreign, _ = _list(test_client, other)
    assert {e["id"] for e in events}.isdisjoint(i["id"] for i in foreign)


def test_csv_export_has_documented_header_and_same_rows(test_client, cast):
    admin = cast["admin"]
    listed, total = _list(test_client, admin)  # what exists when the export starts
    r = _export(test_client, admin, "csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "occurred_at", "action", "actor_type", "actor_email", "actor_id",
                       "resource_type", "resource_id", "title", "ip_address", "user_agent", "details_json"]
    assert len(rows) - 1 == total == int(r.headers["x-total-count"])
    assert {row[0] for row in rows[1:]} == {i["id"] for i in listed}


def test_csv_export_neutralizes_formula_cells(test_client, bootstrap_admin):
    admin = bootstrap_admin()
    r = test_client.post("/api/reports", json={"title": "=HYPERLINK(\"http://x\")"}, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(_export(test_client, admin, "csv", resource_type="report").text)))
    titles = [row[8] for row in rows[1:] if row[8]]
    assert titles and all(not t.startswith("=") for t in titles)


def test_export_is_audited(test_client, cast):
    admin = cast["admin"]
    _export(test_client, admin, "csv", resource_type="api_key")
    rows, _ = _list(test_client, admin, action="audit_log.exported")
    assert rows and rows[0]["details"]["format"] == "csv"
    assert rows[0]["details"]["filters"]["resource_type"] == "api_key"


def test_export_and_resource_types_are_gated_like_viewing(test_client, cast, invite_user_to_org):
    admin = cast["admin"]
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    mh = h(member["token"], admin["org_id"])
    assert test_client.get(f"{BASE}/export", headers=mh).status_code == 403
    assert test_client.get(f"{BASE}/resource-types", headers=mh).status_code == 403


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_export_at_the_cap_holds_exactly_the_snapshot(test_client, cast, monkeypatch, fmt):
    """Counted rows are the file: the export's own audit_log.exported event
    (written after the count) never pushes it past the cap."""
    import app.ee.audit.routes as routes

    admin = cast["admin"]
    before, total = _list(test_client, admin)
    monkeypatch.setattr(routes, "EXPORT_MAX_ROWS", total)
    r = _export(test_client, admin, fmt)
    if fmt == "json":
        ids = [json.loads(line)["id"] for line in r.text.splitlines() if line.strip()]
    else:
        ids = [row[0] for row in list(csv.reader(io.StringIO(r.text)))[1:]]
    assert len(ids) == total == int(r.headers["x-total-count"])
    assert set(ids) == {i["id"] for i in before}
    exported, _ = _list(test_client, admin, action="audit_log.exported")
    assert exported and exported[0]["id"] not in ids


def test_export_over_the_cap_is_refused(test_client, cast, monkeypatch):
    import app.ee.audit.routes as routes

    monkeypatch.setattr(routes, "EXPORT_MAX_ROWS", 1)
    admin = cast["admin"]
    r = test_client.get(f"{BASE}/export", headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 400
    assert r.json()["error_code"] == "audit_log.export_too_large"
