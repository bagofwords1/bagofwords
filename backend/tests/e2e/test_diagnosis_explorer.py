"""The diagnosis explorer: one query language over agent runs and their tool
calls, served by /console/diagnosis/{runs, runs/tool_calls, facets, fields}.

Runs are seeded directly (there is no API that produces one — see the
fixture) and rolled up the way finish_agent_execution does for live runs.
"""
import uuid
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

NOW = datetime.utcnow().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


@pytest.fixture
def world(create_user, login_user, whoami, create_report, seed_agent_executions, rollup_agent_executions, test_client):
    owner = create_user()
    token = login_user(owner["email"], owner["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    owner_id = info["id"]

    member_email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.json()
    member = create_user(email=member_email, password="test123")
    member_id = whoami(login_user(member_email, "test123"))["id"]

    report = create_report(title="Weekly Sales Analysis", user_token=token, org_id=org_id)
    other = create_report(title="RCA Production Issue", user_token=token, org_id=org_id)

    runs = [
        # 0: failed create_data, negative feedback, expensive, slow, slack
        dict(user_id=owner_id, prompt="Revenue by region for the current month", created_at=NOW - timedelta(days=1),
             status="error", duration_ms=42_000, first_token_ms=800, thinking_ms=3_000, platform="slack",
             error="psycopg2.errors.UndefinedColumn: column region_name does not exist",
             judge={"response": 2, "instructions": 4, "context": 3},
             feedback={"direction": -1, "message": "wrong region"},
             tools=[{"name": "describe_tables", "status": "success", "duration_ms": 600},
                    {"name": "create_data", "action": "execute_sql", "status": "error", "duration_ms": 8_400, "attempt": 1,
                     "error": "column region_name does not exist"},
                    {"name": "create_data", "action": "execute_sql", "status": "error", "duration_ms": 7_900, "attempt": 2,
                     "error": "column region_name does not exist"}],
             usage=[{"model": "gpt-4.1", "provider": "openai", "prompt_tokens": 12_000, "completion_tokens": 3_000, "cost": 0.42, "scope": "planner"},
                    {"model": "gpt-4.1-mini", "provider": "openai", "prompt_tokens": 2_000, "completion_tokens": 400, "cost": 0.02, "scope": "tool_call_judge"}]),
        # 1: success, cheap, fast, positive feedback, web, one create_data ok
        dict(user_id=owner_id, prompt="Top 10 albums by revenue 100% of catalog", created_at=NOW - timedelta(days=2) + timedelta(hours=1),
             status="success", duration_ms=4_900, judge={"response": 5, "instructions": 5, "context": 5},
             feedback={"direction": 1},
             tools=[{"name": "create_data", "action": "execute_sql", "status": "success", "duration_ms": 1_100}],
             usage=[{"model": "claude-haiku", "provider": "anthropic", "prompt_tokens": 3_000, "completion_tokens": 500, "cost": 0.03}]),
        # 2: success but a failed *other* tool (search_reports), no feedback
        dict(user_id=member_id, prompt="Which PRs were merged last week without a review?", created_at=NOW - timedelta(days=3),
             status="success", duration_ms=6_300, judge={"response": 4, "instructions": 2, "context": 4},
             tools=[{"name": "create_data", "action": "execute_sql", "status": "success", "duration_ms": 900},
                    {"name": "search_reports", "status": "error", "duration_ms": 300, "error": "GitHub API rate limit exceeded"}],
             usage=[{"model": "gpt-4.1", "provider": "openai", "prompt_tokens": 5_000, "completion_tokens": 900, "cost": 0.07}]),
        # 3: error with no tools at all (planner timed out), old, member
        dict(user_id=member_id, prompt="Show customer churn by cohort", created_at=NOW - timedelta(days=20),
             status="error", duration_ms=31_500, error="Query timed out after 30s",
             usage=[{"model": "gpt-4.1", "provider": "openai", "prompt_tokens": 9_000, "completion_tokens": 100, "cost": 0.11}]),
        # 4: eval run — hidden unless eval: is mentioned
        dict(user_id=owner_id, prompt="eval case revenue", created_at=NOW - timedelta(days=1), status="success",
             duration_ms=1_000, is_eval_run=True),
        # 5: pre-attribution run: usage rows carry report_id only → window attribution, cost partial
        dict(user_id=owner_id, prompt="Legacy run before attribution", created_at=NOW - timedelta(days=5),
             status="success", duration_ms=2_000,
             usage=[{"model": "gpt-4o", "provider": "openai", "prompt_tokens": 1_000, "completion_tokens": 200, "cost": 0.01, "unattributed": True}]),
    ]
    ids = seed_agent_executions(org_id, report["id"], runs)
    other_ids = seed_agent_executions(org_id, other["id"], [
        # second report, two turns
        dict(user_id=owner_id, prompt="Root cause the drop in production output", created_at=NOW - timedelta(days=4),
             status="success", duration_ms=68_300,
             tools=[{"name": "create_data", "action": "execute_mdx", "status": "success", "duration_ms": 41_000}]),
        dict(user_id=owner_id, prompt="And break it down by shift", created_at=NOW - timedelta(days=4, hours=-1),
             status="success", duration_ms=12_000),
    ])
    rollup_agent_executions()
    return {
        "org_id": org_id, "token": token, "owner": owner, "owner_id": owner_id,
        "member": member, "member_id": member_id, "report": report, "other": other,
        "ids": ids, "other_ids": other_ids,
    }


@pytest.fixture
def runs(test_client, world):
    def _runs(q="", **params):
        query = {
            "q": q,
            "start": _iso(NOW - timedelta(days=30)),
            "end": _iso(NOW + timedelta(days=1)),
            **params,
        }
        resp = test_client.get(
            "/api/console/diagnosis/runs",
            params=query,
            headers={"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]},
        )
        return resp
    return _runs


def _ids(body):
    return {item["id"] for item in body["items"]}


def _ok(resp):
    assert resp.status_code == 200, resp.json()
    return resp.json()


# ---------------------------------------------------------------------------
# Baseline shape
# ---------------------------------------------------------------------------

def test_empty_query_lists_every_non_eval_run_with_agreeing_panels(runs, world):
    body = _ok(runs(""))
    expected = set(world["ids"]) - {world["ids"][4]} | set(world["other_ids"])
    assert _ids(body) == expected
    assert body["total"] == len(expected)
    assert body["summary"]["matched"] == len(expected)
    assert body["summary"]["errors"] == 2
    assert body["summary"]["users"] == 2
    assert body["total_in_range"] == len(expected)
    assert sum(b["matched"] for b in body["histogram"]["buckets"]) == len(expected)
    assert body["histogram"]["granularity"] == "day"
    tools = {t["tool"]: t for t in body["tools"]}
    assert tools["create_data"]["calls"] == 5
    assert tools["create_data"]["errors"] == 2
    assert body["query"]["canonical"] == ""


def test_run_item_carries_what_the_table_shows(runs, world):
    body = _ok(runs("feedback:negative"))
    [item] = body["items"]
    assert item["id"] == world["ids"][0]
    assert item["status"] == "error"
    assert item["user"]["name"] == world["owner"]["name"]
    assert item["platform"] == "slack"
    assert item["feedback"] == "negative"
    assert item["feedback_message"] == "wrong region"
    assert item["judge"] == {"confidence": 2, "instructions": 4, "context": 3}
    assert item["model"] == "gpt-4.1" and item["provider"] == "openai"
    assert abs(item["cost_usd"] - 0.44) < 1e-6 and item["cost_is_partial"] is False
    assert item["tokens"] == 17_400
    assert item["tools"] == {"total": 3, "failed": 2}
    assert item["duration_ms"] == 42_000
    assert item["report"]["title"] == "Weekly Sales Analysis"
    assert item["report"]["turns"] == 5  # eval run excluded from the turn count
    assert item["error"].startswith("psycopg2")
    assert item["prompt"].startswith("Revenue by region")


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

FIELD_CASES = [
    ("status:error", [0, 3]),
    ("status:success", [1, 2, 5, "o0", "o1"]),
    ("status:(error OR in_progress)", [0, 3]),
    ("NOT status:error", [1, 2, 5, "o0", "o1"]),
    ("-status:error -feedback:positive", [2, 5, "o0", "o1"]),
    ("feedback:negative", [0]),
    ("feedback:positive", [1]),
    ("feedback:none", [2, 3, 5, "o0", "o1"]),
    ("has:feedback", [0, 1]),
    ("judge.confidence:<3", [0]),
    ("confidence:>=5", [1]),
    ("judge.instructions:<3", [2]),
    ("coverage:5", [1]),
    ("judge.context:3..4", [0, 2, 3, 5, "o0", "o1"]),  # unscored runs carry the default 4
    ("judge.context:<4", [0]),
    ("model:gpt-4.1", [0, 2, 3]),
    ("provider:anthropic", [1]),
    ("provider:(anthropic OR openai)", [0, 1, 2, 3, 5]),
    ("cost:>$0.10", [0, 3]),
    ("cost:>0.10", [0, 3]),
    ("cost:<=0.03", [1, 5]),
    ("tokens:>10k", [0]),
    ("tokens.in:>=9000 tokens.out:<200", [3]),
    ("duration:>30s", [0, 3, "o0"]),
    ("duration:>=1m", ["o0"]),  # 42s is under a minute
    ("duration:4900", [1]),
    ("first_token:<1s", [0]),
    ("thinking:>2s", [0]),
    ("tools:>=2", [0, 2]),
    ("tools.failed:>0", [0, 2]),
    ("tools:0", [3, 5, "o1"]),
    ("platform:slack", [0]),
    ("platform:web", [1, 2, 3, 5, "o0", "o1"]),
    ('report:"Weekly Sales Analysis"', [0, 1, 2, 3, 5]),
    ("report:weekly*", [0, 1, 2, 3, 5]),
    ("turn:1", [3, "o0"]),  # the earliest run on each report
    ("turn:>3", [0, 1]),
    ('error:"timed out"', [3]),
    ("error:region_name", [0]),
    ("revenue", [0, 1]),
    ('"by cohort"', [3]),
    ("rate limit", [2]),  # bare words search tool errors too
    ("100%", [1]),  # LIKE metacharacters are literal
    ("eval:true", [4]),
    ("eval:false", [0, 1, 2, 3, 5, "o0", "o1"]),
    ("created:-2d", [0, 1]),
    ("created:>-2d", [0, 1]),
    ("created:<-2d", [2, 3, 5, "o0", "o1"]),
]


def test_field_predicates(runs, world):
    """Every field in the registry narrows the runs as documented. One test
    for all cases because the schema is rebuilt per test."""
    def idx(i):
        return world["other_ids"][int(i[1:])] if isinstance(i, str) else world["ids"][i]
    failures = []
    for q, expected_idx in FIELD_CASES:
        body = _ok(runs(q))
        expected = {idx(i) for i in expected_idx}
        if _ids(body) != expected or body["total"] != len(expected_idx):
            failures.append((q, sorted(_ids(body)), sorted(expected)))
    assert not failures, failures


def test_user_matches_name_or_email_case_insensitively(runs, world):
    owner = world["owner"]
    assert _ids(_ok(runs(f'user:"{owner["name"]}"'))) == {world["ids"][i] for i in (0, 1, 5)} | set(world["other_ids"])
    assert _ids(_ok(runs(f"user:{owner['email'].upper()}"))) == {world["ids"][i] for i in (0, 1, 5)} | set(world["other_ids"])
    assert _ids(_ok(runs(f"user:{world['member']['email']}"))) == {world["ids"][2], world["ids"][3]}
    both = _ok(runs(f"user:({owner['email']} OR {world['member']['email']})"))
    assert both["total"] == 7


def test_created_day_respects_the_callers_timezone(runs, world):
    day = (NOW - timedelta(days=20)).strftime("%Y-%m-%d")
    assert _ids(_ok(runs(f"created:{day}"))) == {world["ids"][3]}
    # Shift the caller 14h east: the same UTC instant may fall on the next local day.
    shifted = (NOW - timedelta(days=20) + timedelta(hours=14)).strftime("%Y-%m-%d")
    assert _ids(_ok(runs(f"created:{shifted}", tz=14 * 60))) == {world["ids"][3]}


# ---------------------------------------------------------------------------
# Tool-call correlation
# ---------------------------------------------------------------------------

def test_tool_terms_in_one_and_group_correlate_to_a_single_call(runs, world):
    # Run 2 has a *successful* create_data and a *failed* search_reports:
    # correlated, it must not match "a failed create_data call".
    body = _ok(runs("tool:create_data tool.status:error"))
    assert _ids(body) == {world["ids"][0]}
    [item] = body["items"]
    assert len(item["matched_tool_call_ids"]) == 2  # both failed attempts
    # Un-correlated (OR): any run with a create_data call or any failed call.
    assert _ids(_ok(runs("tool:create_data OR tool.status:error"))) == {world["ids"][i] for i in (0, 1, 2)} | {world["other_ids"][0]}
    # Negated tool term
    assert _ids(_ok(runs("NOT tool:create_data"))) == {world["ids"][3], world["ids"][5], world["other_ids"][1]}
    assert _ids(_ok(runs("tool.attempt:>1"))) == {world["ids"][0]}
    assert _ids(_ok(runs("tool:cre* tool.duration:>5s"))) == {world["ids"][0], world["other_ids"][0]}
    assert _ids(_ok(runs("tool.action:execute_mdx"))) == {world["other_ids"][0]}
    assert _ids(_ok(runs('tool.error:"rate limit"'))) == {world["ids"][2]}
    assert _ids(_ok(runs("has:tool"))) == {world["ids"][i] for i in (0, 1, 2)} | {world["other_ids"][0]}


def test_tool_calls_endpoint_groups_calls_by_run(test_client, world):
    ids = [world["ids"][0], world["ids"][2], world["ids"][3]]
    resp = test_client.get(
        "/api/console/diagnosis/runs/tool_calls",
        params={"run_ids": ",".join(ids)},
        headers={"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]},
    )
    body = _ok(resp)
    assert set(body.keys()) == set(ids)
    assert [c["tool"] for c in body[world["ids"][0]]] == ["describe_tables", "create_data", "create_data"]
    assert body[world["ids"][0]][2]["attempt"] == 2
    assert body[world["ids"][0]][1]["error"].startswith("column region_name")
    assert body[world["ids"][3]] == []


# ---------------------------------------------------------------------------
# Sorting, paging, panels
# ---------------------------------------------------------------------------

def test_cursor_pagination_walks_every_run_once_in_sort_order(runs, world):
    for sort, key in [("cost", "cost_usd"), ("duration", "duration_ms"), ("tokens", "tokens"), ("tools.failed", None), ("created", "created_at")]:
        seen, cursor, values = [], None, []
        for _ in range(10):
            body = _ok(runs("", sort=sort, dir="desc", limit=3, **({"cursor": cursor} if cursor else {})))
            seen.extend(item["id"] for item in body["items"])
            if key:
                values.extend((item[key] if item[key] is not None else -1) for item in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 7, sort
        if key:
            assert values == sorted(values, reverse=True), sort
        body = _ok(runs("", sort=sort, dir="asc", limit=100))
        if key:
            asc = [(item[key] if item[key] is not None else -1) for item in body["items"]]
            assert asc == sorted(asc), sort


def test_default_sort_is_newest_first_and_cursor_pages_skip_panels(runs, world):
    first = _ok(runs("", limit=2))
    assert "histogram" in first and "tools" in first
    created = [item["created_at"] for item in first["items"]]
    assert created == sorted(created, reverse=True)
    second = _ok(runs("", limit=2, cursor=first["next_cursor"], include="items"))
    assert "histogram" not in second and "summary" not in second
    assert not (_ids(first) & _ids(second))


def test_histogram_buckets_reflect_the_range_and_the_query(runs, world):
    body = _ok(runs("status:error"))
    buckets = body["histogram"]["buckets"]
    assert body["total_in_range"] == 7
    assert sum(b["matched"] for b in buckets) == 2 == body["summary"]["matched"]
    assert sum(b["matched_errors"] for b in buckets) == 2
    assert all(b["matched"] <= b["total"] for b in buckets)
    # Hourly buckets for a short range
    short = _ok(runs("", start=_iso(NOW - timedelta(days=1, hours=2)), end=_iso(NOW)))
    assert short["histogram"]["granularity"] == "hour"
    assert sum(b["total"] for b in short["histogram"]["buckets"]) == short["total_in_range"] == 1
    # Weekly buckets for a long range still cover every run
    long = _ok(runs("", start=_iso(NOW - timedelta(days=200)), end=_iso(NOW + timedelta(days=1))))
    assert long["histogram"]["granularity"] == "week"
    assert sum(b["total"] for b in long["histogram"]["buckets"]) == 7


def test_summary_line_is_computed_from_the_query(runs, world):
    s = _ok(runs("status:error"))["summary"]
    assert s == {"matched": 2, "errors": 2, "users": 2, "cost_usd": pytest.approx(0.55), "p50_ms": pytest.approx(42_000)}
    s = _ok(runs("tools:0"))["summary"]
    assert s["matched"] == 3 and s["errors"] == 1


def test_tools_strip_aggregates_the_matched_runs(runs, world):
    tools = {t["tool"]: t for t in _ok(runs("status:success"))["tools"]}
    assert tools["create_data"] == {"tool": "create_data", "calls": 3, "errors": 0, "avg_ms": pytest.approx((1100 + 900 + 41000) / 3)}
    assert tools["search_reports"]["errors"] == 1
    assert "describe_tables" not in tools


# ---------------------------------------------------------------------------
# Facets and fields
# ---------------------------------------------------------------------------

def test_facets_count_values_within_the_query(test_client, world):
    def facets(field, q="", prefix=""):
        resp = test_client.get(
            f"/api/console/diagnosis/facets/{field}",
            params={"q": q, "prefix": prefix, "start": _iso(NOW - timedelta(days=30)), "end": _iso(NOW + timedelta(days=1))},
            headers={"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]},
        )
        return {f["value"]: f["count"] for f in _ok(resp)}

    assert facets("status") == {"success": 5, "error": 2}
    assert facets("status", q="user:" + world["member"]["email"]) == {"success": 1, "error": 1}
    assert facets("model", q="status:error") == {"gpt-4.1": 2}
    assert facets("tool", q="feedback:negative") == {"create_data": 1, "describe_tables": 1}
    assert facets("user", q='report:"RCA Production Issue"') == {world["owner"]["name"]: 2}
    assert facets("feedback") == {"none": 5, "negative": 1, "positive": 1}
    assert facets("model") == {"gpt-4.1": 3, "claude-haiku": 1, "gpt-4o": 1}
    assert facets("provider", prefix="ant") == {"anthropic": 1}
    assert facets("platform") == {"web": 6, "slack": 1}
    assert facets("user")[world["owner"]["name"]] == 5
    assert facets("user", prefix=world["member"]["name"][:12].lower()) == {world["member"]["name"]: 2}
    assert facets("tool") == {"create_data": 4, "describe_tables": 1, "search_reports": 1}
    assert facets("tool.status", q="tool:create_data") == {"success": 3, "error": 1}
    assert facets("tool.action", prefix="execute_m") == {"execute_mdx": 1}


def test_fields_endpoint_publishes_the_registry_and_quick_filters(test_client, world):
    resp = test_client.get(
        "/api/console/diagnosis/fields",
        headers={"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]},
    )
    body = _ok(resp)
    names = {f["name"] for f in body["fields"]}
    assert {"status", "user", "agent", "cost", "tokens", "judge.confidence", "tool", "tool.status", "turn"} <= names
    assert body["ast_version"] == 1
    assert {q["id"] for q in body["quick_filters"]} >= {"errors", "failed_queries", "negative_feedback", "slow"}
    assert "cost" in body["sorts"]


# ---------------------------------------------------------------------------
# Errors and bounds
# ---------------------------------------------------------------------------

def test_bad_queries_are_400_with_a_position(runs, world):
    resp = runs("durations:>3s")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "bad_query" and detail["position"] == 0
    assert "duration" in detail["suggestions"]
    resp = runs("status:(error OR")
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "bad_query"


def test_requests_are_always_time_bounded(test_client, world):
    resp = test_client.get(
        "/api/console/diagnosis/runs",
        params={"q": ""},
        headers={"Authorization": f"Bearer {world['token']}", "X-Organization-Id": world["org_id"]},
    )
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "bad_request"


def test_time_range_bounds_the_result(runs, world):
    body = _ok(runs("", start=_iso(NOW - timedelta(days=10)), end=_iso(NOW + timedelta(days=1))))
    assert world["ids"][3] not in _ids(body)
    assert body["total"] == 6


def test_single_run_rollup_and_batched_backfill_agree(runs, world, rollup_agent_executions):
    """The write-path hook (one run at a time) and the backfill (set-based
    batches) share one value builder; every column the page shows must come
    out identical from both."""
    def snapshot():
        body = _ok(runs("", limit=100))
        return {i["id"]: {k: v for k, v in i.items() if k != "matched_tool_call_ids"} for i in body["items"]}
    batched = snapshot()
    rollup_agent_executions(single=True)
    single = snapshot()
    assert single == batched


def test_pre_attribution_runs_get_window_cost_flagged_partial(runs, world):
    body = _ok(runs(f"run_id:{world['ids'][5]}"))
    [item] = body["items"]
    assert item["cost_is_partial"] is True
    assert item["cost_usd"] == pytest.approx(0.01)
    assert item["model"] == "gpt-4o"
