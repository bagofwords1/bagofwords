import base64
import logging
import os
from typing import Any, AsyncIterator, Dict, Type

import aiofiles
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolEndEvent
from app.ai.tools.schemas.read_image import ReadImageInput, ReadImageOutput
from app.ai.tools.implementations._file_tool_common import allow_llm_see_data
from app.models.file import File

logger = logging.getLogger(__name__)


class ReadImageTool(Tool):
    """Read an image file (by id) into the model's context for vision.

    Complements generate_image: the agent can look at an image it generated (or
    an uploaded image) — e.g. to describe or critique it — by passing the file_id.
    The image is attached to the next model turn as a vision input.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_image",
            description=(
                "Read an image file into context so you can see it. Pass the "
                "file_id of an image — e.g. the file_id returned by a previous "
                "generate_image call, or an uploaded image. The image is attached "
                "to your next turn for vision. Requires a vision-capable model."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadImageInput.model_json_schema(),
            output_schema=ReadImageOutput.model_json_schema(),
            tags=["image", "vision", "read"],
            idempotent=True,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadImageInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadImageOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadImageInput(**tool_input)
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        model = runtime_ctx.get("model")

        yield ToolStartEvent(type="tool.start", payload={"title": f"Reading image {data.file_id}"})

        def _fail(msg: str):
            return ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadImageOutput(success=False, file_id=data.file_id, error_message=msg).model_dump(),
                    "observation": {"summary": f"read_image: {msg}", "success": False},
                },
            )

        if db is None or organization is None:
            yield _fail("missing execution context")
            return

        if not (model and getattr(model, "supports_vision", False)):
            yield _fail("the current model can't view images — select a vision-capable model")
            return
        if not allow_llm_see_data(runtime_ctx):
            yield _fail("viewing data is disabled for this organization")
            return

        try:
            import uuid as _uuid
            _uuid.UUID(str(data.file_id))
        except (ValueError, AttributeError):
            yield _fail("invalid file id")
            return

        f = (await db.execute(
            select(File).where(File.id == str(data.file_id), File.organization_id == str(organization.id))
        )).scalars().first()
        if f is None:
            yield _fail("file not found")
            return

        ct = (f.content_type or "").lower()
        if "image" not in ct:
            yield _fail(f"file is not an image (content_type={f.content_type})")
            return

        try:
            disk_path = os.path.join(os.getcwd(), "uploads", "files", os.path.basename(f.path or ""))
            async with aiofiles.open(disk_path, "rb") as fh:
                raw = await fh.read()
        except Exception as e:
            logger.warning("read_image: could not read %s: %s", data.file_id, e)
            yield _fail("could not read image bytes")
            return

        block = {
            "data": base64.b64encode(raw).decode("utf-8"),
            "media_type": f.content_type or "image/png",
            "source_type": "base64",
        }
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": ReadImageOutput(
                    success=True, file_id=str(f.id), content_type=f.content_type, filename=f.filename
                ).model_dump(),
                "observation": {
                    "summary": f"Read image '{f.filename}' ({f.content_type}) into context for vision.",
                    "success": True,
                    "images": [block],
                },
            },
        )
