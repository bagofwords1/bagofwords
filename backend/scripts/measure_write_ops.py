"""Time the instruction write paths over real HTTP, and measure the collateral
damage they do to other requests on the same worker.

Deterministic: targets are chosen by sorted id from the seeded corpus, so a
before-run and an after-run from the same pristine DB touch the same rows.

Three things are measured:
  save        PUT    /api/instructions/{id}
  delete      DELETE /api/instructions/{id}   (the "hot" row, many suggestions)
  collateral  a trivial GET fired CONCURRENTLY with the delete. If the CPU
              diff runs on the event loop, this cheap request cannot be served
              until the delete finishes -- that is the page-wide stall.

Usage:
  python scripts/measure_write_ops.py <label> [out.json]
"""
import os
import sys
import json
import time
import asyncio
import sqlite3

import httpx

BASE = os.environ.get("BOW_BASE", "http://localhost:8000")
DB = os.environ.get("BOW_SQLITE", "db/app.db")


def db_counts():
    c = sqlite3.connect(DB)
    q = lambda s: c.execute(s).fetchone()[0]
    out = {
        "build_contents": q("select count(*) from build_contents"),
        "builds": q("select count(*) from instruction_builds where deleted_at is null"),
        "instructions_live": q("select count(*) from instructions where deleted_at is null"),
    }
    c.close()
    return out


def pick_targets():
    """Deterministic: the row carrying the most pending suggestions is the
    delete target; the lowest-id row that is live in main is the save target."""
    c = sqlite3.connect(DB)
    # Count only rows that actually PROPOSE a change (is_change), not the
    # carry-over rows every full-snapshot build holds for every instruction.
    hot = c.execute("""
        select bc.instruction_id, count(*) n
          from build_contents bc
          join instruction_builds ib on ib.id = bc.build_id
         where ib.is_main = 0 and ib.deleted_at is null
           and ib.status in ('draft','pending_approval')
           and bc.is_change = 1
         group by bc.instruction_id order by n desc, bc.instruction_id asc limit 1
    """).fetchone()
    save = c.execute("""
        select i.id from instructions i
          join build_contents bc on bc.instruction_id = i.id
          join instruction_builds ib on ib.id = bc.build_id and ib.is_main = 1
         where i.deleted_at is null and i.id != ?
         order by i.id asc limit 1
    """, (hot[0],)).fetchone()
    c.close()
    return save[0], hot[0], hot[1]


async def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    out_path = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/measure_{label}.json"

    async with httpx.AsyncClient(base_url=BASE, timeout=600.0, trust_env=False) as cl:
        r = await cl.post("/api/auth/jwt/login",
                          data={"username": "sandbox@bow.dev", "password": "Sandbox123!"})
        tok = r.json()["access_token"]
        org = (await cl.get("/api/organizations",
                            headers={"Authorization": f"Bearer {tok}"})).json()[0]["id"]
        H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

        save_id, del_id, n_sug = pick_targets()
        before = db_counts()
        res = {"label": label, "save_target": save_id, "delete_target": del_id,
               "suggestions_on_delete_target": n_sug, "db_before": before}

        # ---- coherence guard ----
        # The server must be serving the SAME database file this script reads
        # its counts from. A backend whose workers survived a restart keeps an
        # open handle on the replaced (now unlinked) file and answers 200 from
        # it, which silently turns a benchmark into fiction. Compare the live
        # row against the file before trusting a single number.
        warm = await cl.get(f"/api/instructions/{save_id}", headers=H)
        if warm.status_code != 200:
            raise SystemExit(f"ABORT: save target not readable over HTTP ({warm.status_code})")
        api_text = warm.json().get("text") or ""
        c = sqlite3.connect(DB)
        file_text = c.execute(
            "select text from instructions where id=?", (save_id,)
        ).fetchone()[0] or ""
        c.close()
        if api_text != file_text:
            raise SystemExit(
                "ABORT: the API and the database file disagree about the save "
                "target — the backend is serving a stale/unlinked DB. "
                f"api={len(api_text)} chars, file={len(file_text)} chars. "
                "Restart the backend (scripts/sandbox_reset.sh) before measuring."
            )

        # ---- baseline latency of the cheap probe, worker idle ----
        t0 = time.perf_counter()
        await cl.get("/api/organizations", headers=H)
        res["probe_idle_s"] = round(time.perf_counter() - t0, 3)

        # ---- SAVE ----
        cur = (await cl.get(f"/api/instructions/{save_id}", headers=H)).json()
        t0 = time.perf_counter()
        r = await cl.put(f"/api/instructions/{save_id}", headers=H,
                         json={"text": (cur.get("text") or "") + " Always exclude test accounts."})
        res["save_s"] = round(time.perf_counter() - t0, 3)
        res["save_status"] = r.status_code

        # ---- DELETE, with a cheap probe fired concurrently ----
        probe_lat = []

        async def probe():
            # let the delete get going first, then time a trivial request
            await asyncio.sleep(0.25)
            t = time.perf_counter()
            await cl.get("/api/organizations", headers=H)
            probe_lat.append(time.perf_counter() - t)

        t0 = time.perf_counter()
        rr, _ = await asyncio.gather(
            cl.delete(f"/api/instructions/{del_id}", headers=H),
            probe(),
        )
        res["delete_s"] = round(time.perf_counter() - t0, 3)
        res["delete_status"] = rr.status_code
        res["probe_during_delete_s"] = round(probe_lat[0], 3) if probe_lat else None

        res["db_after"] = db_counts()
        res["build_contents_added"] = res["db_after"]["build_contents"] - before["build_contents"]

    with open(out_path, "w") as f:
        json.dump(res, f, indent=2)

    print(f"=== {label} ===")
    print(f"  save                     {res['save_s']:7.2f}s  (HTTP {res['save_status']})")
    print(f"  delete ({n_sug} suggestions)  {res['delete_s']:7.2f}s  (HTTP {res['delete_status']})")
    print(f"  cheap GET, worker idle   {res['probe_idle_s']:7.2f}s")
    print(f"  cheap GET during delete  {res['probe_during_delete_s']:7.2f}s   <- event-loop stall")
    print(f"  build_contents rows added by the two ops: {res['build_contents_added']}")
    print(f"  wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
