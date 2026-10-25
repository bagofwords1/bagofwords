"""The owner publishes a dashboard (phase 5, part B).

`POST /api/artifacts/{id}/publish` checks, copies the version into a release
in 'publishing' and returns; the background run (captured here and run by
the test) executes every copy with the owner's credentials, what a step
loads first. All of it succeeded: readers switch to the new release at once
and the previous one is deleted with its rows and files. Anything failed:
readers keep the live release, the attempt is 'failed' with the reason and
leaves no copies behind. `GET` tells the owner where it stands.
"""
import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own, publish, share

STALE = {"rows": [{"חודש": "טיוטה", "הכנסות": 0}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}
BROKEN = "def generate_df(ds_clients, excel_files):\n    raise ValueError('השאילתה נשברה')\n"


@pytest.fixture
def drawn(monkeypatch):
    """Thumbnails drawn: (name, page). The page is what the screenshot
    would render, as the arguments it is built from (the built page needs
    the frontend's bundled libraries); no browser starts and a tiny PNG
    stands in for its screenshot."""
    from app.services.thumbnail_service import ThumbnailService
    from tests.fixtures.dashboard_corpus.kinds import TINY_PNG

    calls = []

    def page(self, **kwargs):
        return json.dumps(kwargs, ensure_ascii=False, default=str)

    monkeypatch.setattr(ThumbnailService, "_build_thumbnail_html", page)

    async def draw(self, artifact_id, html_content, mode="page"):
        calls.append((artifact_id, html_content))
        (self.UPLOADS_DIR / f"{artifact_id}.png").write_bytes(TINY_PNG)
        return f"thumbnails/{artifact_id}.png"

    monkeypatch.setattr(ThumbnailService, "generate_thumbnail", draw)
    return calls


@pytest.fixture
def spawned(monkeypatch, drawn):
    """The background runs the route starts, run when the test says."""
    jobs = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", jobs.append)
    return jobs


def _finish(spawned):
    while spawned:
        asyncio.run(spawned.pop(0))


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _post(test_client, corpus, aid, person="owner", version_id=None):
    body = {"version_id": version_id} if version_id else None
    resp = test_client.post(f"/api/artifacts/{aid}/publish", json=body, headers=as_(corpus, person))
    if resp.status_code == 202:
        PUBLISHED.append(resp.json()["latest"]["id"])  # its files are removed after the test
    return resp


def _status(test_client, corpus, aid, person="owner"):
    resp = test_client.get(f"/api/artifacts/{aid}/publish", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _copies(corpus_rows, release):
    return _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step, s.data FROM queries q JOIN steps s "
                              "ON s.id = q.default_step_id WHERE q.artifact_release_id = :r", r=release)


def _months(data) -> set:
    return {r.get("חודש") for r in (_json(data) or {}).get("rows", [])}


def _left_behind(corpus_rows, release) -> dict:
    """What of a release is still in the database."""
    return {
        "queries": _rows(corpus_rows, "SELECT id FROM queries WHERE artifact_release_id = :r", r=release),
        "visualizations": _rows(corpus_rows, "SELECT id FROM visualizations WHERE artifact_release_id = :r",
                                r=release),
        "sources": _rows(corpus_rows, "SELECT data_source_id FROM artifact_release_data_source_association "
                                      "WHERE artifact_release_id = :r", r=release),
    }


EMPTY = {"queries": [], "visualizations": [], "sources": []}


@pytest.mark.e2e
def test_publish_runs_the_version_and_switches_readers_to_it(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    old = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (old_copy,) = _copies(corpus_rows, old)
    assert test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).status_code == 200
    draft_step = corpus.ids["conv_three_dash/q_sales/step"]
    corpus_rows.update("steps", draft_step, data=STALE)  # the draft's snapshot is not what gets published

    started = _post(test_client, corpus, aid)
    assert started.status_code == 202, started.text
    body = started.json()
    new = body["latest"]["id"]
    assert body["latest"]["status"] == "publishing" and body["published"]["id"] == old
    assert body["latest"]["source_version_id"] == corpus.ids["conv_three_dash/dash_sales/v1"]

    # While it runs, readers see the live release.
    queries = test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, "member")).json()
    assert {q["id"] for q in queries} == {old_copy["id"]}

    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["published"]["id"] == new and status["latest"]["id"] == new
    assert status["latest"]["status"] == "ready" and status["latest"]["error"] is None
    assert status["has_unpublished_changes"] is False
    assert _rows(corpus_rows, "SELECT published_release_id FROM artifacts WHERE id = :a", a=aid) == [
        {"published_release_id": new}]

    (copy,) = _copies(corpus_rows, new)
    assert _months(copy["data"]) == {"ינואר", "פברואר"}  # ran again, with the owner's credentials
    queries = test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, "member")).json()
    assert {q["id"] for q in queries} == {copy["id"]}
    assert _months(_rows(corpus_rows, "SELECT data FROM steps WHERE id = :s", s=draft_step)[0]["data"]) == {"טיוטה"}

    # The previous release is kept for Roll back (collab01) -- ready, not
    # live, its copies intact -- and readers no longer reach it.
    assert _rows(corpus_rows, "SELECT status FROM artifact_releases WHERE id = :r", r=old) == [{"status": "ready"}]
    assert _rows(corpus_rows, "SELECT id FROM steps WHERE id = :s", s=old_copy["step"]) != []
    assert old_copy["id"] not in {q["id"] for q in queries}


@pytest.mark.e2e
def test_a_failed_publish_leaves_readers_on_the_live_release(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    live = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    draft_step = corpus.ids["conv_three_dash/q_sales/step"]
    corpus_rows.update("steps", draft_step, code=BROKEN)

    attempt = _post(test_client, corpus, aid).json()["latest"]["id"]
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["published"]["id"] == live
    assert status["latest"]["id"] == attempt and status["latest"]["status"] == "failed"
    assert "השאילתה נשברה" in status["latest"]["error"]
    assert _rows(corpus_rows, "SELECT published_release_id FROM artifacts WHERE id = :a", a=aid) == [
        {"published_release_id": live}]
    assert _left_behind(corpus_rows, attempt) == EMPTY  # the record stays, its copies go
    assert len(_copies(corpus_rows, live)) == 1

    # Fixed, it publishes; the failed record goes, the old release is kept
    # for Roll back (collab01).
    corpus_rows.update("steps", draft_step, code=corpus_rows_code(corpus_rows, corpus))
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    assert _status(test_client, corpus, aid)["latest"]["status"] == "ready"
    assert _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE id = :a", a=attempt) == []
    assert _rows(corpus_rows, "SELECT status FROM artifact_releases WHERE id = :b", b=live) == [{"status": "ready"}]


def corpus_rows_code(corpus_rows, corpus):
    """The corpus's ordinary step code, read from a step nobody broke."""
    return _rows(corpus_rows, "SELECT code FROM steps WHERE id = :s",
                 s=corpus.ids["conv_three_dash/q_stock/step"])[0]["code"]


@pytest.mark.e2e
def test_one_publish_at_a_time(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    assert _post(test_client, corpus, aid).status_code == 202
    again = _post(test_client, corpus, aid)
    assert again.status_code == 409 and again.json()["error_code"] == "dashboard.publish_in_progress"
    # The database holds it too, for two servers racing past the check.
    with pytest.raises(IntegrityError):
        corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                           status="publishing")
    corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id, status="failed")
    _finish(spawned)
    assert _post(test_client, corpus, aid).status_code == 202


@pytest.mark.e2e
def test_a_publish_that_stopped_beating_fails_and_another_can_start(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    stuck = _post(test_client, corpus, aid).json()["latest"]["id"]
    stopped_job = spawned.pop()  # its server died before running it
    corpus_rows.update("artifact_releases", stuck, updated_at=datetime(2026, 1, 1))

    status = _status(test_client, corpus, aid)
    assert status["latest"]["id"] == stuck and status["latest"]["status"] == "failed"
    assert status["latest"]["error"] and status["published"] is None
    assert _left_behind(corpus_rows, stuck) == EMPTY

    asyncio.run(stopped_job)  # it wakes up after all: it does nothing
    assert _status(test_client, corpus, aid)["latest"]["status"] == "failed"
    assert _rows(corpus_rows, "SELECT published_release_id FROM artifacts WHERE id = :a", a=aid) == [
        {"published_release_id": None}]

    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    assert _status(test_client, corpus, aid)["latest"]["status"] == "ready"


@pytest.mark.e2e
def test_a_publish_failed_before_its_switch_never_goes_live(corpus, corpus_rows, test_client, spawned,
                                                            monkeypatch):
    """Every copy ran, then a status read failed it (it looked stopped): the
    switch refuses it, and readers keep the live release."""
    import app.services.dashboard_publish as publishing

    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    live = publish(aid)
    attempt = _post(test_client, corpus, aid).json()["latest"]["id"]
    switch = publishing._go_live

    async def failed_first(db, release_id):
        corpus_rows.update("artifact_releases", release_id, updated_at=datetime(2026, 1, 1))
        assert _status(test_client, corpus, aid)["latest"]["status"] == "failed"
        return await switch(db, release_id)

    monkeypatch.setattr(publishing, "_go_live", failed_first)
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["published"]["id"] == live
    assert status["latest"]["id"] == attempt and status["latest"]["status"] == "failed"
    assert _left_behind(corpus_rows, attempt) == EMPTY and len(_copies(corpus_rows, live)) == 1


@pytest.mark.e2e
def test_a_publish_that_beats_is_not_stopped(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    running = _post(test_client, corpus, aid).json()["latest"]["id"]
    assert _status(test_client, corpus, aid)["latest"]["status"] == "publishing"
    assert len(_copies(corpus_rows, running)) == 1
    _finish(spawned)
    assert _status(test_client, corpus, aid)["published"]["id"] == running


@pytest.mark.e2e
def test_connections_are_checked_before_anything_is_created(corpus, corpus_rows, test_client, spawned,
                                                            monkeypatch):
    from app.services.data_source_service import DataSourceService

    async def expired(self, db, data_source, current_user=None, connection_errors=None, **kwargs):
        connection_errors.append({"connection_name": "Salesforce", "data_source_name": data_source.name,
                                  "data_source_id": str(data_source.id), "error": "token expired"})
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", expired)
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    resp = _post(test_client, corpus, aid)
    assert resp.status_code == 400 and resp.json()["error_code"] == "dashboard.publish_connection"
    assert "Salesforce" in resp.text
    assert _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE artifact_id = :a", a=aid) == []
    assert spawned == []


@pytest.mark.e2e
def test_a_connection_that_breaks_during_the_run_fails_the_publish(corpus, corpus_rows, test_client, spawned,
                                                                   monkeypatch):
    from app.services.data_source_service import DataSourceService

    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    attempt = _post(test_client, corpus, aid).json()["latest"]["id"]

    async def broke(self, db, data_source, current_user=None, connection_errors=None, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(DataSourceService, "construct_clients", broke)
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "failed" and "connection refused" in status["latest"]["error"]
    assert status["published"] is None and _left_behind(corpus_rows, attempt) == EMPTY


@pytest.mark.e2e
def test_only_a_version_that_completed_publishes(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "dash_many_versions/dash")
    v = {n: corpus.ids[f"dash_many_versions/dash/v{n}"] for n in (1, 2, 3, 4)}
    failed = _post(test_client, corpus, aid, version_id=v[3])
    assert failed.status_code == 409 and failed.json()["error_code"] == "dashboard.version_not_publishable"
    other = corpus.ids["conv_three_dash/dash_sales/v1"]
    assert _post(test_client, corpus, aid, version_id=other).status_code == 404

    # "Use this version": an older one that completed.
    older = _post(test_client, corpus, aid, version_id=v[2])
    assert older.status_code == 202 and older.json()["latest"]["source_version_id"] == v[2]
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["published"]["source_version_id"] == v[2] and status["has_unpublished_changes"] is True

    newest = _post(test_client, corpus, aid)  # by default, the newest completed
    assert newest.json()["latest"]["source_version_id"] == v[4]
    _finish(spawned)
    assert _status(test_client, corpus, aid)["has_unpublished_changes"] is False

    for key in ("dash_newest_failed/dash", "dash_newest_pending/dash"):
        aid = own(corpus_rows, corpus, key)
        newest_version = corpus.ids[f"{key}/v2"]
        refused = _post(test_client, corpus, aid, version_id=newest_version)
        assert refused.status_code == 409, key
        default = _post(test_client, corpus, aid)  # the newest that completed
        assert default.json()["latest"]["source_version_id"] == corpus.ids[f"{key}/v1"], key
        _finish(spawned)


@pytest.mark.e2e
def test_a_deck_publishes_frozen(corpus, corpus_rows, test_client, spawned, monkeypatch):
    from app.services.step_service import StepService

    async def never(*args, **kwargs):
        raise AssertionError("a published deck does not run")

    monkeypatch.setattr(StepService, "_execute_step_code", never)
    aid = own(corpus_rows, corpus, "slides_previews/deck")
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["published"]["id"] == release and status["latest"]["status"] == "ready"


@pytest.mark.e2e
def test_a_public_dashboard_cannot_publish_monitoring_data(corpus, corpus_rows, test_client, spawned, drawn):
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped")
    corpus_rows.update("artifacts", aid, visibility="public")
    resp = _post(test_client, corpus, aid, person="bow_scoped")
    assert resp.status_code == 409 and resp.json()["error_code"] == "dashboard.publish_public_monitoring"
    corpus_rows.update("artifacts", aid, visibility="internal")
    assert _post(test_client, corpus, aid, person="bow_scoped").status_code == 202
    _finish(spawned)
    assert _status(test_client, corpus, aid, person="bow_scoped")["latest"]["status"] == "ready"
    assert drawn == []  # no thumbnail of monitoring data: the image is served without a user


@pytest.mark.e2e
def test_only_the_owner_publishes(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    for person, code in (("anonymous", 401), ("member", 403), ("admin", 403), ("other_org", 403)):
        assert _post(test_client, corpus, aid, person=person).status_code == code, person
        assert test_client.get(f"/api/artifacts/{aid}/publish",
                               headers=as_(corpus, person)).status_code == code, person
    assert spawned == []
    assert _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE artifact_id = :a", a=aid) == []


@pytest.mark.e2e
def test_what_a_step_loads_runs_first(corpus, corpus_rows, test_client, spawned):
    """The derived step loads its base's fresh rows, not the copied snapshot."""
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=(
        "def generate_df(ds_clients, excel_files, load_step):\n"
        "    return load_step('בסיס · ריצה')\n"))
    aid = own(corpus_rows, corpus, "load_step/dash")
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    by_title = {c["title"]: c for c in _copies(corpus_rows, release)}
    # The derived copy comes first in every other order the run could take.
    corpus_rows.update("queries", by_title["נגזרת"]["id"], created_at=datetime(2020, 1, 1))
    _finish(spawned)
    assert _status(test_client, corpus, aid)["latest"]["status"] == "ready"
    by_title = {c["title"]: c for c in _copies(corpus_rows, release)}
    assert _months(by_title["בסיס"]["data"]) == {"ינואר", "פברואר"}
    assert _months(by_title["נגזרת"]["data"]) == {"ינואר", "פברואר"}


@pytest.mark.e2e
def test_the_previous_releases_files_go_and_a_failed_attempts_too(corpus, corpus_rows, test_client, spawned):
    from tests.e2e.dashboards.dashboard_setup import uploads_dir

    aid = own(corpus_rows, corpus, "thumbnail/dash")
    old = publish(aid)
    thumbs = uploads_dir() / "thumbnails"
    assert (thumbs / f"release-{old}.png").is_file()
    new = _post(test_client, corpus, aid).json()["latest"]["id"]
    assert (thumbs / f"release-{new}.png").is_file()
    _finish(spawned)
    assert (thumbs / f"release-{old}.png").is_file()  # kept with its release, for Roll back (collab01)
    assert (thumbs / f"release-{new}.png").is_file()

    corpus_rows.update("steps", corpus.ids["thumbnail/q_sales/step"], code=BROKEN)
    failed = _post(test_client, corpus, aid).json()["latest"]["id"]
    assert (thumbs / f"release-{failed}.png").is_file()
    _finish(spawned)
    assert not (thumbs / f"release-{failed}.png").exists()
    assert (thumbs / f"release-{new}.png").is_file()


# ── after publish (part C) ───────────────────────────────────────────────

@pytest.mark.e2e
def test_the_thumbnail_is_drawn_from_the_new_release(corpus, corpus_rows, test_client, spawned, drawn):
    from tests.e2e.dashboards.dashboard_setup import uploads_dir

    aid = own(corpus_rows, corpus, "thumbnail/dash")
    corpus_rows.update("steps", corpus.ids["thumbnail/q_sales/step"], data=STALE)
    before = datetime.utcnow().replace(microsecond=0)
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    _finish(spawned)
    assert [name for name, _ in drawn] == [f"release-{release}"]
    html = drawn[0][1]
    assert "ינואר" in html and "טיוטה" not in html  # the release's rows, not the draft's snapshot
    assert _rows(corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE id = :r", r=release) == [
        {"thumbnail_path": f"thumbnails/release-{release}.png"}]
    assert (uploads_dir() / "thumbnails" / f"release-{release}.png").is_file()
    (ran,) = _rows(corpus_rows, "SELECT last_run_at FROM artifacts WHERE id = :a", a=aid)
    last = ran["last_run_at"]
    last = datetime.fromisoformat(last) if isinstance(last, str) else last
    assert last >= before


@pytest.mark.e2e
def test_no_thumbnail_when_readers_may_not_get_the_rows(corpus, corpus_rows, test_client, spawned, drawn):
    """The image is served without a user: one identity's rows never go in
    it, and the thumbnail copied from the draft goes."""
    from tests.e2e.dashboards.dashboard_setup import uploads_dir

    corpus_rows.update("queries", corpus.ids["thumbnail/q_sales"],
                       parameters=[{"name": "UserEmail", "type": "string", "source": "identity"}])
    aid = own(corpus_rows, corpus, "thumbnail/dash")
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    assert (uploads_dir() / "thumbnails" / f"release-{release}.png").is_file()  # copied with the version
    _finish(spawned)
    assert _status(test_client, corpus, aid)["published"]["id"] == release
    assert drawn == []
    assert _rows(corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE id = :r", r=release) == [
        {"thumbnail_path": None}]
    assert not (uploads_dir() / "thumbnails" / f"release-{release}.png").exists()


@pytest.mark.e2e
def test_a_decks_thumbnail_stays_as_published(corpus, corpus_rows, test_client, spawned, drawn):
    aid = own(corpus_rows, corpus, "slides_previews/deck")
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    (copied,) = _rows(corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE id = :r", r=release)
    _finish(spawned)
    assert drawn == []
    assert _rows(corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE id = :r", r=release) == [copied]


@pytest.mark.e2e
def test_a_decks_thumbnail_goes_when_readers_may_not_get_its_rows(corpus, corpus_rows, test_client, spawned,
                                                                  drawn):
    """A deck keeps the picture it was published with -- unless its rows are
    one identity's: the picture (its first slide) is served without a user
    (2026-10-21 review)."""
    from tests.e2e.dashboards.dashboard_setup import uploads_dir

    corpus_rows.update("queries", corpus.ids["slides_previews/q_sales"],
                       parameters=[{"name": "UserEmail", "type": "string", "source": "identity"}])
    aid = own(corpus_rows, corpus, "slides_previews/deck")
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    (copied,) = _rows(corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE id = :r", r=release)
    assert copied["thumbnail_path"], "the deck was published with no picture"
    _finish(spawned)
    assert drawn == []
    assert _rows(corpus_rows, "SELECT thumbnail_path FROM artifact_releases WHERE id = :r", r=release) == [
        {"thumbnail_path": None}]
    assert not (uploads_dir() / copied["thumbnail_path"]).exists()


# ── review fixes ─────────────────────────────────────────────────────────

BOW = {"version": 1, "source_id": "builtin:bow", "report_ids": [], "scope_ids": None}


@pytest.mark.e2e
def test_a_dashboard_made_public_while_publishing_monitoring_data_fails_it(corpus, corpus_rows, test_client,
                                                                          spawned):
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped")
    corpus_rows.update("artifacts", aid, visibility="internal")
    attempt = _post(test_client, corpus, aid, person="bow_scoped").json()["latest"]["id"]
    corpus_rows.update("artifacts", aid, visibility="public")
    _finish(spawned)
    status = _status(test_client, corpus, aid, person="bow_scoped")
    assert status["latest"]["status"] == "failed" and "public" in status["latest"]["error"]
    assert status["published"] is None and _left_behind(corpus_rows, attempt) == EMPTY


@pytest.mark.e2e
def test_only_connections_the_dashboard_uses_are_checked(corpus, corpus_rows, test_client, spawned, monkeypatch):
    """The conversation has two sources; its code names one. A broken
    connection of the other blocks nothing, before or during the run."""
    from app.services.data_source_service import DataSourceService

    broken = {"name": "מחסן"}

    async def construct(self, db, data_source, current_user=None, connection_errors=None, **kwargs):
        if data_source.name == broken["name"]:
            connection_errors.append({"connection_name": f"{data_source.name} · SQL", "error": "token expired",
                                      "data_source_name": data_source.name, "data_source_id": str(data_source.id)})
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", construct)
    step = corpus.ids["multi_source/q_sales/step"]
    code = corpus_rows_code(corpus_rows, corpus)
    corpus_rows.update("steps", step, code=code + "# uses ds_clients['חנות מוזיקה:sqlite-1']\n")
    aid = own(corpus_rows, corpus, "multi_source/dash")
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    assert _status(test_client, corpus, aid)["latest"]["status"] == "ready"

    broken["name"] = "חנות מוזיקה"  # the one it uses
    refused = _post(test_client, corpus, aid)
    assert refused.status_code == 400 and refused.json()["params"] == {"names": "חנות מוזיקה · SQL"}


@pytest.mark.e2e
def test_a_publish_beats_while_a_long_step_runs(corpus, corpus_rows, test_client, spawned, monkeypatch):
    """One step may run longer than the stale window; the publish still
    beats, so a status read meanwhile does not fail it."""
    import app.services.dashboard_publish as publishing
    from app.services.step_service import StepService

    monkeypatch.setattr(publishing, "PUBLISH_HEARTBEAT_SECONDS", 0.05)
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    release = _post(test_client, corpus, aid).json()["latest"]["id"]
    seen = []

    async def slow(self, db, step, ctx, **kwargs):
        corpus_rows.update("artifact_releases", release, updated_at=datetime(2026, 1, 1))
        await asyncio.sleep(0.5)
        (row,) = _rows(corpus_rows, "SELECT updated_at FROM artifact_releases WHERE id = :r", r=release)
        seen.append(row["updated_at"])
        return STALE

    monkeypatch.setattr(StepService, "_execute_step_code", slow)
    _finish(spawned)
    (beat,) = seen
    beat = datetime.fromisoformat(beat) if isinstance(beat, str) else beat
    assert beat > datetime(2026, 1, 2)
    assert _status(test_client, corpus, aid)["published"]["id"] == release


@pytest.mark.e2e
def test_a_publish_records_monitoring_access_on_its_release(corpus, corpus_rows, test_client, spawned):
    """Monitoring data a publish runs -- the bow client, or an entity built
    on it -- marks the release it builds (and its copies' sources), never
    the conversation."""
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"], code=(
        "def generate_df(ds_clients, excel_files, load_entity):\n"
        "    return load_entity('מודל הכנסות')\n"))
    corpus_rows.update("entities", corpus.ids["entity_from_step"], owner_id=corpus.cast.id("admin"),
                       bow_source_access={**BOW, "organization_id": corpus.cast.org_id})
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales", owner="admin")
    release = _post(test_client, corpus, aid, person="admin").json()["latest"]["id"]
    _finish(spawned)
    assert _status(test_client, corpus, aid, person="admin")["latest"]["status"] == "ready"
    (row,) = _rows(corpus_rows, "SELECT bow_source_access FROM artifact_releases WHERE id = :r", r=release)
    assert _json(row["bow_source_access"])["source_id"] == "builtin:bow"
    refs = _rows(corpus_rows, "SELECT source_refs FROM queries WHERE artifact_release_id = :r", r=release)
    assert refs and all("builtin:bow" in str(r["source_refs"]) for r in refs)
    report = corpus.reports["conv_three_dash"]
    (draft,) = _rows(corpus_rows, "SELECT bow_source_access FROM reports WHERE id = :r", r=report)
    assert _json(draft["bow_source_access"]) is None


@pytest.mark.e2e
def test_a_publish_that_fails_to_start_saves_nothing_after_failing_a_stale_one(corpus, corpus_rows, test_client,
                                                                              spawned, monkeypatch):
    """Failing a stale publish is part of starting the new one: when the
    start fails, neither is saved and the dashboard stays locked to the end
    (PR #1455 review)."""
    from datetime import timedelta

    from app.services import dashboard_publish

    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    stale = corpus_rows.insert("artifact_releases", artifact_id=aid, organization_id=corpus.cast.org_id,
                               status="publishing", updated_at=datetime.utcnow() - timedelta(hours=1))

    async def broken_copy(*args, **kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr("app.services.dashboard_copy.copy_dashboard", broken_copy)
    try:
        assert _post(test_client, corpus, aid).status_code >= 500
    except RuntimeError:
        pass  # the test client re-raises what the app did not handle
    with corpus_rows.engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM artifact_releases WHERE id = :r"), {"r": stale}).scalar()
    assert status == "publishing"


@pytest.mark.e2e
def test_the_connection_check_runs_before_the_dashboard_is_locked(corpus, corpus_rows, test_client, spawned,
                                                                 monkeypatch):
    """Building the clients may take a warehouse's handshake: a conversation
    delete or a second Publish must not wait on it (PR #1455 review).
    Postgres only: SQLite has no row locks."""
    from app.services import dashboard_publish

    if corpus_rows.engine.dialect.name != "postgresql":
        pytest.skip("needs row locks")
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    seen = []
    check = dashboard_publish._check_connections

    async def probing(*args, **kwargs):
        with corpus_rows.engine.connect() as conn:
            try:
                conn.execute(text("SELECT id FROM artifacts WHERE id = :a FOR UPDATE NOWAIT"), {"a": aid})
                seen.append("free")
            except Exception:
                seen.append("locked")
            conn.rollback()
        return await check(*args, **kwargs)

    monkeypatch.setattr(dashboard_publish, "_check_connections", probing)
    assert _post(test_client, corpus, aid).status_code == 202
    assert seen == ["free"]
