"""Apply reviewed local exact edits through the mechanical artifact tool (no LLM)."""

import argparse, asyncio, json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--manifest", required=True)
p.add_argument("--app", required=True)
p.add_argument("--edits", required=True)
a = p.parse_args()


async def run():
    import main
    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.models.report import Report
    from app.models.user import User
    from app.models.organization import Organization
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool

    path = Path(a.manifest)
    manifest = json.loads(path.read_text())
    app = manifest[a.app]
    async with async_session_maker() as db:
        artifact = await db.get(Artifact, app["artifact_id"])
        report = await db.get(Report, app["report_id"])
        user = await db.get(User, report.user_id)
        org = await db.get(Organization, report.organization_id)
        async for event in EditArtifactTool().run_stream(
            {
                "artifact_id": str(artifact.id),
                "edits": json.loads(Path(a.edits).read_text()),
                "purpose": "requested_change",
            },
            {"db": db, "report": report, "user": user, "organization": org},
        ):
            if event.type == "tool.end":
                ob = event.payload.get("observation", {})
                output = event.payload.get("output", {})
                print(ob.get("summary"), ob.get("error"))
                if ob.get("error"):
                    raise RuntimeError(str(ob["error"]))
                app["artifact_id"] = (
                    output.get("artifact_id")
                    or ob.get("artifact_id")
                    or app["artifact_id"]
                )
                app["local_edits"] = app.get("local_edits", 0) + 1
    latest = json.loads(path.read_text())
    latest[a.app] = app
    path.write_text(json.dumps(latest, indent=2))


asyncio.run(run())
