"""The trace modal's per-step tokens and per-run models.

A step's input tokens mean the same for every provider: the whole prompt,
cache reads and writes included (the bow.runs convention). Providers disagree
on the wire — the Anthropic Messages API reports cache beside prompt_tokens,
OpenAI-format clients fold it in — so the trace normalizes on the server.

The run's models come from its llm_usage_records: the primary (planner) model
from the rollup, plus every model it called with its share. Runs without
attributed usage report no model rather than a guess. The data stays behind
the trace's console permission.
"""
import uuid
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

NOW = datetime.utcnow().replace(microsecond=0)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _tu(prompt, completion, cache_read=0, cache_write=0):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            "cache_read_tokens": cache_read or None, "cache_creation_tokens": cache_write or None}


def _usage(model, provider, prompt, completion, cache_read=0, cache_write=0, scope="planner", **kw):
    return {"model": model, "provider": provider, "prompt_tokens": prompt, "completion_tokens": completion,
            "cache_read_tokens": cache_read, "cache_creation_tokens": cache_write, "scope": scope, **kw}


# Per run: planner decisions (raw provider token usage) and the run's usage records.
RUNS = {
    "anthropic": dict(
        decisions=[{"tool": "describe_tables", "token_usage": _tu(1_200, 90, cache_read=6_000, cache_write=800)},
                   {"tool": "create_data", "token_usage": _tu(300, 140, cache_read=7_500)},
                   {"token_usage": _tu(50, 400, cache_read=7_900, cache_write=120)}],
        usage=[_usage("claude-haiku-4-5", "anthropic", 1_200, 90, 6_000, 800, cost=0.01),
               _usage("claude-haiku-4-5", "anthropic", 300, 140, 7_500, cost=0.01),
               _usage("claude-haiku-4-5", "anthropic", 50, 400, 7_900, 120, cost=0.01),
               _usage("gpt-4.1-mini", "openai", 2_000, 300, 1_024, scope="create_data.code_gen", cost=0.02)],
    ),
    "openai": dict(
        decisions=[{"tool": "create_data", "token_usage": _tu(9_000, 200, cache_read=4_096)},
                   {"token_usage": _tu(9_400, 350, cache_read=8_960)}],
        usage=[_usage("gpt-4.1", "openai", 9_000, 200, 4_096, cost=0.03),
               _usage("gpt-4.1", "openai", 9_400, 350, 8_960, cost=0.03)],
    ),
    "routed": dict(
        decisions=[{"token_usage": _tu(700, 60, cache_read=2_000)}],
        usage=[_usage("claude-haiku-4-5", "anthropic", 700, 60, 2_000, routed=True,
                      baseline_model="claude-sonnet-4-5", cost=0.004)],
    ),
    # Auto routing started on the small model, then escalated back to the
    # baseline: the router stamps routed=True on the baseline's calls too.
    "escalated": dict(
        decisions=[{"tool": "create_data", "token_usage": _tu(500, 40, cache_read=1_000)},
                   {"token_usage": _tu(600, 90, cache_read=1_500)}],
        usage=[_usage("claude-haiku-4-5", "anthropic", 500, 40, 1_000, routed=True,
                      baseline_model="claude-sonnet-4-5", cost=0.002),
               _usage("claude-sonnet-4-5", "anthropic", 600, 90, 1_500, routed=True,
                      baseline_model="claude-sonnet-4-5", cost=0.01)],
    ),
    # The planner moved from an Anthropic model to an OpenAI one mid-run: the
    # two report cache differently and a decision can't be tied to its call.
    "mixed_planner": dict(
        decisions=[{"tool": "create_data", "token_usage": _tu(400, 30, cache_read=3_000)},
                   {"token_usage": _tu(5_000, 80, cache_read=4_096)}],
        usage=[_usage("claude-haiku-4-5", "anthropic", 400, 30, 3_000, routed=True,
                      baseline_model="gpt-4.1", cost=0.002),
               _usage("gpt-4.1", "openai", 5_000, 80, 4_096, cost=0.01)],
    ),
    # Before per-call usage attribution: decisions exist, usage records do not.
    "legacy": dict(decisions=[{"token_usage": _tu(800, 70)}], usage=[]),
}

# Expected normalized input per step: Anthropic adds cache, OpenAI already includes it.
STEP_INPUT = {
    "anthropic": [1_200 + 6_000 + 800, 300 + 7_500, 50 + 7_900 + 120],
    "openai": [9_000, 9_400],
    "routed": [700 + 2_000],
    "escalated": [500 + 1_000, 600 + 1_500],
}


@pytest.fixture
def world(bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource, create_report,
          seed_agent_executions, rollup_agent_executions):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"alpha-{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"beta-{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    manager_b = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(resource_type="data_source", resource_id=ds_b["id"], principal_type="user",
                   principal_id=manager_b["user_id"], permissions=["manage"], user_token=admin["token"], org_id=org_id)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    r_a = create_report(title="Alpha only", user_token=admin["token"], org_id=org_id, data_sources=[ds_a["id"]])
    r_b = create_report(title="Beta only", user_token=admin["token"], org_id=org_id, data_sources=[ds_b["id"]])

    ids = {}
    for i, (name, run) in enumerate(RUNS.items()):
        report = r_b if name in ("openai", "legacy") else r_a
        ids[name] = seed_agent_executions(org_id, report["id"], [dict(
            user_id=admin["user_id"], prompt=f"{name} run", created_at=NOW - timedelta(hours=i + 1),
            status="success", duration_ms=5_000, **run)])[0]
    rollup_agent_executions()
    return {"org_id": org_id, "admin": admin, "manager_b": manager_b, "member": member, "ids": ids,
            "report_a": r_a["id"], "ds_a": ds_a, "ds_b": ds_b}


def _trace(test_client, world, actor, name):
    return test_client.get(f"/api/console/agent_executions/{world['ids'][name]}",
                           headers=_headers(world[actor]["token"], world["org_id"]))


def _ok(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()


def _step_usage(body):
    seen, out = set(), []
    for b in body["completion_blocks"]:
        pd = b.get("plan_decision")
        if pd and pd["id"] not in seen:
            seen.add(pd["id"])
            out.append(pd["metrics_json"]["token_usage"])
    return out


@pytest.mark.parametrize("name", list(STEP_INPUT))
def test_step_input_tokens_include_cache_for_every_provider(test_client, world, name):
    steps = _step_usage(_ok(_trace(test_client, world, "admin", name)))
    assert [s["input_tokens"] for s in steps] == STEP_INPUT[name]
    raw = RUNS[name]["decisions"]
    # The raw provider fields are kept as they were reported.
    assert [s["prompt_tokens"] for s in steps] == [d["token_usage"]["prompt_tokens"] for d in raw]
    assert [s["completion_tokens"] for s in steps] == [d["token_usage"]["completion_tokens"] for d in raw]
    for s in steps:
        assert s["input_tokens"] >= (s.get("cache_read_tokens") or 0)


@pytest.mark.parametrize("name", ["legacy", "mixed_planner"])
def test_step_input_tokens_are_not_guessed(test_client, world, name):
    """No usage records, or planner calls on providers that count cache
    differently: the step's input is left unset rather than guessed."""
    steps = _step_usage(_ok(_trace(test_client, world, "admin", name)))
    assert len(steps) == len(RUNS[name]["decisions"])
    assert all("input_tokens" not in s for s in steps)


@pytest.mark.parametrize("name", list(RUNS))
def test_models_break_the_run_down_by_model(test_client, world, name):
    body = _ok(_trace(test_client, world, "admin", name))
    usage = RUNS[name]["usage"]
    models = body["models"]
    assert {(m["model_id"], m["provider"]) for m in models} == {(u["model"], u["provider"]) for u in usage}
    assert sum(m["calls"] for m in models) == len(usage)
    assert sum(m["output_tokens"] for m in models) == sum(u["completion_tokens"] for u in usage)
    assert sum(m["cache_read_tokens"] for m in models) == sum(u["cache_read_tokens"] for u in usage)
    assert sum(m["cost_usd"] for m in models) == pytest.approx(sum(u["cost"] for u in usage))
    shares = [m["input_tokens"] + m["output_tokens"] for m in models]
    assert shares == sorted(shares, reverse=True)
    # A model is routed only where it ran instead of the baseline, never on
    # the baseline's own calls; the baseline is named by its model id.
    for m in models:
        substituted = [u for u in usage if u["model"] == m["model_id"]
                       and u.get("routed") and u.get("baseline_model") not in (None, u["model"])]
        assert m["routed"] == bool(substituted)
        assert m["baseline_model_id"] == (substituted[0]["baseline_model"] if substituted else None)
    assert body["auto_routed"] == any(u.get("routed") for u in usage)


def test_model_input_tokens_match_the_bow_runs_convention(test_client, world):
    """The breakdown sums to the run's rolled-up input (bow.runs input_tokens)."""
    for name in ("anthropic", "openai", "routed"):
        body = _ok(_trace(test_client, world, "admin", name))
        usage = RUNS[name]["usage"]
        expected = sum(u["prompt_tokens"] + (0 if u["provider"] == "openai" else u["cache_read_tokens"] + u["cache_creation_tokens"])
                       for u in usage)
        assert sum(m["input_tokens"] for m in body["models"]) == expected


@pytest.mark.parametrize("name, model, provider", [
    ("anthropic", "claude-haiku-4-5", "anthropic"),  # the planner's model, not the larger codegen call's
    ("openai", "gpt-4.1", "openai"),
    ("legacy", None, None),
])
def test_primary_model_is_the_rolled_up_planner_model(test_client, world, name, model, provider):
    ae = _ok(_trace(test_client, world, "admin", name))["agent_execution"]
    assert (ae["primary_model_id"], ae["primary_provider"]) == (model, provider)


def test_primary_model_before_rollup_is_the_planner_model(test_client, world, seed_agent_executions):
    """A run not rolled up yet names the planner's model, not a larger
    codegen call's, and its steps are still normalized."""
    run_id = seed_agent_executions(world["org_id"], world["report_a"], [dict(
        user_id=world["admin"]["user_id"], prompt="running", created_at=NOW, status="in_progress",
        decisions=[{"tool": "create_data", "token_usage": _tu(200, 20, cache_read=900)}],
        usage=[_usage("claude-haiku-4-5", "anthropic", 200, 20, 900),
               _usage("gpt-4.1", "openai", 9_000, 900, scope="create_data.code_gen")])])[0]
    body = _ok(test_client.get(f"/api/console/agent_executions/{run_id}",
                               headers=_headers(world["admin"]["token"], world["org_id"])))
    ae = body["agent_execution"]
    assert (ae["primary_model_id"], ae["primary_provider"]) == ("claude-haiku-4-5", "anthropic")
    assert body["models"][0]["model_id"] == "gpt-4.1"  # the largest share, but not the planner
    assert [s["input_tokens"] for s in _step_usage(body)] == [200 + 900]


def test_legacy_run_reports_no_models(test_client, world):
    assert _ok(_trace(test_client, world, "admin", "legacy"))["models"] == []


@pytest.mark.parametrize("actor, name, allowed", [
    ("manager_b", "openai", True),      # a report on the agent they manage
    ("manager_b", "anthropic", False),  # a report on an agent they do not
    ("member", "openai", False),
    ("member", "anthropic", False),
])
def test_tokens_and_models_follow_the_trace_permission(test_client, world, actor, name, allowed):
    resp = _trace(test_client, world, actor, name)
    if allowed:
        body = _ok(resp)
        assert body["models"] and body["agent_execution"]["primary_model_id"] == "gpt-4.1"
    else:
        assert resp.status_code in (403, 404), resp.text
        assert "models" not in resp.text


@pytest.mark.parametrize("name, agent", [("anthropic", "ds_a"), ("routed", "ds_a"), ("openai", "ds_b"), ("legacy", "ds_b")])
def test_trace_names_the_agents_the_run_drew_on(test_client, world, name, agent):
    """An ordinary run's agents are its report's agents, as the diagnosis
    runs table shows them, with what the icon needs."""
    agents = _ok(_trace(test_client, world, "admin", name))["agents"]
    assert [(a["id"], a["name"]) for a in agents] == [(world[agent]["id"], world[agent]["name"])]
    assert all("type" in a and "icon" in a for a in agents)


def test_a_run_on_a_report_without_agents_names_none(test_client, world, create_report, seed_agent_executions):
    report = create_report(title="No agent", user_token=world["admin"]["token"], org_id=world["org_id"])
    run_id = seed_agent_executions(world["org_id"], report["id"], [dict(
        user_id=world["admin"]["user_id"], prompt="hello", created_at=NOW, status="success")])[0]
    body = _ok(test_client.get(f"/api/console/agent_executions/{run_id}",
                               headers=_headers(world["admin"]["token"], world["org_id"])))
    assert body["agents"] == []


def test_a_scoped_manager_sees_the_agents_of_runs_they_may_trace(test_client, world):
    body = _ok(_trace(test_client, world, "manager_b", "openai"))
    assert [a["name"] for a in body["agents"]] == [world["ds_b"]["name"]]
