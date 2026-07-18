"""update_user_memory tool — persist durable per-user memory (full rewrite).

The agent's long-term memory about the CURRENT user, scoped to (user, org) on
the Membership row. Unlike Notes (per-report scratchpad) this survives across
reports and sessions, and unlike org instructions it is private to the user and
needs no approval. The agent submits the entire new document each call — the
current memory is shown to it in the <user_memory> block, so a rewrite is how
it prunes to stay under the cap.

Available in chat/deep only (allowed_modes), never in training runs — training
shapes org-wide instructions, not one user's personal memory.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.update_user_memory import (
    UpdateUserMemoryInput,
    UpdateUserMemoryOutput,
)
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolProgressEvent,
    ToolStartEvent,
)
from app.models.membership import Membership
from app.schemas.organization_schema import MEMBERSHIP_MEMORY_MAX_LENGTH

logger = logging.getLogger(__name__)


class UpdateUserMemoryTool(Tool):
    """Rewrite the current user's durable per-org memory document."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="update_user_memory",
            description=(
                "Save durable MEMORY about the current user — a small, curated document that is "
                "shown back to you (in <user_memory>) on every future conversation with this user. "
                "Use it to remember things worth carrying across sessions: their preferences and "
                "writing/formatting style, recurring goals, and analyses they liked (reference the "
                "report by title — don't paste data). Submit the COMPLETE new document each time "
                f"(full rewrite, max {MEMBERSHIP_MEMORY_MAX_LENGTH} chars); to add a line when near "
                "the limit, prune a stale one. This is YOUR memory of the user, NOT org-wide "
                "instructions (use create_instruction for knowledge the whole team should share) "
                "and NOT this report's working notes (use create_note/edit_note). Only save when "
                "the user states a lasting preference or explicitly asks you to remember — don't "
                "record one-off task details."
            ),
            category="action",
            version="1.0.0",
            input_schema=UpdateUserMemoryInput.model_json_schema(),
            output_schema=UpdateUserMemoryOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["memory"],
            # Personal user memory only makes sense in interactive analysis
            # (chat/deep). Training mode shapes org instructions, not memory.
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return UpdateUserMemoryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return UpdateUserMemoryOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = UpdateUserMemoryInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Updating memory"})

        # Normalize: an empty/whitespace document clears the memory.
        new_content = (data.content or "").strip()
        if len(new_content) > MEMBERSHIP_MEMORY_MAX_LENGTH:
            yield self._fail(
                f"Memory is too long ({len(new_content)} chars; max {MEMBERSHIP_MEMORY_MAX_LENGTH}). "
                "Prune older or less important lines and try again."
            )
            return

        db = runtime_ctx.get("db")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        if not all([db, user, organization]):
            yield self._fail("Missing required context (db, user, organization).")
            return

        result = await db.execute(
            select(Membership).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization.id),
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            yield self._fail("No membership found for this user in the organization.")
            return

        previous = membership.memory or ""
        membership.memory = new_content or None
        await db.commit()

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "memory_updated", "char_count": len(new_content), "timing": False},
        )

        cleared = not new_content
        summary = (
            "Cleared user memory."
            if cleared
            else f"Updated user memory ({len(previous)} → {len(new_content)} chars)."
        )
        output = {"success": True, "char_count": len(new_content), "error": None}
        observation = {
            "summary": summary,
            "char_count": len(new_content),
            "content_snapshot": new_content[:2000],
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail(self, message: str) -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"success": False, "char_count": 0, "error": message},
                "observation": {
                    "summary": f"Failed to update user memory: {message}",
                    "error": {"type": "validation_error", "message": message},
                },
            },
        )
