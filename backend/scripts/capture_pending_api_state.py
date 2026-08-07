"""Capture what the /agents page actually renders about pending changes.

The fingerprint proves the stored rows match. This proves the *observable*
contract matches: the badge counts, the org-wide sweep, the pending-only list,
and the authoritative per-instruction review must all report the same thing
before and after the write-path refactor — even though the delete path now
records one void marker where it used to record per-hunk rejections.

Usage:
  python scripts/capture_pending_api_state.py <label> <out.json> [instruction_id ...]
"""
import os
import sys
import json
import asyncio

import httpx

BASE = os.environ.get("BOW_BASE", "http://localhost:8000")


async def main():
    label = sys.argv[1]
    out_path = sys.argv[2]
    probe_ids = sys.argv[3:]

    async with httpx.AsyncClient(base_url=BASE, timeout=900.0, trust_env=False) as cl:
        tok = (await cl.post("/api/auth/jwt/login",
                             data={"username": "sandbox@bow.dev",
                                   "password": "Sandbox123!"})).json()["access_token"]
        org = (await cl.get("/api/organizations",
                            headers={"Authorization": f"Bearer {tok}"})).json()[0]["id"]
        H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

        counts = (await cl.get("/api/instructions/counts", headers=H)).json()
        sweep = (await cl.get("/api/instructions/pending-changes", headers=H)).json()
        plist = (await cl.get("/api/instructions", headers=H, params={
            "pending_only": "true", "include_own": "true", "include_drafts": "true",
            "include_archived": "true", "limit": 2000,
        })).json()

        state = {
            "label": label,
            "counts_total": counts.get("total"),
            "counts_pending_total": counts.get("pending_total"),
            "counts_pending_ids": sorted(str(i) for i in counts.get("pending_instruction_ids", [])),
            "sweep_ids": sorted(str(i) for i in sweep.get("instruction_ids", [])),
            "pending_list_ids": sorted(str(r["id"]) for r in plist.get("items", [])),
            "review": {},
        }
        for iid in probe_ids:
            r = await cl.get(f"/api/instructions/{iid}/review-hunks", headers=H)
            state["review"][iid] = {
                "status": r.status_code,
                "n_suggestions": (len(r.json().get("suggestions", []))
                                  if r.status_code == 200 else None),
            }

    with open(out_path, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    print(f"[{label}] total={state['counts_total']} pending_total={state['counts_pending_total']} "
          f"sweep={len(state['sweep_ids'])} list={len(state['pending_list_ids'])}")
    for iid, v in state["review"].items():
        print(f"   review {iid[:8]}: HTTP {v['status']} suggestions={v['n_suggestions']}")
    print(f"   wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
