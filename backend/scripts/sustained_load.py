"""Sustained concurrent API load to simulate a multi-user deployment sharing the
connection pool, so a browser navigating at the same time experiences the real
production slowness. Loops for DURATION seconds at fixed concurrency.

Usage: python scripts/sustained_load.py <concurrency> <duration_s>
"""
import os, sys, time, asyncio
import httpx

BASE = "http://localhost:8000"
TOK = open("/tmp/token.txt").read().strip()
ORG = open("/tmp/org.txt").read().strip()
H = {"Authorization": f"Bearer {TOK}", "X-Organization-Id": ORG}
MIX = [
    "/api/users/whoami",
    "/api/reports?filter=my&limit=50&view=minimal",
    "/api/instructions?skip=0&limit=200&include_own=true&include_drafts=true&include_archived=true",
    "/api/instructions/pending-changes",
    "/api/data_sources/active?include_unconnected=true",
]


async def worker(client, stop_at, i):
    k = i
    while time.perf_counter() < stop_at:
        url = MIX[k % len(MIX)]; k += 1
        try:
            await client.get(BASE + url, headers=H, timeout=60)
        except Exception:
            pass


async def main():
    conc = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    stop_at = time.perf_counter() + dur
    print(f"sustained load: concurrency={conc} duration={dur}s")
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=conc + 10)) as client:
        await asyncio.gather(*[worker(client, stop_at, i) for i in range(conc)])
    print("load done")


if __name__ == "__main__":
    asyncio.run(main())
