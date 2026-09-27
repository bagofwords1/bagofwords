"""suggest_personal_instruction — offer the user one of their own rules for how
to answer them, to save in their Custom instructions with one click.

Rules change behavior, so they are proposed, never saved silently: this tool
persists nothing. It validates the rule, checks whether the user's Custom
instructions already hold it, and the report renders a card with an "Add to
my instructions" button (POST /api/users/me/instructions/rules) that only the
user can use. Facts about the user go to memory instead (create_memory);
rules that hold for everyone go to org instructions.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.implementations._memory_common import fail, is_machine_turn, memory_session
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.memory import SuggestPersonalInstructionInput, SuggestPersonalInstructionOutput
from app.models.membership import Membership
from app.services.memory_rules import clean_text, contains_secret, note_has_rule

logger = logging.getLogger(__name__)

MAX_RULE_CHARS = 200


class SuggestPersonalInstructionTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="suggest_personal_instruction",
            description=(
                "Offer to save the user's own lasting rule for how you answer THEM (format, units, "
                "length, tone, what to show) to their Custom instructions. The user accepts with one "
                "click; nothing is saved otherwise. Use it when the user states or corrects such a "
                "preference and it sounds lasting, not for a one-off request. Call it at most once per "
                "rule, and apply the rule to your current answer either way. Not for facts about the "
                "user (create_memory) and not for business definitions or rules that hold for everyone "
                "(org instructions)."
            ),
            category="action",
            version="1.0.0",
            input_schema=SuggestPersonalInstructionInput.model_json_schema(),
            output_schema=SuggestPersonalInstructionOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=[],
            is_active=True,
            tags=["instructions", "personal"],
            allowed_modes=["chat"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SuggestPersonalInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SuggestPersonalInstructionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = SuggestPersonalInstructionInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Suggesting an instruction"})

        user = runtime_ctx.get("user")
        org = runtime_ctx.get("organization")
        if user is None or org is None or is_machine_turn(runtime_ctx.get("head_completion")):
            yield fail("Not suggested", "Only available when the user is in the conversation.", code="unavailable")
            return
        if runtime_ctx.get("mode") not in (None, "chat"):
            yield fail("Not suggested", "Only available in normal chat turns.", code="unavailable")
            return

        text = clean_text(data.text)
        if not text:
            yield fail("Not suggested", "The rule is empty.")
            return
        if len(text) > MAX_RULE_CHARS:
            yield fail("Not suggested", f"Keep the rule under {MAX_RULE_CHARS} characters — one short instruction.")
            return
        if contains_secret(text):
            yield fail("Not suggested", "That looks like a secret; never store secrets.", code="memory.sensitive")
            return

        try:
            async with memory_session(runtime_ctx) as db:
                row = await db.execute(
                    select(Membership.note).where(
                        Membership.user_id == str(user.id), Membership.organization_id == str(org.id)
                    )
                )
                note = row.scalar_one_or_none()
        except Exception as e:  # pragma: no cover
            logger.exception("suggest_personal_instruction failed")
            yield fail("Not suggested", f"Unexpected error: {e}", code="internal")
            return

        already = note_has_rule(note, text)
        output = {"success": True, "text": text, "already_saved": already}
        summary = (
            "The user's Custom instructions already contain this rule — just follow it."
            if already else
            "Offered the user a one-click option to save this to their Custom instructions. Apply it to "
            "your answer now; don't mention memory."
        )
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": {"summary": summary}})
