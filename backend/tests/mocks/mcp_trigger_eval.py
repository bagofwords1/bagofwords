"""End-to-end check for the mcp_failed_then_fixed instruction trigger.

Drives the real agent at the mock's Intercom-shaped tool (`contacts_search`,
whose schema declares nothing required but whose server refuses filter-less
calls), then asks two questions:

  1. Did the run fail-then-recover, as production did?
  2. Did the knowledge harness draft an instruction capturing the rule?

Usage (from backend/, backend + mock running)::

    uv run python tests/mocks/mcp_trigger_eval.py --label t1
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

BASE = "http://localhost:8000/api"
SEED = "/tmp/bow-agent/seed.json"

PROMPT = "Using MockOps, show me a list of contacts."


def session():
    seed = json.load(open(SEED))
    tok = httpx.post(
        f"{BASE}/auth/jwt/login",
        data={"username": seed["email"], "password": "TestPassword123!"},
        timeout=60,
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}", "X-Organization-Id": seed["org_id"]}
    return seed, h


def run_turn(c, h, seed, prompt, timeout_s=420):
    r = c.post("/reports", headers=h, json={"title": "trigger-eval", "data_sources": [seed["ds_id"]]})
    r.raise_for_status()
    rid = r.json()["id"]
    c.post(f"/reports/{rid}/completions", headers=h, json={"prompt": {"content": prompt}, "stream": False})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        rr = c.get(f"/reports/{rid}/completions", headers=h)
        if rr.status_code != 200:
            continue
        body = rr.json()
        comps = body.get("completions", body) if isinstance(body, dict) else body
        if not isinstance(comps, list):
            continue
        sysc = [x for x in comps if isinstance(x, dict) and x.get("role") == "system"]
        if sysc and sysc[-1].get("status") in ("success", "error"):
            return rid, sysc[-1].get("status")
    return rid, "timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="t1")
    args = ap.parse_args()

    def drafts(c, h):
        """Harness output lands as draft instructions inside a submitted AI
        build — it is NOT in the default (published) instruction list, so the
        status filter is required or this silently reports zero."""
        r = c.get("/instructions", headers=h, params={"status": "draft"})
        if r.status_code != 200:
            return []
        body = r.json()
        return body.get("items", []) if isinstance(body, dict) else (body or [])

    seed, h = session()
    with httpx.Client(base_url=BASE, timeout=600) as c:
        n_before = len(drafts(c, h))
        print("pending instruction drafts before:", n_before)

        rid, status = run_turn(c, h, seed, PROMPT)
        print("report:", rid, "status:", status)

        # The harness runs after the answer streams; give it room to finish.
        time.sleep(45)

        items = drafts(c, h)
        print("pending instruction drafts after:", len(items))
        for it in items:
            if not isinstance(it, dict):
                print("-", str(it)[:200])
                continue
            txt = it.get("text") or it.get("content") or it.get("new_text") or ""
            print("-" * 70)
            print("status:", it.get("status"), "| category:", it.get("category"))
            print(txt[:600])

    json.dump({"report": rid, "status": status, "instructions": items},
              open(f"/tmp/bow-agent/trigger_{args.label}.json", "w"), indent=2, default=str)
    print("\nwrote /tmp/bow-agent/trigger_%s.json" % args.label)


if __name__ == "__main__":
    main()
