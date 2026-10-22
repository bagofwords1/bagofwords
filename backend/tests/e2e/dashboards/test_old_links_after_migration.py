"""After the data migration (phase 9), a conversation whose dashboard took over
its link answers through the dashboard (decisions 13 and 15).

The migration leaves the conversation's old sharing and schedule columns as
they were (decision 14), so nothing that reads them changes on upgrade day.
But they no longer decide anything: the /r page forwards to /d, so the old
/r and artifact paths stop admitting readers by them, and the conversations
list reads its badge, schedule icon, filters and "shared with me" from the
conversation's dashboards -- which, right after the migration, say the same.
A conversation without a dashboard (widget era) is served as before.
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, share


def _did(test_client, c, key):
    resp = test_client.get(f"/api/r/{c.reports[key]}/dashboard")
    assert resp.status_code == 200, resp.text
    return resp.json()["dashboard_id"]


def _reports(test_client, c, reader, **params):
    resp = test_client.get("/api/reports", params={"limit": 100, **params}, headers=as_(c, reader))
    assert resp.status_code == 200, resp.text
    return {r["id"]: r for r in resp.json()["reports"]}


def _settings(test_client, c, did, body):
    resp = test_client.put(f"/api/dashboards/{did}/settings", json=body, headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text


OLD_PATHS = ("/api/r/{rid}", "/api/r/{rid}/queries", "/api/r/{rid}/artifacts")


@pytest.mark.e2e
def test_old_conversation_paths_admit_only_its_editors(migrated_corpus, test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    did = _did(test_client, c, "vis_internal")
    for path in OLD_PATHS:
        url = path.format(rid=rid)
        assert test_client.get(url, headers=as_(c, "member")).status_code in (403, 404), url
        assert test_client.get(url, headers=as_(c, "owner")).status_code == 200, url
        assert test_client.get(url, headers=as_(c, "admin")).status_code == 200, url
    # Running and refreshing through the old paths are closed to readers too.
    for path in (f"/api/r/{rid}/run", f"/api/r/{rid}/rerun"):
        assert test_client.post(path, headers=as_(c, "member")).status_code in (401, 403, 404), path
    # The in-app artifact paths read the same old setting.
    assert test_client.get(f"/api/artifacts/report/{rid}", headers=as_(c, "member")).status_code in (403, 404)
    assert test_client.get(f"/api/artifacts/{did}", headers=as_(c, "member")).status_code in (403, 404)
    assert test_client.get(f"/api/artifacts/report/{rid}", headers=as_(c, "owner")).status_code == 200
    # The member reads it where the link now leads.
    assert test_client.get(f"/api/d/{did}", headers=as_(c, "member")).status_code == 200


@pytest.mark.e2e
def test_a_public_conversation_is_read_on_its_dashboard(migrated_corpus, test_client):
    c = migrated_corpus
    rid = c.reports["vis_public"]
    did = _did(test_client, c, "vis_public")
    assert test_client.get(f"/api/r/{rid}/queries").status_code in (401, 403, 404)
    assert test_client.get(f"/api/d/{did}").status_code == 200


@pytest.mark.e2e
def test_a_formerly_public_conversation_may_read_monitoring_data_once_its_dashboard_is_private(
        migrated_corpus, test_client):
    """The conversation's old "public" is only the migration's backup: once
    its dashboard is private, the owner's draft may use the monitoring source
    (a public dashboard still may not -- protect_release)."""
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.services.bow_source_access import protect_report

    c = migrated_corpus
    rid = c.reports["vis_public"]
    assert share(test_client, c, _did(test_client, c, "vis_public"), "none").status_code == 200
    access = {"version": 1, "source_id": "builtin:bow", "organization_id": c.cast.org_id,
              "report_ids": [], "scope_ids": None}

    async def run():
        async with async_session_maker() as db:
            await protect_report(db, rid, access)
            return (await db.get(Report, rid)).bow_source_access

    assert asyncio.run(run())["source_id"] == "builtin:bow"


@pytest.mark.e2e
def test_making_the_dashboard_private_closes_every_way_in(migrated_corpus, test_client):
    c = migrated_corpus
    rid = c.reports["vis_internal"]
    did = _did(test_client, c, "vis_internal")
    assert share(test_client, c, did, "none").status_code == 200
    assert test_client.get(f"/api/d/{did}", headers=as_(c, "member")).status_code in (403, 404)
    for path in OLD_PATHS:
        assert test_client.get(path.format(rid=rid), headers=as_(c, "member")).status_code in (403, 404)


@pytest.mark.e2e
def test_a_conversation_without_a_dashboard_is_served_as_before(migrated_corpus, test_client):
    c = migrated_corpus
    rid = c.reports["legacy_widgets"]
    assert test_client.get(f"/api/r/{rid}/dashboard").status_code == 404
    assert test_client.get(f"/api/r/{rid}").status_code == 200


@pytest.mark.e2e
def test_the_conversations_list_reads_the_dashboard(migrated_corpus, test_client):
    c = migrated_corpus
    rid, did = c.reports["vis_internal"], _did(test_client, c, "vis_internal")

    # Right after the migration: what the list showed before.
    assert _reports(test_client, c, "owner")[rid]["artifact_visibility"] == "internal"
    assert rid in _reports(test_client, c, "owner", status="published")
    assert rid not in _reports(test_client, c, "owner", status="draft")
    assert rid in _reports(test_client, c, "member", filter="shared")

    # Shared on the dashboard: the badge follows.
    assert share(test_client, c, did, "public").status_code == 200
    assert _reports(test_client, c, "owner")[rid]["artifact_visibility"] == "public"

    # Made private on the dashboard: badge, filters and "shared with me" follow.
    assert share(test_client, c, did, "none").status_code == 200
    assert _reports(test_client, c, "owner")[rid]["artifact_visibility"] == "none"
    assert rid not in _reports(test_client, c, "owner", status="published")
    assert rid in _reports(test_client, c, "owner", status="draft")
    assert rid not in _reports(test_client, c, "member", filter="shared")


@pytest.mark.e2e
def test_the_schedule_icon_follows_the_dashboard(migrated_corpus, test_client):
    c = migrated_corpus
    rid, did = c.reports["cron_job"], _did(test_client, c, "cron_job")
    assert _reports(test_client, c, "owner")[rid]["cron_schedule"]
    assert rid in _reports(test_client, c, "owner", scheduled=True)

    _settings(test_client, c, did, {"cron_schedule": ""})
    assert not _reports(test_client, c, "owner")[rid]["cron_schedule"]
    assert rid not in _reports(test_client, c, "owner", scheduled=True)
    assert rid in _reports(test_client, c, "owner", scheduled=False)


@pytest.mark.e2e
def test_a_widget_era_schedule_that_was_removed_shows_no_icon(migrated_corpus, test_client):
    """Decision 7: the widget-era schedule is removed; the list stops saying it runs."""
    c = migrated_corpus
    rid = c.reports["widgets_cron"]
    assert not _reports(test_client, c, "owner")[rid]["cron_schedule"]
    assert rid not in _reports(test_client, c, "owner", scheduled=True)


# ── a conversation made after the upgrade: no recorded link ──────────────

NEW = "conv_three_dash"


def _as_made_after_the_upgrade(corpus_rows, c):
    """Its dashboards own their sharing; the conversation's columns were
    never set (phase 8 shares on the dashboard)."""
    from tests.e2e.dashboards.dashboard_setup import own, publish

    corpus_rows.update("reports", c.reports[NEW], artifact_visibility="none", status="draft")
    sales, stock = own(corpus_rows, c, f"{NEW}/dash_sales"), own(corpus_rows, c, f"{NEW}/dash_stock")
    publish(sales)
    publish(stock)
    return sales, stock


@pytest.mark.e2e
def test_its_link_leads_each_reader_to_a_dashboard_they_can_open(corpus, corpus_rows, test_client):
    c = corpus
    sales, stock = _as_made_after_the_upgrade(corpus_rows, c)
    assert share(test_client, c, sales, "shared", users=["direct_share"]).status_code == 200
    rid = c.reports[NEW]

    def target(reader):
        resp = test_client.get(f"/api/r/{rid}/dashboard", headers=as_(c, reader))
        assert resp.status_code == 200, resp.text
        return resp.json()["dashboard_id"]

    # The newest published page leads, unless the reader can open only another.
    assert target("owner") == stock
    assert target("direct_share") == sales
    # One who can open none stays on the conversation's page (as before).
    assert test_client.get(f"/api/r/{rid}/dashboard").status_code == 404
    # And the conversations list offers it to that reader only.
    assert rid in _reports(test_client, c, "direct_share", filter="shared")
    assert rid not in _reports(test_client, c, "member", filter="shared")
    assert _reports(test_client, c, "owner")[rid]["artifact_visibility"] == "shared"


@pytest.mark.e2e
def test_an_unpublished_dashboard_is_still_its_conversations_door(corpus, corpus_rows, test_client):
    """The editor's handoff and old bookmarks reach /d, which sends editors to
    the draft and tells others it does not exist."""
    from tests.e2e.dashboards.dashboard_setup import own

    c = corpus
    corpus_rows.update("reports", c.reports["conv_one_dash"], artifact_visibility="none", status="draft")
    dash = own(corpus_rows, c, "conv_one_dash/dash")
    resp = test_client.get(f"/api/r/{c.reports['conv_one_dash']}/dashboard", headers=as_(c, "owner"))
    assert resp.status_code == 200 and resp.json()["dashboard_id"] == dash


# ── the project page ─────────────────────────────────────────────────────

def _into_project(corpus_rows, c, key):
    pid = c.cast.projects["private"]
    corpus_rows.update("reports", c.reports[key], project_id=pid)
    corpus_rows.update("artifacts", c.ids[f"{key}/dash"], project_id=pid)
    return pid


def _automations(test_client, c, pid):
    resp = test_client.get(f"/api/projects/{pid}/automations", headers=as_(c, "proj_owner"))
    assert resp.status_code == 200, resp.text
    return [a for a in resp.json() if a["kind"] == "refresh"]


def _automation_count(test_client, c, pid):
    resp = test_client.get(f"/api/projects/{pid}", headers=as_(c, "proj_owner"))
    assert resp.status_code == 200, resp.text
    return resp.json()["automation_count"]


@pytest.mark.e2e
def test_the_projects_automations_are_its_dashboards_schedules(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    pid = _into_project(corpus_rows, c, "cron_job")
    did = _did(test_client, c, "cron_job")
    before = _automation_count(test_client, c, pid)
    (refresh,) = [a for a in _automations(test_client, c, pid) if a["report_id"] == c.reports["cron_job"]]
    assert refresh["cron_schedule"] == "0 8 * * *"
    # Named as main named it: by its conversation (as on the dashboards list).
    corpus_rows.update("reports", c.reports["cron_job"], title="שיחת התזמון")
    (refresh,) = [a for a in _automations(test_client, c, pid) if a["report_id"] == c.reports["cron_job"]]
    assert refresh["label"] == "שיחת התזמון"

    _settings(test_client, c, did, {"cron_schedule": "0 9 * * *"})
    (refresh,) = [a for a in _automations(test_client, c, pid) if a["report_id"] == c.reports["cron_job"]]
    assert refresh["cron_schedule"] == "0 9 * * *"

    _settings(test_client, c, did, {"cron_schedule": ""})
    assert not [a for a in _automations(test_client, c, pid) if a["report_id"] == c.reports["cron_job"]]
    assert _automation_count(test_client, c, pid) == before - 1


# ── the agent's report search ────────────────────────────────────────────

def _search(c, reader, **tool_input):
    import asyncio
    from types import SimpleNamespace

    from app.ai.tools.implementations.search_reports import SearchReportsTool
    from app.dependencies import async_session_maker

    async def run():
        async with async_session_maker() as db:
            ctx = {"db": db, "user": SimpleNamespace(id=c.cast.id(reader)),
                   "organization": SimpleNamespace(id=c.cast.org_id)}
            async for evt in SearchReportsTool().run_stream({"query": "חיפוש הסוכן", **tool_input}, ctx):
                assert evt.type != "tool.error", evt.payload
                if evt.type == "tool.end":
                    return {r["id"]: r for r in evt.payload["output"]["reports"]}

    return asyncio.run(run())


@pytest.mark.e2e
def test_the_agents_report_search_reads_the_dashboards_sharing(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    rid, did = c.reports["vis_internal"], _did(test_client, c, "vis_internal")
    corpus_rows.update("reports", rid, title="חיפוש הסוכן")
    assert _search(c, "owner")[rid]["status"] == "published"
    assert rid in _search(c, "owner", status="published")

    assert share(test_client, c, did, "none").status_code == 200
    assert _search(c, "owner")[rid]["status"] == "draft"
    assert rid not in _search(c, "owner", status="published")
    assert rid in _search(c, "owner", status="draft")
    assert _read(c, "owner", rid)["status"] == "draft"


def _read(c, reader, rid):
    import asyncio
    from types import SimpleNamespace

    from app.ai.tools.implementations.read_report import ReadReportTool
    from app.dependencies import async_session_maker

    async def run():
        async with async_session_maker() as db:
            ctx = {"db": db, "user": SimpleNamespace(id=c.cast.id(reader)),
                   "organization": SimpleNamespace(id=c.cast.org_id)}
            async for evt in ReadReportTool().run_stream({"report_id": rid}, ctx):
                assert evt.type != "tool.error", evt.payload
                if evt.type == "tool.end":
                    return evt.payload["output"]

    return asyncio.run(run())


# ── the dashboards list names an old dashboard as before ─────────────────

def _cards(test_client, c, reader, **params):
    resp = test_client.get("/api/dashboards", params={"limit": 100, **params}, headers=as_(c, reader))
    assert resp.status_code == 200, resp.text
    return {d["id"]: d for d in resp.json()["dashboards"]}


@pytest.mark.e2e
def test_an_old_dashboards_card_keeps_its_conversations_name(migrated_corpus, corpus_rows, test_client):
    """Main's Dashboards page listed conversations, by their names: the
    dashboard an old link leads to keeps that name on its card. Its other
    dashboards, and every dashboard made since, show their own."""
    c = migrated_corpus
    corpus_rows.update("reports", c.reports["conv_three_dash"], title="השיחה של שלושת הלוחות")
    shown = _did(test_client, c, "conv_three_dash")         # the one the old link leads to
    sales = c.ids["conv_three_dash/dash_sales"]
    cards = _cards(test_client, c, "owner")
    assert cards[shown]["title"] == "השיחה של שלושת הלוחות"
    assert cards[sales]["title"] == "לוח מכירות"

    # Found by either name.
    assert shown in _cards(test_client, c, "owner", search="שלושת הלוחות")
    assert shown in _cards(test_client, c, "owner", search="לוח מלאי")
    # A reader it is shared with sees the same name.
    assert _cards(test_client, c, "member", filter="shared")[shown]["title"] == "השיחה של שלושת הלוחות"


# ── deleting an old conversation ─────────────────────────────────────────

@pytest.mark.e2e
def test_deleting_an_old_conversation_does_not_list_its_dashboards_old_versions(migrated_corpus, test_client):
    """artver01 made each old version a dashboard of its own; the delete
    window counts dashboards as the list does (decision 4): the versions of
    a published dashboard are not unpublished dashboards about to go."""
    c = migrated_corpus
    rid = c.reports["artver01_chain"]
    resp = test_client.get(f"/api/reports/{rid}/delete_preview", headers=as_(c, "owner"))
    assert resp.status_code == 200, resp.text
    listed = {d["id"]: d["outcome"] for d in resp.json()["dashboards"]}
    assert c.ids["artver01_chain/legacy_1"] not in listed
    assert c.ids["artver01_chain/legacy_2"] not in listed
    assert listed[c.ids["artver01_chain/legacy_3"]] == "survives"


@pytest.mark.e2e
def test_a_reader_no_dashboard_admits_stays_on_the_old_page(corpus, corpus_rows, test_client):
    """A conversation made since, shared the old way (the API's publish
    toggle), whose dashboards are not shared: /r is not forwarded to a /d
    that would refuse the reader -- the conversation page serves them as
    before."""
    from tests.e2e.dashboards.dashboard_setup import own, publish

    c = corpus
    rid = c.reports[NEW]
    corpus_rows.update("reports", rid, artifact_visibility="public", status="published")
    publish(own(corpus_rows, c, f"{NEW}/dash_sales"))
    assert test_client.get(f"/api/r/{rid}/dashboard").status_code == 404
    assert test_client.get(f"/api/r/{rid}/dashboard", headers=as_(c, "member")).status_code == 404
    assert test_client.get(f"/api/r/{rid}").status_code == 200
    # Its editor is still led to the dashboard.
    assert test_client.get(f"/api/r/{rid}/dashboard", headers=as_(c, "owner")).status_code == 200


@pytest.mark.e2e
@pytest.mark.parametrize("how", ["published", "shared", "scheduled"])
def test_an_old_version_its_owner_put_to_use_is_listed(migrated_corpus, corpus_rows, test_client, how):
    """artver01's old versions are hidden from the list as versions (decision
    4) -- until the owner publishes, shares or schedules one from the editor:
    it is then a dashboard people use, and its card shows."""
    from tests.e2e.dashboards.dashboard_setup import publish

    c = migrated_corpus
    old = c.ids["artver01_chain/legacy_1"]
    assert old not in _cards(test_client, c, "owner", search="לוח מכירות")
    if how == "published":
        publish(old)
    elif how == "shared":
        corpus_rows.update("artifacts", old, visibility="internal")
    else:
        corpus_rows.update("artifacts", old, cron_schedule="0 8 * * *")
    assert old in _cards(test_client, c, "owner", search="לוח מכירות")
    # And deleting its conversation names it if it is about to go.
    listed = {d["id"] for d in test_client.get(f"/api/reports/{c.reports['artver01_chain']}/delete_preview",
                                               headers=as_(c, "owner")).json()["dashboards"]}
    assert old in listed


@pytest.mark.e2e
def test_a_deck_is_not_listed_as_a_projects_automation(migrated_corpus, corpus_rows, test_client):
    """A deck never refreshes (no job is made for it): even with a schedule
    written on it, it is no automation."""
    c = migrated_corpus
    pid = c.cast.projects["private"]
    corpus_rows.update("reports", c.reports["slides_previews"], project_id=pid)
    corpus_rows.update("artifacts", c.ids["slides_previews/deck"], project_id=pid, cron_schedule="0 8 * * *")
    assert not [a for a in _automations(test_client, c, pid) if a["report_id"] == c.reports["slides_previews"]]


@pytest.mark.e2e
def test_a_reader_sees_the_published_picture_not_the_drafts(migrated_corpus, corpus_rows, test_client):
    """The conversations list pictures a conversation by its newest version:
    on main that was what /r showed. Its readers now see the published
    release, so the picture they get is the release's -- never a draft's
    changes not yet published. The owner keeps the draft's."""
    c = migrated_corpus
    rid, did = c.reports["vis_internal"], _did(test_client, c, "vis_internal")
    (version,) = _rows_of(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a", a=did)
    corpus_rows.update("artifact_versions", version["id"], thumbnail_path="thumbnails/draft-picture.png")
    release = _rows_of(corpus_rows, "SELECT published_release_id AS r FROM artifacts WHERE id = :a", a=did)[0]["r"]
    corpus_rows.update("artifact_releases", release, thumbnail_path="thumbnails/release-picture.png")

    assert _reports(test_client, c, "owner")[rid]["thumbnail_url"] == "/thumbnails/draft-picture.png"
    assert _reports(test_client, c, "member", filter="shared")[rid]["thumbnail_url"] == "/thumbnails/release-picture.png"
    corpus_rows.update("artifact_releases", release, thumbnail_path=None)
    assert _reports(test_client, c, "member", filter="shared")[rid]["thumbnail_url"] is None


def _rows_of(corpus_rows, sql, **params):
    from sqlalchemy import text

    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


@pytest.mark.e2e
def test_an_old_schedule_does_not_come_back_when_the_dashboard_goes(migrated_corpus, corpus_rows, test_client):
    """The migration keeps the conversation's schedule column as a backup
    (decision 14); once a dashboard took the conversation over, the column
    says nothing -- also after that dashboard is deleted (no job runs it)."""
    c = migrated_corpus
    rid, did = c.reports["cron_job"], _did(test_client, c, "cron_job")
    pid = _into_project(corpus_rows, c, "cron_job")
    corpus_rows.update("artifacts", did, deleted_at=corpus_rows.clock())
    assert not _reports(test_client, c, "owner")[rid]["cron_schedule"]
    assert rid not in _reports(test_client, c, "owner", scheduled=True)
    assert rid not in _reports(test_client, c, "owner", status="published")
    assert not [a for a in _automations(test_client, c, pid) if a["report_id"] == rid]


@pytest.mark.e2e
@pytest.mark.parametrize("how", ["archived", "detached"])
def test_a_scheduled_dashboard_whose_conversation_went_is_still_an_automation(
        migrated_corpus, corpus_rows, test_client, how):
    """Published, it keeps refreshing and emailing after its conversation is
    deleted (phase 7): the project lists it, linked to the dashboard."""
    c = migrated_corpus
    pid = _into_project(corpus_rows, c, "cron_job")
    rid, did = c.reports["cron_job"], _did(test_client, c, "cron_job")
    before = _automation_count(test_client, c, pid)
    if how == "archived":
        corpus_rows.update("reports", rid, status="archived")
    else:
        corpus_rows.update("artifacts", did, report_id=None)
    (auto,) = [a for a in _automations(test_client, c, pid) if a.get("dashboard_id") == did]
    assert auto["cron_schedule"] == "0 8 * * *"
    assert auto["report_id"] is None  # nothing links to the conversation that went
    assert _automation_count(test_client, c, pid) == before


@pytest.mark.e2e
def test_its_link_does_not_move_when_its_dashboard_is_deleted(migrated_corpus, corpus_rows, test_client):
    """The dashboard an old link leads to was deleted: the link does not lead
    to another dashboard of the conversation (what it showed is gone)."""
    c = migrated_corpus
    rid = c.reports["conv_three_dash"]
    did = _did(test_client, c, "conv_three_dash")
    corpus_rows.update("artifacts", did, deleted_at=corpus_rows.clock())
    assert test_client.get(f"/api/r/{rid}/dashboard", headers=as_(c, "owner")).status_code == 404


@pytest.mark.e2e
def test_a_readers_picture_comes_from_a_dashboard_they_can_open(corpus, corpus_rows, test_client):
    """Two published dashboards in one conversation: one for the
    organization, a newer one shared with one person. Another member sees
    the conversation through the first -- and gets its picture, never the
    other's."""
    from tests.e2e.dashboards.dashboard_setup import own, publish

    c = corpus
    rid = c.reports[NEW]
    corpus_rows.update("reports", rid, artifact_visibility="none", status="draft")
    sales, stock = own(corpus_rows, c, f"{NEW}/dash_sales"), own(corpus_rows, c, f"{NEW}/dash_stock")
    sales_release, stock_release = publish(sales), publish(stock)
    corpus_rows.update("artifact_releases", sales_release, thumbnail_path="thumbnails/for-everyone.png")
    corpus_rows.update("artifact_releases", stock_release, thumbnail_path="thumbnails/for-one.png")
    assert share(test_client, c, sales, "internal").status_code == 200
    assert share(test_client, c, stock, "shared", users=["direct_share"]).status_code == 200
    assert _reports(test_client, c, "member", filter="shared")[rid]["thumbnail_url"] == "/thumbnails/for-everyone.png"
    assert _reports(test_client, c, "direct_share", filter="shared")[rid]["thumbnail_url"] == "/thumbnails/for-one.png"
