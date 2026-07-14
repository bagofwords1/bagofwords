#!/usr/bin/env python3
"""Stress the catalog: 500 extra sourcetypes (20 events each) via HEC."""
import gzip, json, os, random, time
from datetime import datetime, timedelta, timezone
import requests, urllib3
urllib3.disable_warnings()

HEC = "https://localhost:8088/services/collector/event"
HEC_TOKEN = os.environ.get("SPLUNK_HEC_TOKEN", "bow-hec-token-1234")
random.seed(99)
now = datetime.now(timezone.utc)

sess = requests.Session()
sess.headers.update({"Authorization": f"Splunk {HEC_TOKEN}", "Content-Encoding": "gzip"})

def flush(batch):
    payload = gzip.compress("\n".join(json.dumps(e) for e in batch).encode())
    for attempt in range(5):
        r = sess.post(HEC, data=payload, verify=False, timeout=120)
        if r.status_code == 200:
            return len(batch)
        print("HEC error", r.status_code, r.text[:150]); time.sleep(2 * (attempt + 1))
    raise SystemExit("HEC failed")

TEAMS = ["payments", "orders", "inventory", "shipping", "billing", "notify",
         "search", "profile", "gateway", "batch"]
total, batch = 0, []
for i in range(500):
    st = f"svc_{TEAMS[i % len(TEAMS)]}_{i:03d}"
    idx = ["app", "web", "security"][i % 3]
    for j in range(20):
        ts = now - timedelta(hours=random.uniform(0, 72))
        evt = {"level": random.choice(["INFO", "WARN", "ERROR"]),
               "component": st, "seq": j,
               "message": f"heartbeat {j} from {st}",
               "duration_ms": round(random.uniform(1, 500), 1)}
        batch.append({"time": ts.timestamp(), "host": f"{st}-host",
                      "index": idx, "sourcetype": st,
                      "source": f"/var/log/{st}.log", "event": evt})
    while len(batch) >= 2000:
        total += flush(batch[:2000]); batch = batch[2000:]
if batch:
    total += flush(batch)
print(f"SEEDED {total} events across 500 new sourcetypes")
