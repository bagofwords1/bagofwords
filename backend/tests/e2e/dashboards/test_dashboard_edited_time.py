"""A dashboard's "Edited" time (dashboards list findability, P2).

Each card says when the dashboard was created and when its content last
changed (`edited_at` = `content_updated_at`, else `created_at`). Edited moves
on a completed version, a rename, a publish, a restore, and a Bring that puts
other content than the draft's newest version on top of the draft (a revert).
It does not move on a data refresh (which moves `updated_at`), on an editor
joining, on the owner's Edit after every conversation was deleted, on a new
named draft, or on a Bring of the draft's newest version: those copy the
content as it is.

Each test first dates Edited far in the past, so a move shows whatever the
clock's resolution.
"""
import asyncio
from datetime import datetime

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own, publish, share

CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"
MANY = "dash_many_versions/dash"  # completed v1, v2, v4 (v3 failed)
LONG_AGO = datetime(2026, 1, 1)


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
    try:
        yield jobs
    finally:
        _drop(jobs)


def _finish(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            asyncio.run(job)


def _drop(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            job.close()


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _when(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _card(test_client, corpus, aid, person="owner") -> dict:
    resp = test_client.get("/api/artifacts", params={"filter": "my", "limit": 100}, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return next(d for d in resp.json()["dashboards"] if d["id"] == aid)


def _edited(test_client, corpus, aid) -> datetime:
    return _when(_card(test_client, corpus, aid)["edited_at"])


def _date_long_ago(corpus_rows, aid):
    # Written directly: no route sets a past Edited time, and a time far in
    # the past makes any move visible regardless of the clock's resolution.
    corpus_rows.update("artifacts", aid, content_updated_at=LONG_AGO)


def _publish_api(test_client, corpus, aid, person="owner", **body):
    resp = test_client.post(f"/api/artifacts/{aid}/publish", json=body or None, headers=as_(corpus, person))
    if resp.status_code == 202:
        PUBLISHED.append(resp.json()["latest"]["id"])
    return resp


def _live(test_client, corpus, aid):
    return test_client.get(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner")).json()["published"]["id"]


async def _new_version(aid: str, report_id: str, user_id: str) -> str:
    """A new completed version in one conversation, as the agent's edit
    tools write it (no route writes a version without an LLM run)."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.artifact import ArtifactVersion
    from app.services.artifact_service import new_version

    async with async_session_maker() as db:
        source = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.artifact_id == aid, ArtifactVersion.report_id == report_id,
                                          ArtifactVersion.status == "completed")
            .order_by(ArtifactVersion.version.desc()).limit(1))).scalar_one()
        version = await new_version(db, source, content=dict(source.content or {}), user_id=user_id)
        await db.commit()
        return str(version.id)


def _grant_editor(test_client, corpus, aid, person):
    visibility = test_client.get(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner")).json()["visibility"]
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"), json={
        "visibility": visibility, "editor_user_ids": [corpus.cast.id(person)], "editor_group_ids": []})
    assert resp.status_code == 200, resp.text


def _new_conversation(test_client, corpus, person="owner") -> str:
    resp = test_client.post("/api/reports", json={"title": "More questions"}, headers=as_(corpus, person))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _bring(test_client, corpus, aid, report_id, version_id=None) -> dict:
    body = {"report_id": report_id, **({"version_id": version_id} if version_id else {})}
    resp = test_client.post(f"/api/artifacts/{aid}/bring", json=body, headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert not resp.json().get("unchanged"), "the Bring must copy"
    return resp.json()


# ── the card ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_card_says_when_it_was_created_and_last_edited(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    (row,) = _rows(corpus_rows, "SELECT created_at FROM artifacts WHERE id = :a", a=aid)
    # Written directly: a dashboard from before Edited was recorded (the
    # migration's backfill fills these; the card must not rely on it).
    corpus_rows.update("artifacts", aid, content_updated_at=None)
    card = _card(test_client, corpus, aid)
    assert _when(card["created_at"]) == _when(row["created_at"])
    assert _when(card["edited_at"]) == _when(row["created_at"])  # never edited: its creation

    _date_long_ago(corpus_rows, aid)
    card = _card(test_client, corpus, aid)
    assert _when(card["edited_at"]) == LONG_AGO and _when(card["created_at"]) == _when(row["created_at"])


# ── what moves Edited ──────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_new_version_moves_edited(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "doc_only/doc")
    _date_long_ago(corpus_rows, aid)
    resp = test_client.post(f"/api/artifacts/{corpus.ids['doc_only/doc/v1']}/doc_edit",
                            json={"markdown": "# A new text\n"}, headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert _edited(test_client, corpus, aid) > LONG_AGO


@pytest.mark.e2e
def test_a_rename_moves_edited(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    _date_long_ago(corpus_rows, aid)
    resp = test_client.patch(f"/api/artifacts/{corpus.ids[f'{DASH}/v1']}", json={"title": "Sales, renamed"},
                             headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    card = _card(test_client, corpus, aid)
    assert card["title"] == "Sales, renamed" and _when(card["edited_at"]) > LONG_AGO


@pytest.mark.e2e
def test_patching_the_same_title_does_not_move_edited(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    title = _card(test_client, corpus, aid)["title"]
    _date_long_ago(corpus_rows, aid)
    resp = test_client.patch(f"/api/artifacts/{corpus.ids[f'{DASH}/v1']}", json={"title": title},
                             headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert _edited(test_client, corpus, aid) == LONG_AGO


@pytest.mark.e2e
def test_patching_the_same_content_does_not_move_edited(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    version_id = corpus.ids[f"{DASH}/v1"]
    content = test_client.get(f"/api/artifacts/{version_id}", headers=as_(corpus, "owner")).json()["content"]
    _date_long_ago(corpus_rows, aid)
    resp = test_client.patch(f"/api/artifacts/{version_id}", json={"content": content}, headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert _edited(test_client, corpus, aid) == LONG_AGO


@pytest.mark.e2e
def test_a_new_version_moves_edited_but_not_updated_at(corpus, corpus_rows, test_client):
    """updated_at keeps its own writers (a refresh, record writes, the agent
    tools): writing Edited does not move it."""
    aid = own(corpus_rows, corpus, "doc_only/doc")
    _date_long_ago(corpus_rows, aid)
    corpus_rows.update("artifacts", aid, updated_at=LONG_AGO)  # direct, as above: a visible move
    resp = test_client.post(f"/api/artifacts/{corpus.ids['doc_only/doc/v1']}/doc_edit",
                            json={"markdown": "# Another text\n"}, headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert _edited(test_client, corpus, aid) > LONG_AGO
    (row,) = _rows(corpus_rows, "SELECT updated_at FROM artifacts WHERE id = :a", a=aid)
    assert _when(row["updated_at"]) == LONG_AGO


@pytest.mark.e2e
def test_a_dashboard_created_complete_was_edited_when_it_was_created(corpus, corpus_rows, test_client):
    """Through the version factory, as every route and tool creates one."""
    from app.dependencies import async_session_maker
    from app.services.artifact_service import new_artifact

    async def create() -> str:
        async with async_session_maker() as db:
            version = await new_artifact(
                db, report_id=corpus.reports[CONV], organization_id=corpus.cast.org_id,
                user_id=corpus.cast.id("owner"), mode="page", title="Made complete",
                content={"code": "<div/>", "visualization_ids": []}, status="completed")
            await db.commit()
            return str(version.artifact_id)

    aid = asyncio.run(create())
    card = _card(test_client, corpus, aid)
    assert card["edited_at"] is not None and _when(card["edited_at"]) == _when(card["created_at"])


@pytest.mark.e2e
def test_a_content_change_of_a_completed_version_moves_edited(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    version_id = corpus.ids[f"{DASH}/v1"]
    content = test_client.get(f"/api/artifacts/{version_id}", headers=as_(corpus, "owner")).json()["content"]
    _date_long_ago(corpus_rows, aid)
    resp = test_client.patch(f"/api/artifacts/{version_id}", headers=as_(corpus, "owner"),
                             json={"content": {**content, "code": content["code"] + "\n<!-- edited -->"}})
    assert resp.status_code == 200, resp.text
    assert _edited(test_client, corpus, aid) > LONG_AGO


@pytest.mark.e2e
def test_publish_moves_edited(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _date_long_ago(corpus_rows, aid)
    assert _publish_api(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    assert _card(test_client, corpus, aid)["published"] is True
    assert _edited(test_client, corpus, aid) > LONG_AGO


@pytest.mark.e2e
def test_restore_moves_edited(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    assert _publish_api(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    first = _live(test_client, corpus, aid)
    newer = asyncio.run(_new_version(aid, corpus.reports[CONV], corpus.cast.id("owner")))
    assert _publish_api(test_client, corpus, aid, version_id=newer).status_code == 202
    _finish(spawned)
    assert _live(test_client, corpus, aid) != first
    _date_long_ago(corpus_rows, aid)

    resp = test_client.post(f"/api/artifacts/{aid}/restore", json={"release_id": first}, headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert _edited(test_client, corpus, aid) > LONG_AGO


@pytest.mark.e2e
def test_bringing_an_older_version_moves_edited(corpus, corpus_rows, test_client, spawned):
    """The draft's newest version becomes older content: a revert."""
    aid = own(corpus_rows, corpus, MANY)
    mine = _new_conversation(test_client, corpus)
    _date_long_ago(corpus_rows, aid)
    _bring(test_client, corpus, aid, mine, version_id=corpus.ids[f"{MANY}/v1"])
    _finish(spawned)
    assert _edited(test_client, corpus, aid) > LONG_AGO


@pytest.mark.e2e
def test_bringing_the_live_release_over_newer_draft_versions_moves_edited(corpus, corpus_rows, test_client,
                                                                          spawned):
    aid = own(corpus_rows, corpus, DASH)
    assert _publish_api(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    asyncio.run(_new_version(aid, corpus.reports[CONV], corpus.cast.id("owner")))  # the draft moves on
    mine = _new_conversation(test_client, corpus)
    _date_long_ago(corpus_rows, aid)
    _bring(test_client, corpus, aid, mine)  # no version: the live one
    _finish(spawned)
    assert _edited(test_client, corpus, aid) > LONG_AGO


# ── what does not ──────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_refresh_moves_updated_at_but_not_edited(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    _date_long_ago(corpus_rows, aid)
    corpus_rows.update("artifacts", aid, updated_at=LONG_AGO)  # direct, as above: a visible move

    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success", resp.json()
    assert _edited(test_client, corpus, aid) == LONG_AGO
    (row,) = _rows(corpus_rows, "SELECT updated_at FROM artifacts WHERE id = :a", a=aid)
    assert _when(row["updated_at"]) > LONG_AGO


@pytest.mark.e2e
def test_an_editor_joining_does_not_move_edited(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member")
    _date_long_ago(corpus_rows, aid)
    resp = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "member"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True  # a copy was made
    _finish(spawned)
    assert _edited(test_client, corpus, aid) == LONG_AGO


@pytest.mark.e2e
def test_the_owners_edit_after_the_conversation_was_deleted_does_not_move_edited(corpus, corpus_rows, test_client,
                                                                                spawned):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    _date_long_ago(corpus_rows, aid)
    resp = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True  # a copy of the release was made
    _finish(spawned)
    assert _edited(test_client, corpus, aid) == LONG_AGO


@pytest.mark.e2e
def test_a_new_named_draft_does_not_move_edited(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, MANY)
    _date_long_ago(corpus_rows, aid)
    resp = test_client.post(f"/api/artifacts/{aid}/drafts", json={"name": "Redesign"}, headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    _finish(spawned)
    assert _edited(test_client, corpus, aid) == LONG_AGO


@pytest.mark.e2e
def test_bringing_the_newest_version_does_not_move_edited(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, MANY)
    mine = _new_conversation(test_client, corpus)
    _date_long_ago(corpus_rows, aid)
    _bring(test_client, corpus, aid, mine, version_id=corpus.ids[f"{MANY}/v4"])
    _finish(spawned)
    assert _edited(test_client, corpus, aid) == LONG_AGO
