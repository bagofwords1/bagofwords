"""Benchmark the /monitoring/diagnosis backend endpoints.

Times each API call the diagnosis page fires on load (median of R runs,
sequential, warm), exactly as the frontend sends them ("All time" period =
only end_date). Prints one markdown table row per endpoint.

Usage:
  uv run python scripts/bench_diagnosis_endpoints.py <token_file> <org_file> [runs]
"""
import statistics
import sys
import time
from datetime import datetime, timezone

import httpx

BASE = "http://localhost:8000"
token = open(sys.argv[1]).read().strip()
org = open(sys.argv[2]).read().strip()
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 5

end = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")

client = httpx.Client(
    headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org},
    timeout=300,
)

n_ae = None
CALLS = [
    ("summaries p1 (table)", f"/api/console/agent_executions/summaries?page=1&page_size=10&end_date={end}"),
    ("summaries deep page", None),  # filled in after we know total_items
    ("diagnosis/metrics (KPIs)", f"/api/console/diagnosis/metrics?end_date={end}"),
    ("diagnosis/timeseries (chart)", f"/api/console/diagnosis/timeseries?end_date={end}"),
]

# discover total_items for the deep-page case
first = client.get(BASE + CALLS[0][1]).json()
total = first.get("total_items", 0)
last_page = max(1, total // 10)
CALLS[1] = ("summaries deep page", f"/api/console/agent_executions/summaries?page={last_page}&page_size=10&end_date={end}")

print(f"agent executions in range: {total}\n")
print("| endpoint | median | min | max |")
print("|---|---|---|---|")
results = {}
for name, path in CALLS:
    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        r = client.get(BASE + path)
        r.raise_for_status()
        times.append(time.perf_counter() - t0)
    results[name] = statistics.median(times)
    print(f"| {name} | {statistics.median(times)*1000:,.0f} ms "
          f"| {min(times)*1000:,.0f} ms | {max(times)*1000:,.0f} ms |")

# page paints when the slowest of the 3 parallel initial calls returns
initial = [results["summaries p1 (table)"], results["diagnosis/metrics (KPIs)"],
           results["diagnosis/timeseries (chart)"]]
print(f"\npage-load critical path (max of the 3 parallel calls): "
      f"{max(initial)*1000:,.0f} ms")
