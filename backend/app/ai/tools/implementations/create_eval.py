"""Create Eval Tool — author a TestCase from inside the agent loop.

In knowledge mode (the post-completion harness) this tool:
- Forces ``status='draft'`` (drafts are inert until promoted).
- Forces the suite to the per-org default drafts suite (lazy-create).
- Sets ``auto_generated=True``.
- Populates provenance FKs from ``runtime_ctx`` (head_completion,
  agent_execution_id) so manage-evals reviewers can trace the case
  back to the conversation that produced it.

In training mode the agent author is in the loop, so we respect the
``status`` / ``suite_id`` arguments. ``suite_id`` falls back to the
default drafts suite when omitted.
"""
from typing import AsyncIterator, Any, Dict, Type
import logging

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.create_eval import (
    CreateEvalInput,
    CreateEvalOutput,
)
from app.core.permission_resolver import resolve_permissions
from app.models.completion_feedback import CompletionFeedback
from app.models.eval import (
    TEST_CASE_STATUS_ACTIVE,
    TEST_CASE_STATUS_DRAFT,
    TestCase,
    TestSuite,
)

logger = logging.getLogger(__name__)


class CreateEvalTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_eval",
            description=(
                "ACTION: Create an eval test case. Call AFTER ``search_evals`` "
                "has confirmed there's no near-duplicate. Use ``tool.calls`` "
                "rules for expected tool-call set membership and ``judge`` "
                "rules for LLM-as-judge rubrics. Do NOT assert on raw SQL/data "
                "(``field`` rules) — they're brittle across schema drift. "
                "In knowledge mode the case is always created as a draft in "
                "the org's default drafts suite, regardless of inputs."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateEvalInput.model_json_schema(),
            output_schema=CreateEvalOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=False,
            required_permissions=["manage_evals"],
            tags=["eval", "create", "knowledge"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {
                        "name": "Active users last 30 days",
                        "prompt": {"content": "How many active users last 30 days?"},
                        "expectations": {
                            "spec_version": 1,
                            "rules": [
                                {"type": "tool.calls", "tool": "create_data", "min_calls": 1},
                                {
                                    "type": "judge",
                                    "prompt": (
                                        "The answer is a single count (one number), scoped to "
                                        "users with at least one session event in the last 30 "
                                        "days based on session.created_at. 'Active' = has a "
                                        "session event; a login row alone does not count. "
                                        "Reject if the answer returns a list of users instead "
                                        "of a count. Reject if it includes users whose only "
                                        "activity in the window was a login event."
                                    ),
                                },
                            ],
                            "order_mode": "flexible",
                        },
                    },
                    "description": (
                        "Anatomy: entity/shape (single count), filter ('session event in last "
                        "30 days'), definition ('active' resolved to session events, not "
                        "logins), 2 negative criteria (wrong shape, wrong inclusion)."
                    ),
                },
                {
                    "input": {
                        "name": "My open opportunities",
                        "prompt": {"content": "show me list of my opps"},
                        "expectations": {
                            "spec_version": 1,
                            "rules": [
                                {"type": "tool.calls", "tool": "create_data", "min_calls": 1},
                                {
                                    "type": "judge",
                                    "prompt": (
                                        "The answer is a list of opportunities (one row per "
                                        "opp), scoped to opps where owner_id = the requesting "
                                        "user and stage is open — Closed-Won and Closed-Lost "
                                        "are excluded. 'My opps' means owned, not on the same "
                                        "account team. Reject if it counts opps instead of "
                                        "listing them. Reject if it includes any closed stages."
                                    ),
                                },
                            ],
                            "order_mode": "flexible",
                        },
                    },
                    "description": (
                        "Anatomy: entity/shape (list of opps, one row each), filter (owner = "
                        "user, open stages only), definition ('my' = owned), 2 negative "
                        "criteria (wrong shape, wrong stage inclusion)."
                    ),
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateEvalInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateEvalOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = CreateEvalInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "name": data.name,
                "suite_id": data.suite_id,
                "status": data.status,
                "rule_count": len(data.expectations.rules or []),
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        mode = runtime_ctx.get("mode") or ""
        head_completion = runtime_ctx.get("head_completion")
        agent_execution_id = runtime_ctx.get("agent_execution_id")

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
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not resolved.has_org_permission("manage_evals"):
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={"error": "Missing manage_evals permission", "code": "PERMISSION_DENIED"},
                )
                return

            from app.services.test_case_service import TestCaseService

            case_service = TestCaseService()
            is_knowledge = mode == "knowledge"

            # --- Resolve target suite ---
            suite: TestSuite
            if is_knowledge:
                suite = await case_service.get_or_create_drafts_suite(
                    db, str(organization.id),
                )
            elif data.suite_id:
                stmt = (
                    select(TestSuite)
                    .where(TestSuite.id == str(data.suite_id))
                    .where(TestSuite.organization_id == str(organization.id))
                    .where(TestSuite.deleted_at.is_(None))
                )
                suite = (await db.execute(stmt)).scalar_one_or_none()
                if not suite:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": CreateEvalOutput(
                                success=False,
                                rejected_reason="suite_not_found",
                                message=f"Suite {data.suite_id} not found in this organization.",
                            ).model_dump(),
                            "observation": {
                                "summary": f"create_eval rejected: suite {data.suite_id} not found",
                                "artifacts": [],
                            },
                        },
                    )
                    return
            else:
                suite = await case_service.get_or_create_drafts_suite(
                    db, str(organization.id),
                )

            # --- Resolve final status / auto_generated / provenance ---
            if is_knowledge:
                final_status = TEST_CASE_STATUS_DRAFT
                auto_generated = True
            else:
                # Training (or any other mode): default to active when not given.
                final_status = data.status or TEST_CASE_STATUS_ACTIVE
                auto_generated = False

            source_completion_id = (
                str(head_completion.id) if head_completion is not None else None
            )
            source_agent_execution_id = (
                str(agent_execution_id) if agent_execution_id else None
            )
            source_feedback_id = None
            # For knowledge mode the harness was triggered by a feedback row;
            # link the most recent positive feedback for this completion if
            # one exists. Best-effort — failure to find it isn't fatal.
            if is_knowledge and head_completion is not None:
                try:
                    fb_stmt = (
                        select(CompletionFeedback)
                        .where(CompletionFeedback.completion_id == str(head_completion.id))
                        .where(CompletionFeedback.user_id == str(user.id))
                        .where(CompletionFeedback.organization_id == str(organization.id))
                        .where(CompletionFeedback.direction == 1)
                        .order_by(CompletionFeedback.updated_at.desc())
                        .limit(1)
                    )
                    fb = (await db.execute(fb_stmt)).scalar_one_or_none()
                    if fb is not None:
                        source_feedback_id = str(fb.id)
                except Exception:
                    source_feedback_id = None

            # --- Build the prompt + expectations payloads ---
            prompt_json: Dict[str, Any] = {
                "content": data.prompt.content,
                "mode": data.prompt.mode,
                "model_id": data.prompt.model_id,
            }
            try:
                expectations_dict = data.expectations.model_dump()
            except Exception:
                expectations_dict = dict(data.expectations or {})

            # --- Persist the case ---
            case = TestCase(
                suite_id=str(suite.id),
                name=data.name.strip(),
                prompt_json=prompt_json,
                expectations_json=expectations_dict or {},
                data_source_ids_json=list(data.data_source_ids or []),
                tags_json=list(data.tags or []) or None,
                status=final_status,
                auto_generated=auto_generated,
                source_completion_id=source_completion_id,
                source_agent_execution_id=source_agent_execution_id,
                source_feedback_id=source_feedback_id,
            )
            db.add(case)
            await db.commit()
            await db.refresh(case)

            output = CreateEvalOutput(
                success=True,
                case_id=str(case.id),
                name=case.name,
                suite_id=str(case.suite_id),
                suite_name=suite.name,
                status=case.status,
                auto_generated=bool(case.auto_generated),
                message=(
                    f"Eval case created in suite '{suite.name}' "
                    f"(status={case.status}, auto={case.auto_generated})"
                ),
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": (
                            f"Created eval '{case.name}' in '{suite.name}' "
                            f"(status={case.status})"
                        ),
                        "artifacts": [
                            {
                                "type": "eval_case",
                                "id": str(case.id),
                                "name": case.name,
                                "suite_name": suite.name,
                                "status": case.status,
                                "auto_generated": bool(case.auto_generated),
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"create_eval failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to create eval: {e}", "code": "CREATE_FAILED"},
            )
