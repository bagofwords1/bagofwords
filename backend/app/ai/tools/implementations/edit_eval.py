"""Edit Eval Tool — partial update of an existing TestCase.

Training-mode only. The headline use-case is promoting a knowledge-harness
draft to ``active`` after review; also renames, re-tags, moves between
suites, archives, and expectation rewrites. Knowledge mode stays
append-only by design (the harness must never mutate or promote cases).

Authorization mirrors ``create_eval``: org-level ``manage_evals``, or a
per-data-source ``manage_evals`` grant on EVERY agent the case is scoped
to. A global (unscoped) case requires the org-level permission.
"""
from typing import Any, AsyncIterator, Dict, Type
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
from app.ai.tools.schemas.edit_eval import EditEvalInput, EditEvalOutput
from app.core.permission_resolver import resolve_permissions
from app.models.eval import TEST_CASE_STATUSES, TestCase, TestSuite

logger = logging.getLogger(__name__)


class EditEvalTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_eval",
            description=(
                "ACTION: Edit an existing eval case — promote a draft to "
                "active, archive, rename, re-tag, move to another suite, or "
                "replace the prompt/expectations. Partial update: only the "
                "fields you pass change. Changing a prompt's meaning "
                "invalidates run-history comparisons — prefer create_eval for "
                "a genuinely new scenario. Find cases with search_evals."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditEvalInput.model_json_schema(),
            output_schema=EditEvalOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=False,
            required_permissions=["manage_evals"],
            tags=["eval", "edit"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"case_id": "<case-uuid>", "status": "active"},
                    "description": "Promote a reviewed draft into suite runs",
                },
                {
                    "input": {"case_id": "<case-uuid>", "tags": ["smoke", "revenue"]},
                    "description": "Re-tag for grouping",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditEvalInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditEvalOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = EditEvalInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(type="tool.start", payload={"case_id": data.case_id})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": "Missing required runtime context (db, organization, user)", "code": "MISSING_CONTEXT"},
            )
            return

        try:
            case = (
                await db.execute(
                    select(TestCase)
                    .where(TestCase.id == str(data.case_id))
                    .where(TestCase.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            suite = None
            if case is not None:
                suite = (
                    await db.execute(
                        select(TestSuite)
                        .where(TestSuite.id == str(case.suite_id))
                        .where(TestSuite.organization_id == str(organization.id))
                        .where(TestSuite.deleted_at.is_(None))
                    )
                ).scalar_one_or_none()
            if case is None or suite is None:
                output = EditEvalOutput(
                    success=False,
                    rejected_reason="case_not_found",
                    message=f"Case {data.case_id} not found in this organization.",
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {"summary": output.message, "artifacts": []},
                    },
                )
                return

            # Authorization: org manage_evals, or per-DS manage_evals on every
            # agent the case is scoped to (unscoped/global → org-level only).
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            ds_ids = [str(x) for x in (case.data_source_ids_json or [])]
            if resolved.has_org_permission("manage_evals"):
                authorized = True
            elif ds_ids:
                authorized = all(
                    resolved.has_resource_permission("data_source", ds, "manage_evals")
                    for ds in ds_ids
                )
            else:
                authorized = False
            if not authorized:
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={"error": "Missing manage_evals permission", "code": "PERMISSION_DENIED"},
                )
                return

            changed: list[str] = []

            if data.name and data.name.strip() != case.name:
                case.name = data.name.strip()
                changed.append("name")

            if data.status and data.status != case.status:
                if data.status not in TEST_CASE_STATUSES:
                    raise ValueError(f"Invalid status: {data.status}")
                case.status = data.status
                changed.append("status")

            if data.prompt is not None:
                case.prompt_json = {
                    "content": data.prompt.content,
                    "mode": data.prompt.mode,
                    "model_id": data.prompt.model_id,
                }
                changed.append("prompt")

            if data.expectations is not None:
                case.expectations_json = data.expectations.model_dump()
                changed.append("expectations")

            if data.tags is not None:
                case.tags_json = list(data.tags) or None
                changed.append("tags")

            target_suite = suite
            if data.suite_id and str(data.suite_id) != str(case.suite_id):
                target_suite = (
                    await db.execute(
                        select(TestSuite)
                        .where(TestSuite.id == str(data.suite_id))
                        .where(TestSuite.organization_id == str(organization.id))
                        .where(TestSuite.deleted_at.is_(None))
                    )
                ).scalar_one_or_none()
                if target_suite is None:
                    output = EditEvalOutput(
                        success=False,
                        rejected_reason="suite_not_found",
                        message=f"Suite {data.suite_id} not found in this organization.",
                    )
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": output.model_dump(),
                            "observation": {"summary": output.message, "artifacts": []},
                        },
                    )
                    return
                case.suite_id = str(target_suite.id)
                changed.append("suite")

            if changed:
                db.add(case)
                await db.commit()
                await db.refresh(case)

            output = EditEvalOutput(
                success=True,
                case_id=str(case.id),
                name=case.name,
                status=case.status,
                suite_id=str(case.suite_id),
                suite_name=target_suite.name if target_suite is not None else None,
                changed_fields=changed,
                message=(
                    f"Updated eval '{case.name}': changed {', '.join(changed)}."
                    if changed else f"Eval '{case.name}' already matched — nothing changed."
                ),
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": output.message,
                        "artifacts": [
                            {
                                "type": "eval_case",
                                "id": str(case.id),
                                "name": case.name,
                                "status": case.status,
                                "changed_fields": changed,
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"edit_eval failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Edit failed: {e}", "code": "EDIT_FAILED"},
            )
