"""Agent managers can monitor Auto runs — only the ones that never reached an
agent they don't manage, and only up to the first turn that did.

An Auto report pins no agent: each run works against every agent its user can
reach, and records that set (its *exposure*). Visibility for an agent manager:

* a run is visible when its exposure is non-empty and every agent in it is one
  they manage — and the same holds for every earlier run in the report, because
  a run's context replays the conversation so far;
* report-level totals (messages, usage) count a report only when ALL of its
  runs are visible, since they cannot be split by turn;
* everything a hidden run carries (prompt, answer, trace, snapshots, tool
  calls, usage) stays out of every console surface.

Cast:
    admin    — org admin, org-wide console
    manager  — `manage` on agents A and B (not C)
    member   — plain member whose runs are seeded; reaches A, B and sometimes C
"""
import uuid
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

NOW = datetime.utcnow().replace(microsecond=0)
START = NOW - timedelta(days=1)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _iso(dt):
    return dt.isoformat() + "Z"


@pytest.fixture
def auto_world(bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource,
               create_report, seed_agent_executions, rollup_agent_executions):
    admin = bootstrap_admin("autoconsole")
    org_id = admin["org_id"]
    tag = uuid.uuid4().hex[:6]
    a = sqlite_data_source(name=f"sales_{tag}", user_token=admin["token"], org_id=org_id)
    b = sqlite_data_source(name=f"support_{tag}", user_token=admin["token"], org_id=org_id)
    c = sqlite_data_source(name=f"payroll_{tag}", user_token=admin["token"], org_id=org_id)

    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for agent in (a, b):
        g = grant_resource(resource_type="data_source", resource_id=agent["id"],
                           principal_type="user", principal_id=manager["user_id"],
                           permissions=["manage"], user_token=admin["token"], org_id=org_id)
        assert g.status_code in (200, 201), g.json()

    # Every report below is Auto: no agent pinned.
    inside = create_report(title="Auto within managed agents", user_token=admin["token"], org_id=org_id)
    outside = create_report(title="Auto reaching payroll", user_token=admin["token"], org_id=org_id)
    mixed = create_report(title="Auto that changes topic mid-way", user_token=admin["token"], org_id=org_id)
    legacy = create_report(title="Auto before exposure was recorded", user_token=admin["token"], org_id=org_id)
    # Had payroll in reach but never queried it: its context still carried
    # payroll's schema, so it is as out of scope as a run that used it.
    glimpsed = create_report(title="Auto with payroll in reach", user_token=admin["token"], org_id=org_id)

    def run(prompt, minutes, exposed, used=(), tokens=0):
        return {
            "user_id": member["user_id"], "prompt": prompt, "created_at": START + timedelta(minutes=minutes),
            "exposed": list(exposed), "used": list(used),
            "tools": [{"name": "create_data", "status": "success"}],
            "usage": [{"prompt_tokens": tokens, "completion_tokens": 0, "cost": tokens / 1000}] if tokens else [],
        }

    ids = {
        "inside": seed_agent_executions(org_id, inside["id"], [
            run("inside one", 1, [a["id"], b["id"]], used=[a["id"]], tokens=11),
            run("inside two", 2, [a["id"], b["id"]], tokens=22),
        ]),
        "outside": seed_agent_executions(org_id, outside["id"], [
            run("outside payroll salaries", 3, [a["id"], c["id"]], used=[c["id"]], tokens=300),
        ]),
        "mixed": seed_agent_executions(org_id, mixed["id"], [
            run("mixed first turn", 4, [a["id"]], tokens=4000),
            run("mixed payroll salaries", 5, [a["id"], c["id"]], used=[c["id"]], tokens=50000),
            run("mixed after payroll", 6, [a["id"]], tokens=600000),
        ]),
        "glimpsed": seed_agent_executions(org_id, glimpsed["id"], [
            run("glimpsed headcount by team", 8, [a["id"], c["id"]], used=[a["id"]], tokens=7000000),
        ]),
        "legacy": seed_agent_executions(org_id, legacy["id"], [
            {"user_id": member["user_id"], "prompt": "legacy unrecorded", "created_at": START + timedelta(minutes=7)},
        ]),
    }
    rollup_agent_executions()
    return {
        "org_id": org_id, "admin": admin, "manager": manager, "member": member,
        "a": a, "b": b, "c": c, "ids": ids,
        "reports": {"inside": inside["id"], "outside": outside["id"], "mixed": mixed["id"], "legacy": legacy["id"],
                    "glimpsed": glimpsed["id"]},
    }


def _get(test_client, w, who, path, **params):
    return test_client.get(path, params=params, headers=_headers(w[who]["token"], w["org_id"]))


def _summary_prompts(test_client, w, who, **params):
    resp = _get(test_client, w, who, "/api/console/agent_executions/summaries",
                start_date=_iso(START - timedelta(days=1)), end_date=_iso(NOW + timedelta(days=1)), **params)
    assert resp.status_code == 200, resp.text
    return {i["prompt"] for i in resp.json()["items"]}


def _diagnosis_ids(test_client, w, who, q=""):
    resp = _get(test_client, w, who, "/api/console/diagnosis/runs", q=q,
                start=_iso(START - timedelta(days=1)), end=_iso(NOW + timedelta(days=1)))
    assert resp.status_code == 200, resp.text
    return {i["id"] for i in resp.json()["items"]}


def _visible_to_manager(w):
    return set(w["ids"]["inside"]) | {w["ids"]["mixed"][0]}


def test_manager_sees_auto_runs_confined_to_their_agents(test_client, auto_world):
    w = auto_world
    assert _summary_prompts(test_client, w, "manager") == {"inside one", "inside two", "mixed first turn"}
    assert _diagnosis_ids(test_client, w, "manager") == _visible_to_manager(w)

    conv = _get(test_client, w, "manager", f"/api/console/reports/{w['reports']['inside']}/conversation")
    assert conv.status_code == 200, conv.text
    body = conv.json()
    assert [t["user_prompt"] for t in body["turns"]] == ["inside one", "inside two"]
    assert body["hidden_turns"] == 0

    for run_id in w["ids"]["inside"]:
        assert _get(test_client, w, "manager", f"/api/console/agent_executions/{run_id}").status_code == 200


def test_admin_view_is_unchanged(test_client, auto_world):
    w = auto_world
    every = {i for group in w["ids"].values() for i in group}
    assert _diagnosis_ids(test_client, w, "admin") == every
    conv = _get(test_client, w, "admin", f"/api/console/reports/{w['reports']['mixed']}/conversation").json()
    assert len(conv["turns"]) == 3 and conv["hidden_turns"] == 0


@pytest.mark.parametrize("q", ["", "payroll", "salaries", "status:success", "tool:create_data", "NOT status:error"])
def test_runs_that_reached_an_unmanaged_agent_never_surface(test_client, auto_world, q):
    """Whatever the query: the run that reached payroll, and every later turn of
    its report, stay out of the run list and the summaries."""
    w = auto_world
    hidden = (set(w["ids"]["outside"]) | set(w["ids"]["mixed"][1:]) | set(w["ids"]["legacy"])
              | set(w["ids"]["glimpsed"]))
    assert not (_diagnosis_ids(test_client, w, "manager", q) & hidden)
    prompts = _summary_prompts(test_client, w, "manager")
    assert not {p for p in prompts if "payroll" in p or "after" in p or "legacy" in p or "glimpsed" in p}


def test_trace_shows_the_conversation_only_up_to_the_first_out_of_scope_turn(test_client, auto_world):
    w = auto_world
    conv = _get(test_client, w, "manager", f"/api/console/reports/{w['reports']['mixed']}/conversation")
    assert conv.status_code == 200, conv.text
    body = conv.json()
    assert [t["user_prompt"] for t in body["turns"]] == ["mixed first turn"]
    assert body["hidden_turns"] == 2
    # The roll-up counts only the shown turn's usage — not the hidden ones'.
    assert body["total_llm_tokens"] == 4000
    assert "payroll" not in conv.text and "after payroll" not in conv.text

    first, payroll, after = w["ids"]["mixed"]
    assert _get(test_client, w, "manager", f"/api/console/agent_executions/{first}").status_code == 200
    for run_id in (payroll, after):
        for path in (f"/api/console/agent_executions/{run_id}",
                     f"/api/console/agent_executions/{run_id}/context_snapshot/{uuid.uuid4()}"):
            assert _get(test_client, w, "manager", path).status_code == 403, path


def test_reports_out_of_scope_are_refused_on_every_drill_down(test_client, auto_world):
    w = auto_world
    for key in ("outside", "legacy", "glimpsed"):
        rid = w["reports"][key]
        assert _get(test_client, w, "manager", f"/api/console/reports/{rid}/conversation").status_code == 403
        for run_id in w["ids"][key]:
            assert _get(test_client, w, "manager", f"/api/console/agent_executions/{run_id}").status_code == 403
    # The legacy per-completion trace needs the WHOLE report in scope.
    for key, expected in (("inside", 200), ("mixed", 403), ("outside", 403), ("glimpsed", 403)):
        rid = w["reports"][key]
        resp = _get(test_client, w, "manager", f"/api/console/trace/{rid}/{uuid.uuid4()}")
        assert (resp.status_code == 403) == (expected == 403), (key, resp.status_code)


def test_tool_calls_of_hidden_runs_are_withheld(test_client, auto_world):
    w = auto_world
    ids = [w["ids"]["inside"][0], *w["ids"]["mixed"], w["ids"]["outside"][0], w["ids"]["glimpsed"][0]]
    body = _get(test_client, w, "manager", "/api/console/diagnosis/runs/tool_calls", run_ids=",".join(ids)).json()
    assert len(body[w["ids"]["inside"][0]]) == 1 and len(body[w["ids"]["mixed"][0]]) == 1
    for run_id in (*w["ids"]["mixed"][1:], w["ids"]["outside"][0], w["ids"]["glimpsed"][0]):
        assert body[run_id] == [], run_id


def test_report_totals_count_only_reports_entirely_in_scope(test_client, auto_world):
    """Usage can't be split by turn everywhere, so totals take a report only
    when all of its runs are visible: the mixed report is left out whole."""
    w = auto_world
    window = {"start_date": _iso(START - timedelta(days=1)), "end_date": _iso(NOW + timedelta(days=1))}
    usage = _get(test_client, w, "manager", "/api/console/metrics/llm-usage", **window)
    assert usage.status_code == 200, usage.text
    assert usage.json()["total_prompt_tokens"] == 11 + 22

    admin_usage = _get(test_client, w, "admin", "/api/console/metrics/llm-usage", **window).json()
    assert admin_usage["total_prompt_tokens"] == 11 + 22 + 300 + 4000 + 50000 + 600000 + 7000000

    metrics = _get(test_client, w, "manager", "/api/console/metrics", **window)
    assert metrics.status_code == 200
    assert metrics.json()["total_messages"] == 4  # two runs × (user + system completion), inside only


def test_manager_agent_filter_narrows_auto_runs_by_the_agents_they_had(test_client, auto_world):
    """Auto pins nothing; a manager's "runs involving support" matches the Auto
    reports whose runs had support — and never widens past what they may see."""
    w = auto_world
    assert _summary_prompts(test_client, w, "manager", data_source_ids=w["b"]["id"]) == {"inside one", "inside two"}
    assert _summary_prompts(test_client, w, "manager", data_source_ids=w["a"]["id"]) == {
        "inside one", "inside two", "mixed first turn"}
    refused = _get(test_client, w, "manager", "/api/console/agent_executions/summaries", data_source_ids=w["c"]["id"])
    assert refused.status_code == 403


def test_admin_agent_filter_files_auto_runs_under_agents_they_used(test_client, auto_world):
    """Org-wide, an Auto run counts under the agents it actually used, not
    under every agent its user could have reached."""
    w = auto_world
    assert _summary_prompts(test_client, w, "admin", data_source_ids=w["b"]["id"]) == set()
    assert _summary_prompts(test_client, w, "admin", data_source_ids=w["c"]["id"]) == {
        "outside payroll salaries", "mixed first turn", "mixed payroll salaries", "mixed after payroll"}


def test_granting_the_missing_agent_reveals_the_rest(test_client, auto_world, grant_resource):
    w = auto_world
    g = grant_resource(resource_type="data_source", resource_id=w["c"]["id"],
                       principal_type="user", principal_id=w["manager"]["user_id"],
                       permissions=["manage"], user_token=w["admin"]["token"], org_id=w["org_id"])
    assert g.status_code in (200, 201), g.json()
    every_recorded = (set(w["ids"]["inside"]) | set(w["ids"]["outside"]) | set(w["ids"]["mixed"])
                      | set(w["ids"]["glimpsed"]))
    assert _diagnosis_ids(test_client, w, "manager") == every_recorded
    conv = _get(test_client, w, "manager", f"/api/console/reports/{w['reports']['mixed']}/conversation").json()
    assert len(conv["turns"]) == 3 and conv["hidden_turns"] == 0
    # An Auto run that recorded nothing is still attributable to no one.
    assert _get(test_client, w, "manager",
                f"/api/console/reports/{w['reports']['legacy']}/conversation").status_code == 403


def test_plain_member_still_has_no_console(test_client, auto_world):
    w = auto_world
    assert _get(test_client, w, "member", "/api/console/agent_executions/summaries").status_code == 403
