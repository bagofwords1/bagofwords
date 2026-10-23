"""Collaborative dashboards (collab01;
docs/feedback-loops/2026-10-23-collaborative-dashboards.md).

The world: the corpus's owner ("Dana", `owner`) and her dashboards; `member`
("Avi") made an editor by a grant; `group_share` an editor through a group;
`proj_viewer` an editor through the dashboard's project; `direct_share` a
viewer; the org admin; a stranger from another organization; anonymous.

The rule every test here leans on: a result reaches only the person whose
credentials produced it. An editor who joins gets the dashboard's structure,
never another person's rows; their copies run again as them.
"""
import asyncio
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own, publish, share

CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"
# A value only the owner's rows hold: it must never reach anyone else.
MARKER = "סוד-של-דנה-7731"
SECRET_ROWS = {"rows": [{"חודש": MARKER, "הכנסות": 424242}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}


@pytest.fixture
def drawn(monkeypatch):
    from app.services.thumbnail_service import ThumbnailService
    from tests.fixtures.dashboard_corpus.kinds import TINY_PNG

    monkeypatch.setattr(ThumbnailService, "_build_thumbnail_html", lambda self, **kw: "")

    async def draw(self, artifact_id, html_content, mode="page"):
        (self.UPLOADS_DIR / f"{artifact_id}.png").write_bytes(TINY_PNG)
        return f"thumbnails/{artifact_id}.png"

    monkeypatch.setattr(ThumbnailService, "generate_thumbnail", draw)


@pytest.fixture
def spawned(monkeypatch, drawn):
    """The background runs a route starts, run when the test says."""
    jobs = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", jobs.append)
    return jobs


def _finish(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            asyncio.run(job)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _grant_editor(test_client, corpus, aid, *people, by="owner", groups=()):
    resp = test_client.put(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, by), json={
        "visibility": _visibility(test_client, corpus, aid),
        "editor_user_ids": [corpus.cast.id(p) for p in people],
        "editor_group_ids": [corpus.cast.groups[g] for g in groups]})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _visibility(test_client, corpus, aid):
    return test_client.get(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, "owner")).json()["visibility"]


def _edit(test_client, corpus, aid, person, **body):
    return test_client.post(f"/api/dashboards/{aid}/edit", json=body or None, headers=as_(corpus, person))


def _publish_api(test_client, corpus, aid, person="owner", **body):
    resp = test_client.post(f"/api/dashboards/{aid}/publish", json=body or None, headers=as_(corpus, person))
    if resp.status_code == 202:
        PUBLISHED.append(resp.json()["latest"]["id"])
    return resp


def _steps_of(corpus_rows, report_id):
    return _rows(corpus_rows, "SELECT s.id, s.status, s.data, s.code FROM queries q JOIN steps s "
                              "ON s.id = q.default_step_id WHERE q.report_id = :r", r=report_id)


def _seed_secret(corpus_rows, corpus, conv=CONV):
    """The owner's step rows hold the marker."""
    for key, value in corpus.ids.items():
        if key.startswith(f"{conv}/") and key.endswith("/step"):
            corpus_rows.update("steps", value, data=SECRET_ROWS)


async def _new_version(artifact_id: str, report_id: str, user_id: str, *, retitle_viz: str | None = None) -> int:
    """A new version of the dashboard in one conversation, as the agent's
    edit tools write it (no route writes a version without an LLM run)."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.artifact import ArtifactVersion
    from app.models.query import Query
    from app.models.visualization import Visualization
    from app.services.artifact_service import new_version

    async with async_session_maker() as db:
        source = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id,
                                          ArtifactVersion.report_id == report_id,
                                          ArtifactVersion.status == "completed")
            .order_by(ArtifactVersion.version.desc()).limit(1))).scalar_one()
        if retitle_viz:
            ids = [str(v) for v in (source.content or {}).get("visualization_ids") or []]
            viz = (await db.execute(select(Visualization).join(Query, Query.id == Visualization.query_id)
                                    .where(Query.report_id == report_id,
                                           Visualization.id.in_(ids) if ids else True))).scalars().first()
            viz.title = retitle_viz
            await db.flush()
        version = await new_version(db, source, content=dict(source.content or {}), user_id=user_id)
        await db.commit()
        return version.version


def new_version(artifact_id, report_id, user_id, **kw) -> int:
    return asyncio.run(_new_version(artifact_id, report_id, user_id, **kw))


# ── roles ──────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_an_edit_grant_makes_an_editor_before_the_first_publish(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    sharing = _grant_editor(test_client, corpus, aid, "member", groups=("analysts",))
    assert sharing["editor_user_ids"] == [corpus.cast.id("member")]
    assert sharing["editor_group_ids"] == [corpus.cast.groups["analysts"]]
    assert sharing["visibility"] == "none" and sharing["user_ids"] == []

    for person in ("member", "group_share"):
        draft = test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, person))
        assert draft.status_code == 200, (person, draft.text)
        assert {"edit", "publish", "share", "settings", "analytics"} <= set(draft.json()["capabilities"])
        assert not {"make_public", "run_identity", "transfer"} & set(draft.json()["capabilities"])
        listed = test_client.get("/api/dashboards?filter=shared", headers=as_(corpus, person)).json()
        assert [(c["id"], c["role"]) for c in listed["dashboards"]] == [(aid, "edit")]
    # Someone with no grant still gets nothing.
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "direct_share")).status_code == 404


@pytest.mark.e2e
def test_what_each_role_may_do(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH, project="private")
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    _grant_editor(test_client, corpus, aid, "member")
    settings = f"/api/dashboards/{aid}/settings"
    sharing = f"/api/dashboards/{aid}/sharing"

    # Editors (a grant, the project) change settings and notes, not the
    # credentials viewers run with.
    for editor in ("member", "proj_viewer"):
        ok = test_client.put(settings, json={"notes": f"by {editor}"}, headers=as_(corpus, editor))
        assert ok.status_code == 200 and ok.json()["notes"] == f"by {editor}"
        denied = test_client.put(settings, json={"shared_run_identity": "creator"}, headers=as_(corpus, editor))
        assert denied.status_code == 403
        public = test_client.put(sharing, json={"visibility": "public"}, headers=as_(corpus, editor))
        assert public.status_code == 403 and public.json()["error_code"] == "dashboard.public_owner_only"
        assert test_client.post(f"/api/dashboards/{aid}/transfer", json={"user_id": corpus.cast.id(editor)},
                                headers=as_(corpus, editor)).status_code == 403
    # A viewer and a stranger change nothing.
    for outsider in ("direct_share", "other_org"):
        assert test_client.put(settings, json={"notes": "x"}, headers=as_(corpus, outsider)).status_code in (403, 404)
        assert test_client.get(f"/api/dashboards/{aid}/history",
                               headers=as_(corpus, outsider)).status_code in (403, 404)
    viewer = test_client.get(f"/api/d/{aid}", headers=as_(corpus, "direct_share")).json()
    assert viewer["capabilities"] == ["view"]

    # Editors share unless the owner turns it off -- the owner's switch alone.
    assert test_client.put(sharing, json={"visibility": "shared", "editors_can_share": False},
                           headers=as_(corpus, "member")).status_code == 403
    assert test_client.put(sharing, json={"visibility": "shared", "editors_can_share": False},
                           headers=as_(corpus, "owner")).status_code == 200
    assert test_client.put(sharing, json={"visibility": "shared", "user_ids": []},
                           headers=as_(corpus, "member")).status_code == 403

    # A dashboard that runs as its creator: only the owner publishes it.
    assert test_client.put(settings, json={"shared_run_identity": "creator"},
                           headers=as_(corpus, "owner")).status_code == 200
    refused = _publish_api(test_client, corpus, aid, "member")
    assert refused.status_code == 403 and refused.json()["error_code"] == "dashboard.creator_publish_owner_only"


@pytest.mark.e2e
def test_ownership_moves_to_an_editor_and_the_previous_owner_stays_one(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    bad = test_client.post(f"/api/dashboards/{aid}/transfer", json={"user_id": corpus.cast.id("direct_share")},
                           headers=as_(corpus, "owner"))
    assert bad.status_code == 400 and bad.json()["error_code"] == "dashboard.transfer_not_editor"
    moved = test_client.post(f"/api/dashboards/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                             headers=as_(corpus, "owner"))
    assert moved.status_code == 200, moved.text
    assert moved.json()["owner_id"] == corpus.cast.id("member")
    assert moved.json()["editor_user_ids"] == [corpus.cast.id("owner")]
    caps = set(test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).json()["capabilities"])
    assert "edit" in caps and "transfer" not in caps


# ── joining: structure, never another person's rows ───────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("kind", [CONV, "user_required", "rls"])
def test_a_joining_editor_never_receives_the_owners_rows(corpus, corpus_rows, test_client, spawned, kind):
    aid = own(corpus_rows, corpus, f"{kind}/dash" if kind != CONV else DASH)
    _seed_secret(corpus_rows, corpus, kind)
    _grant_editor(test_client, corpus, aid, "member")

    resp = _edit(test_client, corpus, aid, "member")
    assert resp.status_code == 200, resp.text
    joined = resp.json()
    assert joined["created"] is True and joined["rerunning"] is True
    report_id = joined["report_id"]
    assert report_id != corpus.reports[kind]
    (conv_owner,) = _rows(corpus_rows, "SELECT user_id FROM reports WHERE id = :r", r=report_id)
    assert conv_owner["user_id"] == corpus.cast.id("member")

    steps = _steps_of(corpus_rows, report_id)
    assert steps and all(s["status"] == "pending" for s in steps)
    assert MARKER not in json.dumps(_rows(corpus_rows, "SELECT data FROM steps s JOIN queries q ON "
                                                       "q.id = s.query_id WHERE q.report_id = :r", r=report_id),
                                    ensure_ascii=False, default=str)
    (row,) = _rows(corpus_rows, "SELECT summary FROM artifact_conversations WHERE report_id = :r", r=report_id)
    assert MARKER not in json.dumps(row["summary"], ensure_ascii=False, default=str)
    card = test_client.get(f"/api/dashboards/conversations/{report_id}", headers=as_(corpus, "member"))
    assert card.status_code == 200 and MARKER not in card.text and card.json()[0]["rerunning"] is True
    draft = test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "member")).json()
    assert draft["my_report_id"] == report_id

    # The copies run as the editor (Fork's hydration, never retiring this
    # conversation); still no marker, whatever the run gave.
    _finish(spawned)
    after = _steps_of(corpus_rows, report_id)
    assert all(s["status"] in ("success", "error") for s in after)
    assert MARKER not in json.dumps(after, ensure_ascii=False, default=str)
    (status,) = _rows(corpus_rows, "SELECT status FROM reports WHERE id = :r", r=report_id)
    assert status["status"] != "archived"


@pytest.mark.e2e
def test_the_owner_rejoining_their_own_dashboard_keeps_their_rows(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    _seed_secret(corpus_rows, corpus)
    resp = _edit(test_client, corpus, aid, "owner")
    assert resp.json() == {"report_id": corpus.reports[CONV], "created": False, "rerunning": False}


@pytest.mark.e2e
def test_joining_needs_access_to_every_source(corpus, corpus_rows, test_client, monkeypatch):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    from app.core import permission_resolver

    async def no_access(*args, **kwargs):
        return False

    monkeypatch.setattr(permission_resolver, "user_can_access_data_source", no_access)
    resp = _edit(test_client, corpus, aid, "member")
    assert resp.status_code == 403 and resp.json()["error_code"] == "dashboard.join_no_access"
    assert _rows(corpus_rows, "SELECT id FROM artifact_conversations WHERE artifact_id = :a", a=aid) == []


# ── several conversations, one dashboard ──────────────────────────────────

@pytest.mark.e2e
def test_two_conversations_edit_one_dashboard_and_each_keeps_its_own(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    avi = _edit(test_client, corpus, aid, "member").json()["report_id"]
    _finish(spawned)
    assert _edit(test_client, corpus, aid, "member").json() == {"report_id": avi, "created": False,
                                                                "rerunning": False}

    # The newest version wins the draft, whoever made it; each version is
    # served from its own conversation.
    n = new_version(aid, avi, corpus.cast.id("member"))
    owner_draft = test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).json()
    assert owner_draft["version_number"] == n and owner_draft["report_id"] == avi
    assert owner_draft["version_author"]["id"] == corpus.cast.id("member")
    assert owner_draft["version_is_mine"] is False and owner_draft["my_report_id"] == corpus.reports[CONV]
    queries = test_client.get(f"/api/d/{aid}/draft/queries", headers=as_(corpus, "owner")).json()
    assert queries and {q["id"] for q in queries} <= {s for (s,) in [(r["id"],) for r in _rows(
        corpus_rows, "SELECT id FROM queries WHERE report_id = :r", r=avi)]}
    # An earlier version, by number: the owner's own, in her conversation.
    first = test_client.get(f"/api/d/{aid}/draft?version=1", headers=as_(corpus, "owner")).json()
    assert first["report_id"] == corpus.reports[CONV] and first["version_is_mine"] is True
    assert test_client.get(f"/api/d/{aid}/draft?version=999", headers=as_(corpus, "owner")).status_code == 404

    # Deleting the owner's conversation keeps the dashboard -- in Avi's hands.
    preview = test_client.get(f"/api/reports/{corpus.reports[CONV]}/delete_preview", headers=as_(corpus, "owner"))
    assert preview.status_code == 200, preview.text
    assert {d["id"]: d["outcome"] for d in preview.json()["dashboards"]}.get(aid) == "others_editing"
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    (row,) = _rows(corpus_rows, "SELECT report_id, deleted_at FROM artifacts WHERE id = :a", a=aid)
    assert row["deleted_at"] is None and row["report_id"] == avi
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).json()["report_id"] == avi


# ── publish since you started, rollback, kept releases ────────────────────

@pytest.mark.e2e
def test_publishing_over_someone_elses_newer_publish_asks_first(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    avi = _edit(test_client, corpus, aid, "member").json()["report_id"]
    _finish(spawned)
    avi_version = new_version(aid, avi, corpus.cast.id("member"))
    # Meanwhile the owner changes and publishes her own draft line.
    new_version(aid, corpus.reports[CONV], corpus.cast.id("owner"), retitle_viz="מכירות מעודכן")
    owners = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND report_id = :r "
                                "ORDER BY version DESC", a=aid, r=corpus.reports[CONV])[0]["id"]
    assert _publish_api(test_client, corpus, aid, "owner", version_id=owners).status_code == 202
    _finish(spawned)

    (mine,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND version = :v",
                    a=aid, v=avi_version)
    asked = _publish_api(test_client, corpus, aid, "member", version_id=mine["id"])
    assert asked.status_code == 409 and asked.json()["error_code"] == "dashboard.published_since"
    assert asked.json()["params"]["by"]  # who published since
    changes = test_client.get(f"/api/d/{aid}/draft/changes?version={avi_version}", headers=as_(corpus, "member"))
    assert changes.status_code == 200 and changes.json()["published_since"]["by"]["id"] == corpus.cast.id("owner")
    forced = _publish_api(test_client, corpus, aid, "member", version_id=mine["id"], force=True)
    assert forced.status_code == 202, forced.text
    _finish(spawned)
    # Building on what is live asks nothing.
    again = new_version(aid, avi, corpus.cast.id("member"))
    (latest,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND version = :v",
                      a=aid, v=again)
    assert _publish_api(test_client, corpus, aid, "member", version_id=latest["id"]).status_code == 202


@pytest.mark.e2e
def test_roll_back_is_instant_and_serves_the_old_rows(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    assert _publish_api(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    first = test_client.get(f"/api/dashboards/{aid}/publish", headers=as_(corpus, "owner")).json()["published"]["id"]
    first_queries = {q["id"] for q in test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, "owner")).json()}
    assert _publish_api(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    second = test_client.get(f"/api/dashboards/{aid}/publish", headers=as_(corpus, "owner")).json()["published"]["id"]
    assert second != first

    history = test_client.get(f"/api/dashboards/{aid}/history", headers=as_(corpus, "owner")).json()
    assert [r["id"] for r in history["releases"] if r["status"] == "ready"] == [second, first]
    assert [r["live"] for r in history["releases"] if r["status"] == "ready"] == [True, False]

    back = test_client.post(f"/api/dashboards/{aid}/rollback", json={"release_id": first}, headers=as_(corpus, "owner"))
    assert back.status_code == 200, back.text
    assert spawned == []  # nothing runs
    assert back.json()["published"]["id"] == first
    assert {q["id"] for q in test_client.get(f"/api/d/{aid}/queries",
                                             headers=as_(corpus, "owner")).json()} == first_queries
    gone = test_client.post(f"/api/dashboards/{aid}/rollback", json={"release_id": "00000000-0000-0000-0000-000000000000"},
                            headers=as_(corpus, "owner"))
    assert gone.status_code == 404 and gone.json()["error_code"] == "dashboard.release_not_kept"
    assert test_client.post(f"/api/dashboards/{aid}/rollback", json={"release_id": second},
                            headers=as_(corpus, "direct_share")).status_code in (403, 404)


@pytest.mark.e2e
def test_the_last_five_releases_are_kept(corpus, corpus_rows, test_client, spawned):
    from app.services.dashboard_collab import KEEP_RELEASES

    aid = own(corpus_rows, corpus, DASH)
    made = []
    for _ in range(KEEP_RELEASES + 2):
        assert _publish_api(test_client, corpus, aid).status_code == 202
        _finish(spawned)
        made.append(test_client.get(f"/api/dashboards/{aid}/publish",
                                    headers=as_(corpus, "owner")).json()["published"]["id"])
    kept = {r["id"] for r in _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE artifact_id = :a", a=aid)}
    assert kept == set(made[-KEEP_RELEASES:])


# ── preview: any version, each with their own data ────────────────────────

@pytest.mark.e2e
def test_an_editor_previews_someone_elses_version_with_their_own_run(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, f"user_required/dash")
    _seed_secret(corpus_rows, corpus, "user_required")
    _grant_editor(test_client, corpus, aid, "member")
    # The owner's version, over a personal connection: Avi sees the chart,
    # never her rows, and runs it as himself.
    queries = test_client.get(f"/api/d/{aid}/draft?version=1", headers=as_(corpus, "member"))
    assert queries.status_code == 200 and queries.json()["version_is_mine"] is False
    listed = test_client.get(f"/api/d/{aid}/draft/queries?version=1", headers=as_(corpus, "member")).json()
    assert listed
    step = test_client.get(f"/api/d/{aid}/draft/queries/{listed[0]['id']}/step?version=1",
                           headers=as_(corpus, "member"))
    assert step.status_code == 200 and MARKER not in step.text and step.json()["snapshot_withheld"] is True
    ran = test_client.post(f"/api/d/{aid}/draft/run?version=1", headers=as_(corpus, "member"))
    assert ran.status_code == 200, ran.text
    assert ran.json()["steps_total"] >= 1 and MARKER not in ran.text
    own_run = test_client.post(f"/api/d/{aid}/draft/run?version=1", headers=as_(corpus, "owner"))
    assert own_run.json().get("own") is True
    assert test_client.post(f"/api/d/{aid}/draft/run?version=1",
                            headers=as_(corpus, "direct_share")).status_code == 404


# ── who changed what ───────────────────────────────────────────────────────

@pytest.mark.e2e
def test_each_chart_says_who_changed_it_last(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    v_owner = new_version(aid, corpus.reports[CONV], corpus.cast.id("owner"))
    avi = _edit(test_client, corpus, aid, "member").json()["report_id"]
    _finish(spawned)
    copied = test_client.get(f"/api/d/{aid}/draft/changes", headers=as_(corpus, "member")).json()
    assert copied["elements"]
    assert {e["changed_by"]["id"] for e in copied["elements"]} == {corpus.cast.id("owner")}  # copies keep authorship

    v_avi = new_version(aid, avi, corpus.cast.id("member"), retitle_viz="הכנסות — גרסת אבי")
    changes = test_client.get(f"/api/d/{aid}/draft/changes?version={v_avi}", headers=as_(corpus, "owner")).json()
    by_title = {e["title"]: e for e in changes["elements"]}
    assert by_title["הכנסות — גרסת אבי"]["changed_by"]["id"] == corpus.cast.id("member")
    assert by_title["הכנסות — גרסת אבי"]["version_number"] == v_avi
    assert all(e["vs_live"] == "added" for e in changes["elements"])  # nothing live yet
    assert v_owner < v_avi


# ── who is editing right now ──────────────────────────────────────────────

@pytest.mark.e2e
def test_presence_follows_the_browser_and_the_agent(corpus, corpus_rows, test_client, spawned):
    from app.services import dashboard_presence

    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    avi = _edit(test_client, corpus, aid, "member").json()["report_id"]
    _finish(spawned)
    beat = test_client.put(f"/api/dashboards/{aid}/presence", json={"report_id": avi}, headers=as_(corpus, "member"))
    assert beat.status_code == 200
    seen = test_client.get(f"/api/dashboards/{aid}/presence", headers=as_(corpus, "owner")).json()["entries"]
    assert [(e["user"]["id"], e["state"], e["report_id"]) for e in seen] == [(corpus.cast.id("member"), "viewing", avi)]

    asyncio.run(dashboard_presence.agent_started(avi, corpus.cast.id("member"), None))
    (viz,) = _rows(corpus_rows, "SELECT id FROM visualizations WHERE report_id = :r LIMIT 1", r=avi)
    asyncio.run(dashboard_presence.agent_touched(avi, {"visualization_id": viz["id"]}))
    (entry,) = test_client.get(f"/api/dashboards/{aid}/presence", headers=as_(corpus, "owner")).json()["entries"]
    assert entry["state"] == "agent_editing" and entry["element_keys"]
    asyncio.run(dashboard_presence.agent_finished(avi, None))
    (entry,) = test_client.get(f"/api/dashboards/{aid}/presence", headers=as_(corpus, "owner")).json()["entries"]
    assert entry["state"] == "viewing" and entry["element_keys"] == []

    # A tab that stopped beating (crash, lost network) counts as gone.
    corpus_rows_exec(corpus_rows, "UPDATE artifact_presence SET seen_at = '2020-01-01 00:00:00'")
    assert test_client.get(f"/api/dashboards/{aid}/presence", headers=as_(corpus, "owner")).json()["entries"] == []
    # Viewers have no presence to read.
    assert test_client.get(f"/api/dashboards/{aid}/presence", headers=as_(corpus, "direct_share")).status_code == 403


def corpus_rows_exec(corpus_rows, sql, **params):
    with corpus_rows.engine.begin() as conn:
        conn.execute(text(sql), params)


# ── drafts ─────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_separate_draft_is_opt_in_and_the_implicit_one_stays(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    drafts = test_client.get(f"/api/dashboards/{aid}/drafts", headers=as_(corpus, "owner")).json()
    assert [d["name"] for d in drafts] == ["Main"]  # nobody sees a picker yet

    made = test_client.post(f"/api/dashboards/{aid}/drafts", json={"name": "Redesign v2"},
                            headers=as_(corpus, "member"))
    assert made.status_code == 200, made.text
    redesign = made.json()["report_id"]
    _finish(spawned)
    v = new_version(aid, redesign, corpus.cast.id("member"))
    drafts = test_client.get(f"/api/dashboards/{aid}/drafts", headers=as_(corpus, "owner")).json()
    assert [(d["name"], d["slug"]) for d in drafts] == [("Main", "main"), ("Redesign v2", "redesign-v2")]
    assert drafts[1]["head_version_number"] == v
    # The implicit draft is not moved by the separate one.
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).json()["version_number"] != v
    named = test_client.get(f"/api/d/{aid}/draft?draft=redesign-v2", headers=as_(corpus, "owner")).json()
    assert named["version_number"] == v and named["draft"]["slug"] == "redesign-v2"
    taken = test_client.post(f"/api/dashboards/{aid}/drafts", json={"name": "redesign v2"},
                             headers=as_(corpus, "owner"))
    assert taken.status_code == 409 and taken.json()["error_code"] == "dashboard.draft_name_taken"
    assert test_client.get(f"/api/d/{aid}/draft?draft=nope", headers=as_(corpus, "owner")).status_code == 404

    # Publishing it promotes it.
    (row,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND version = :v", a=aid, v=v)
    assert _publish_api(test_client, corpus, aid, "member", version_id=row["id"]).status_code == 202
    _finish(spawned)
    drafts = test_client.get(f"/api/dashboards/{aid}/drafts", headers=as_(corpus, "owner")).json()
    assert drafts[1]["status"] == "promoted"


# ── the summary and the agent's context ───────────────────────────────────

@pytest.mark.e2e
def test_the_agent_gets_notes_and_a_summary_without_data(corpus, corpus_rows, test_client, spawned):
    from app.dependencies import async_session_maker
    from app.services.dashboard_collab import agent_context

    aid = own(corpus_rows, corpus, DASH)
    _seed_secret(corpus_rows, corpus)
    assert test_client.put(f"/api/dashboards/{aid}/settings", json={"notes": "Revenue excludes test accounts."},
                           headers=as_(corpus, "owner")).status_code == 200
    _grant_editor(test_client, corpus, aid, "member")
    avi = _edit(test_client, corpus, aid, "member").json()["report_id"]

    async def context():
        async with async_session_maker() as db:
            return await agent_context(db, avi, aid)

    text_ = asyncio.run(context())
    assert "Revenue excludes test accounts." in text_
    assert "edited" in text_ and "own credentials" in text_
    assert MARKER not in text_ and "424242" not in text_
    card = test_client.get(f"/api/dashboards/conversations/{avi}", headers=as_(corpus, "member")).json()
    assert card[0]["continued_from"]["id"] == corpus.cast.id("owner") and card[0]["summary"]["charts"]
    # Someone else's conversation shows no card.
    assert test_client.get(f"/api/dashboards/conversations/{avi}", headers=as_(corpus, "owner")).json() == []


# ── whose credentials refresh it ──────────────────────────────────────────

@pytest.mark.e2e
def test_the_last_publisher_refreshes_while_they_can_still_edit(corpus, corpus_rows, test_client, spawned):
    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.services.dashboard_refresh import refresh_identity

    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    assert _publish_api(test_client, corpus, aid, "member").status_code == 202
    _finish(spawned)

    async def who():
        async with async_session_maker() as db:
            user = await refresh_identity(db, await db.get(Artifact, aid))
            return str(user.id) if user else None

    assert asyncio.run(who()) == corpus.cast.id("member")
    _grant_editor(test_client, corpus, aid)  # Avi is no longer an editor
    assert asyncio.run(who()) == corpus.cast.id("owner")


# ── analytics ──────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_editors_read_the_analytics_and_their_visits_are_not_counted(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    _grant_editor(test_client, corpus, aid, "member")
    base = f"/api/artifacts/{aid}/runtime"

    def visit(person):
        token = test_client.get(f"{base}/view-token?surface=standalone", headers=as_(corpus, person))
        assert token.status_code == 200, token.text
        return test_client.post(f"{base}/views", json={"token": token.json()["token"]}, headers=as_(corpus, person))

    assert visit("member").json().get("counted") is False
    assert visit("owner").json().get("counted") is False
    assert visit("direct_share").json() == {"accepted": True}
    stats = test_client.get(f"{base}/analytics", headers=as_(corpus, "member"))
    assert stats.status_code == 200 and stats.json()["views"] == 1
    assert test_client.get(f"{base}/analytics", headers=as_(corpus, "direct_share")).status_code == 403
