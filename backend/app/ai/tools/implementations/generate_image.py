import base64
import logging
import re
from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.generate_image import GenerateImageInput, GenerateImageOutput
from app.ai.llm import LLM
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.services.file_service import FileService
from app.dependencies import async_session_maker

logger = logging.getLogger(__name__)


def _slug(text: str, default: str = "generated-image") -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:40] or default)


class GenerateImageTool(Tool):
    """Generate an image from a text prompt and store it as a File.

    The stored file's id can be handed to create_artifact / edit_artifact to
    embed the image inside a dashboard (rendered by the <BowFile> sandbox global).
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="generate_image",
            description="""
Purpose:
Generate an image from a natural-language prompt using an image-generation
model (e.g. gpt-image-1) and store it as a file in the report. Returns a
`file_id` you can embed in a dashboard.

Use when:
    - The user asks for an illustration, diagram concept, logo, icon,
      background, or any generated picture
    - A dashboard/report would benefit from a generated visual asset

Flow:
    generate_image  ->  (returns file_id)  ->  create_artifact / edit_artifact
    referencing that file_id to place the image on the canvas (the <BowFile>
    component renders it).

Do not use when:
    - The user wants a data chart (use create_data — charts are rendered from
      data, not generated as images)
            """,
            category="action",
            version="1.0.0",
            input_schema=GenerateImageInput.model_json_schema(),
            output_schema=GenerateImageOutput.model_json_schema(),
            tags=["image", "generation", "media", "asset"],
            timeout_seconds=120,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GenerateImageInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GenerateImageOutput

    async def _find_image_model(self, db, organization_id: str) -> Optional[LLMModel]:
        """Pick an enabled image-generation model for the org (provider enabled)."""
        result = await db.execute(
            select(LLMModel)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .options(selectinload(LLMModel.provider))
            .filter(
                LLMModel.organization_id == organization_id,
                LLMModel.is_enabled == True,  # noqa: E712
                LLMModel.supports_image_generation == True,  # noqa: E712
                LLMProvider.is_enabled == True,  # noqa: E712
            )
        )
        return result.scalars().first()

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = GenerateImageInput(**tool_input)
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        report = runtime_ctx.get("report")
        report_id = str(report.id) if report else None

        yield ToolStartEvent(
            type="tool.start",
            payload={"title": f"Generating image: {data.title or data.prompt[:60]}"},
        )

        if db is None or organization is None or user is None:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": GenerateImageOutput(
                        success=False,
                        error_message="Missing execution context (db/org/user).",
                    ).model_dump(),
                    "observation": {"summary": "generate_image: missing context", "success": False},
                },
            )
            return

        model = await self._find_image_model(db, organization.id)
        if model is None:
            msg = (
                "No image-generation model is enabled for this organization. "
                "Enable an image model (e.g. gpt-image-1) in Settings → AI/LLMs."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": GenerateImageOutput(success=False, error_message=msg).model_dump(),
                    "observation": {"summary": "generate_image: no image model", "success": False},
                },
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating"})

        try:
            llm = LLM(model, usage_session_maker=async_session_maker)
            image = await llm.generate_image(
                data.prompt,
                size=data.size,
                quality=data.quality,
                usage_scope="generate_image",
                usage_scope_ref_id=report_id,
            )
        except Exception as e:
            logger.warning("generate_image failed: %s", e)
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": GenerateImageOutput(
                        success=False, error_message=f"Image generation failed: {e}"
                    ).model_dump(),
                    "observation": {"summary": f"generate_image failed: {e}", "success": False},
                },
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving"})

        try:
            image_bytes = base64.b64decode(image.data)
            filename = f"{_slug(data.title or data.prompt)}.png"
            db_file = await FileService().save_bytes_as_file(
                db=db,
                content=image_bytes,
                filename=filename,
                content_type=image.media_type or "image/png",
                current_user=user,
                organization=organization,
                report_id=report_id,
            )
        except Exception as e:
            logger.warning("generate_image: saving file failed: %s", e)
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": GenerateImageOutput(
                        success=False, error_message=f"Failed to store image: {e}"
                    ).model_dump(),
                    "observation": {"summary": f"generate_image: store failed: {e}", "success": False},
                },
            )
            return

        file_id = str(db_file.id)
        output = GenerateImageOutput(
            success=True,
            file_id=file_id,
            filename=db_file.filename,
            content_type=db_file.content_type,
            revised_prompt=image.revised_prompt,
        )
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {
                    "summary": (
                        f"Generated image '{db_file.filename}' (file_id={file_id}, "
                        f"{db_file.content_type}). Pass this file_id to create_artifact / "
                        "edit_artifact (file_ids) to embed it in a dashboard."
                    ),
                    "success": True,
                    "file_id": file_id,
                    "filename": db_file.filename,
                    "content_type": db_file.content_type,
                    "revised_prompt": image.revised_prompt,
                },
            },
        )
