"""submit_list — persist typed records into an Agent List.

Never listed in the planner catalog directly. Each list is registered natively
as ``submit_<slug>`` (input schema = the list's compiled schema; see
``app.ai.tools.list_tool_registry``) and AgentV2 rewrites that call into
``submit_list`` with the list id, mirroring native MCP tools → execute_mcp.

The executor re-checks that the list belongs to an agent attached to this
report and that the acting user can view it, validates every record against
the full schema (atomic: invalid -> nothing written, path-qualified errors),
verifies evidence quotes against text read in this report, and upserts rows.
"""
import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.submit_list import SubmitListInput, SubmitListOutput

logger = logging.getLogger(__name__)

TOOL_NAME = "submit_list"


class SubmitListTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name=TOOL_NAME,
            description="Internal gateway for submit_<list> tools. Not called directly.",
            category="action",
            version="1.0.0",
            input_schema=SubmitListInput.model_json_schema(),
            output_schema=SubmitListOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=60,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["lists", "catalog_hidden"],
            digest_keys=["list_name", "inserted", "updated", "unchanged", "rows", "errors"],
        )

    @property
    def input_model(self) -> type[BaseModel]:
        return SubmitListInput

    @property
    def output_model(self) -> type[BaseModel]:
        return SubmitListOutput

    async def run_stream(self, tool_input: dict[str, Any], runtime_ctx: dict[str, Any]) -> AsyncIterator[ToolEvent]:
        from app.models.agent_list import AgentList
        from app.models.data_source import DataSource
        from app.models.report_data_source_association import report_data_source_association
        from app.services.agent_lists.access import can_submit_to_list
        from app.services.agent_lists.records import ListValidationError, apply_submission_with_retry
        from app.services.agent_lists.verify import collect_report_texts

        data = SubmitListInput(**tool_input)
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        session_maker = runtime_ctx.get("session_maker")
        report_id = str(report.id) if report is not None else None

        yield ToolStartEvent(type="tool.start", payload={"title": "Saving to list"})

        async def _session():
            if session_maker is not None:
                return session_maker()
            raise RuntimeError("no session")

        try:
            ctx = await _session()
        except Exception:
            ctx = None
        if ctx is None:
            yield self._fail(data.list_id, None, None, ["internal: no database session"])
            return

        async with ctx as db:
            lst = (await db.execute(
                select(AgentList).where(AgentList.id == data.list_id, AgentList.deleted_at.is_(None))
            )).scalar_one_or_none()
            if lst is None or organization is None or str(lst.organization_id) != str(organization.id):
                yield self._fail(data.list_id, None, None, ["list_id: list not found"])
                return
            ds = await db.get(DataSource, lst.data_source_id)
            attached = set()
            if report_id:
                attached = {str(r[0]) for r in (await db.execute(
                    select(report_data_source_association.c.data_source_id)
                    .where(report_data_source_association.c.report_id == report_id)
                )).all()}
            read_only_chat = getattr(report, "report_type", "regular") == "artifact_chat"
            if (read_only_chat or str(lst.data_source_id) not in attached
                    or not await can_submit_to_list(db, user, organization, ds, lst)):
                yield self._fail(lst.id, lst.name, str(lst.data_source_id),
                                 [f"list '{lst.name}' is not available in this conversation"])
                return

            sources = await collect_report_texts(db, report_id)
            try:
                result = await apply_submission_with_retry(
                    db, lst, data.records,
                    report_id=report_id,
                    tool_execution_id=runtime_ctx.get("tool_call_id"),
                    user_id=str(user.id) if user is not None else None,
                    sources=sources,
                )
            except ListValidationError as exc:
                yield self._fail(lst.id, lst.name, str(lst.data_source_id), exc.errors)
                return

        parts = []
        for label, n in (("inserted", result["inserted"]), ("updated", result["updated"]),
                         ("unchanged", result["unchanged"])):
            if n:
                parts.append(f"{n} {label}")
        summary = f"Saved to list '{lst.name}': " + (", ".join(parts) or "no changes") + "."
        if result["locked_fields_skipped"]:
            skipped = sorted({s["field"] for s in result["locked_fields_skipped"]})
            summary += (f" Skipped human-edited (locked) fields: {', '.join(skipped)} — "
                        "these keep the user's value; do not retry them.")
        if result["unverified_quotes"]:
            summary += (f" {len(result['unverified_quotes'])} quote(s) could not be matched to text you read; "
                        "prefer exact short quotes copied verbatim.")

        output = SubmitListOutput(
            success=True, list_id=str(lst.id), list_name=lst.name, data_source_id=str(lst.data_source_id),
            inserted=result["inserted"], updated=result["updated"], unchanged=result["unchanged"],
            rows=result["rows"],
        ).model_dump()
        observation = {
            "summary": summary,
            "success": True,
            "list_name": lst.name,
            "inserted": result["inserted"],
            "updated": result["updated"],
            "unchanged": result["unchanged"],
            "rows": result["rows"][:50],
            "locked_fields_skipped": result["locked_fields_skipped"][:50],
            "unverified_quotes": result["unverified_quotes"][:20],
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail(self, list_id, list_name, data_source_id, errors) -> ToolEndEvent:
        msg = "; ".join(errors[:12])
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": SubmitListOutput(
                    success=False, list_id=list_id, list_name=list_name, data_source_id=data_source_id,
                    errors=errors[:50],
                ).model_dump(),
                "observation": {
                    "summary": f"Nothing was saved. Fix these and call the tool again with ALL records: {msg}",
                    "success": False,
                    "error": {"type": "validation_error", "message": msg, "errors": errors[:50]},
                },
            },
        )
