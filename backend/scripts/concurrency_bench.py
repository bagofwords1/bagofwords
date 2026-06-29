"""Fire a burst of concurrent GET requests (mimicking the page load storm) and
report per-request latency distribution. Demonstrates Cause A: connection-pool
saturation + last_seen write contention inflate latency under concurrency, even
for endpoints that are fast in isolation.

Usage: python scripts/concurrency_bench.py <concurrency> [endpoint]
  endpoint default = mix of the 3 slow endpoints
"""
import os, sys, time, asyncio, statistics
import httpx

BASE = "http://localhost:8000"
TOK = open("/tmp/token.txt").read().strip()
ORG = open("/tmp/org.txt").read().strip()
H = {"Authorization": f"Bearer {TOK}", "X-Organization-Id": ORG}

MIX = [
    "/api/reports?filter=my&limit=50",
    "/api/instructions?skip=0&limit=200&include_own=true&include_drafts=true&include_archived=true",
    "/api/instructions/pending-changes",
]


async def one(client, url, idx):
    t0 = time.perf_counter()
    try:
        r = await client.get(BASE + url, headers=H, timeout=60)
        return time.perf_counter() - t0, r.status_code
    except Exception as e:
        return time.perf_counter() - t0, f"ERR:{type(e).__name__}"


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    ep = sys.argv[2] if len(sys.argv) > 2 else None
    urls = [ep] * n if ep else [MIX[i % len(MIX)] for i in range(n)]
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=n + 10)) as client:
        t0 = time.perf_counter()
        results = await asyncio.gather(*[one(client, urls[i], i) for i in range(n)])
        wall = time.perf_counter() - t0
    lat = sorted(t for t, _ in results)
    codes = {}
    for _, c in results:
        codes[c] = codes.get(c, 0) + 1
    def pct(p): return lat[min(len(lat) - 1, int(len(lat) * p))]
    print(f"concurrency:   {n}   endpoint: {ep or 'MIX(reports,instructions,pending-changes)'}")
    print(f"wall (all):    {wall:.2f}s")
    print(f"latency p50:   {statistics.median(lat):.2f}s")
    print(f"latency p95:   {pct(0.95):.2f}s")
    print(f"latency max:   {max(lat):.2f}s")
    print(f"status codes:  {codes}")


if __name__ == "__main__":
    asyncio.run(main())
