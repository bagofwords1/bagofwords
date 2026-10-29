from typing import AsyncIterator, Dict, Any, Type, Optional

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ReadRoutineInput,
    ReadRoutineOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.models.connection import Connection
from app.models.connection_routine import ConnectionRoutine
from app.models.data_source import DataSource


class ReadRoutineTool(Tool):
    """Fetch the source of one stored procedure / function on demand.

    The schema context lists routine signatures (the <routines> block) but never
    their bodies — bodies are large. This tool reads one routine's source live
    through the owning connection's client, so the planner can inspect business
    logic that lives in the database. Read-only: it fetches source text, it does
    NOT execute the routine.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_routine",
            description=(
                "Read the source code of ONE stored procedure or function shown in a "
                "<routines> block of the schema. Use it to inspect the business logic a "
                "database routine implements (e.g. to scan for anomalies). Pass the "
                "qualified name exactly as listed (schema.name). This reads source text "
                "only — it does NOT run the routine, and routines are not queryable tables."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadRoutineInput.model_json_schema(),
            output_schema=ReadRoutineOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["routine", "procedure", "function", "source", "schema"],
            observation_policy="on_trigger",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadRoutineInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadRoutineOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ReadRoutineInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"name": data.name})

        db = runtime_ctx.get("db")
        context_hub = runtime_ctx.get("context_hub")
        user = getattr(context_hub, "user", None) if context_hub else None
        ds_ids = [str(d.id) for d in (getattr(context_hub, "data_sources", None) or [])]

        def _end(out: ReadRoutineOutput, summary: str):
            return ToolEndEvent(type="tool.end", payload={
                "output": out.model_dump(),
                "observation": {"summary": summary, "artifacts": []},
            })

        if db is None or not ds_ids:
            yield _end(
                ReadRoutineOutput(found=False, name=data.name,
                                  message="No data sources in scope to read a routine from."),
                f"No routine '{data.name}' is available in this run's scope.",
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "locating_routine"})

        # Scope to connections whose data source is attached to this run — the
        # run's data sources are already access-filtered, so this inherits that.
        q = (
            select(ConnectionRoutine, Connection)
            .join(Connection, Connection.id == ConnectionRoutine.connection_id)
            .join(Connection.data_sources)
            .where(
                DataSource.id.in_(ds_ids),
                ConnectionRoutine.name == data.name,
                ConnectionRoutine.deleted_at.is_(None),
            )
        )
        if data.connection_id:
            q = q.where(ConnectionRoutine.connection_id == str(data.connection_id))

        rows = (await db.execute(q)).all()
        # De-dup on connection (the m2m join can repeat a routine once per linked
        # data source); keep one (routine_row, connection) per connection id.
        by_conn: Dict[str, Any] = {}
        for routine_row, connection in rows:
            by_conn.setdefault(str(connection.id), (routine_row, connection))

        if not by_conn:
            yield _end(
                ReadRoutineOutput(found=False, name=data.name,
                                  message="No routine with that name is indexed on any connection in scope."),
                f"No routine named '{data.name}' was found. Check the <routines> block for the exact name.",
            )
            return

        if len(by_conn) > 1 and not data.connection_id:
            names = ", ".join(sorted(by_conn.keys()))
            yield _end(
                ReadRoutineOutput(found=True, name=data.name,
                                  message=f"'{data.name}' exists on multiple connections: {names}. "
                                          f"Call again with connection_id to pick one."),
                f"'{data.name}' is defined on multiple connections; pass connection_id to disambiguate.",
            )
            return

        routine_row, connection = next(iter(by_conn.values()))

        if not routine_row.definition_available:
            yield _end(
                ReadRoutineOutput(
                    found=True, name=data.name, connection_id=str(connection.id),
                    connection_name=connection.name, routine_type=routine_row.routine_type,
                    definition_available=False,
                    message="The routine exists but its source is not visible to the connection's "
                            "account (missing VIEW DEFINITION or equivalent).",
                ),
                f"'{data.name}' exists on {connection.name} but its source is not readable "
                f"(the connection's account lacks permission to view it).",
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "reading_source"})

        source: Optional[str] = None
        error: Optional[str] = None
        try:
            from app.services.connection_service import ConnectionService
            client = await ConnectionService().construct_client(db, connection, user)
            source = await client.aget_routine_definition(data.name)
        except NotImplementedError:
            error = "This data source does not support reading routine source."
        except Exception as exc:  # non-fatal: report, don't crash the run
            error = f"Could not read the routine source: {exc}"

        if source:
            out = ReadRoutineOutput(
                found=True, name=data.name, connection_id=str(connection.id),
                connection_name=connection.name, routine_type=routine_row.routine_type,
                definition_available=True, source=source,
            )
            # The observation must carry the actual source — the planner reads
            # the summary, so a char-count alone left the agent reporting metadata
            # instead of the code it was asked for.
            yield _end(
                out,
                f"Source of {routine_row.routine_type} '{data.name}' on {connection.name}:\n\n"
                f"```sql\n{source}\n```",
            )
        else:
            out = ReadRoutineOutput(
                found=True, name=data.name, connection_id=str(connection.id),
                connection_name=connection.name, routine_type=routine_row.routine_type,
                definition_available=routine_row.definition_available,
                message=error or "The routine's source came back empty.",
            )
            yield _end(out, error or f"'{data.name}' returned no source text.")
