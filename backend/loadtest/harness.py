#!/usr/bin/env python3
"""
Load-test harness for BoW completion SSE endpoint.

Fires N concurrent streaming completions against a report and measures the
client-visible reliability/latency characteristics that matter for the
"network error" concurrency bug:

  - connect / time-to-first-byte (response headers)
  - time-to-first-SSE-event
  - total stream duration
  - terminal outcome: finished | http_error | sse_drop | timeout | exception
  - event count + whether completion.finished / [DONE] was seen

Usage:
  python harness.py --concurrency 1,2,5,10,20 --prompt "Show revenue by month" \
      --base http://localhost:8000 --timeout 180 --out results.json
"""
import argparse, asyncio, json, os, statistics, sys, time
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "sandbox_state.json")


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


@dataclass
class RunResult:
    idx: int
    report_id: str
    ok: bool = False
    outcome: str = "unknown"          # finished|http_error|sse_drop|timeout|exception
    status_code: Optional[int] = None
    t_connect: Optional[float] = None     # seconds to response headers
    t_first_event: Optional[float] = None # seconds to first SSE data line
    t_total: Optional[float] = None       # seconds to stream end
    n_events: int = 0
    saw_finished: bool = False
    error: Optional[str] = None


async def create_report(client, base, headers, ds_id) -> str:
    """Create a report, retrying on 5xx (clients retry; we want to measure the
    completion path, not transient report-creation contention)."""
    body = {"title": f"loadtest-{int(time.time()*1000)}", "data_sources": [ds_id]}
    last = None
    for attempt in range(5):
        r = await client.post(f"{base}/api/reports", headers=headers, json=body)
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()["id"]
        last = r.status_code
        await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"create_report failed after retries (last={last})")


async def one_completion(client, base, headers, report_id, prompt, timeout, idx) -> RunResult:
    res = RunResult(idx=idx, report_id=report_id)
    url = f"{base}/api/reports/{report_id}/completions"
    h = {**headers, "Accept": "text/event-stream", "Content-Type": "application/json"}
    body = {"prompt": {"content": prompt}}
    t0 = time.perf_counter()
    try:
        async with client.stream("POST", url, headers=h, json=body, timeout=timeout) as r:
            res.status_code = r.status_code
            res.t_connect = time.perf_counter() - t0
            if r.status_code != 200:
                res.outcome = "http_error"
                try:
                    res.error = (await r.aread()).decode()[:300]
                except Exception:
                    pass
                return res
            async for line in r.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:") or line.startswith("event:"):
                    if res.t_first_event is None:
                        res.t_first_event = time.perf_counter() - t0
                    res.n_events += 1
                    if "completion.finished" in line or "[DONE]" in line:
                        res.saw_finished = True
            res.t_total = time.perf_counter() - t0
            # Stream ended. If we never saw a terminal marker, the connection
            # was cut mid-stream -> this is the "network error" the UI shows.
            if res.saw_finished:
                res.ok = True
                res.outcome = "finished"
            else:
                res.outcome = "sse_drop"
                res.error = "stream ended without completion.finished/[DONE]"
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
        res.outcome = "timeout"
        res.error = f"{type(e).__name__}: {e}"
        res.t_total = time.perf_counter() - t0
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
        # connection reset mid-stream == browser "network error"
        res.outcome = "sse_drop"
        res.error = f"{type(e).__name__}: {e}"
        res.t_total = time.perf_counter() - t0
    except Exception as e:
        res.outcome = "exception"
        res.error = f"{type(e).__name__}: {e}"
        res.t_total = time.perf_counter() - t0
    return res


def pct(xs, p):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1)))))
    return round(xs[k], 3)


def _stat(xs, which):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    if which == "min":
        return round(min(xs), 3)
    if which == "max":
        return round(max(xs), 3)
    if which == "mean":
        return round(sum(xs) / len(xs), 3)


async def run_level(client, base, headers, ds_id, prompt, n, timeout, reuse_reports):
    # one report per concurrent client (closer to real usage: distinct chats)
    created = await asyncio.gather(
        *[create_report(client, base, headers, ds_id) for _ in range(n)],
        return_exceptions=True,
    )
    reports = [r for r in created if isinstance(r, str)]
    report_failures = n - len(reports)
    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        one_completion(client, base, headers, reports[i], prompt, timeout, i)
        for i in range(len(reports))
    ])
    wall = time.perf_counter() - t0
    ok = [r for r in results if r.ok]
    by_outcome = {}
    for r in results:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    if report_failures:
        by_outcome["report_create_failed"] = report_failures
    summary = {
        "concurrency": n,
        "wall_seconds": round(wall, 2),
        "success": len(ok),
        "success_rate": round(len(ok) / n, 3),
        "report_failures": report_failures,
        "outcomes": by_outcome,
        "t_connect_p50": pct([r.t_connect for r in results], 50),
        "t_connect_p95": pct([r.t_connect for r in results], 95),
        "t_first_event_p50": pct([r.t_first_event for r in results], 50),
        "t_first_event_p95": pct([r.t_first_event for r in results], 95),
        # Completion DURATION (end-to-end, successful runs): full distribution.
        "duration_min": _stat([r.t_total for r in ok], "min"),
        "duration_mean": _stat([r.t_total for r in ok], "mean"),
        "t_total_p50": pct([r.t_total for r in ok], 50),
        "t_total_p95": pct([r.t_total for r in ok], 95),
        "t_total_p99": pct([r.t_total for r in ok], 99),
        "duration_max": _stat([r.t_total for r in ok], "max"),
        "raw": [asdict(r) for r in results],
    }
    return summary


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--concurrency", default="1,2,5,10,20")
    ap.add_argument("--prompt", default="Show total revenue by month for 2010, as a table.")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results.json"))
    ap.add_argument("--settle", type=float, default=5.0, help="seconds to wait between levels")
    ap.add_argument("--reuse-reports", action="store_true")
    args = ap.parse_args()

    st = load_state()
    token = st["session"]["token"]
    org_id = st["session"]["org_id"]
    ds_id = st["session"]["ds_id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    limits = httpx.Limits(max_connections=max(levels) + 10, max_keepalive_connections=max(levels) + 10)
    out = {"base": args.base, "prompt": args.prompt, "levels": []}
    async with httpx.AsyncClient(limits=limits, timeout=args.timeout) as client:
        for n in levels:
            print(f"\n=== concurrency {n} ===", flush=True)
            s = await run_level(client, args.base, headers, ds_id, args.prompt, n, args.timeout, args.reuse_reports)
            print(json.dumps({k: v for k, v in s.items() if k != "raw"}, indent=2), flush=True)
            out["levels"].append(s)
            with open(args.out, "w") as f:
                json.dump(out, f, indent=2)
            if n != levels[-1]:
                await asyncio.sleep(args.settle)
    print(f"\nwrote {args.out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
