"""Search Evals Tool — list/find existing eval test cases.

Used by the knowledge harness (and training mode) before ``create_eval``
to dedupe against prior cases or surface a related case the agent
should reference rather than re-author.
"""
from typing import AsyncIterator, Dict, Any, Type
import logging

from pydantic import BaseModel
from sqlalchemy import or_, cast, String as SAString, select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.search_evals import (
    SearchEvalsInput,
    SearchEvalsOutput,
    SearchEvalsItem,
)
from app.core.permission_resolver import resolve_permissions
from app.models.eval import TestCase, TestSuite

logger = logging.getLogger(__name__)


class SearchEvalsTool(Tool):
    """List or substring-search the organization's eval test cases."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_evals",
            description=(
                "RESEARCH: List or substring-search eval test cases in this "
                "organization. Use BEFORE create_eval to check for an "
                "existing case covering the same prompt — duplicates are "
                "noise. Returns case id, name, prompt, suite, status, and "
                "rule count. Substring is case-insensitive and matches "
                "against name and prompt content."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchEvalsInput.model_json_schema(),
            output_schema=SearchEvalsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=True,
            required_permissions=["manage_evals"],
            tags=["eval", "search"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {"query": "active users last 30", "limit": 5},
                    "description": "Substring search before drafting a new case",
                },
                {
                    "input": {"suite_id": "<suite-uuid>", "status": "draft", "limit": 20},
                    "description": "List all drafts in a specific suite",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchEvalsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchEvalsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SearchEvalsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "query": data.query,
                "suite_id": data.suite_id,
                "status": data.status,
                "limit": data.limit,
            },
        )

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
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not resolved.has_org_permission("manage_evals"):
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={"error": "Missing manage_evals permission", "code": "PERMISSION_DENIED"},
                )
                return

            stmt = (
                select(TestCase, TestSuite.name)
                .join(TestSuite, TestSuite.id == TestCase.suite_id)
                .where(TestSuite.organization_id == str(organization.id))
                .where(TestCase.deleted_at.is_(None))
            )

            if data.suite_id:
                stmt = stmt.where(TestCase.suite_id == str(data.suite_id))

            if data.status != "all":
                stmt = stmt.where(TestCase.status == data.status)

            if data.query and data.query.strip():
                like = f"%{data.query.strip()}%"
                # ILIKE on name is portable; cast prompt_json to text for a
                # coarse substring match (works on both SQLite and Postgres).
                stmt = stmt.where(
                    or_(
                        TestCase.name.ilike(like),
                        cast(TestCase.prompt_json, SAString).ilike(like),
                    )
                )

            stmt = stmt.order_by(TestCase.created_at.desc()).limit(data.limit)
            rows = (await db.execute(stmt)).all()

            items = []
            for case, suite_name in rows:
                prompt_content = ""
                pj = case.prompt_json or {}
                if isinstance(pj, dict):
                    prompt_content = (pj.get("content") or "")[:500]
                rules = (case.expectations_json or {}).get("rules") or []
                items.append(
                    SearchEvalsItem(
                        id=str(case.id),
                        name=case.name,
                        prompt_content=prompt_content,
                        suite_id=str(case.suite_id),
                        suite_name=str(suite_name or ""),
                        status=case.status,
                        auto_generated=bool(case.auto_generated),
                        rule_count=len(rules) if isinstance(rules, list) else 0,
                    )
                )

            output = SearchEvalsOutput(
                success=True,
                items=items,
                total=len(items),
                message=f"Found {len(items)} eval case(s)",
            )

            summary = (
                f"Found {len(items)} eval case(s) "
                f"for query='{data.query or ''}' status='{data.status}'"
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "eval_search_result",
                                "count": len(items),
                                "items": [
                                    {"id": i.id, "name": i.name, "suite_name": i.suite_name, "status": i.status}
                                    for i in items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"search_evals failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Search failed: {e}", "code": "SEARCH_FAILED"},
            )
