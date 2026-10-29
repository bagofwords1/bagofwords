import asyncio, json, uuid, httpx
from pathlib import Path

h = json.loads(Path("/tmp/artifact-fixture.json").read_text())["headers"]


async def main():
    async with httpx.AsyncClient(timeout=45) as c:
        base = "http://127.0.0.1:8108"
        r = await c.post(
            base + "/api/reports",
            headers=h,
            json={"title": "Worker contention check", "files": [], "data_sources": []},
        )
        r.raise_for_status()
        a = await c.post(
            base + "/api/artifacts",
            headers=h,
            json={
                "report_id": r.json()["id"],
                "title": "Quota fixture",
                "content": {
                    "code": "<div>Quota fixture</div>",
                    "visualization_ids": [],
                },
            },
        )
        a.raise_for_status()
        path = "/api/artifacts/" + a.json()["artifact_id"] + "/runtime"
        d = await c.post(
            base + path + "/resources",
            headers=h,
            json={
                "action": "create",
                "idempotency_key": str(uuid.uuid4()),
                "definition": {
                    "name": "entries",
                    "max_records": 3,
                    "fields": {"title": {"type": "string"}},
                },
            },
        )
        d.raise_for_status()

        async def write(i):
            return await c.post(
                f"http://127.0.0.1:{8108 + i % 2}"
                + path
                + "/collections/entries/records",
                headers=h,
                json={
                    "action": "create",
                    "data": {"title": str(i)},
                    "idempotency_key": str(uuid.uuid4()),
                },
            )

        responses = await asyncio.gather(*(write(i) for i in range(10)))
        codes = [r.status_code for r in responses]
        assert codes.count(200) == 3 and codes.count(429) == 7, codes
        for port in (8108, 8109):
            response = await c.post(
                f"http://127.0.0.1:{port}" + path + "/collections/entries/records",
                headers=h,
                json={"action": "list"},
            )
            response.raise_for_status()
            assert len(response.json()["items"]) == 3
        print(
            "PASS: two API processes share durable rows and atomically enforce the same quota (3 admitted, 7 rejected)"
        )


asyncio.run(main())
