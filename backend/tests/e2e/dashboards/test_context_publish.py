"""Publishing a dashboard whose query waits for a value only its viewer gives.

A context-driven app gets, say, the customer from the host application; its
query declares a required `input` parameter with no default. Publish (and a
refresh, which shares its run) used to run that query without the value and
fail the whole publish. Now such a step is not run: its copy holds no rows and
the marker `info.needs_input`. At view time the value comes through the
existing filter run, with today's rules.

A reader's run of the whole dashboard waits on that step too, with three
safety rules: the wait is not a run of theirs (a withheld snapshot stays
withheld, and the export still wants a real run), and a source the step uses
that failed for them keeps its "connect" / "no access" gate.

Queries whose parameters the agent declared cannot be produced through the
API, so the corpus rows (draft and copy queries and steps) are edited
directly with `corpus_rows.update`.
"""
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share
from tests.e2e.dashboards.test_dashboard_exports import no_vendor_libs, pdf_pages  # noqa: F401 (fixtures)
from tests.e2e.dashboards.test_dashboard_publish import BROKEN, _finish, _post, _status, drawn, spawned  # noqa: F401

DASH = "options_sources/dash"
WAITING_TITLE = "שירים לפי ז'אנר"      # q_by_id: the chart that waits for the customer
GENRES_TITLE = "ז'אנרים"               # q_genres
OPTIONAL_TITLE = "אלבומים לפי ז'אנר"   # q_by_title: an optional `genre`, no default

CUSTOMER = {"name": "customer_id", "type": "number", "required": True, "default": None, "source": "input"}
# Returns a row even when the value is None: a step that ran without the value
# would leave a row behind, so `rows == []` proves it never ran.
NEEDS_CUSTOMER = (
    "def generate_df(ds_clients, excel_files, params):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'לקוח': [params.get('customer_id')], 'הכנסות': [1]})\n"
)
DRAFT_ROWS = {"rows": [{"לקוח": "טיוטה", "הכנסות": 1}], "columns": [{"field": "לקוח"}, {"field": "הכנסות"}]}
SECRET = {"rows": [{"עמודה_סודית": "של הבעלים"}], "columns": [{"field": "עמודה_סודית"}]}


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _plain(value):
    """A JSON column as stored: decoded, and opened if it is sealed."""
    from app.ee.encryption.types import decrypt_value, is_encrypted_envelope

    value = json.loads(value) if isinstance(value, str) else value
    return decrypt_value(value) if is_encrypted_envelope(value) else value


def _copies(corpus_rows, release) -> dict:
    """The release's copies by title: query id, step id, data, applied params."""
    out = {}
    for r in _rows(corpus_rows, "SELECT q.id, q.title, s.id AS step, s.data, s.applied_params FROM queries q "
                                "JOIN steps s ON s.id = q.default_step_id WHERE q.artifact_release_id = :r",
                   r=release):
        out[r["title"]] = {**r, "data": _plain(r["data"]) or {}, "applied_params": _plain(r["applied_params"])}
    return out


def _own_rows(corpus_rows, step, person_id):
    rows = _rows(corpus_rows, "SELECT id, status, data FROM step_user_results WHERE step_id = :s AND user_id = :u",
                 s=step, u=person_id)
    return [{**r, "data": _plain(r["data"])} for r in rows]


def _draft(corpus, corpus_rows, *, applied=None):
    """The dashboard of a context app: q_by_id waits for the customer."""
    aid = own(corpus_rows, corpus, DASH)
    corpus_rows.update("queries", corpus.ids["options_sources/q_by_id"], parameters=[CUSTOMER])
    corpus_rows.update("steps", corpus.ids["options_sources/q_by_id/step"], code=NEEDS_CUSTOMER, data=DRAFT_ROWS,
                       applied_params=applied if applied is not None else {"customer_id": None})
    for key in ("q_genres", "q_by_title"):  # so a fresh run is told apart from the draft's rows
        corpus_rows.update("steps", corpus.ids[f"options_sources/{key}/step"],
                           data={"rows": [{"חודש": "טיוטה", "הכנסות": 0}],
                                 "columns": [{"field": "חודש"}, {"field": "הכנסות"}]})
    return aid


def _published_via_publish(corpus, corpus_rows, test_client, spawned, **draft):  # noqa: F811
    aid = _draft(corpus, corpus_rows, **draft)
    started = _post(test_client, corpus, aid)
    assert started.status_code == 202, started.text
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    return aid, status


def _months(data) -> set:
    return {r.get("חודש") for r in (data or {}).get("rows", [])}


def _step(test_client, corpus, aid, qid, person):
    resp = test_client.get(f"/api/d/{aid}/queries/{qid}/step", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run(test_client, corpus, aid, person="member"):
    resp = test_client.post(f"/api/d/{aid}/run", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _filter_run(test_client, corpus, aid, qid, body, person="member"):
    return test_client.post(f"/api/d/{aid}/queries/{qid}/run", json=body, headers=as_(corpus, person))


# ── Publish ──────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_query_waiting_for_a_required_input_publishes_without_running(  # S1, S2
        corpus, corpus_rows, test_client, spawned):  # noqa: F811
    aid, status = _published_via_publish(corpus, corpus_rows, test_client, spawned)
    assert status["latest"]["status"] == "ready", status
    assert status["published"] and status["published"]["id"] == status["latest"]["id"]

    copies = _copies(corpus_rows, status["published"]["id"])
    waiting = copies[WAITING_TITLE]
    assert waiting["data"]["rows"] == [], "never run, and none of the draft's rows"
    assert waiting["data"]["info"]["needs_input"] == ["customer_id"]
    assert waiting["applied_params"] == {"customer_id": None}, "the stored values are kept; the missing one stays None"
    assert "טיוטה" not in json.dumps(waiting["data"], ensure_ascii=False)

    # The other charts ran as today, the optional one included.
    assert _months(copies[GENRES_TITLE]["data"]) == {"ינואר", "פברואר"}
    optional = copies[OPTIONAL_TITLE]["data"]
    assert _months(optional) == {"ינואר", "פברואר"}
    assert "needs_input" not in (optional.get("info") or {})


@pytest.mark.e2e
def test_a_required_input_with_a_stored_value_runs_as_before(corpus, corpus_rows, test_client, spawned):  # S8 # noqa: F811
    aid, status = _published_via_publish(corpus, corpus_rows, test_client, spawned, applied={"customer_id": 14})
    assert status["latest"]["status"] == "ready", status
    waiting = _copies(corpus_rows, status["published"]["id"])[WAITING_TITLE]
    assert [r["לקוח"] for r in waiting["data"]["rows"]] == [14]
    assert "needs_input" not in (waiting["data"].get("info") or {})


@pytest.mark.e2e
def test_publish_stays_all_or_nothing(corpus, corpus_rows, test_client, spawned):  # S7 # noqa: F811
    aid = _draft(corpus, corpus_rows)
    corpus_rows.update("steps", corpus.ids["options_sources/q_genres/step"], code=BROKEN)
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "failed"
    assert GENRES_TITLE in (status["latest"]["error"] or "")
    assert status["published"] is None


# ── the viewer gives the value ───────────────────────────────────────────

@pytest.fixture
def context_dash(corpus, corpus_rows, test_client, spawned):  # noqa: F811
    aid, status = _published_via_publish(corpus, corpus_rows, test_client, spawned)
    assert status["latest"]["status"] == "ready", status
    assert share(test_client, corpus, aid, "internal").status_code == 200
    return aid, status["published"]["id"]


@pytest.mark.e2e
def test_the_reader_supplies_the_input_at_view_time(corpus, corpus_rows, test_client, context_dash):  # S3
    aid, release = context_dash
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    for value in (14, 27):
        resp = _filter_run(test_client, corpus, aid, copy["id"], {"params": {"customer_id": value}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert [r["לקוח"] for r in body["data"]["rows"]] == [value]
    assert _copies(corpus_rows, release)[WAITING_TITLE]["data"]["rows"] == [], "the copy is not the reader's"


@pytest.mark.e2e
@pytest.mark.parametrize("body", [{"params": {}}, {}])
def test_without_the_input_the_reader_gets_a_typed_error(corpus, corpus_rows, test_client, context_dash, body):  # S4
    aid, release = context_dash
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    resp = _filter_run(test_client, corpus, aid, copy["id"], body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error_code"] == "validation"
    assert _rows(corpus_rows, "SELECT id FROM step_user_results WHERE step_id = :s AND status = 'error'",
                 s=copy["step"]) == []


@pytest.mark.e2e
def test_a_readers_dashboard_run_waits_for_input_without_failing(  # S5
        corpus, corpus_rows, test_client, context_dash):
    aid, release = context_dash
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    run = _run(test_client, corpus, aid)
    assert run["steps_failed"] == 0, run
    assert run["steps_needing_input"] == 1, run

    step = _step(test_client, corpus, aid, copy["id"], "member")
    assert step["viewer_result"]["status"] == "success"
    assert step["viewer_result"]["needs_input"] == ["customer_id"]
    assert step["snapshot_withheld"] is False
    assert step["data"]["rows"] == []
    (mine,) = _own_rows(corpus_rows, copy["step"], corpus.cast.id("member"))
    assert mine["data"]["columns"] == [] and mine["data"]["rows"] == []

    # Once the reader gave a value, their next dashboard run re-runs with it.
    assert _filter_run(test_client, corpus, aid, copy["id"], {"params": {"customer_id": 14}}).status_code == 200
    again = _run(test_client, corpus, aid)
    assert again["steps_failed"] == 0 and again["steps_needing_input"] == 0, again
    step = _step(test_client, corpus, aid, copy["id"], "member")
    assert [r["לקוח"] for r in step["data"]["rows"]] == [14]
    assert "needs_input" not in step["viewer_result"]


# ── refresh ──────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_refresh_keeps_waiting_for_input(corpus, corpus_rows, test_client, context_dash):  # S6
    aid, release = context_dash
    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success", resp.json()
    waiting = _copies(corpus_rows, release)[WAITING_TITLE]
    assert waiting["data"]["rows"] == [] and waiting["data"]["info"]["needs_input"] == ["customer_id"]


@pytest.mark.e2e
def test_a_refresh_clears_a_readers_rows_on_a_waiting_step_and_their_next_run_waits_again(  # S15
        corpus, corpus_rows, test_client, context_dash):
    aid, release = context_dash
    copies = _copies(corpus_rows, release)
    waiting, genres = copies[WAITING_TITLE], copies[GENRES_TITLE]
    member = corpus.cast.id("member")
    assert _filter_run(test_client, corpus, aid, waiting["id"], {"params": {"customer_id": 14}}).status_code == 200
    _run(test_client, corpus, aid)
    assert _own_rows(corpus_rows, waiting["step"], member) and _own_rows(corpus_rows, genres["step"], member)

    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["status"] == "success", resp.text
    # Today's delete, waiting steps included: the page's auto-run then runs again.
    assert _own_rows(corpus_rows, waiting["step"], member) == []
    assert _own_rows(corpus_rows, genres["step"], member) == []
    after = _copies(corpus_rows, release)[WAITING_TITLE]
    assert after["data"]["rows"] == [] and after["data"]["info"]["needs_input"] == ["customer_id"]

    run = _run(test_client, corpus, aid)
    assert run["steps_needing_input"] == 1 and run["steps_failed"] == 0, run
    step = _step(test_client, corpus, aid, waiting["id"], "member")
    assert step["viewer_result"]["needs_input"] == ["customer_id"] and step["data"]["rows"] == []


# ── a wait is not access ─────────────────────────────────────────────────

def _edit_copy(corpus_rows, copy, *, parameters, applied, code):
    corpus_rows.update("queries", copy["id"], parameters=parameters)
    corpus_rows.update("steps", copy["step"], code=code, data=SECRET, applied_params=applied)


@pytest.mark.e2e
def test_a_readers_wait_for_input_never_opens_a_withheld_chart(corpus, corpus_rows, test_client):  # S13
    # The snapshot is withheld from `member` because the copy binds an identity
    # (one identity's slice). The corpus's RLS dashboard (`rls/dash`) would be
    # the other case, but its source cannot be built for a reader in the test
    # environment (connection_failed), so its run never reaches the wait.
    aid = own(corpus_rows, corpus, DASH)
    release = publish(aid)
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    parameters = [{"name": "UserEmail", "type": "string", "required": False, "default": None,
                   "source": "identity", "identity_binding": "viewer.email"}, CUSTOMER]
    applied = {"UserEmail": "owner@corpus.com"}
    assert share(test_client, corpus, aid, "internal").status_code == 200
    _edit_copy(corpus_rows, copy, parameters=parameters, applied=applied, code=NEEDS_CUSTOMER)
    assert _step(test_client, corpus, aid, copy["id"], "owner")["snapshot_withheld"] is False

    run = _run(test_client, corpus, aid)
    assert run["data_source_errors"] == [], run  # K10: the run reaches the waiting path
    assert run["steps_needing_input"] >= 1, run
    assert [r for r in _own_rows(corpus_rows, copy["step"], corpus.cast.id("member")) if r["status"] == "error"] == []

    resp = test_client.get(f"/api/d/{aid}/queries/{copy['id']}/step", headers=as_(corpus, "member"))
    assert resp.status_code == 200, resp.text
    step = resp.json()
    assert step["snapshot_withheld"] is True
    assert step["code"] == "" and step["data"] == {} and step["applied_params"] is None
    assert step["viewer_result"]["status"] == "success"
    assert step["viewer_result"]["needs_input"] == ["customer_id"]
    assert "עמודה_סודית" not in resp.text and "owner@corpus.com" not in resp.text
    export = test_client.get(f"/api/d/{aid}/queries/{copy['id']}/export", headers=as_(corpus, "member"))
    assert export.status_code == 403, export.text


@pytest.mark.e2e
@pytest.mark.parametrize("case", ["connect", "no_access"])
def test_a_reader_who_must_connect_or_has_no_access_still_gets_that_gate(  # S14
        corpus, corpus_rows, test_client, case):
    from app.services.access_errors import CREDENTIALS_REQUIRED_REASON, NO_ACCESS_REASON

    source = "מחסן אישי"
    aid = own(corpus_rows, corpus, "user_required/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _copies(corpus_rows, release).values()
    _edit_copy(corpus_rows, copy, parameters=[CUSTOMER], applied=None,
               code=NEEDS_CUSTOMER + f"    # reads ds_clients['{source}']\n")
    if case == "no_access":
        (ds,) = _rows(corpus_rows, "SELECT id FROM data_sources WHERE name = :n AND organization_id = :o",
                      n=source, o=corpus.cast.org_id)
        corpus_rows.update("data_sources", ds["id"], is_public=False)
    code = "credentials_required" if case == "connect" else "no_access"

    run = _run(test_client, corpus, aid)
    assert run["data_source_errors"] and run["data_source_errors"][0]["code"] == code, run  # K11
    assert run["steps_needing_input"] == 0 and run["steps_failed"] >= 1, run

    step = _step(test_client, corpus, aid, copy["id"], "member")
    result = step["viewer_result"]
    assert result["status"] == "error" and result["error_code"] == code
    assert result["status_reason"] == (CREDENTIALS_REQUIRED_REASON if case == "connect" else NO_ACCESS_REASON)
    assert step["snapshot_withheld"] is True and step["code"] == ""


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["html", "pdf"])
def test_a_waiting_row_is_not_a_run_of_theirs_for_the_export(  # S16
        corpus, corpus_rows, test_client, no_vendor_libs, pdf_pages, fmt):  # noqa: F811
    from app.services.dashboard_runs import needs_input_frame

    aid = own(corpus_rows, corpus, "user_required/dash")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    (copy,) = _copies(corpus_rows, release).values()
    # What a reader's dashboard run leaves on a waiting step; the run itself
    # needs the personal connection's provider, so the row is written directly.
    row = corpus_rows.insert("step_user_results", step_id=copy["step"], user_id=corpus.cast.id("has_results"),
                             organization_id=corpus.cast.org_id, report_id=None, status="success",
                             executed_as="viewer", params_fingerprint="",
                             data=needs_input_frame(None, ["customer_id"]), applied_params=None)
    resp = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "has_results"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "dashboard.export_needs_run"

    # The control: the same row holding a real run counts.
    corpus_rows.update("step_user_results", row, data={"rows": [{"מי": "has_results"}], "columns": [{"field": "מי"}]})
    assert test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "has_results")).status_code == 200


# ── review fixes (item 3, cycle 1) ───────────────────────────────────────

# A required identity param whose binding has no value for anyone (D3: it still fails).
UNBOUND = {"name": "region", "type": "string", "required": True, "default": None, "source": "identity",
           "identity_binding": "viewer.profile_attributes.no_such_attribute"}


@pytest.mark.parametrize("invalid,stored", [
    (UNBOUND, {"customer_id": None}),
    ({"name": "limit", "type": "number", "source": "input"}, {"customer_id": None, "limit": "invalid"}),
])
def test_publish_validates_other_parameters_while_waiting_for_host_input(
        corpus, corpus_rows, test_client, spawned, invalid, stored):
    aid = _draft(corpus, corpus_rows, applied=stored)
    corpus_rows.update("queries", corpus.ids["options_sources/q_by_id"], parameters=[invalid, CUSTOMER])
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "failed", status
    assert invalid["name"] in status["latest"]["error"]


@pytest.mark.e2e
def test_a_required_identity_param_that_resolves_to_none_fails_on_every_run(  # D3
        corpus, corpus_rows, test_client, context_dash):
    """The error row has no applied params; the next run must not read that as
    "the customer is missing" and store a wait over the real error."""
    aid, release = context_dash
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    _edit_copy(corpus_rows, copy, parameters=[UNBOUND, CUSTOMER], applied={"customer_id": 14}, code=NEEDS_CUSTOMER)
    member = corpus.cast.id("member")
    for attempt in (1, 2):
        run = _run(test_client, corpus, aid)
        assert run["steps_failed"] >= 1 and run["steps_needing_input"] == 0, (attempt, run)
        rows = _own_rows(corpus_rows, copy["step"], member)
        assert [r["status"] for r in rows] == ["error"], (attempt, rows)
        result = _step(test_client, corpus, aid, copy["id"], "member")["viewer_result"]
        assert result["status"] == "error" and "region" in (result["status_reason"] or ""), (attempt, result)
        assert "needs_input" not in result, (attempt, result)


@pytest.mark.e2e
def test_a_required_identity_param_that_resolves_to_none_fails_even_while_an_input_is_missing(  # D3
        corpus, corpus_rows, test_client, context_dash):
    aid, release = context_dash
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    _edit_copy(corpus_rows, copy, parameters=[UNBOUND, CUSTOMER], applied=None, code=NEEDS_CUSTOMER)
    run = _run(test_client, corpus, aid)
    assert run["steps_failed"] >= 1 and run["steps_needing_input"] == 0, run
    result = _step(test_client, corpus, aid, copy["id"], "member")["viewer_result"]
    assert result["status"] == "error" and "region" in (result["status_reason"] or ""), result


LOADS_BASE = (
    "def generate_df(ds_clients, excel_files, load_step):\n"
    "    import pandas as pd\n"
    "    base = load_step('בסיס · ריצה')\n"
    "    return pd.DataFrame({'שורות': [len(base)]})\n"
)


@pytest.mark.e2e
def test_a_step_that_loads_a_waiting_step_waits_too(corpus, corpus_rows, test_client, spawned):  # noqa: F811
    """Its loader is never handed the waiting step's empty frame as if it were
    data: at Publish and in a reader's run it waits for the same input."""
    corpus_rows.update("queries", corpus.ids["load_step/q_base"], parameters=[CUSTOMER])
    corpus_rows.update("steps", corpus.ids["load_step/q_base/step"], code=NEEDS_CUSTOMER, data=DRAFT_ROWS,
                       applied_params={"customer_id": None})
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=LOADS_BASE)
    aid = own(corpus_rows, corpus, "load_step/dash")
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "ready", status
    copies = _copies(corpus_rows, status["published"]["id"])
    derived = copies["נגזרת"]
    assert derived["data"]["rows"] == [], "the loader never ran on an empty frame"
    assert derived["data"]["info"]["needs_input"] == ["customer_id"]

    assert share(test_client, corpus, aid, "internal").status_code == 200
    run = _run(test_client, corpus, aid)
    assert run["steps_failed"] == 0 and run["steps_needing_input"] == 2, run
    step = _step(test_client, corpus, aid, derived["id"], "member")
    assert step["viewer_result"]["needs_input"] == ["customer_id"], step["viewer_result"]
    (mine,) = _own_rows(corpus_rows, derived["step"], corpus.cast.id("member"))
    assert mine["data"]["rows"] == [] and mine["data"]["columns"] == []

    # Once the reader gave the value, both run: the loader reads their fresh base.
    base = copies["בסיס"]
    assert _filter_run(test_client, corpus, aid, base["id"], {"params": {"customer_id": 14}}).status_code == 200
    again = _run(test_client, corpus, aid)
    assert again["steps_failed"] == 0 and again["steps_needing_input"] == 0, again
    step = _step(test_client, corpus, aid, derived["id"], "member")
    assert step["data"]["rows"] == [{"שורות": 1}] and "needs_input" not in step["viewer_result"]


def _step_title(corpus_rows, step_id):
    (row,) = _rows(corpus_rows, "SELECT title FROM steps WHERE id = :s", s=step_id)
    return row["title"]


def _audit(corpus_rows, action, aid):
    rows = _rows(corpus_rows, "SELECT details FROM audit_logs WHERE action = :a AND resource_id = :r",
                 a=action, r=aid)
    return [json.loads(r["details"]) if isinstance(r["details"], str) else r["details"] for r in rows]


@pytest.mark.e2e
def test_the_owner_is_told_which_steps_wait_for_input(corpus, corpus_rows, test_client, context_dash):
    aid, release = context_dash
    label = _step_title(corpus_rows, _copies(corpus_rows, release)[WAITING_TITLE]["step"])
    status = _status(test_client, corpus, aid)
    assert status["published"]["steps_needing_input"] == 1, status
    assert status["published"]["steps_needing_input_labels"] == [label], status
    (published,) = _audit(corpus_rows, "dashboard.published", aid)
    assert published["steps_needing_input"] == 1 and published["steps_needing_input_labels"] == [label]

    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success", body
    assert body["steps_needing_input"] == 1 and body["steps_needing_input_labels"] == [label], body
    (refreshed,) = _audit(corpus_rows, "dashboard.refreshed", aid)
    assert refreshed["steps_needing_input"] == 1 and refreshed["steps_needing_input_labels"] == [label]


@pytest.mark.e2e
def test_a_stale_stored_value_fails_publish_naming_the_key(corpus, corpus_rows, test_client, spawned):  # noqa: F811
    """The parameter was renamed: its stored value sits under a name no
    parameter declares. Not a silent wait for input: the publish fails."""
    aid, status = _published_via_publish(corpus, corpus_rows, test_client, spawned, applied={"cust_id": 14})
    assert status["latest"]["status"] == "failed", status
    assert "cust_id" in (status["latest"]["error"] or ""), status
    assert status["published"] is None


@pytest.mark.e2e
def test_a_stale_stored_value_fails_a_refresh_naming_the_key(corpus, corpus_rows, test_client, context_dash):
    aid, release = context_dash
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    corpus_rows.update("steps", copy["step"], applied_params={"cust_id": 14})
    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed" and "cust_id" in (body["error"] or ""), body


@pytest.mark.e2e
def test_a_filter_run_of_a_step_loading_a_waiting_step_never_computes_over_its_empty_frame(
        corpus, corpus_rows, test_client, spawned):  # noqa: F811
    """Outside a dashboard run (no wait to propagate to), load_step refuses the
    waiting step: the loader's own run fails instead of computing over an
    empty frame as if it were the loaded step's rows."""
    corpus_rows.update("queries", corpus.ids["load_step/q_base"], parameters=[CUSTOMER])
    corpus_rows.update("steps", corpus.ids["load_step/q_base/step"], code=NEEDS_CUSTOMER, data=DRAFT_ROWS,
                       applied_params={"customer_id": None})
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=LOADS_BASE)
    aid = own(corpus_rows, corpus, "load_step/dash")
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    release = _status(test_client, corpus, aid)["published"]["id"]
    assert share(test_client, corpus, aid, "internal").status_code == 200
    derived = _copies(corpus_rows, release)["נגזרת"]
    resp = _filter_run(test_client, corpus, aid, derived["id"], {"force_refresh": True})
    body = resp.json()
    assert body.get("status") == "error", body
    assert "load_step" in (body.get("error") or ""), body
    assert not ((body.get("data") or {}).get("rows")), body


YEAR = {"name": "year", "type": "number", "required": True, "default": None, "source": "input"}
NEEDS_CUSTOMER_AND_YEAR = (
    "def generate_df(ds_clients, excel_files, params):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'לקוח': [params.get('customer_id')], 'שנה': [params.get('year')]})\n"
)


@pytest.mark.e2e
def test_two_required_inputs_one_stored_the_reader_gives_only_the_context_one(  # E8
        corpus, corpus_rows, test_client, spawned):  # noqa: F811
    """The author stored the year; only the customer comes from the host. The
    copy keeps the year, so the reader waits for the customer alone (on every
    run), and the page, which seeds its values from the served params, renders
    once the app gives the customer."""
    aid = own(corpus_rows, corpus, DASH)
    corpus_rows.update("queries", corpus.ids["options_sources/q_by_id"], parameters=[CUSTOMER, YEAR])
    corpus_rows.update("steps", corpus.ids["options_sources/q_by_id/step"], code=NEEDS_CUSTOMER_AND_YEAR,
                       data=DRAFT_ROWS, applied_params={"customer_id": None, "year": 2024})
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "ready", status
    release = status["published"]["id"]
    copy = _copies(corpus_rows, release)[WAITING_TITLE]
    assert copy["data"]["rows"] == [] and copy["data"]["info"]["needs_input"] == ["customer_id"]
    assert copy["applied_params"] == {"customer_id": None, "year": 2024}
    assert share(test_client, corpus, aid, "internal").status_code == 200

    for attempt in (1, 2):  # the second run reads the reader's own waiting row
        run = _run(test_client, corpus, aid)
        assert run["steps_failed"] == 0 and run["steps_needing_input"] == 1, (attempt, run)
        step = _step(test_client, corpus, aid, copy["id"], "member")
        assert step["viewer_result"]["needs_input"] == ["customer_id"], (attempt, step["viewer_result"])
    served = step["applied_params"]
    assert served == {"customer_id": None, "year": 2024}, served

    resp = _filter_run(test_client, corpus, aid, copy["id"], {"params": {**served, "customer_id": 14}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["rows"] == [{"לקוח": 14, "שנה": 2024}], resp.json()
    again = _run(test_client, corpus, aid)
    assert again["steps_failed"] == 0 and again["steps_needing_input"] == 0, again
    step = _step(test_client, corpus, aid, copy["id"], "member")
    assert step["data"]["rows"] == [{"לקוח": 14, "שנה": 2024}] and "needs_input" not in step["viewer_result"]

    # A refresh keeps the copy's stored values too.
    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["status"] == "success", resp.text
    assert _copies(corpus_rows, release)[WAITING_TITLE]["applied_params"] == {"customer_id": None, "year": 2024}


REGION = {"name": "region", "type": "string", "required": True, "default": None, "source": "input"}
LOADS_BASE_IN_REGION = (
    "def generate_df(ds_clients, excel_files, load_step, params):\n"
    "    import pandas as pd\n"
    "    base = load_step('בסיס · ריצה')\n"
    "    return pd.DataFrame({'שורות': [len(base)], 'אזור': [params.get('region')]})\n"
)


@pytest.mark.e2e
def test_a_loader_waiting_on_what_it_loads_keeps_its_own_authored_values(
        corpus, corpus_rows, test_client, spawned):  # noqa: F811
    """The loader waits only because the step it loads waits for the reader:
    its own input, which the author stored, is never what it waits for, at
    Publish, at a refresh, or on any reader run."""
    corpus_rows.update("queries", corpus.ids["load_step/q_base"], parameters=[CUSTOMER])
    corpus_rows.update("steps", corpus.ids["load_step/q_base/step"], code=NEEDS_CUSTOMER, data=DRAFT_ROWS,
                       applied_params={"customer_id": None})
    corpus_rows.update("queries", corpus.ids["load_step/q_derived"], parameters=[REGION])
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=LOADS_BASE_IN_REGION,
                       applied_params={"region": "צפון"})
    aid = own(corpus_rows, corpus, "load_step/dash")
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "ready", status
    release = status["published"]["id"]
    copies = _copies(corpus_rows, release)
    derived, base = copies["נגזרת"], copies["בסיס"]
    assert derived["data"]["rows"] == [] and derived["data"]["info"]["needs_input"] == ["customer_id"]
    assert derived["applied_params"] == {"region": "צפון"}, "the author's own value is kept"

    resp = test_client.post(f"/api/artifacts/{aid}/refresh", headers=as_(corpus, "owner"))
    assert resp.status_code == 200 and resp.json()["status"] == "success", resp.text
    assert _copies(corpus_rows, release)["נגזרת"]["applied_params"] == {"region": "צפון"}

    assert share(test_client, corpus, aid, "internal").status_code == 200
    for attempt in (1, 2):  # the second run reads the reader's own waiting row
        run = _run(test_client, corpus, aid)
        assert run["steps_failed"] == 0 and run["steps_needing_input"] == 2, (attempt, run)
        step = _step(test_client, corpus, aid, derived["id"], "member")
        assert step["viewer_result"]["needs_input"] == ["customer_id"], (attempt, step["viewer_result"])

    assert _filter_run(test_client, corpus, aid, base["id"], {"params": {"customer_id": 14}}).status_code == 200
    for attempt in (1, 2):  # and once it ran, a second run still renders with the author's value
        again = _run(test_client, corpus, aid)
        assert again["steps_failed"] == 0 and again["steps_needing_input"] == 0, (attempt, again)
        step = _step(test_client, corpus, aid, derived["id"], "member")
        assert step["data"]["rows"] == [{"שורות": 1, "אזור": "צפון"}], (attempt, step["data"])
        assert "needs_input" not in step["viewer_result"], (attempt, step["viewer_result"])
