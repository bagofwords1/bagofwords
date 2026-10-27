"""PR #1455 review (575197c): whose rows a dashboard serves, and who may
administer its runtime, follow the dashboard -- not who once owned it or who
pressed Publish.

1. Publishing someone else's frozen deck does not make the publisher the
   producer of its rows: the frozen rows stay the author's.
2. A former owner whose edit access was revoked cannot administer the
   dashboard's runtime resources through the conversation they still own.
5. Handing a creator-mode dashboard to another owner drops readers' results
   that ran with the previous owner's credentials.
"""
import asyncio
import json
import uuid
from datetime import datetime

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish
from tests.e2e.dashboards.test_collaboration import (  # noqa: F401 (fixtures)
    _finish, _grant_editor, _publish_api, _rows, drawn, spawned,
)

AUTHOR_MARKER = f"ORIGINAL_AUTHOR_ONLY-{uuid.uuid4().hex[:6]}"
OWNER_MARKER = f"PREVIOUS_OWNER_ONLY-{uuid.uuid4().hex[:6]}"


def _rows_of(marker):
    return {"rows": [{"month": marker, "revenue": 7}], "columns": [{"field": "month"}, {"field": "revenue"}]}


def _page_text(test_client, corpus, aid, person) -> tuple[str, list[dict]]:
    headers = as_(corpus, person)
    queries = test_client.get(f"/api/d/{aid}/queries", headers=headers)
    assert queries.status_code == 200, queries.text
    bodies = []
    for q in queries.json():
        step = test_client.get(f"/api/d/{aid}/queries/{q['id']}/step", headers=headers)
        assert step.status_code == 200, step.text
        bodies.append(step.json())
    assert bodies, "the published dashboard lists no charts"
    return json.dumps(bodies, ensure_ascii=False), bodies


# ── 1. a frozen deck keeps its author as the producer of its rows ────────

@pytest.mark.e2e
@pytest.mark.parametrize("publisher", ["member", "group_share"])
def test_publishing_anothers_frozen_deck_never_hands_the_publisher_their_identity_rows(
        corpus, corpus_rows, test_client, spawned, publisher):
    conv = "conv_three_dash"
    corpus_rows.update("queries", corpus.ids[f"{conv}/q_sales"],
                       parameters=[{"name": "UserEmail", "type": "string", "source": "identity",
                                    "identity_binding": "viewer.email"}])
    corpus_rows.update("steps", corpus.ids[f"{conv}/q_sales/step"], data=_rows_of(AUTHOR_MARKER))
    aid = own(corpus_rows, corpus, f"{conv}/deck")
    if publisher == "group_share":
        _grant_editor(test_client, corpus, aid, groups=("analysts",))
    else:
        _grant_editor(test_client, corpus, aid, publisher)
    started = _publish_api(test_client, corpus, aid, publisher)
    assert started.status_code == 202, started.text
    _finish(spawned)
    live = _rows(corpus_rows, "SELECT r.status FROM artifact_releases r JOIN artifacts a "
                              "ON a.published_release_id = r.id WHERE a.id = :a", a=aid)
    assert live == [{"status": "ready"}]

    text, bodies = _page_text(test_client, corpus, aid, publisher)
    assert AUTHOR_MARKER not in text, "the publisher got the deck author's identity-scoped rows"
    assert all(b["snapshot_withheld"] for b in bodies)
    # The author still reads their own frozen rows.
    owner_text, _ = _page_text(test_client, corpus, aid, "owner")
    assert AUTHOR_MARKER in owner_text


# ── 2. runtime resources follow the dashboard's current access ───────────

def _create_collection(test_client, corpus, aid, person):
    return test_client.post(f"/api/artifacts/{aid}/runtime/resources", headers=as_(corpus, person), json={
        "action": "create", "idempotency_key": str(uuid.uuid4()),
        "definition": {"name": f"entries_{uuid.uuid4().hex[:6]}", "fields": {"title": {"type": "string"}}}})


@pytest.mark.e2e
def test_a_revoked_former_owner_cannot_administer_runtime_resources(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    _grant_editor(test_client, corpus, aid, "member")
    moved = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                             headers=as_(corpus, "owner"))
    assert moved.status_code == 200, moved.text
    # While still an editor, the previous owner (whose conversation it is) manages them.
    assert _create_collection(test_client, corpus, aid, "owner").status_code == 200
    # The new owner makes it private and removes every editor.
    revoked = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "member"),
                              json={"visibility": "none", "editor_user_ids": [], "editor_group_ids": []})
    assert revoked.status_code == 200, revoked.text
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "owner")).status_code == 404

    refused = _create_collection(test_client, corpus, aid, "owner")
    assert refused.status_code in (403, 404), refused.text
    listed = test_client.get(f"/api/artifacts/{aid}/runtime/resources", headers=as_(corpus, "owner"))
    assert listed.status_code in (403, 404), listed.text
    # The current owner still administers them.
    assert _create_collection(test_client, corpus, aid, "member").status_code == 200


# ── 5. transfer drops results run with the previous owner's credentials ──

@pytest.fixture
def clients(monkeypatch):
    from app.services.data_source_service import DataSourceService

    async def none(self, db, data_source, current_user=None, **kwargs):
        return {}

    monkeypatch.setattr(DataSourceService, "construct_clients", none)


async def _seed_reader_results(release_id: str, user_id: str, organization_id: str, marker: str) -> int:
    """A reader's successful run of every copy, on the owner's credentials.
    Seeded directly: a real run needs a live connector (the boundary this
    suite stubs), and the rows are encrypted, so not through raw SQL."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.query import Query
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult

    async with async_session_maker() as db:
        steps = (await db.execute(select(Step.id).join(Query, Query.default_step_id == Step.id)
                                  .where(Query.artifact_release_id == release_id))).scalars().all()
        for step_id in steps:
            db.add(StepUserResult(step_id=str(step_id), user_id=user_id, organization_id=organization_id,
                                  status="success", data=_rows_of(marker), executed_as="creator",
                                  params_fingerprint="", last_run_at=datetime.utcnow()))
        await db.commit()
        return len(steps)


@pytest.mark.e2e
def test_transfer_drops_reader_results_run_with_the_previous_owners_credentials(
        corpus, corpus_rows, test_client, spawned, clients):
    aid = own(corpus_rows, corpus, "user_required/dash")
    publish(aid)
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"), json={
        "visibility": "shared", "user_ids": [corpus.cast.id("direct_share")],
        "editor_user_ids": [corpus.cast.id("member")]})
    assert resp.status_code == 200, resp.text
    resp = test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"shared_run_identity": "creator"})
    assert resp.status_code == 200, resp.text
    _finish(spawned)
    (live,) = _rows(corpus_rows, "SELECT published_release_id AS r FROM artifacts WHERE id = :a", a=aid)
    assert asyncio.run(_seed_reader_results(live["r"], corpus.cast.id("direct_share"), corpus.cast.org_id,
                                            OWNER_MARKER)) > 0
    before, _ = _page_text(test_client, corpus, aid, "direct_share")
    assert OWNER_MARKER in before  # the reader's cached run, under the current owner

    moved = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                             headers=as_(corpus, "owner"))
    assert moved.status_code == 200, moved.text
    after, _ = _page_text(test_client, corpus, aid, "direct_share")
    assert OWNER_MARKER not in after, "a reader kept rows run with the previous owner's credentials"
