"""A published dashboard runs whole (phase 5, part A).

A release's copies used to stop at the two helpers still scoped to a
conversation: `load_step` resolved nothing (no conversation to look in) and
no bow monitoring client was installed. Now `load_step` inside a release
finds that release's own copies -- by id, slug or title, as in a
conversation -- and never the draft's steps or an older release's; the bow
client runs against the release (a publish records the monitoring access on
the release it builds, without committing mid-run; a reader's run never
changes a live release); a copy that loads another is withheld only when
what it loads is one identity's slice.
"""
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

BOW = {"version": 1, "source_id": "builtin:bow", "report_ids": [], "scope_ids": None}


def _grid(label: str) -> dict:
    return {"rows": [{"חודש": label, "הכנסות": 1}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _copies(corpus_rows, release) -> dict:
    """The release's copies by query title: query id, step id, step title."""
    found = _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step, s.title AS step_title FROM queries q "
                               "JOIN steps s ON s.id = q.default_step_id WHERE q.artifact_release_id = :r",
                  r=release)
    return {c["title"]: c for c in found}


def _release_access(corpus_rows, release):
    (row,) = _rows(corpus_rows, "SELECT bow_source_access FROM artifact_releases WHERE id = :r", r=release)
    value = row["bow_source_access"]
    return json.loads(value) if isinstance(value, str) else value


def _months(body) -> set:
    return {r.get("חודש") for r in (body.get("data") or {}).get("rows", [])}


def _loads_base_by_step_title(corpus, corpus_rows):
    """The corpus's derived step names its base by the query's title, which
    load_step does not match (it matches the step's); name the step."""
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=(
        "def generate_df(ds_clients, excel_files, load_step):\n"
        "    return load_step('בסיס · ריצה')\n"))


def _filter(test_client, corpus, aid, qid, person="member"):
    resp = test_client.post(f"/api/d/{aid}/queries/{qid}/run", json={"force_refresh": True},
                            headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.e2e
def test_load_step_in_a_release_finds_that_releases_copies(corpus, corpus_rows, test_client):
    """By id, by slug and by title (the copy engine rewrote ids and slugs to
    the copies'); the draft's steps and an older release's never answer."""
    aid = own(corpus_rows, corpus, "refs_in_code/dash")
    older = publish(aid)
    live = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    old, new = _copies(corpus_rows, older), _copies(corpus_rows, live)
    for title, label in (("בסיס", "בסיס"), ("נסתרת", "נסתרת")):
        corpus_rows.update("steps", old[title]["step"], data=_grid(f"ישן-{label}"))
        corpus_rows.update("steps", new[title]["step"], data=_grid(f"חדש-{label}"))
    corpus_rows.update("steps", corpus.ids["refs_in_code/q_base/step"], data=_grid("טיוטה-בסיס"))
    corpus_rows.update("steps", corpus.ids["refs_in_code/q_hidden/step"], data=_grid("טיוטה-נסתרת"))

    for title in ("לפי מזהה", "לפי שם קוד"):  # load_step(<base copy's id | slug>)
        body = _filter(test_client, corpus, aid, new[title]["id"])
        assert body["status"] == "success", body
        assert _months(body) == {"חדש-בסיס"}, title
    body = _filter(test_client, corpus, aid, new["בסיס"]["id"])  # load_step('נסתרת · ריצה'), a title
    assert body["status"] == "success", body
    assert _months(body) == {"חדש-נסתרת"}

    # Running a copy leaves the draft alone.
    (draft,) = _rows(corpus_rows, "SELECT data FROM steps WHERE id = :s", s=corpus.ids["refs_in_code/q_base/step"])
    data = json.loads(draft["data"]) if isinstance(draft["data"], str) else draft["data"]
    assert data == _grid("טיוטה-בסיס")
    assert not _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id IN (:a, :b)",
                     a=corpus.ids["refs_in_code/q_base/step"], b=corpus.ids["refs_in_code/q_hidden/step"])


@pytest.mark.e2e
def test_a_step_that_loads_another_runs_in_the_release(corpus, corpus_rows, test_client, monkeypatch):
    from app.services.report_service import ReportService

    found = ReportService._rerun_target_steps

    async def derived_first(self, db, query_ids):
        targets, unrunnable = await found(self, db, query_ids)
        return list(reversed(targets)), unrunnable  # whatever order the database gave

    monkeypatch.setattr(ReportService, "_rerun_target_steps", derived_first)
    _loads_base_by_step_title(corpus, corpus_rows)
    aid = own(corpus_rows, corpus, "load_step/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    body = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "member")).json()
    assert (body["steps_total"], body["steps_succeeded"], body["steps_failed"]) == (2, 2, 0), body
    derived = _copies(corpus_rows, release)["נגזרת"]
    mine = test_client.get(f"/api/d/{aid}/queries/{derived['id']}/step", headers=as_(corpus, "member")).json()
    # The base ran first: the derived chart loaded the reader's fresh run of
    # it, not the copied snapshot.
    assert _months(mine) == {"ינואר", "פברואר"}
    assert mine["viewer_result"]["status"] == "success"


@pytest.mark.e2e
def test_a_copy_that_loads_another_is_withheld_only_when_that_one_is_an_identitys(corpus, corpus_rows,
                                                                                    test_client):
    """Its snapshot follows what it loads -- in the release, not the draft."""
    _loads_base_by_step_title(corpus, corpus_rows)
    aid = own(corpus_rows, corpus, "load_step/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    copies = _copies(corpus_rows, release)
    path = f"/api/d/{aid}/queries/{copies['נגזרת']['id']}/step"

    body = test_client.get(path, headers=as_(corpus, "member")).json()
    assert body["snapshot_withheld"] is False and body["data"]["rows"]

    identity = [{"name": "UserEmail", "type": "string", "source": "identity"}]
    corpus_rows.update("queries", corpus.ids["load_step/q_base"], parameters=identity)  # the draft's
    assert test_client.get(path, headers=as_(corpus, "member")).json()["snapshot_withheld"] is False

    corpus_rows.update("queries", copies["בסיס"]["id"], parameters=identity)  # the release's
    body = test_client.get(path, headers=as_(corpus, "member")).json()
    assert body["snapshot_withheld"] is True and body["data"] == {}
    assert test_client.get(path, headers=as_(corpus, "owner")).json()["data"]["rows"]


@pytest.mark.e2e
def test_a_readers_run_cannot_load_another_identitys_slice(corpus, corpus_rows, test_client):
    """load_step in a reader's run gets what the step route would give them:
    the shared snapshot of an identity-scoped copy is not theirs to load."""
    _loads_base_by_step_title(corpus, corpus_rows)
    aid = own(corpus_rows, corpus, "load_step/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    copies = _copies(corpus_rows, release)
    assert _filter(test_client, corpus, aid, copies["נגזרת"]["id"])["status"] == "success"
    corpus_rows.update("queries", copies["בסיס"]["id"],
                       parameters=[{"name": "UserEmail", "type": "string", "source": "identity"}])
    assert _filter(test_client, corpus, aid, copies["נגזרת"]["id"])["status"] == "error"


@pytest.mark.e2e
def test_the_monitoring_client_runs_against_the_release(corpus, corpus_rows, test_client):
    """In a reader's run it reads with the release's access and changes
    neither the live release (frozen) nor the conversation. (A publish
    records the access on the release it builds: test_dashboard_publish.)"""
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped")
    release = publish(aid)
    corpus_rows.update("artifacts", aid, visibility="internal")
    (copy,) = _copies(corpus_rows, release).values()
    corpus_rows.update("steps", copy["step"], code=(
        "def generate_df(ds_clients, excel_files):\n"
        "    return ds_clients['bow'].execute_query({'dataset': 'runs', "
        "'metrics': [{'op': 'count', 'name': 'runs'}]})\n"))
    report = corpus.reports["bow_source"]
    before = _rows(corpus_rows, "SELECT bow_source_access FROM reports WHERE id = :r", r=report)
    draft_refs = _rows(corpus_rows, "SELECT source_refs FROM queries WHERE report_id = :r", r=report)
    release_before = _release_access(corpus_rows, release)
    refs_before = _rows(corpus_rows, "SELECT source_refs FROM queries WHERE artifact_release_id = :r", r=release)

    resp = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, "admin"))
    assert resp.status_code == 200, resp.text
    assert (resp.json()["steps_succeeded"], resp.json()["steps_failed"]) == (1, 0), resp.json()
    mine = _rows(corpus_rows, "SELECT status, status_reason FROM step_user_results WHERE step_id = :s",
                 s=copy["step"])
    assert mine == [{"status": "success", "status_reason": None}]

    assert _rows(corpus_rows, "SELECT bow_source_access FROM reports WHERE id = :r", r=report) == before
    assert _rows(corpus_rows, "SELECT source_refs FROM queries WHERE report_id = :r", r=report) == draft_refs
    assert _release_access(corpus_rows, release) == release_before
    assert _rows(corpus_rows, "SELECT source_refs FROM queries WHERE artifact_release_id = :r",
                 r=release) == refs_before


@pytest.mark.e2e
def test_a_chart_that_loads_a_monitoring_entity_runs_for_a_reader(corpus, corpus_rows, test_client):
    """An entity built on monitoring data runs in a reader's run (which used
    to fail: there was no conversation to record its access on); the live
    release stays as it is. (A publish records the access on the release it
    builds: test_dashboard_publish.)"""
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"], code=(
        "def generate_df(ds_clients, excel_files, load_entity):\n"
        "    return load_entity('מודל הכנסות')\n"))
    # The admin's own entity: they may read monitoring data, and by today's
    # entity rules load only an entity materialized with their credentials.
    corpus_rows.update("entities", corpus.ids["entity_from_step"], owner_id=corpus.cast.id("admin"),
                       bow_source_access={**BOW, "organization_id": corpus.cast.org_id})
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _copies(corpus_rows, release).values()
    body = _filter(test_client, corpus, aid, copy["id"], person="admin")
    assert body["status"] == "success", body
    assert body["data"]["rows"]
    assert _release_access(corpus_rows, release) is None
    report = corpus.reports["conv_three_dash"]
    assert _rows(corpus_rows, "SELECT bow_source_access FROM reports WHERE id = :r", r=report) == [
        {"bow_source_access": None}]


@pytest.mark.e2e
def test_a_public_dashboard_never_takes_on_monitoring_data(corpus, corpus_rows, test_client):
    """As a public conversation refuses it: the run fails and the release
    stays as it was."""
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"], code=(
        "def generate_df(ds_clients, excel_files, load_entity):\n"
        "    return load_entity('מודל הכנסות')\n"))
    corpus_rows.update("entities", corpus.ids["entity_from_step"], owner_id=corpus.cast.id("admin"),
                       bow_source_access={**BOW, "organization_id": corpus.cast.org_id})
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    release = publish(aid)
    assert share(test_client, corpus, aid, "public").status_code == 200
    (copy,) = _copies(corpus_rows, release).values()
    resp = test_client.post(f"/api/d/{aid}/queries/{copy['id']}/run", json={"force_refresh": True},
                            headers=as_(corpus, "admin"))
    assert resp.status_code == 403 or resp.json()["status"] == "error", resp.text
    assert _release_access(corpus_rows, release) is None
    refs = _rows(corpus_rows, "SELECT source_refs FROM queries WHERE artifact_release_id = :r", r=release)
    assert "builtin:bow" not in str(refs)
