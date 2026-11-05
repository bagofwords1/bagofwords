"""The dashboards page's counts, and the org admin's "needs attention"
(dashboards list findability, P7).

`GET /api/artifacts/facets` takes the list's own query and answers how many
dashboards each filter value would list:

- `tabs`: My and Shared, by the tab alone (no filter applies);
- `access` (Shared only): each reason chip;
- `agents`: each agent the reader may see named, by the card's rule;
- `owners` (Shared only): each owner, by name (else email) -- never a
  dashboard's title or id;
- `status` (My only): published, with unpublished changes, draft;
- `new_count`: Shared's New cards, no filter applied;
- `attention` (org admins only, else null): the organization's dashboards
  (Shared, `access=org`) whose last refresh failed, or not edited in 90 days.

Each count leaves its own filter out and keeps every other, so it equals the
list's `meta.total` with that value applied. Both count the SQL candidates,
so both are upper bounds of what the reader can open (Known limitations 3).

`attention=true` narrows the list (and the facets) to those dashboards; for
anyone but an org admin it is a 403, on both routes.

Times, refresh outcomes and monitoring lineage are written directly where no
route makes them (each place says so).
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish
from tests.e2e.dashboards.test_dashboard_list_query import (
    CHIPS,
    CONV,
    DECK,
    MANY,
    ONE,
    PAGE_DASH,
    PAGE_DOC,
    SALES,
    STOCK,
    _date,
    _new_version,
    _reasons,
    _rows,
    _sharing,
    _star,
)

MULTI = "multi_source/dash"  # its conversation has two agents
STATUSES = ("published", "changes", "draft")
FACET_KEYS = {"tabs", "access", "agents", "owners", "status", "new_count", "attention"}


# ── helpers ───────────────────────────────────────────────────────────────

def _facets_resp(test_client, corpus, person, **params):
    return test_client.get("/api/artifacts/facets", params=params, headers=as_(corpus, person))


def _facets(test_client, corpus, person, **params) -> dict:
    resp = _facets_resp(test_client, corpus, person, **params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _list(test_client, corpus, person, **params) -> dict:
    params.setdefault("limit", 100)
    resp = test_client.get("/api/artifacts", params=params, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _total(test_client, corpus, person, **params) -> int:
    return _list(test_client, corpus, person, **params)["meta"]["total"]


def _world(test_client, corpus, corpus_rows) -> dict:
    """The reasons of test_dashboard_list_query (sales: direct_share by name;
    stock: the analysts group; deck: the private project, a deck; one: the
    organization's; many: member may edit it), and two more of the
    organization's: `multi` (two agents) and `page` (by proj_owner). Sales
    has unpublished changes; some are created in 2025, some starred."""
    d = _reasons(test_client, corpus, corpus_rows)
    d["multi"] = own(corpus_rows, corpus, MULTI)
    d["page"] = own(corpus_rows, corpus, PAGE_DASH, owner="proj_owner")
    for key, by in (("multi", "owner"), ("page", "proj_owner")):
        publish(d[key])
        _sharing(test_client, corpus, d[key], "internal", by=by)
    asyncio.run(_new_version(d["sales"], corpus.reports[CONV], corpus.cast.id("owner")))
    for n, key in enumerate(("sales", "one", "page")):
        _date(corpus_rows, d[key], created=datetime(2025, 6, 1 + n))
    for n, key in enumerate(("stock", "deck", "many", "multi")):
        _date(corpus_rows, d[key], created=datetime(2026, 2, 1 + n))
    _star(test_client, corpus, "member", d["one"])
    _star(test_client, corpus, "proj_group", d["deck"])
    _star(test_client, corpus, "owner", d["stock"])
    return d


SHARED_MIXES = (
    {}, {"mode": "page"}, {"access": "org"}, {"search": "proj_owner"}, {"starred": "true"},
    {"created_from": "2026-01-01T00:00:00Z"}, {"mode": "page", "created_to": "2026-01-01T00:00:00Z"},
)
MY_MIXES = ({}, {"mode": "page"}, {"starred": "true"}, {"created_from": "2026-01-01T00:00:00Z"},
            {"search": "zzz-no-such-title"})


# ── each count is the list's total with its value applied ─────────────────

@pytest.mark.e2e
def test_each_facet_count_equals_the_list_total_with_it_applied(corpus, corpus_rows, test_client):
    _world(test_client, corpus, corpus_rows)
    agents = sorted(corpus.cast.data_sources.values())
    seen = {"access": 0, "agents": 0, "owners": 0, "status": 0}

    for person in ("member", "direct_share", "group_share", "proj_group"):
        for mix in SHARED_MIXES:
            f = _facets(test_client, corpus, person, filter="shared", **mix)
            other = {k: v for k, v in mix.items() if k != "access"}
            for chip in CHIPS:
                expected = _total(test_client, corpus, person, filter="shared", access=chip, **other)
                assert f["access"][chip] == expected, (person, mix, chip)
                seen["access"] += expected
            counted = {a["id"]: a["count"] for a in f["agents"]}
            for agent in agents:
                expected = _total(test_client, corpus, person, filter="shared", agent_ids=agent, **mix)
                assert counted.get(agent, 0) == expected, (person, mix, agent)
                seen["agents"] += expected
            assert all(n > 0 for n in counted.values()), f["agents"]
            for owner in f["owners"]:
                expected = _total(test_client, corpus, person, filter="shared", owner_ids=owner["id"], **mix)
                assert owner["count"] == expected and expected > 0, (person, mix, owner)
                seen["owners"] += expected
            # Each dashboard has one owner: the owners never reach wider than the list.
            assert sum(o["count"] for o in f["owners"]) == _total(
                test_client, corpus, person, filter="shared", **mix), (person, mix)

    for mix in MY_MIXES:
        f = _facets(test_client, corpus, "owner", filter="my", **mix)
        for status in STATUSES:
            expected = _total(test_client, corpus, "owner", filter="my", status=status, **mix)
            assert f["status"][status] == expected, (mix, status)
            seen["status"] += expected
        assert sum(f["status"].values()) == _total(test_client, corpus, "owner", filter="my", **mix), mix
        counted = {a["id"]: a["count"] for a in f["agents"]}
        for agent in agents:
            expected = _total(test_client, corpus, "owner", filter="my", agent_ids=agent, **mix)
            assert counted.get(agent, 0) == expected, (mix, agent)

    assert all(seen.values()), seen  # every kind of count was non-zero somewhere


@pytest.mark.e2e
def test_a_facet_ignores_its_own_filter(corpus, corpus_rows, test_client):
    d = _world(test_client, corpus, corpus_rows)
    chinook = corpus.cast.data_sources["chinook"]
    proj_owner = corpus.cast.id("proj_owner")
    plain = _facets(test_client, corpus, "group_share", filter="shared")
    assert plain["access"]["all"] > plain["access"]["group"] > 0

    narrowed = _facets(test_client, corpus, "group_share", filter="shared", access="group")
    assert narrowed["access"] == plain["access"]
    assert narrowed["owners"] != plain["owners"]  # the other facets do take it

    by_agent = _facets(test_client, corpus, "group_share", filter="shared", agent_ids=chinook)
    assert by_agent["agents"] == plain["agents"]
    assert len(plain["agents"]) > 1

    by_owner = _facets(test_client, corpus, "group_share", filter="shared", owner_ids=proj_owner)
    assert by_owner["owners"] == plain["owners"]
    assert len(plain["owners"]) > 1
    assert by_owner["access"] != plain["access"]

    mine = _facets(test_client, corpus, "owner", filter="my")
    assert all(mine["status"].values()), mine["status"]
    for status in STATUSES:
        assert _facets(test_client, corpus, "owner", filter="my", status=status)["status"] == mine["status"]
    assert d["sales"] in {c["id"] for c in _list(test_client, corpus, "owner", filter="my",
                                                    status="changes")["dashboards"]}


@pytest.mark.e2e
def test_tab_totals_and_new_count(corpus, corpus_rows, test_client):
    d = _world(test_client, corpus, corpus_rows)
    for person in ("owner", "member", "group_share"):
        tabs = {"my": _total(test_client, corpus, person, filter="my"),
                "shared": _total(test_client, corpus, person, filter="shared")}
        for tab in ("my", "shared"):
            # The tab alone: the other filters do not move them.
            for mix in ({}, {"mode": "slides"}, {"starred": "true"}, {"search": "zzz-no-such-title"}):
                assert _facets(test_client, corpus, person, filter=tab, **mix)["tabs"] == tabs, (person, tab, mix)
    assert _facets(test_client, corpus, "owner")["tabs"] == {"my": len(d) - 1, "shared": 1}  # page is proj_owner's

    # No watermark yet: nothing is New.
    assert _facets(test_client, corpus, "member", filter="shared")["new_count"] == 0
    # Every share and publish here is after 2000 and before 2100.
    for since, person in (("2000-01-01T00:00:00Z", "member"), ("2000-01-01T00:00:00Z", "group_share")):
        cards = _list(test_client, corpus, person, filter="shared", new_since=since)["dashboards"]
        new = sum(1 for c in cards if c["is_new"])
        assert new > 0, person
        for mix in ({}, {"mode": "slides"}, {"access": "direct"}):
            for tab in ("my", "shared"):
                got = _facets(test_client, corpus, person, filter=tab, new_since=since, **mix)["new_count"]
                assert got == new, (person, tab, mix)
    assert _facets(test_client, corpus, "member", filter="shared",
                   new_since="2100-01-01T00:00:00Z")["new_count"] == 0


@pytest.mark.e2e
def test_access_counts_only_on_shared_and_status_only_on_my(corpus, corpus_rows, test_client):
    _world(test_client, corpus, corpus_rows)
    mine = _facets(test_client, corpus, "owner", filter="my", access="direct")
    assert set(mine) == FACET_KEYS
    assert mine["access"] is None and mine["owners"] == []
    assert set(mine["status"]) == set(STATUSES)
    for status in (None, "changes"):
        params = {"status": status} if status else {}
        shared = _facets(test_client, corpus, "member", filter="shared", **params)
        assert shared["status"] is None
        assert set(shared["access"]) == set(CHIPS) and shared["access"]["all"] > 0
        assert shared["owners"]


@pytest.mark.e2e
def test_the_agents_facet_names_only_agents_the_reader_may_see(corpus, corpus_rows, test_client,
                                                               sqlite_data_source):
    """As the card (default 1): an agent the reader cannot view, or that the
    agents page hides (disabled), is never in the facet -- though the
    dashboard has it."""
    chinook = corpus.cast.data_sources["chinook"]
    admin = corpus.cast.people["admin"]
    private = sqlite_data_source(name="Private agent", user_token=admin.token, org_id=corpus.cast.org_id,
                                 is_public=False)["id"]
    disabled = sqlite_data_source(name="Disabled agent", user_token=admin.token, org_id=corpus.cast.org_id,
                                  is_public=True)["id"]
    resp = test_client.put(f"/api/data_sources/{disabled}", json={"publish_status": "disabled"},
                           headers=as_(corpus, "admin"))
    assert resp.status_code == 200, resp.text
    sales = own(corpus_rows, corpus, SALES)
    own(corpus_rows, corpus, STOCK)  # a draft of the same conversation: My counts it too
    # Direct writes: a conversation's agents change only inside an agent run;
    # the release takes them when it is published.
    for dsid in (private, disabled):
        corpus_rows.insert("report_data_source_association", report_id=corpus.reports[CONV], data_source_id=dsid)
    publish(sales)
    _sharing(test_client, corpus, sales, "internal")

    def counted(person, tab):
        return {a["id"]: a["count"] for a in _facets(test_client, corpus, person, filter=tab)["agents"]}

    assert counted("member", "shared") == {chinook: 1}
    assert counted("owner", "my") == {chinook: 2}  # no grant on the private one either
    # The org admin views every agent; a disabled one is off for everyone.
    assert counted("admin", "shared") == {chinook: 1, private: 1}


# ── the org admin's "needs attention" ─────────────────────────────────────

@pytest.mark.e2e
def test_attention_is_for_org_admins_only(corpus, corpus_rows, test_client):
    d = _world(test_client, corpus, corpus_rows)
    # Direct writes (no route back-dates an edit): of the admin's Shared
    # dashboards (the organization's: one, multi, page) only `one` is stale;
    # none failed. The admin owns nothing.
    recent = datetime.utcnow() - timedelta(days=1)
    _date(corpus_rows, d["one"], edited=datetime(2000, 1, 1))
    for key in ("multi", "page"):
        _date(corpus_rows, d[key], edited=recent)
    for key in ("one", "multi", "page"):
        corpus_rows.update("artifacts", d[key], last_run_status="success")
    # The counts are over Shared + access=org whatever the tab; the list with
    # access=org matches them (the admin views every project, so Shared's
    # other reasons are not pinned here).
    for tab, expected in (("my", 0), ("shared", 1)):
        admin = _facets(test_client, corpus, "admin", filter=tab)
        assert admin["attention"] == {"failing": 0, "stale": 1, "total": 1}, admin["attention"]
        listed = _list(test_client, corpus, "admin", filter=tab, access="org", attention="true")
        assert listed["meta"]["total"] == expected, tab
        assert _facets_resp(test_client, corpus, "admin", filter=tab, attention="true").status_code == 200
    # bow_scoped holds manage_settings: an admin of some things, not the organization's.
    for person in ("member", "owner", "bow_scoped"):
        for tab in ("my", "shared"):
            assert _facets(test_client, corpus, person, filter=tab)["attention"] is None, (person, tab)
            for path in ("/api/artifacts", "/api/artifacts/facets"):
                resp = test_client.get(path, params={"filter": tab, "attention": "true"}, headers=as_(corpus, person))
                assert resp.status_code == 403, (person, tab, path, resp.status_code, resp.text)
            # attention=false is simply no filter.
            assert test_client.get("/api/artifacts", params={"filter": tab, "attention": "false"},
                                   headers=as_(corpus, person)).status_code == 200


@pytest.mark.e2e
def test_attention_lists_failing_or_not_edited_in_90_days(corpus, corpus_rows, test_client):
    keys = {"failing": SALES, "stale": STOCK, "fresh": ONE, "both": MANY, "old_created": PAGE_DASH,
            "by_name": DECK, "day_89": PAGE_DOC, "day_91": MULTI}
    d = {k: own(corpus_rows, corpus, key) for k, key in keys.items()}
    for key, aid in d.items():
        publish(aid)
        if key == "by_name":
            _sharing(test_client, corpus, aid, "shared", users=["admin"])  # not the organization's
        else:
            _sharing(test_client, corpus, aid, "internal")
    # Direct writes: no route back-dates an edit or records a failed refresh
    # without running one. "Recent" is a day ago and "old" in 2000: far from
    # the 90-day line whenever the test runs. The line itself: 89 days ago is
    # not stale, 91 days ago is (a day either side: no clock race).
    now = datetime.utcnow()
    recent, old = now - timedelta(days=1), datetime(2000, 1, 1)
    for key in ("failing", "fresh", "by_name"):
        _date(corpus_rows, d[key], edited=recent)
    _date(corpus_rows, d["day_89"], edited=now - timedelta(days=89))
    _date(corpus_rows, d["day_91"], edited=now - timedelta(days=91))
    for key in ("stale", "both"):
        _date(corpus_rows, d[key], edited=old)
    # Never edited since it was made, long ago: Edited is its creation.
    corpus_rows.update("artifacts", d["old_created"], content_updated_at=None, created_at=old)
    for key in ("failing", "both", "by_name"):
        corpus_rows.update("artifacts", d[key], last_run_status="failed")
    for key in ("fresh", "day_89", "day_91"):
        corpus_rows.update("artifacts", d[key], last_run_status="success")

    f = _facets(test_client, corpus, "admin", filter="shared")
    # `both` is failing and stale: counted once in the total.
    assert f["attention"] == {"failing": 2, "stale": 4, "total": 5}, f["attention"]
    listed = _list(test_client, corpus, "admin", filter="shared", access="org", attention="true")
    assert {c["id"] for c in listed["dashboards"]} == {
        d[k] for k in ("failing", "stale", "both", "old_created", "day_91")}
    assert listed["meta"]["total"] == 5
    # It narrows the other facets too, and ignores itself.
    narrowed = _facets(test_client, corpus, "admin", filter="shared", access="org", attention="true")
    assert narrowed["attention"] == f["attention"]
    assert narrowed["access"]["org"] == 5
    # The other filters do apply to the counts.
    assert _facets(test_client, corpus, "admin", filter="shared", mode="slides")["attention"] == {
        "failing": 0, "stale": 0, "total": 0}


# ── the route ─────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_facets_route_is_not_taken_by_artifact_id(corpus, corpus_rows, test_client):
    own(corpus_rows, corpus, SALES)
    for person in ("owner", "member"):
        body = _facets(test_client, corpus, person)
        assert set(body) == FACET_KEYS
        assert set(body["tabs"]) == {"my", "shared"}
    assert _facets_resp(test_client, corpus, "anonymous").status_code in (401, 403)
    assert _facets_resp(test_client, corpus, "owner", sort="newest").status_code == 422


# ── upper bounds, together ────────────────────────────────────────────────

@pytest.mark.e2e
def test_facets_and_total_are_upper_bounds_together(corpus, corpus_rows, test_client):
    """A monitoring dashboard whose lineage member cannot read (as
    test_pr1455_monitoring_lineage.py restricts one) is a candidate: counted
    in meta.total and in the facets alike, never on a page -- and nothing in
    the facets names it."""
    hidden, shown = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, ONE)
    releases = {aid: publish(aid) for aid in (hidden, shown)}
    for aid in (hidden, shown):
        _sharing(test_client, corpus, aid, "internal")
    secret = f"monitoring board {uuid.uuid4().hex}"
    # Direct writes: the lineage a monitoring run records on the release
    # (protect_release), org-wide scope only; and a title to look for.
    corpus_rows.update("artifact_releases", releases[hidden], bow_source_access={
        "version": 1, "source_id": "builtin:bow", "organization_id": corpus.cast.org_id,
        "report_ids": [], "scope_ids": None})
    corpus_rows.update("artifacts", hidden, title=secret)
    (row,) = _rows(corpus_rows, "SELECT title FROM artifacts WHERE id = :a", a=shown)

    body = _list(test_client, corpus, "member", filter="shared")
    assert [c["id"] for c in body["dashboards"]] == [shown]
    assert body["meta"]["total"] == 2
    resp = _facets_resp(test_client, corpus, "member", filter="shared")
    assert resp.status_code == 200, resp.text
    f = resp.json()
    assert f["tabs"]["shared"] == 2
    assert f["access"]["all"] == 2 and f["access"]["org"] == 2
    assert [o["count"] for o in f["owners"]] == [2]
    chinook = corpus.cast.data_sources["chinook"]
    assert {a["id"]: a["count"] for a in f["agents"]}.get(chinook) == 2
    assert _total(test_client, corpus, "member", filter="shared", agent_ids=chinook) == 2
    for word in (hidden, secret, shown, row["title"]):
        assert word not in resp.text, word
