"""Edit Prompt Tool - Update an existing reusable prompt (training mode).

Mirrors create_prompt's authorization: PromptService.update_prompt requires the
caller to own the prompt, hold `manage` on all its agents, or be org admin.
Edits are live immediately.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from fastapi import HTTPException

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.edit_prompt import EditPromptInput, EditPromptOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

VALID_SCOPES = {"agent", "private", "global"}
VALID_MODES = {"chat", "deep", "training"}


class EditPromptTool(Tool):
    """Edit an existing reusable prompt."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_prompt",
            description=(
                "ACTION: Update an existing reusable prompt — refine its text/title, change "
                "the agents it's attached to (data_source_ids), toggle conversation-starter, "
                "or adjust mode/parameters. Only the fields you set are changed. Find the "
                "prompt_id via search_prompts. You must manage the prompt (own it, manage its "
                "agents, or be org admin)."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditPromptInput.model_json_schema(),
            output_schema=EditPromptOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            tags=["training", "prompt", "curation"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"prompt_id": "abc123", "title": "Monthly revenue by category (v2)"},
                    "description": "Rename a prompt.",
                },
                {
                    "input": {"prompt_id": "abc123", "is_starter": True, "data_source_ids": ["ds-1", "ds-2"]},
                    "description": "Make it a starter and re-attach it to two agents.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditPromptInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditPromptOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = EditPromptInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"prompt_id": data.prompt_id},
        )

        if data.scope is not None and data.scope not in VALID_SCOPES:
            yield self._reject(data.prompt_id, f"Invalid scope '{data.scope}'.", "invalid_scope")
            return
        if data.mode is not None and data.mode not in VALID_MODES:
            yield self._reject(data.prompt_id, f"Invalid mode '{data.mode}'.", "invalid_mode")
            return

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            from app.schemas.prompt_schema import PromptUpdate, PromptParameter
            from app.services.prompt_service import prompt_service

            fields: Dict[str, Any] = {}
            for key in ("text", "title", "scope", "mode", "is_starter", "data_source_ids"):
                val = getattr(data, key)
                if val is not None:
                    fields[key] = val
            if data.parameters is not None:
                fields["parameters"] = [PromptParameter(**p.model_dump()) for p in data.parameters]

            if not fields:
                yield self._reject(data.prompt_id, "No fields to update.", "no_changes")
                return

            payload = PromptUpdate(**fields)
            prompt = await prompt_service.update_prompt(
                db, data.prompt_id, payload, user, organization
            )

            title = prompt.title or (prompt.text or "")[:60]
            logger.info(f"Training mode edited prompt {prompt.id}: '{title}'")

            output = EditPromptOutput(
                success=True,
                prompt_id=str(prompt.id),
                title=title,
                message=f"Prompt updated: {title}",
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": f"Edited prompt '{title}' (fields: {', '.join(sorted(fields))})",
                        "artifacts": [
                            {
                                "type": "prompt",
                                "id": str(prompt.id),
                                "title": title,
                                "scope": prompt.scope,
                                "is_starter": prompt.is_starter,
                            }
                        ],
                    },
                },
            )
        except HTTPException as he:
            if he.status_code == 404:
                reason = "not_found"
            elif he.status_code == 403:
                reason = "permission_denied"
            else:
                reason = "rejected"
            yield self._reject(data.prompt_id, str(he.detail), reason)
        except Exception as e:
            logger.exception(f"edit_prompt failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to edit prompt: {e}", "code": "EDIT_FAILED"},
            )

    @staticmethod
    def _reject(prompt_id: str, message: str, reason: str) -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": EditPromptOutput(
                    success=False, prompt_id=prompt_id, message=message, rejected_reason=reason
                ).model_dump(),
                "observation": {"summary": f"Prompt edit rejected: {message}", "artifacts": []},
            },
        )
