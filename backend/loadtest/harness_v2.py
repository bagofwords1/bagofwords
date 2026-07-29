#!/usr/bin/env python3
"""
Mixed-workload load harness for the BoW completion flow.

Three cohorts run *simultaneously*, because the question is not "how fast is
the agent alone" but "what does the agent flow do to everyone else, and what do
they do to it":

  A. agent   — POST /completions with stream=True, drives the real agent_v2
               loop end to end, measures client-visible SSE reliability.
  B. browse  — the /agents page and instruction CRUD: list agents, list
               instructions, create an instruction, edit it, list again.
               Read *and* write, so it competes for the same tables and the
               same pool the agent flow uses.
  C. ui      — driven separately by ui_driver.js (Playwright); this module
               only reserves its share of the user pool.

Every cohort-B request is timed individually, and the run is bracketed by a
quiet baseline so the report can state interference as a ratio (p95 under load
vs p95 alone) rather than an unanchored number.

Self-instrumentation: the harness records its own event-loop lag. If the client
is saturated, its latency numbers describe the client, not the server — so the
report can either trust them or say plainly that it cannot.

Usage:
  python harness_v2.py --level 30 --out results_L30.json
  python harness_v2.py --level 30 --baseline-only     # cohort B with no agents
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "sandbox_state.json")

# trust_env=False: the sandbox's HTTPS_PROXY must not intercept loopback.
CLIENT_KW = dict(trust_env=False)


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Result records
# --------------------------------------------------------------------------
@dataclass
class AgentRun:
    idx: int
    user: str
    report_id: Optional[str] = None
    completion_id: Optional[str] = None
    ok: bool = False
    outcome: str = "unknown"   # finished|http_error|sse_drop|timeout|exception
    status_code: Optional[int] = None
    t_connect: Optional[float] = None
    t_first_event: Optional[float] = None
    t_total: Optional[float] = None
    n_events: int = 0
    saw_finished: bool = False
    error: Optional[str] = None


@dataclass
class BrowseCall:
    op: str
    t: float
    status: int
    ok: bool


@dataclass
class LevelResult:
    level: int
    n_agent: int
    n_browse: int
    started_at: float = 0.0
    wall_s: float = 0.0
    agent_runs: list = field(default_factory=list)
    browse_calls: list = field(default_factory=list)
    client_loop_lag_ms: dict = field(default_factory=dict)
    mock_llm: dict = field(default_factory=dict)
    server_side: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Cohort A — agent users
# --------------------------------------------------------------------------
async def agent_user(idx, user, base, ds_id, prompt, timeout, out: list, gate: asyncio.Event):
    r = AgentRun(idx=idx, user=user["email"])
    H = {"Authorization": f"Bearer {user['token']}",
         "X-Organization-Id": user["org_id"], "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout, **CLIENT_KW) as c:
            # Report creation happens before the gate so that the measured
            # window contains only the completion flow — otherwise N report
            # POSTs land in the same instant and pollute time-to-first-event.
            body = {"title": f"lt-{idx}-{int(time.time()*1000)}",
                    "data_sources": [ds_id] if ds_id else []}
            rr = await c.post(f"{base}/api/reports", headers=H, json=body)
            if rr.status_code >= 400:
                r.outcome, r.status_code = "http_error", rr.status_code
                r.error = rr.text[:200]
                out.append(r)
                return
            r.report_id = rr.json()["id"]

            await gate.wait()

            t0 = time.monotonic()
            async with c.stream(
                "POST", f"{base}/api/reports/{r.report_id}/completions",
                headers=H,
                json={"prompt": {"content": prompt, "mode": "chat"}, "stream": True},
            ) as resp:
                r.status_code = resp.status_code
                r.t_connect = time.monotonic() - t0
                if resp.status_code >= 400:
                    r.outcome = "http_error"
                    r.error = (await resp.aread())[:200].decode("utf8", "replace")
                    out.append(r)
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if r.t_first_event is None:
                        r.t_first_event = time.monotonic() - t0
                    r.n_events += 1
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        r.saw_finished = True
                    elif '"completion.finished"' in payload:
                        r.saw_finished = True
                        if r.completion_id is None:
                            try:
                                r.completion_id = json.loads(payload).get("completion_id")
                            except Exception:
                                pass
                    elif r.completion_id is None and "system_completion_id" in payload:
                        try:
                            r.completion_id = json.loads(payload)["data"]["system_completion_id"]
                        except Exception:
                            pass
            r.t_total = time.monotonic() - t0
            # A stream that ends without [DONE] is the exact shape the browser
            # renders as "network error" — count it separately from an error
            # status, which the client at least sees as a failure.
            r.ok = r.saw_finished
            r.outcome = "finished" if r.saw_finished else "sse_drop"
    except (httpx.ReadTimeout, httpx.ConnectTimeout, asyncio.TimeoutError):
        r.outcome = "timeout"
    except Exception as e:
        r.outcome, r.error = "exception", f"{type(e).__name__}: {e}"[:200]
    out.append(r)


# --------------------------------------------------------------------------
# Cohort B — browsing / CRUD users (the "just using the app" traffic)
# --------------------------------------------------------------------------
async def browse_user(idx, user, base, stop: asyncio.Event, out: list, gate: asyncio.Event):
    H = {"Authorization": f"Bearer {user['token']}",
         "X-Organization-Id": user["org_id"], "Content-Type": "application/json"}
    await gate.wait()
    async with httpx.AsyncClient(timeout=60.0, **CLIENT_KW) as c:
        async def call(op, method, path, **kw):
            t0 = time.monotonic()
            try:
                resp = await c.request(method, f"{base}{path}", headers=H, **kw)
                dt, sc = time.monotonic() - t0, resp.status_code
                out.append(BrowseCall(op, dt, sc, sc < 400))
                return resp
            except Exception:
                out.append(BrowseCall(op, time.monotonic() - t0, 0, False))
                return None

        while not stop.is_set():
            # /agents page load
            await call("list_agents", "GET", "/api/data_sources")
            if stop.is_set():
                break
            await call("list_instructions", "GET", "/api/instructions?limit=50")
            # create an instruction (write contention)
            resp = await call("create_instruction", "POST", "/api/instructions", json={
                "text": f"Load-test instruction from {user['email']} at {time.time():.3f}",
                "category": "general", "status": "published",
            })
            iid = None
            if resp is not None and resp.status_code < 400:
                try:
                    iid = resp.json().get("id")
                except Exception:
                    pass
            if iid:
                await call("edit_instruction", "PUT", f"/api/instructions/{iid}",
                           json={"text": f"edited at {time.time():.3f}"})
            await call("list_reports", "GET", "/api/reports?limit=20")
            await asyncio.sleep(0.5)   # human-ish think time


# --------------------------------------------------------------------------
# Client self-instrumentation
# --------------------------------------------------------------------------
async def loop_lag_monitor(stop: asyncio.Event, samples: list, interval=0.25):
    """Measure how late our own scheduler is. If this climbs into the hundreds
    of ms, the harness is the bottleneck and its timings are not trustworthy."""
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        samples.append((time.monotonic() - t0 - interval) * 1000.0)


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def verify_server_side(runs):
    """Cross-check client-visible outcomes against the completions table.

    The SSE stream terminates with [DONE] even when the agent run failed or
    never reached a terminal state, so a client that "saw the stream finish"
    is not evidence the completion succeeded. Under load these diverge sharply:
    rows left in `in_progress` are the spinners that never resolve in the UI.
    """
    import sqlalchemy as sa

    ids = [r["completion_id"] for r in runs if r.get("completion_id")]
    if not ids:
        return {"checked": 0, "note": "no completion ids captured"}
    url = os.getenv("BOW_DATABASE_URL",
                    "postgresql://bow:bow@127.0.0.1:5432/bow")
    eng = sa.create_engine(url)
    counts, errors = {}, []
    try:
        with eng.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, status, completion::text FROM completions "
                "WHERE id = ANY(:ids)"), {"ids": ids}).fetchall()
            found = {str(r[0]): (r[1], r[2]) for r in rows}
        for r in runs:
            cid = r.get("completion_id")
            st = found.get(str(cid), ("missing", None))[0] if cid else "no_id"
            r["db_status"] = st
            counts[st] = counts.get(st, 0) + 1
            if st == "error":
                body = found.get(str(cid), (None, ""))[1] or ""
                errors.append(body[:160])
    finally:
        eng.dispose()
    from collections import Counter
    return {"checked": len(ids), "by_status": counts,
            "error_samples": dict(Counter(errors).most_common(5))}


async def fetch_mock_stats(mock_base):
    try:
        async with httpx.AsyncClient(timeout=10, **CLIENT_KW) as c:
            r = await c.get(mock_base.replace("/v1", "") + "/stats")
            return r.json()
    except Exception:
        return {}


async def reset_mock_stats(mock_base):
    try:
        async with httpx.AsyncClient(timeout=10, **CLIENT_KW) as c:
            await c.post(mock_base.replace("/v1", "") + "/stats/reset")
    except Exception:
        pass


# --------------------------------------------------------------------------
async def run_level(state, level, agent_frac, prompt, timeout, duration,
                    baseline_only=False):
    base, ds_id = state["base"], state.get("data_source_id")
    users = state["users"]
    if len(users) < level:
        raise SystemExit(f"seeded {len(users)} users but level={level}")

    n_agent = 0 if baseline_only else max(1, int(round(level * agent_frac)))
    n_browse = level - n_agent if not baseline_only else max(1, int(round(level * 0.3)))

    res = LevelResult(level=level, n_agent=n_agent, n_browse=n_browse)
    agent_out, browse_out, lag = [], [], []
    stop = asyncio.Event()
    gate = asyncio.Event()

    await reset_mock_stats(state.get("mock_llm", ""))

    tasks = []
    for i in range(n_agent):
        tasks.append(asyncio.create_task(
            agent_user(i, users[i], base, ds_id, prompt, timeout, agent_out, gate)))
    for j in range(n_browse):
        u = users[(n_agent + j) % len(users)]
        tasks.append(asyncio.create_task(
            browse_user(j, u, base, stop, browse_out, gate)))
    lag_task = asyncio.create_task(loop_lag_monitor(stop, lag))

    # Let every task reach the gate, then release them together.
    await asyncio.sleep(2.0)
    res.started_at = time.time()
    t0 = time.monotonic()
    gate.set()

    if baseline_only:
        await asyncio.sleep(duration)
        stop.set()
    else:
        agent_tasks = tasks[:n_agent]
        try:
            await asyncio.wait_for(asyncio.gather(*agent_tasks, return_exceptions=True),
                                   timeout=timeout + 120)
        except asyncio.TimeoutError:
            pass
        stop.set()

    await asyncio.gather(*tasks, return_exceptions=True)
    lag_task.cancel()
    res.wall_s = time.monotonic() - t0

    res.agent_runs = [asdict(r) for r in agent_out]
    res.browse_calls = [asdict(b) for b in browse_out]
    res.client_loop_lag_ms = {
        "p50": pct(lag, 50), "p95": pct(lag, 95), "max": max(lag) if lag else None,
        "samples": len(lag),
    }
    res.mock_llm = await fetch_mock_stats(state.get("mock_llm", ""))
    # Give detached background work (queue drain, status writes) a moment to
    # land before asking the DB what actually happened.
    await asyncio.sleep(5.0)
    res.server_side = verify_server_side(res.agent_runs)
    return res


def summarize(res: LevelResult):
    runs = res.agent_runs
    fin = [r for r in runs if r["outcome"] == "finished"]
    tot = [r["t_total"] for r in fin if r["t_total"]]
    tfe = [r["t_first_event"] for r in fin if r["t_first_event"]]
    by_outcome = {}
    for r in runs:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    b_ok = [b for b in res.browse_calls if b["ok"]]
    b_t = [b["t"] for b in b_ok]
    per_op = {}
    for b in res.browse_calls:
        d = per_op.setdefault(b["op"], {"n": 0, "fail": 0, "t": []})
        d["n"] += 1
        if b["ok"]:
            d["t"].append(b["t"])
        else:
            d["fail"] += 1
    return {
        "level": res.level, "n_agent": res.n_agent, "n_browse": res.n_browse,
        "wall_s": round(res.wall_s, 1),
        "agent": {
            "n": len(runs),
            "success_pct": round(100.0 * len(fin) / len(runs), 1) if runs else None,
            "outcomes": by_outcome,
            "t_total_p50": pct(tot, 50), "t_total_p95": pct(tot, 95),
            "t_total_max": max(tot) if tot else None,
            "t_first_event_p50": pct(tfe, 50), "t_first_event_p95": pct(tfe, 95),
        },
        "browse": {
            "n": len(res.browse_calls),
            "fail": len(res.browse_calls) - len(b_ok),
            "p50": pct(b_t, 50), "p95": pct(b_t, 95), "p99": pct(b_t, 99),
            "per_op": {k: {"n": v["n"], "fail": v["fail"],
                           "p50": pct(v["t"], 50), "p95": pct(v["t"], 95)}
                       for k, v in per_op.items()},
        },
        "client_loop_lag_ms": res.client_loop_lag_ms,
        "mock_llm": res.mock_llm,
        "server_side": res.server_side,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, required=True)
    ap.add_argument("--agent-frac", type=float, default=0.6)
    ap.add_argument("--prompt", default="List a few albums")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--duration", type=float, default=60.0,
                    help="baseline-only: seconds of browse traffic")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    state = load_state()
    res = await run_level(state, args.level, args.agent_frac, args.prompt,
                          args.timeout, args.duration, args.baseline_only)
    summ = summarize(res)
    print(json.dumps(summ, indent=2, default=str))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": summ, "raw": asdict(res)}, f, default=str)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
