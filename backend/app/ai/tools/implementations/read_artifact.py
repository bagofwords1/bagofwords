"""
read_artifact tool - Read an existing artifact's code and metadata.

Use this to load previous artifact code into context before modifying with create_artifact.
"""

from typing import AsyncIterator, Dict, Any, Type, List

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.read_artifact import ReadArtifactInput, ReadArtifactOutput
from app.models.artifact import Artifact
from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_OBSERVATION


class ReadArtifactTool(Tool):
    """Tool to read an existing artifact's code and metadata."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_artifact",
            description=(
                "Read an existing dashboard, slides, and artifact's code and metadata from the current report. "
                "Use this to load previous artifact code into context before modifying with edit_artifact (or create_artifact) or when the user wants to inspect or analyze an existing artifact. "
                "Use this also if the user is saying something is not working like filters or UI elements are not showing up - to check the existing code and visualizations for debugging. "
                "ALWAYS use this before editing an artifact (edit_artifact) to have a full view of the existing code, visualizations, and layout. "
                "If the user refers to a specific version of an artifact, ALWAYS load that version with this tool to have the correct code context for the edit. "
                "Pass load_screenshot=true to include the last rendered preview screenshot in the observation — use this when debugging visual issues or when you need to see the current state before deciding how to edit. "
                "IMPORTANT: The artifact_id is found in previous create_artifact results shown as 'artifact_id: <uuid>' in the conversation. "
                "Do NOT ask the user for URLs or artifact IDs - extract the artifact_id from the conversation context."
            ),
            category="research",  # Must be research/action/both to be discovered by registry
            version="1.0.0",
            input_schema=ReadArtifactInput.model_json_schema(),
            output_schema=ReadArtifactOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["artifact", "dashboard", "read"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadArtifactInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadArtifactOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadArtifactInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"artifact_id": data.artifact_id})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "reading_artifact"})

        # Get context
        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
        report = context_hub.report if context_hub else runtime_ctx.get("report")

        if not db or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadArtifactOutput(
                        artifact_id=data.artifact_id,
                        mode="page",
                        code="",
                    ).model_dump(),
                    "observation": {
                        "summary": "Failed to read artifact: missing context",
                        "error": {"type": "context_error", "message": "Missing db or organization"},
                    },
                },
            )
            return

        # Fetch the artifact
        try:
            result = await db.execute(
                select(Artifact).where(
                    Artifact.id == data.artifact_id,
                    Artifact.organization_id == str(organization.id),
                )
            )
            artifact = result.scalar_one_or_none()
        except Exception as e:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadArtifactOutput(
                        artifact_id=data.artifact_id,
                        mode="page",
                        code="",
                    ).model_dump(),
                    "observation": {
                        "summary": f"Failed to read artifact: {str(e)}",
                        "error": {"type": "db_error", "message": str(e)},
                    },
                },
            )
            return

        if not artifact:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadArtifactOutput(
                        artifact_id=data.artifact_id,
                        mode="page",
                        code="",
                    ).model_dump(),
                    "observation": {
                        "summary": f"Artifact not found: {data.artifact_id}",
                        "error": {"type": "not_found", "message": f"No artifact with id {data.artifact_id}"},
                    },
                },
            )
            return

        # Extract code from content
        content = artifact.content or {}
        code = content.get("code", "")

        # For slides mode, concatenate all slide codes
        if artifact.mode == "slides" and "slides" in content:
            slides = content.get("slides", [])
            code = "\n\n".join(
                f"// Slide {i+1}: {s.get('title', 'Untitled')}\n{s.get('code', '')}"
                for i, s in enumerate(slides)
            )

        # Extract visualization_ids if stored
        visualization_ids: List[str] = content.get("visualization_ids", [])

        # Build output
        output = ReadArtifactOutput(
            artifact_id=str(artifact.id),
            title=artifact.title,
            mode=artifact.mode,
            code=code,
            visualization_ids=visualization_ids,
            version=artifact.version,
        ).model_dump()

        # Add UI preview fields (similar to describe_tables top_tables)
        code_lines = code.count('\n') + 1 if code else 0
        output["artifact_preview"] = {
            "artifact_id": str(artifact.id),
            "title": artifact.title or "Untitled",
            "mode": artifact.mode,
            "version": artifact.version,
            "code_stats": {
                "chars": len(code),
                "lines": code_lines,
            },
            "visualization_ids": visualization_ids,
            "created_at": str(artifact.created_at) if artifact.created_at else None,
        }
        # Code for collapsible toggle (collapsed by default in UI)
        output["code_preview"] = {
            "language": "jsx",
            "code": code,
            "collapsed_default": True,
        }

        # Build observation with code for context
        summary = f"Read artifact '{artifact.title or 'Untitled'}' ({artifact.mode}, v{artifact.version}) - {len(code)} chars of code"

        observation = {
            "summary": summary,
            "artifact_id": str(artifact.id),
            "title": artifact.title,
            "mode": artifact.mode,
            "code": code,  # Available for 1 iteration; compacted by observation builder on next tool call
            "visualization_ids": visualization_ids,
            "version": artifact.version,
            "runtime_environment": SANDBOX_RUNTIME_OBSERVATION,
        }

        # Include stored screenshot if requested, gated by privacy and vision support
        if data.load_screenshot:
            # Check allow_llm_see_data privacy setting
            organization_settings = runtime_ctx.get("settings")
            allow_llm_see_data = True
            if organization_settings:
                try:
                    allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
                except Exception:
                    allow_llm_see_data = True

            # Check model supports vision
            model = runtime_ctx.get("model")
            supports_vision = model and getattr(model, "supports_vision", False)

            if allow_llm_see_data and supports_vision and artifact.screenshot_base64:
                observation["images"] = [{
                    "data": artifact.screenshot_base64,
                    "media_type": "image/png",
                    "source_type": "base64",
                }]
                observation["summary"] += " (screenshot included)"

            # Include render errors if stored (useful even without screenshot)
            if artifact.render_errors:
                observation["render_errors"] = artifact.render_errors

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )
