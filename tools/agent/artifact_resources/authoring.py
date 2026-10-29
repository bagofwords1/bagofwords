"""Exercise real authoring tools and browser validation with supplied synthetic code."""

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
import httpx
import main as _application  # noqa: F401 — Register the application model graph before using ORM sessions.
from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.ai.tools.implementations.edit_artifact import EditArtifactTool


async def main():
    headers = json.loads(Path("/tmp/artifact-fixture.json").read_text())["headers"]
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8108", timeout=120
    ) as client:
        response = await client.post(
            "/api/reports",
            headers=headers,
            json={"title": "Authoring contract check", "files": [], "data_sources": []},
        )
        response.raise_for_status()
        report_id = response.json()["id"]
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            ctx = {
                "db": db,
                "report": report,
                "user": await db.get(User, report.user_id),
                "organization": await db.get(Organization, report.organization_id),
            }
            code = '<script type="text/babel">function App(){return <main>Project notes</main>} ReactDOM.createRoot(document.getElementById("root")).render(<App/>);</script>'
            events = [
                event
                async for event in CreateArtifactTool().run_stream(
                    {
                        "prompt": "A place for project notes",
                        "code": code,
                        "title": "Project notes",
                        "resources": [
                            {
                                "name": "notes",
                                "fields": {
                                    "title": {"type": "string", "required": True}
                                },
                            }
                        ],
                    },
                    ctx,
                )
            ]
            result = [e.payload for e in events if e.type == "tool.end"][-1]
            assert result["output"].get("artifact_id"), result.get("observation")
            parent = result["output"]["resource_artifact_id"]
            version = result["output"]["artifact_id"]
            base = f"/api/artifacts/{parent}/runtime"
            response = await client.post(
                base + "/collections/notes/records",
                headers=headers,
                json={
                    "action": "create",
                    "data": {"title": "Persist across UI edits"},
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
            response.raise_for_status()
            record = response.json()
            edit = {
                "artifact_id": version,
                "expected_latest_version": 1,
                "edits": [{"find": "Project notes", "replace": "Team notes"}],
            }
            ends = [
                e.payload
                async for e in EditArtifactTool().run_stream(edit, ctx)
                if e.type == "tool.end"
            ]
            assert ends[-1]["output"]["success"], ends[-1]
            assert ends[-1]["output"]["version"] == 2
            stale = [
                e.payload
                async for e in EditArtifactTool().run_stream(edit, ctx)
                if e.type == "tool.end"
            ]
            assert stale[-1]["output"]["success"] is False, stale[-1]
            await db.rollback()
            response = await client.post(
                base + "/collections/notes/records",
                headers=headers,
                json={"action": "get", "id": record["id"]},
            )
            response.raise_for_status()
            assert response.json()["data"]["title"] == "Persist across UI edits"
            await db.refresh(ctx["user"])
            await db.refresh(ctx["organization"])
            from app.ai.tools.mcp.create_artifact import CreateArtifactMCPTool
            from app.ai.tools.mcp.edit_artifact import EditArtifactMCPTool

            created = await CreateArtifactMCPTool().execute(
                {
                    "report_id": report_id,
                    "prompt": "A small notes app",
                    "code": code,
                    "resources": [
                        {"name": "notes", "fields": {"title": {"type": "string"}}}
                    ],
                },
                db,
                ctx["user"],
                ctx["organization"],
            )
            assert created["success"] and created["resource_artifact_id"], created
            edited = await EditArtifactMCPTool().execute(
                {
                    "report_id": report_id,
                    "artifact_id": created["artifact_id"],
                    "expected_latest_version": 1,
                    "edits": [{"find": "Project notes", "replace": "Shared notes"}],
                },
                db,
                ctx["user"],
                ctx["organization"],
            )
            assert edited["success"] and edited["version"] == 2, edited
            assert edited["resource_artifact_id"] == created["resource_artifact_id"]
            print(
                "PASS: MCP create and exact-edit use the real shared authoring validator and retain stable identity"
            )
            print(
                "PASS: real create/edit tools render-validate code, configure resources, retain data and reject stale UI edits"
            )

    thumbnails = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and "_generate_thumbnail_background" in task.get_coro().__qualname__
    ]
    if thumbnails:
        await asyncio.wait_for(asyncio.gather(*thumbnails, return_exceptions=True), 60)


asyncio.run(main())
