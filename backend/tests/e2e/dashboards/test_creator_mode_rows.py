"""Run as the creator: readers see the owner's rows, whoever published.

With "run on my behalf" (shared_run_identity = creator) readers run with the
owner's credentials, so the stored rows they are shown must be the owner's
run too. An editor may have published before the owner turned it on: over a
connection each person signs in to, those rows are the editor's own slice.
They are withheld from readers until the owner's run replaces them; turning
the setting on starts that run at once, and later refreshes run as the
owner. Over a connection everyone shares, the rows are the same whoever ran
them, and nothing changes.
"""
import asyncio
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own, publish

MARKER = "פרוסה-של-אבי-5150"
EDITOR_ROWS = {"rows": [{"חודש": MARKER, "הכנסות": 515}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}


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


@pytest.fixture
def clients(monkeypatch):
    """Whose credentials each data source client is built with (no query
    reaches a real source)."""
    from app.services.data_source_service import DataSourceService

    seen = []

    async def record(self, db, data_source, current_user=None, **kwargs):
        seen.append(str(current_user.id) if current_user is not None else None)
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", record)
    return seen


def _finish(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            asyncio.run(job)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _live(corpus_rows, aid):
    (row,) = _rows(corpus_rows, "SELECT r.id, r.run_by FROM artifact_releases r JOIN artifacts a "
                                "ON a.published_release_id = r.id WHERE a.id = :a", a=aid)
    return row


def _editor_published(test_client, corpus, corpus_rows, aid, spawned):
    """Avi (`member`), an editor, publishes; the live rows are his run --
    here, his own slice of the data (the marker)."""
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"), json={
        "visibility": "shared", "user_ids": [corpus.cast.id("direct_share")],
        "editor_user_ids": [corpus.cast.id("member")]})
    assert resp.status_code == 200, resp.text
    resp = test_client.post(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "member"))
    assert resp.status_code == 202, resp.text
    PUBLISHED.append(resp.json()["latest"]["id"])
    _finish(spawned)
    live = _live(corpus_rows, aid)
    assert live["run_by"] == corpus.cast.id("member")
    # The rows his run gave (the stub client returns none): his slice.
    for (step_id,) in [(r["id"],) for r in _rows(corpus_rows, "SELECT s.id FROM steps s JOIN queries q ON "
                                                             "q.default_step_id = s.id WHERE q.artifact_release_id = :r",
                                                  r=live["id"])]:
        corpus_rows.update("steps", step_id, data=EDITOR_ROWS, status="success")
    return live


def _reader_sees(test_client, corpus, aid, person="direct_share") -> str:
    """Everything the reader's page gets for every chart, as text."""
    headers = as_(corpus, person)
    queries = test_client.get(f"/api/d/{aid}/queries", headers=headers)
    assert queries.status_code == 200, queries.text
    out = []
    for q in queries.json():
        step = test_client.get(f"/api/d/{aid}/queries/{q['id']}/step", headers=headers)
        out.append(step.text)
    return json.dumps(out, ensure_ascii=False)


def _creator(test_client, corpus, aid):
    resp = test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"shared_run_identity": "creator"})
    assert resp.status_code == 200, resp.text


def _withheld(aid) -> bool:
    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.services.dashboard_access import published_release
    from app.services.dashboard_data import release_snapshot_withheld

    async def run():
        async with async_session_maker() as db:
            artifact = await db.get(Artifact, aid)
            return await release_snapshot_withheld(db, artifact, await published_release(db, artifact))

    return asyncio.run(run())


@pytest.mark.e2e
def test_readers_never_get_an_editors_own_slice_as_the_owners(corpus, corpus_rows, test_client, spawned, clients):
    aid = own(corpus_rows, corpus, "user_required/dash")
    publish(aid)
    _editor_published(test_client, corpus, corpus_rows, aid, spawned)
    # As viewer mode: the slice is withheld from readers (each runs their own).
    assert MARKER not in _reader_sees(test_client, corpus, aid)

    _creator(test_client, corpus, aid)
    # Before the owner's run has replaced it, the editor's slice is not
    # shown as the owner's -- not on the page, not in a PDF or a picture.
    assert MARKER not in _reader_sees(test_client, corpus, aid)
    assert _withheld(aid) is True


@pytest.mark.e2e
def test_turning_it_on_refreshes_as_the_owner(corpus, corpus_rows, test_client, spawned, clients):
    aid = own(corpus_rows, corpus, "user_required/dash")
    publish(aid)
    _editor_published(test_client, corpus, corpus_rows, aid, spawned)
    clients.clear()

    _creator(test_client, corpus, aid)
    _finish(spawned)
    assert clients and set(clients) == {corpus.cast.id("owner")}
    assert _live(corpus_rows, aid)["run_by"] == corpus.cast.id("owner")
    # The owner's rows now: readers get them as the setting says.
    assert _withheld(aid) is False
    assert MARKER not in _reader_sees(test_client, corpus, aid)


@pytest.mark.e2e
def test_refreshes_run_as_the_owner_while_it_runs_as_the_creator(corpus, corpus_rows, test_client, spawned, clients):
    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.services.dashboard_refresh import refresh_identity

    aid = own(corpus_rows, corpus, "user_required/dash")
    publish(aid)
    _editor_published(test_client, corpus, corpus_rows, aid, spawned)

    async def who():
        async with async_session_maker() as db:
            user = await refresh_identity(db, await db.get(Artifact, aid))
            return str(user.id) if user else None

    assert asyncio.run(who()) == corpus.cast.id("member")  # viewer mode: the last publisher, as before
    _creator(test_client, corpus, aid)
    assert asyncio.run(who()) == corpus.cast.id("owner")


@pytest.mark.e2e
def test_over_a_shared_connection_nothing_is_withheld_or_rerun(corpus, corpus_rows, test_client, spawned, clients):
    """Everyone's run gives the same rows there: the editor's publish stays
    shown, and turning the setting on starts no run."""
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    publish(aid)
    _editor_published(test_client, corpus, corpus_rows, aid, spawned)
    _creator(test_client, corpus, aid)
    assert not [job for job in spawned if asyncio.iscoroutine(job)
                and "refresh" in getattr(job, "__qualname__", "")]
    _finish(spawned)
    assert _live(corpus_rows, aid)["run_by"] == corpus.cast.id("member")
    assert _withheld(aid) is False
    assert MARKER in _reader_sees(test_client, corpus, aid)
