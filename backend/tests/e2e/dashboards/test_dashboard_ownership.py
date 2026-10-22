"""A dashboard knows its owner and project, and outlives no group (phase 4).

- A dashboard made from now on is owned by its conversation's owner and sits
  in its conversation's project; moving the conversation moves it.
- Deleting a group removes its dashboard grants. SQLite does not enforce the
  foreign keys' ON DELETE here, so the code removes them itself.
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share


def _artifact_row(corpus_rows, aid):
    with corpus_rows.engine.connect() as conn:
        return conn.execute(text("SELECT owner_id, project_id FROM artifacts WHERE id = :a"), {"a": aid}).one()


def _new_dashboard(test_client, corpus, report_id, by="owner"):
    resp = test_client.post("/api/artifacts", headers=as_(corpus, by), json={
        "report_id": report_id, "title": "לוח חדש", "mode": "page",
        "content": {"code": "function App() { return null }", "visualization_ids": []}})
    assert resp.status_code == 200, resp.text
    return resp.json()["artifact_id"]


@pytest.mark.e2e
def test_a_new_dashboard_belongs_to_its_conversations_owner_and_project(
        corpus, corpus_rows, test_client, create_report, create_project):
    owner = corpus.cast.people["owner"]
    project = create_project(name="פרויקט של הבעלים", user_token=owner.token, org_id=corpus.cast.org_id)["id"]
    in_project = create_report(title="בתוך פרויקט", user_token=owner.token, org_id=corpus.cast.org_id,
                               project_id=project)["id"]
    at_root = create_report(title="בלי פרויקט", user_token=owner.token, org_id=corpus.cast.org_id)["id"]
    assert tuple(_artifact_row(corpus_rows, _new_dashboard(test_client, corpus, in_project))) == (owner.user_id, project)
    assert tuple(_artifact_row(corpus_rows, _new_dashboard(test_client, corpus, at_root))) == (owner.user_id, None)


@pytest.mark.e2e
def test_moving_a_conversation_moves_its_dashboards(corpus, corpus_rows, test_client, create_report, create_project):
    owner = corpus.cast.people["owner"]
    org = corpus.cast.org_id
    project = create_project(name="יעד", user_token=owner.token, org_id=org)["id"]
    report = create_report(title="לזוז", user_token=owner.token, org_id=org)["id"]
    other = create_report(title="נשארת", user_token=owner.token, org_id=org)["id"]
    first, second = (_new_dashboard(test_client, corpus, report) for _ in range(2))
    stays = _new_dashboard(test_client, corpus, other)

    moved = test_client.post("/api/reports/bulk/move", headers=as_(corpus, "owner"),
                             json={"report_ids": [report], "project_id": project})
    assert moved.status_code == 200, moved.text
    assert [_artifact_row(corpus_rows, a).project_id for a in (first, second, stays)] == [project, project, None]

    back = test_client.put(f"/api/reports/{report}", headers=as_(corpus, "owner"), json={"project_id": ""})
    assert back.status_code == 200, back.text
    assert [_artifact_row(corpus_rows, a).project_id for a in (first, second)] == [None, None]

    into = test_client.put(f"/api/reports/{report}", headers=as_(corpus, "owner"), json={"project_id": project})
    assert into.status_code == 200, into.text
    assert _artifact_row(corpus_rows, first).project_id == project


@pytest.mark.e2e
def test_deleting_a_group_removes_its_dashboard_grants(corpus, corpus_rows, test_client):
    org = corpus.cast.org_id
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    publish(aid)
    group = test_client.post(f"/api/organizations/{org}/groups", headers=as_(corpus, "admin"),
                             json={"name": "קבוצה זמנית"})
    assert group.status_code in (200, 201), group.text
    gid = group.json()["id"]
    assert test_client.put(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": "shared", "group_ids": [gid, corpus.cast.groups["analysts"]]}
                           ).status_code == 200

    assert test_client.delete(f"/api/organizations/{org}/groups/{gid}",
                              headers=as_(corpus, "admin")).status_code == 204
    with corpus_rows.engine.connect() as conn:
        left = conn.execute(text("SELECT count(*) FROM artifact_shares WHERE group_id = :g"), {"g": gid}).scalar()
    assert left == 0
    sharing = test_client.get(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, "owner")).json()
    assert sharing["group_ids"] == [corpus.cast.groups["analysts"]]
    assert share(test_client, corpus, aid, "shared").status_code == 200
