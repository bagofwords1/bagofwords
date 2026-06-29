"""Create Prompt Tool - Save a reusable prompt for an agent (training mode).

A prompt is a saved, completion-shaped instruction users can re-run or that
surfaces as a conversation starter. This tool lets the training-mode agent
curate the reusable prompts attached to the agent(s) the user manages.

Authoring is governed by the agent-manager tier: PromptService.create_prompt
requires `manage` on each target data source (or org admin for 'global').
Created prompts go live immediately — there is no draft/approval build flow for
prompts (unlike instructions).
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from fastapi import HTTPException

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_prompt import CreatePromptInput, CreatePromptOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

VALID_SCOPES = {"agent", "private", "global"}
VALID_MODES = {"chat", "deep", "training"}


class CreatePromptTool(Tool):
    """Create a reusable prompt and attach it to the agent(s) being trained."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_prompt",
            description=(
                "ACTION: Save a reusable prompt for an agent. A prompt is a saved, "
                "re-runnable analytical request (optionally a conversation starter or a "
                "templated prompt with {{parameters}}). Use search_prompts FIRST to avoid "
                "duplicates. Defaults to scope='agent', attaching to the agent(s) on the "
                "current report — omit data_source_ids to use them. You must manage the "
                "target agent(s). Created prompts are live immediately."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreatePromptInput.model_json_schema(),
            output_schema=CreatePromptOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            tags=["training", "prompt", "curation"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {
                        "text": "Show monthly revenue by product category for the last 12 months.",
                        "title": "Monthly revenue by category",
                        "is_starter": True,
                    },
                    "description": "Agent-scoped conversation starter attached to the current report's agents.",
                },
                {
                    "input": {
                        "text": "Summarize {{metric}} trends for {{period}}.",
                        "title": "Metric trend summary",
                        "parameters": [
                            {"name": "metric", "type": "text", "required": True},
                            {"name": "period", "type": "date_range", "required": True},
                        ],
                    },
                    "description": "Templated prompt with two parameters.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreatePromptInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreatePromptOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = CreatePromptInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "title": data.title,
                "scope": data.scope,
                "is_starter": data.is_starter,
            },
        )

        if data.scope not in VALID_SCOPES:
            yield self._reject(f"Invalid scope '{data.scope}'. Must be one of: {', '.join(sorted(VALID_SCOPES))}", "invalid_scope")
            return
        if data.mode not in VALID_MODES:
            yield self._reject(f"Invalid mode '{data.mode}'. Must be one of: {', '.join(sorted(VALID_MODES))}", "invalid_mode")
            return

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        # Resolve target agents for agent-scoped prompts: explicit ids win, else
        # fall back to the active agents attached to the current report.
        ds_ids = list(data.data_source_ids or [])
        if data.scope == "agent" and not ds_ids and report is not None:
            try:
                ds_ids = [
                    str(ds.id)
                    for ds in (report.data_sources or [])
                    if getattr(ds, "is_active", False) and getattr(ds, "deleted_at", None) is None
                ]
            except Exception:
                ds_ids = []

        if data.scope == "agent" and not ds_ids:
            yield self._reject(
                "An agent-scoped prompt needs at least one agent. Pass data_source_ids or run on a report with agents attached.",
                "no_data_sources",
            )
            return

        try:
            from app.schemas.prompt_schema import PromptCreate, PromptParameter
            from app.services.prompt_service import prompt_service

            parameters = None
            if data.parameters:
                parameters = [PromptParameter(**p.model_dump()) for p in data.parameters]

            payload = PromptCreate(
                title=data.title,
                text=data.text,
                mode=data.mode,
                scope=data.scope,
                is_starter=data.is_starter,
                parameters=parameters,
                data_source_ids=ds_ids if data.scope == "agent" else [],
            )

            prompt = await prompt_service.create_prompt(db, payload, user, organization)

            title = prompt.title or (prompt.text or "")[:60]
            attached = [str(ds.id) for ds in (prompt.data_sources or [])]
            logger.info(
                f"Training mode created prompt {prompt.id}: '{title}' "
                f"(scope={prompt.scope}, starter={prompt.is_starter}, agents={attached})"
            )

            output = CreatePromptOutput(
                success=True,
                prompt_id=str(prompt.id),
                title=title,
                scope=prompt.scope,
                data_source_ids=attached,
                is_starter=prompt.is_starter,
                message=f"Prompt created: {title}",
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": (
                            f"Created prompt '{title}' (scope={prompt.scope}, "
                            f"starter={prompt.is_starter}, agents={len(attached)})"
                        ),
                        "artifacts": [
                            {
                                "type": "prompt",
                                "id": str(prompt.id),
                                "title": title,
                                "scope": prompt.scope,
                                "is_starter": prompt.is_starter,
                                "data_source_ids": attached,
                            }
                        ],
                    },
                },
            )
        except HTTPException as he:
            reason = "permission_denied" if he.status_code == 403 else "rejected"
            yield self._reject(str(he.detail), reason)
        except Exception as e:
            logger.exception(f"create_prompt failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to create prompt: {e}", "code": "CREATE_FAILED"},
            )

    @staticmethod
    def _reject(message: str, reason: str) -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": CreatePromptOutput(
                    success=False, message=message, rejected_reason=reason
                ).model_dump(),
                "observation": {"summary": f"Prompt rejected: {message}", "artifacts": []},
            },
        )
