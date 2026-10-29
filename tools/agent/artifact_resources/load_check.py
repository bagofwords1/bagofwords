"""Small local contention/page-size check; not production capacity certification."""

import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path
import httpx


async def main():
    headers = json.loads(Path("/tmp/artifact-fixture.json").read_text())["headers"]
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8108", timeout=60
    ) as client:
        response = await client.post(
            "/api/reports",
            headers=headers,
            json={"title": "Bounded page load check", "files": [], "data_sources": []},
        )
        response.raise_for_status()
        artifact = await client.post(
            "/api/artifacts",
            headers=headers,
            json={
                "report_id": response.json()["id"],
                "title": "Load check",
                "content": {
                    "code": "<div>Local load fixture</div>",
                    "visualization_ids": [],
                },
            },
        )
        artifact.raise_for_status()
        base = "/api/artifacts/" + artifact.json()["artifact_id"] + "/runtime"
        response = await client.post(
            base + "/resources",
            headers=headers,
            json={
                "action": "create",
                "idempotency_key": str(uuid.uuid4()),
                "definition": {
                    "name": "documents",
                    "fields": {
                        k: {"type": "string", "max_length": 100000}
                        for k in ("a", "b", "c")
                    },
                },
            },
        )
        response.raise_for_status()
        semaphore = asyncio.Semaphore(6)

        async def insert(number):
            async with semaphore:
                response = await client.post(
                    base + "/collections/documents/records",
                    headers=headers,
                    json={
                        "action": "create",
                        "data": {k: str(number) + "x" * 49990 for k in ("a", "b", "c")},
                        "idempotency_key": str(uuid.uuid4()),
                    },
                )
                response.raise_for_status()

        await asyncio.gather(*(insert(i) for i in range(100)))

        async def read(number):
            start = time.perf_counter()
            response = await client.post(
                f"http://127.0.0.1:{8108 + number % 2}"
                + base
                + "/collections/documents/records",
                headers=headers,
                json={"action": "list", "limit": 100},
            )
            response.raise_for_status()
            page = response.json()
            assert 0 < len(page["items"]) < 100 and page["nextCursor"]
            assert len(response.content) <= 1048576
            return (time.perf_counter() - start) * 1000, len(response.content)

        results = await asyncio.gather(*(read(i) for i in range(16)))
        latencies = sorted(r[0] for r in results)
        print(
            json.dumps(
                {
                    "records": 100,
                    "approxPayloadMiB": 14.3,
                    "concurrentReads": 16,
                    "workers": 2,
                    "medianMs": round(statistics.median(latencies)),
                    "p95Ms": round(latencies[-1]),
                    "maxResponseBytes": max(r[1] for r in results),
                    "status": "passed",
                    "scope": "local SQLite smoke, not production capacity",
                }
            )
        )


asyncio.run(main())
