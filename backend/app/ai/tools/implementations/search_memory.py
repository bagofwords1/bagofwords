"""search_memory — look up memory entries not already injected this turn."""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.implementations._memory_common import BOUNDARY, fail, gate, memory_session
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.memory import SearchMemoryInput, SearchMemoryOutput
from app.models.report import Report
from app.services.memory_rules import MemoryValidationError
from app.services.memory_service import memory_service

logger = logging.getLogger(__name__)


def _iso(dt):
    return dt.date().isoformat() if dt is not None else None


class SearchMemoryTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_memory",
            description=(
                "Search the current user's memory for entries NOT already shown in <memory> — use it when "
                "they refer to something personal the injected memory doesn't cover (\"like last time\", "
                "\"my usual format for…\", a shorthand you don't recognize), or before creating an entry "
                "when the index says related entries are hidden. Returns full entries with handles. Matching is "
                "by words, aliases and tags — if a query finds nothing that fits, search again without a "
                "query, by section (e.g. section='preferences' or 'style') or by a tag from the <memory> index. "
                + BOUNDARY
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchMemoryInput.model_json_schema(),
            output_schema=SearchMemoryOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=[],
            is_active=True,
            tags=["memory"],
            allowed_modes=["chat"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchMemoryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchMemoryOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = SearchMemoryInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Checking memory"})

        blocked = gate(runtime_ctx)
        if blocked:
            yield fail("Memory search failed", blocked, code="unavailable")
            return

        org = runtime_ctx["organization"]
        user = runtime_ctx["user"]
        injected = [str(x) for x in (runtime_ctx.get("memory_injected_ids") or [])]
        try:
            async with memory_session(runtime_ctx) as db:
                entries = await memory_service.search(
                    db, str(org.id), str(user.id),
                    query=data.query, tags=data.tags, section=data.section,
                    include_past_events=data.include_past_events, limit=data.limit,
                    exclude_ids=injected,
                )
                report_ids = {
                    (e.evidence or {}).get("report_id") for e in entries if isinstance(e.evidence, dict)
                } - {None}
                titles: Dict[str, str] = {}
                if report_ids:
                    rows = await db.execute(
                        select(Report.id, Report.title).where(
                            Report.id.in_(list(report_ids)), Report.organization_id == str(org.id)
                        )
                    )
                    titles = {str(r[0]): (r[1] or "Untitled report") for r in rows.all()}
                results = []
                for e in entries:
                    ev = e.evidence if isinstance(e.evidence, dict) else {}
                    rid = ev.get("report_id")
                    item = {
                        "handle": e.handle,
                        "section": e.section,
                        "text": e.text,
                        "tags": list(e.tags or []),
                        "aliases": list(e.aliases or []),
                        "source": e.source,
                    }
                    if e.event_start:
                        item["event_start"] = _iso(e.event_start)
                    if e.event_end:
                        item["event_end"] = _iso(e.event_end)
                    if rid and rid in titles:
                        item["from_report"] = {"title": titles[rid], "link": f"/reports/{rid}"}
                    results.append(item)
        except MemoryValidationError as e:
            yield fail("Memory search failed", e.message, code=e.code)
            return
        except Exception as e:  # pragma: no cover
            logger.exception("search_memory failed")
            yield fail("Memory search failed", f"Unexpected error: {e}", code="internal")
            return

        note = (
            f"{len(injected)} entries already shown in <memory> this turn were excluded."
            if injected else "No entries were injected this turn."
        )
        summary = f"Found {len(results)} memory entr{'y' if len(results) == 1 else 'ies'}. {note}"
        output = {"success": True, "count": len(results)}
        observation = {"summary": summary, "entries": results, "excluded_injected": len(injected)}
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})
