#!/usr/bin/env python3
"""
Experiment: does _AGENT_RUN_SEMAPHORE actually bound concurrent agent runs?

`_AGENT_RUN_SEMAPHORE` (completion_service.py) is acquired in exactly two
places — the streaming path and the queued-prompt dispatcher. Every other
entry point into `agent_v2` (`create_completion`, both `background=True` and
`background=False`) runs the agent without it. Callers on that path today
include Slack/Teams/WhatsApp (`background=True`), scheduled prompts, webhooks
and machine turns (`background=False`).

Measurement: the mock LLM counts how many requests are in flight at once.
Every executing agent holds at least one LLM call for most of its life, so
peak in-flight LLM calls is a lower bound on concurrently executing agents.

  streaming path  -> peak should sit at/below BOW_MAX_CONCURRENT_AGENTS
  background path -> peak should track N, ignoring the cap

Run with a low cap to make the difference unambiguous:
  BOW_MAX_CONCURRENT_AGENTS=4 (restart backend) then
  python exp_semaphore_bypass.py --n 16
"""
import argparse
import asyncio
import json
import os
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = json.load(open(os.path.join(HERE, "sandbox_state.json")))
CLIENT_KW = dict(trust_env=False)
MOCK = STATE.get("mock_llm", "http://127.0.0.1:9001/v1").replace("/v1", "")


async def mock_reset():
    async with httpx.AsyncClient(timeout=10, **CLIENT_KW) as c:
        await c.post(f"{MOCK}/stats/reset")


async def mock_stats():
    async with httpx.AsyncClient(timeout=10, **CLIENT_KW) as c:
        return (await c.get(f"{MOCK}/stats")).json()


async def one(mode, user, ds_id, prompt, timeout, out):
    base = STATE["base"]
    H = {"Authorization": f"Bearer {user['token']}",
         "X-Organization-Id": user["org_id"], "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, **CLIENT_KW) as c:
        r = await c.post(f"{base}/api/reports", headers=H,
                         json={"title": f"exp-{mode}-{time.time()}",
                               "data_sources": [ds_id] if ds_id else []})
        if r.status_code >= 400:
            out.append(("report_error", r.status_code))
            return
        rid = r.json()["id"]
        body = {"prompt": {"content": prompt, "mode": "chat"}}
        t0 = time.monotonic()
        if mode == "stream":
            body["stream"] = True
            async with c.stream("POST", f"{base}/api/reports/{rid}/completions",
                                headers=H, json=body) as resp:
                async for _ in resp.aiter_lines():
                    pass
            out.append(("done", time.monotonic() - t0))
        else:
            # background=true returns immediately; the agent runs detached.
            r = await c.post(f"{base}/api/reports/{rid}/completions?background=true",
                             headers=H, json=body)
            out.append((f"accepted_{r.status_code}", time.monotonic() - t0))


async def run_mode(mode, n, prompt, timeout, settle):
    users = [u for u in STATE["users"] if u.get("verified")]
    ds = STATE.get("data_source_id")
    await mock_reset()
    out = []
    tasks = [asyncio.create_task(one(mode, users[i % len(users)], ds, prompt, timeout, out))
             for i in range(n)]
    await asyncio.gather(*tasks, return_exceptions=True)
    if mode != "stream":
        # Background runs outlive the HTTP response; watch the peak until the
        # in-flight count drains, otherwise we'd sample before they ramp up.
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            s = await mock_stats()
            if s.get("in_flight", 0) == 0 and s.get("completed", 0) >= n:
                break
            await asyncio.sleep(1.0)
    return out, await mock_stats()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--prompt", default="List a few albums")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--settle", type=float, default=240.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cap = os.getenv("BOW_MAX_CONCURRENT_AGENTS", "12 (default)")
    print(f"BOW_MAX_CONCURRENT_AGENTS reported by env: {cap}")
    print(f"firing n={args.n} per mode\n")

    report = {"n": args.n, "cap_env": cap, "modes": {}}
    for mode in ("stream", "background"):
        print(f"--- {mode} ---")
        out, stats = await run_mode(mode, args.n, args.prompt, args.timeout, args.settle)
        peak = stats.get("peak_in_flight")
        print(f"  peak concurrent LLM calls: {peak}")
        print(f"  llm requests total: {stats.get('requests')}  completed: {stats.get('completed')}")
        report["modes"][mode] = {"peak_in_flight": peak, "stats": stats,
                                 "outcomes": [o[0] for o in out]}
        await asyncio.sleep(10)

    s_peak = report["modes"]["stream"]["peak_in_flight"]
    b_peak = report["modes"]["background"]["peak_in_flight"]
    print(f"\nstream peak={s_peak}  background peak={b_peak}")
    print("=> background path is NOT bounded by the agent semaphore"
          if (b_peak or 0) > (s_peak or 0) else
          "=> no difference observed at this n")

    if args.out:
        json.dump(report, open(args.out, "w"), indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
