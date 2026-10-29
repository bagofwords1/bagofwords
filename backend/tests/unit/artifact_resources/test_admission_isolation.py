import asyncio
import pytest
from app.services.artifact_body_limit import ArtifactBodyLimit


@pytest.mark.asyncio
async def test_stream_saturation_preserves_ordinary_request_capacity():
    gate = asyncio.Event()

    async def app(scope, receive, send):
        if scope["path"].endswith("/stream"):
            await gate.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limiter = ArtifactBodyLimit(app)

    async def request(path):
        statuses = []

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        await limiter(
            {
                "type": "http",
                "method": "POST" if path.endswith("/stream") else "GET",
                "path": "/api/artifacts/a/runtime/" + path,
                "headers": [],
            },
            receive,
            send,
        )
        return statuses[0]

    tasks = []
    try:
        for _ in range(65):
            task = asyncio.create_task(request("ai/op/stream"))
            tasks.append(task)
            await asyncio.sleep(0)
            if task.done():
                assert task.result() == 429
                break
        else:
            pytest.fail("Streams were not bounded")
        assert await request("resources") == 200
    finally:
        gate.set()
        await asyncio.gather(*tasks)
