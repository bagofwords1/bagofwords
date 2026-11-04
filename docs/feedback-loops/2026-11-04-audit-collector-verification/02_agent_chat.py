import sys, os, json, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
H = hdr(ADMIN["token"], ua="Mozilla/5.0 (audit-loop agent run)")
DS = SEED["demo_data_source"]["data_source_id"]
c = httpx.Client(base_url=BASE, headers=H, timeout=None)
rep = c.post("/api/reports", json={"title": "Audit loop agent run", "data_sources": [DS]}); rep.raise_for_status()
rid = rep.json()["id"]; print("report", rid, flush=True)
prompt = ("Run three separate SQL queries against the music store and give me the numbers: (1) total number of invoices, "
          "(2) the top 3 customers by total invoice amount, (3) number of tracks per genre for the top 5 genres. "
          "Execute each as its own query. No charts needed, just the tables.")
t0 = time.time(); last = None
with c.stream("POST", f"/api/reports/{rid}/completions", json={"prompt": {"content": prompt}, "stream": True}, headers={"Accept": "text/event-stream"}) as resp:
    for line in resp.iter_lines():
        if line.startswith("data:"):
            try: d = json.loads(line[5:].strip())
            except Exception: continue
            ev = d.get("event") if isinstance(d, dict) else None
            if ev and ev != last: print(round(time.time()-t0,1), ev, flush=True); last = ev
        if time.time() - t0 > 400: print("timeout"); break
print("done in", round(time.time()-t0,1))
json.dump({"report_id": rid, "seconds": time.time()-t0}, open(os.path.join(S, "agent_run.json"), "w"))
