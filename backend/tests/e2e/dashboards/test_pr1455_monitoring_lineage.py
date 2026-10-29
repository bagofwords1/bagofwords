"""PR #1455 review findings 3 and 4: monitoring (bow_source_access) lineage
must follow the version actually served, and bringing a dashboard into a
conversation must never loosen that conversation's restrictions."""
import json

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own
from tests.e2e.dashboards.test_collaboration import (
    DASH, _edit, _finish, _grant_editor, _publish_api, _rows, drawn, new_version, spawned,  # noqa: F401
)

# A restriction nobody in the corpus satisfies short of org-wide scope.
def _restricted(corpus):
    return {"version": 1, "source_id": "builtin:bow", "organization_id": corpus.cast.org_id,
            "report_ids": [], "scope_ids": ["no-such-data-source"]}


@pytest.mark.e2e
def test_draft_routes_check_the_shown_versions_monitoring_access(corpus, corpus_rows, test_client, spawned):
    """Finding 3: the owner's conversation (editing[0]) is unrestricted, the
    newest version lives in another editor's monitoring-restricted
    conversation. A co-editor without that scope must not read it through
    any draft route, by default or by version number."""
    aid = own(corpus_rows, corpus, DASH)
    _grant_editor(test_client, corpus, aid, "member", "direct_share")
    avi = _edit(test_client, corpus, aid, "member").json()["report_id"]
    _finish(spawned)
    n = new_version(aid, avi, corpus.cast.id("member"))
    corpus_rows.update("reports", avi, bow_source_access=_restricted(corpus))

    reader = as_(corpus, "direct_share")
    # Sanity: the draft door itself is open to this co-editor.
    assert test_client.get(f"/api/d/{aid}/draft?version=1", headers=reader).status_code == 200
    for path in ("draft", "draft/content", "draft/queries", "draft/queries_with_steps"):
        for suffix in ("", f"?version={n}"):
            resp = test_client.get(f"/api/d/{aid}/{path}{suffix}", headers=reader)
            assert resp.status_code == 403, (path, suffix, resp.status_code, resp.text[:200])
    q = _rows(corpus_rows, "SELECT id FROM queries WHERE report_id = :r", r=avi)[0]["id"]
    step = test_client.get(f"/api/d/{aid}/draft/queries/{q}/step", headers=reader)
    assert step.status_code in (403, 404), step.text


@pytest.mark.e2e
def test_bring_merges_monitoring_restrictions_into_the_destination(corpus, corpus_rows, test_client, spawned):
    """Finding 4: a destination conversation that already carries a weaker
    lineage keeps the incoming, stronger restriction too."""
    aid = own(corpus_rows, corpus, DASH)
    owner_conv = corpus.reports["conv_three_dash"]
    # Org-wide only (scope_ids None): the strongest restriction there is.
    strong = {**_restricted(corpus), "scope_ids": None}
    assert _publish_api(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    _grant_editor(test_client, corpus, aid, "bow_scoped")
    # The monitoring lineage the dashboard carries (as protect_report /
    # protect_release record it after a monitoring run).
    corpus_rows.update("reports", owner_conv, bow_source_access=strong)
    for r in _rows(corpus_rows, "SELECT id FROM artifact_releases WHERE artifact_id = :a", a=aid):
        corpus_rows.update("artifact_releases", r["id"], bow_source_access=strong)
    # bow_scoped holds manage_settings: org-wide, so it may read (and
    # bring) the restricted dashboard.
    member = as_(corpus, "bow_scoped")
    mine = test_client.post("/api/reports", json={"title": "Avi's questions"}, headers=member)
    report_id = mine.json()["id"]
    weak = {"version": 1, "source_id": "builtin:bow", "organization_id": corpus.cast.org_id,
            "report_ids": [], "scope_ids": [corpus.cast.data_sources["chinook"]]}
    corpus_rows.update("reports", report_id, bow_source_access=weak)

    brought = test_client.post(f"/api/artifacts/{aid}/bring", json={"report_id": report_id}, headers=member)
    assert brought.status_code == 200, brought.text
    (row,) = _rows(corpus_rows, "SELECT bow_source_access FROM reports WHERE id = :r", r=report_id)
    access = row["bow_source_access"]
    access = json.loads(access) if isinstance(access, str) else access
    # Org-wide dominates: the conversation now needs org-wide scope too.
    assert access["scope_ids"] is None, access
