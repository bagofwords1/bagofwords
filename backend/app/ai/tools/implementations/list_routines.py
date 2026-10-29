from typing import AsyncIterator, Dict, Any, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ListRoutinesInput,
    ListRoutinesOutput,
    RoutineListItem,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.models.connection import Connection
from app.models.connection_routine import ConnectionRoutine
from app.models.data_source import DataSource


class ListRoutinesTool(Tool):
    """Discover ALL stored procedures / functions across the agents in scope.

    The <routines> block shows only the selected (active) routines. This tool
    returns the full indexed catalog — the agent uses it to scan a database for
    anomalies or to find a routine by name, then reads one with read_routine.
    Signatures only; read_routine fetches a specific routine's source.
    """

    # Cap the listing so a database with thousands of routines can't flood the
    # context in one call. The agent narrows with `name` if it hits this.
    _MAX = 300

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_routines",
            description=(
                "List stored procedures and functions across the connected databases — ALL of "
                "them, not just the ones shown in <routines>. Use it to scan a database's routines "
                "(e.g. to look for anomalies) or to find one by name, then read a specific one's "
                "source with read_routine. Optional 'name' filters by case-insensitive substring. "
                "Returns signatures only (read-only; routines are not queryable tables)."
            ),
            category="research",
            version="1.0.0",
            input_schema=ListRoutinesInput.model_json_schema(),
            output_schema=ListRoutinesOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["routine", "procedure", "function", "list", "search", "scan"],
            observation_policy="on_trigger",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListRoutinesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListRoutinesOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ListRoutinesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"name": data.name})

        db = runtime_ctx.get("db")
        context_hub = runtime_ctx.get("context_hub")
        data_sources = list(getattr(context_hub, "data_sources", None) or []) if context_hub else []
        ds_ids = [str(d.id) for d in data_sources]

        def _end(out: ListRoutinesOutput, summary: str):
            return ToolEndEvent(type="tool.end", payload={
                "output": out.model_dump(),
                "observation": {"summary": summary, "artifacts": []},
            })

        if db is None or not ds_ids:
            yield _end(ListRoutinesOutput(), "No connected databases in scope to list routines from.")
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "listing"})

        # Active set across the in-scope agents (NULL = none active for that agent).
        active_ids: set = set()
        for d in data_sources:
            for rid in (getattr(d, "active_routine_ids", None) or []):
                active_ids.add(str(rid))

        q = (
            select(ConnectionRoutine, Connection)
            .join(Connection, Connection.id == ConnectionRoutine.connection_id)
            .join(Connection.data_sources)
            .where(
                DataSource.id.in_(ds_ids),
                ConnectionRoutine.deleted_at.is_(None),
            )
            .order_by(ConnectionRoutine.name)
        )
        if data.connection_id:
            q = q.where(ConnectionRoutine.connection_id == str(data.connection_id))
        if data.name:
            like = f"%{data.name.strip().lower()}%"
            from sqlalchemy import func
            q = q.where(func.lower(ConnectionRoutine.name).like(like))

        rows = (await db.execute(q)).all()
        # De-dup on routine id (the m2m join repeats a routine once per linked agent).
        seen: set = set()
        items: list = []
        for routine, connection in rows:
            rid = str(routine.id)
            if rid in seen:
                continue
            seen.add(rid)
            items.append(RoutineListItem(
                name=routine.name,
                routine_type=routine.routine_type,
                arguments=routine.arguments,
                returns=routine.returns,
                definition_available=bool(routine.definition_available),
                is_active=rid in active_ids,
                connection_id=str(connection.id),
                connection_name=connection.name,
            ))

        total = len(items)
        truncated = total > self._MAX
        items = items[: self._MAX]
        out = ListRoutinesOutput(routines=items, count=total, truncated=truncated)

        if not items:
            yield _end(out, "No routines found"
                       + (f" matching '{data.name}'." if data.name else " on the connected databases."))
            return

        lines = []
        for it in items:
            sig = f"{it.routine_type} {it.name}({it.arguments or ''})"
            if it.returns:
                sig += f" -> {it.returns}"
            flags = []
            if not it.is_active:
                flags.append("not in active context")
            if not it.definition_available:
                flags.append("source not readable")
            if flags:
                sig += "  [" + ", ".join(flags) + "]"
            lines.append(sig)
        body = "\n".join(lines)
        note = (f"{total} routine(s)" + (f" matching '{data.name}'" if data.name else "")
                + (f" (showing first {self._MAX})" if truncated else "")
                + ". Read one's source with read_routine(name).")
        yield _end(out, f"{note}\n\n{body}")
