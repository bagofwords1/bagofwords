"""Restore (roll back) to a kept release: its time, and what the next
publish asks.

Restore makes an earlier release live again, at once, running nothing. Its
rows are as old as that release's last run, so the dashboard says so: its
"refreshed" time is that run's (a successful refresh of that release, else
its publish), and refresh-when-opened sees them as old.

A restore is usually undoing a bad publish. A version still built on the
release it rolled back would bring those changes back: publishing it asks
first, as publishing over someone else's newer work does. A version built on
the restored release, or any publish with no restore in between, does not.
"""
import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own

CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"


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


def _publish(test_client, corpus, aid, person="owner", **body):
    resp = test_client.post(f"/api/artifacts/{aid}/publish", json=body or None, headers=as_(corpus, person))
    if resp.status_code == 202:
        PUBLISHED.append(resp.json()["latest"]["id"])
    return resp


def _live(test_client, corpus, aid):
    return test_client.get(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner")).json()["published"]["id"]


def _restore(test_client, corpus, aid, release_id):
    resp = test_client.post(f"/api/artifacts/{aid}/restore", json={"release_id": release_id},
                            headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text


async def _version(aid: str, report_id: str, user_id: str, *, base_version_id: str | None = None) -> dict:
    """A new completed version in one conversation, as the agent's edit tools
    write it -- on the conversation's newest, or on `base_version_id`."""
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
        if base_version_id:
            version.base_version_id = base_version_id
        await db.commit()
        return {"id": str(version.id), "number": version.version}


def version(aid, report_id, user_id, **kw) -> dict:
    return asyncio.run(_version(aid, report_id, user_id, **kw))


def _two_releases(test_client, corpus, corpus_rows, spawned):
    """v1 published long ago, then v2 published now (the bad one)."""
    aid = own(corpus_rows, corpus, DASH)
    assert _publish(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    first = _live(test_client, corpus, aid)
    corpus_rows.update("artifact_releases", first, published_at=datetime(2026, 1, 1))
    v2 = version(aid, corpus.reports[CONV], corpus.cast.id("owner"))
    assert _publish(test_client, corpus, aid, version_id=v2["id"]).status_code == 202
    _finish(spawned)
    second = _live(test_client, corpus, aid)
    assert second != first
    return aid, first, second, v2


def _refreshed_at(test_client, corpus, aid) -> str:
    meta = test_client.get(f"/api/d/{aid}", headers=as_(corpus, "owner"))
    assert meta.status_code == 200, meta.text
    return meta.json()["last_run_at"]


# ── the restored release's time ────────────────────────────────────────────

@pytest.mark.e2e
def test_after_restore_the_dashboard_shows_when_those_rows_ran(corpus, corpus_rows, test_client, spawned):
    aid, first, second, _ = _two_releases(test_client, corpus, corpus_rows, spawned)
    assert _refreshed_at(test_client, corpus, aid).startswith(datetime.utcnow().strftime("%Y-%m-%d"))

    _restore(test_client, corpus, aid, first)
    assert spawned == []  # still instant: nothing runs
    assert _refreshed_at(test_client, corpus, aid).startswith("2026-01-01")  # its publish: no refresh since


@pytest.mark.e2e
def test_its_last_successful_refresh_counts_not_a_failed_one_or_another_releases(corpus, corpus_rows, test_client,
                                                                                 spawned):
    aid, first, second, _ = _two_releases(test_client, corpus, corpus_rows, spawned)
    org = corpus.cast.org_id

    def ran(release_id, status, when):
        corpus_rows.insert("dashboard_refresh_runs", artifact_id=aid, organization_id=org, release_id=release_id,
                           trigger="schedule", status=status, started_at=when, finished_at=when)

    ran(first, "success", datetime(2026, 2, 1))
    ran(first, "failed", datetime(2026, 3, 1))     # did not replace the rows
    ran(second, "success", datetime(2026, 4, 1))   # another release's rows
    _restore(test_client, corpus, aid, first)
    assert _refreshed_at(test_client, corpus, aid).startswith("2026-02-01")


@pytest.mark.e2e
def test_refresh_when_opened_sees_restored_rows_as_old(corpus, corpus_rows, test_client, spawned):
    aid, first, second, _ = _two_releases(test_client, corpus, corpus_rows, spawned)
    corpus_rows.update("artifacts", aid, refresh_on_view=True)
    _restore(test_client, corpus, aid, first)
    with patch("app.services.dashboard_refresh.claim_scheduled_run", return_value=True):
        opened = test_client.post(f"/api/d/{aid}/refresh", headers=as_(corpus, "owner"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["skipped"] is False, opened.json()  # not "fresh": they ran in January


# ── the next publish after a restore ───────────────────────────────────────

@pytest.mark.e2e
def test_publishing_what_a_restore_undid_asks_first(corpus, corpus_rows, test_client, spawned):
    aid, first, second, v2 = _two_releases(test_client, corpus, corpus_rows, spawned)
    v3 = version(aid, corpus.reports[CONV], corpus.cast.id("owner"))  # built on v2, the release rolled back
    _restore(test_client, corpus, aid, first)

    asked = _publish(test_client, corpus, aid, version_id=v3["id"])
    assert asked.status_code == 409, asked.text
    body = asked.json()
    assert body["error_code"] == "dashboard.published_since"
    params = body["params"]
    assert params["restored"] is True
    assert params["rolled_back_version"] == v2["number"] and params["live_version"] == 1
    assert _live(test_client, corpus, aid) == first

    # The Changes panel says the same before anyone presses Publish.
    changes = test_client.get(f"/api/d/{aid}/draft/changes", headers=as_(corpus, "owner"),
                              params={"version": v3["number"]})
    assert changes.status_code == 200, changes.text
    since = changes.json()["published_since"]
    assert since["restored"] is True and since["rolled_back_version"] == v2["number"]

    # Asked and answered: it publishes.
    assert _publish(test_client, corpus, aid, version_id=v3["id"], force=True).status_code == 202
    _finish(spawned)
    assert _live(test_client, corpus, aid) not in (first, second)


@pytest.mark.e2e
def test_a_version_built_on_the_restored_release_publishes_without_asking(corpus, corpus_rows, test_client, spawned):
    aid, first, second, v2 = _two_releases(test_client, corpus, corpus_rows, spawned)
    _restore(test_client, corpus, aid, first)
    (v1,) = _rows(corpus_rows, "SELECT source_version_id FROM artifact_releases WHERE id = :r", r=first)
    fixed = version(aid, corpus.reports[CONV], corpus.cast.id("owner"), base_version_id=v1["source_version_id"])
    resp = _publish(test_client, corpus, aid, version_id=fixed["id"])
    assert resp.status_code == 202, resp.text
    _finish(spawned)
