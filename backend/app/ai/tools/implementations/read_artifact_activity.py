"""read_artifact_activity: what happened in this conversation's apps while the
user (or the agent's own browser preview) used them -- requests and how they
ended, filters, errors, a click breadcrumb. Reads services/artifact_activity.py.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.read_artifact_activity import ReadArtifactActivityInput, ReadArtifactActivityOutput


async def _artifact_id(db, report_id: str, ref: str | None) -> str | None:
    """The agent knows version ids (create_artifact results); events are kept
    per artifact. Accept either, within this conversation."""
    if not ref:
        return None
    from app.models.artifact import Artifact, ArtifactVersion

    version = (await db.execute(select(ArtifactVersion.artifact_id).where(
        ArtifactVersion.id == ref, ArtifactVersion.report_id == report_id))).scalar_one_or_none()
    if version:
        return str(version)
    artifact = await db.get(Artifact, ref)
    return str(artifact.id) if artifact is not None else ref


async def _event_id(db, report_id: str, ref: str | None) -> str | None:
    if not ref or len(ref) >= 36:
        return ref
    from app.models.artifact_activity import ArtifactActivityEvent

    return (await db.execute(select(ArtifactActivityEvent.id).where(
        ArtifactActivityEvent.report_id == report_id, ArtifactActivityEvent.id.like(f"{ref}%")).limit(1))).scalar_one_or_none()


class ReadArtifactActivityTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_artifact_activity",
            description=(
                "Read what happened in this conversation's apps while the user (source=editor) or your own browser "
                "preview (source=preview) used them: every bow.* request with its status and error code, how answer "
                "streams ended, filter changes, render/console errors, and a click breadcrumb (never typed values). "
                "Use it FIRST when the user says the app doesn't work, shows an error, or 'does nothing', before "
                "reading or editing code; failures not yet shown to you also appear automatically in <app_activity>. "
                "Each line names the app version it ran on (vN): a failure on an older version may already be fixed. "
                "Pass event_id for one event's detail (message, stack). Kept for 7 days."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadArtifactActivityInput.model_json_schema(),
            output_schema=ReadArtifactActivityOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=20,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["artifact", "dashboard", "read", "debug"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "training"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadArtifactActivityInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadArtifactActivityOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        from app.services import artifact_activity

        data = ReadArtifactActivityInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"artifact_id": data.artifact_id})
        hub = runtime_ctx.get("context_hub")
        db = hub.db if hub else runtime_ctx.get("db")
        report = hub.report if hub else runtime_ctx.get("report")
        if db is None or report is None:
            out = ReadArtifactActivityOutput(error="No conversation in context")
            yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(), "observation": {"summary": out.error}})
            return
        report_id = str(report.id)
        lock = getattr(hub, "_db_lock", None)
        if lock is not None:
            async with lock:
                result = await self._read(db, report_id, data, artifact_activity)
        else:
            result = await self._read(db, report_id, data, artifact_activity)
        out = ReadArtifactActivityOutput(**result)
        if out.event:
            summary = f"Activity event: {out.event.get('line')}"
        elif out.events:
            summary = f"{len(out.events)} app activity events, {out.failures} failed"
        else:
            summary = out.error or "No app activity recorded yet (it is recorded while the user uses the app in this conversation)"
        observation = {"summary": summary, **{k: v for k, v in out.model_dump().items() if v not in (None, [], 0)}}
        yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(), "observation": observation})

    @staticmethod
    async def _read(db, report_id, data, service):
        return await service.read(
            db, report_id=report_id, artifact_id=await _artifact_id(db, report_id, data.artifact_id),
            since_minutes=data.since_minutes, kinds=data.kinds, only_errors=data.only_errors,
            source=data.source, event_id=await _event_id(db, report_id, data.event_id), limit=data.limit)
