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
from app.core.eval_scope import (
    eval_agent_scope, holds_any_eval_authority, can_view_case, can_edit_case,
    is_relevant_to_session,
)
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
                "noise. Returns case id, name, prompt, suite, status, rule "
                "types, a judge-rubric excerpt, tags, and agent scoping. "
                "Substring is case-insensitive and matches name, prompt, "
                "expectations (incl. judge rubrics), and tags. Filter to one "
                "agent's cases with data_source_id."
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

        try:
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            # Admission only: org-level OR a grant on at least one agent, the
            # same test the routes apply. Testing has_org_permission alone
            # denied every per-agent eval manager — while the tool catalog,
            # which does resolve per-agent grants, still offered them the tool.
            # An agent owner in training mode was handed a tool that could only
            # fail. What they may then see is decided per case below.
            _unscoped, _agent_ids = await eval_agent_scope(
                db, str(user.id), str(organization.id)
            )
            if not holds_any_eval_authority(_unscoped, _agent_ids):
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

            if data.data_source_id:
                # Portable containment match on the JSON id list; the ids are
                # UUID strings so a quoted-substring match has no false hits.
                stmt = stmt.where(
                    cast(TestCase.data_source_ids_json, SAString).ilike(f'%"{str(data.data_source_id)}"%')
                )

            if data.query and data.query.strip():
                like = f"%{data.query.strip()}%"
                # ILIKE on name is portable; cast the JSON columns to text for
                # a coarse substring match (works on both SQLite and Postgres).
                # expectations_json covers judge rubrics; tags_json covers tags.
                stmt = stmt.where(
                    or_(
                        TestCase.name.ilike(like),
                        cast(TestCase.prompt_json, SAString).ilike(like),
                        cast(TestCase.expectations_json, SAString).ilike(like),
                        cast(TestCase.tags_json, SAString).ilike(like),
                    )
                )

            _session_ids = set()
            if report is not None:
                try:
                    _session_ids = {str(ds.id) for ds in (report.data_sources or [])}
                except Exception:
                    _session_ids = set()

            stmt = stmt.order_by(TestCase.created_at.desc()).limit(data.limit)
            rows = (await db.execute(stmt)).all()

            items = []
            for case, suite_name in rows:
                # Two scopings compose. AUTHORITY: union over the agents a case
                # targets — authority over any one of them, plus org-wide cases,
                # which run against your agent too.
                if not can_view_case(case, _unscoped, _agent_ids):
                    continue
                # SESSION: what is relevant HERE. A session pinned to one agent
                # searched every agent the caller could reach, which is noise and
                # reads as a leak even when authorized. Globals stay visible —
                # they apply to this agent as well, matching how the standing
                # <instructions> block keeps global rules at any scope. An
                # unpinned (Auto) session imposes no bound; authority still does.
                if not is_relevant_to_session(case, _session_ids):
                    continue
                prompt_content = ""
                pj = case.prompt_json or {}
                if isinstance(pj, dict):
                    prompt_content = (pj.get("content") or "")[:500]
                rules = (case.expectations_json or {}).get("rules") or []
                if not isinstance(rules, list):
                    rules = []
                rule_types: list[str] = []
                judge_excerpt = None
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    rt = rule.get("type") or ""
                    if rt and rt not in rule_types:
                        rule_types.append(rt)
                    if rt == "judge" and judge_excerpt is None:
                        judge_excerpt = (rule.get("prompt") or "")[:200] or None
                items.append(
                    SearchEvalsItem(
                        id=str(case.id),
                        name=case.name,
                        prompt_content=prompt_content,
                        suite_id=str(case.suite_id),
                        suite_name=str(suite_name or ""),
                        status=case.status,
                        auto_generated=bool(case.auto_generated),
                        rule_count=len(rules),
                        rule_types=rule_types,
                        judge_excerpt=judge_excerpt,
                        tags=[str(t) for t in (case.tags_json or [])],
                        data_source_ids=[str(d) for d in (case.data_source_ids_json or [])],
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
