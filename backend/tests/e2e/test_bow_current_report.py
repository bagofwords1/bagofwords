"""bow.runs leaves out the conversation that runs the query (no
self-observation), unless the query names it.

A training conversation asking about its own turns names itself with a
top-level report_id:<this report> (or run_id:<a run in it>); anything broader
(no filter, other reports, the name under OR/NOT) keeps the exclusion. The
query is the saved recipe, so a refresh returns what the first run did.
"""
import uuid
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

NOW = datetime.utcnow().replace(microsecond=0)


@pytest.fixture
def world(create_user, login_user, whoami, create_report, seed_agent_executions, rollup_agent_executions, test_client):
    owner = create_user()
    token = login_user(owner["email"], owner["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    created = test_client.post("/api/reports", json={"title": f"Training {uuid.uuid4().hex[:6]}", "mode": "training"},
                               headers=headers)
    assert created.status_code in (200, 201), created.text
    current = created.json()
    assert test_client.put(f"/api/reports/{current['id']}", json={"mode": "training"}, headers=headers).status_code == 200
    other = create_report(title=f"Other {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)

    def runs(n, status="success"):
        return [dict(user_id=info["id"], prompt=f"turn {i}", created_at=NOW - timedelta(hours=i + 1), status=status,
                     duration_ms=1_000, usage=[{"model": "claude-haiku-4-5", "provider": "anthropic",
                                                "prompt_tokens": 100 * (i + 1), "completion_tokens": 10,
                                                "cache_read_tokens": 1_000, "cost": 0.001}])
                for i in range(n)]
    current_ids = seed_agent_executions(org_id, current["id"], runs(3))
    other_ids = seed_agent_executions(org_id, other["id"], runs(2, status="error"))
    rollup_agent_executions()
    return {"headers": headers, "current": current["id"], "other": other["id"],
            "current_ids": current_ids, "other_ids": other_ids}


def _saved_query(test_client, world, query):
    """Run generated code from the training conversation, as a saved query."""
    created = test_client.post("/api/queries", json={"title": "Runs", "report_id": world["current"]},
                               headers=world["headers"])
    assert created.status_code == 200, created.text
    code = f'''def generate_df(ds_clients, excel_files):
    return ds_clients["bow"].execute_query({{"dataset":"runs","query":{query!r},"columns":["run_id","report_id","input_tokens"]}})
'''
    query_id = created.json()["id"]
    ran = test_client.post(f"/api/queries/{query_id}/run", json={"code": code}, headers=world["headers"])
    assert ran.status_code == 200, ran.text
    assert not ran.json().get("error"), ran.text
    saved = test_client.get(f"/api/queries/{query_id}", headers=world["headers"])
    assert saved.status_code == 200, saved.text
    return query_id, {r["run_id"] for r in saved.json()["default_step"]["data"]["rows"]}


def _refresh(test_client, world, query_id):
    rerun = test_client.post(f"/api/queries/{query_id}/run", json={"mode": "viewer"}, headers=world["headers"])
    assert rerun.status_code == 200, rerun.text
    return {r["run_id"] for r in rerun.json()["data"]["rows"]}


@pytest.mark.parametrize("query", [
    "",
    "status:success",
    "claude",
    "report_id:{other}",
    "report_id:{current} OR status:error",  # named only under OR
    "NOT report_id:{current}",
    "run_id:{other_run}",
    # Numeric and boolean terms next to the exclusion.
    "turn:1",
    "eval:false",
    "routed:false",
    "tokens:(100 OR 200)",
])
def test_queries_that_do_not_name_the_conversation_exclude_it(test_client, world, query):
    q = query.format(current=world["current"], other=world["other"], other_run=world["other_ids"][0])
    query_id, rows = _saved_query(test_client, world, q)
    assert not rows & set(world["current_ids"])
    assert _refresh(test_client, world, query_id) == rows


@pytest.mark.parametrize("query, expected", [
    ("report_id:{current}", "all"),
    ("report_id:({other} OR {current})", "all+other"),
    ("report_id:{current} status:success", "all"),
    ("report_id:{current} eval:false routed:false", "all"),
    ("report_id:{current} turn:>=1", "all"),
    ("run_id:{run}", "one"),
    ("run_id:({run} OR {other_run})", "one+other"),
])
def test_a_query_that_names_the_conversation_includes_it(test_client, world, query, expected):
    q = query.format(current=world["current"], other=world["other"], run=world["current_ids"][1],
                     other_run=world["other_ids"][0])
    query_id, rows = _saved_query(test_client, world, q)
    want = {"all": set(world["current_ids"]), "one": {world["current_ids"][1]}}[expected.split("+")[0]]
    if expected == "all+other":
        want |= set(world["other_ids"])
    if expected == "one+other":
        want |= {world["other_ids"][0]}
    assert rows == want
    assert _refresh(test_client, world, query_id) == rows
