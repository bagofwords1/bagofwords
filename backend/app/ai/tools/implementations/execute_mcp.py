import logging
import time
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.execute_mcp import ExecuteMCPInput, ExecuteMCPOutput
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ee.audit.tool_audit import log_tool_audit
from app.utils.tabular_payload import (
    envelope_metadata,
    find_table,
    parse_text_payload,
    table_candidates,
)

logger = logging.getLogger(__name__)

# How many records to inspect when describing the row shape, and how many
# columns to name. Enough for a consumer to write a reader; short enough that
# the observation stays readable.
_SHAPE_SAMPLE_ROWS = 20
_SHAPE_MAX_COLUMNS = 40

# How much of a result goes inline, in characters. The whole response is on
# disk either way (see the materialization block below) — this only decides how
# much the model can read WITHOUT opening the file.
#
# It used to be three records, flat, whoever the caller was. Three records is
# enough to infer a schema and not enough to answer a question, so a 40-record
# purchase order came back, went to disk in full, and the agent still had to
# spend a second tool call to read what it had just fetched. Budgeting by
# characters instead of by record count makes the cutoff track what actually
# costs tokens: narrow rows arrive whole, wide ones still get capped.
_DEFAULT_INLINE_CHARS = 50_000
_MAX_INLINE_CHARS = 500_000
# Floor when the budget is set to 0 — the shape has to survive even when an
# operator has turned inlining off, or the agent cannot write a reader for the
# file without opening it first.
_MIN_PREVIEW_ROWS = 3

# Share of the transcript budget one preview may occupy, and the rough
# chars-per-token used to convert between the two (measured at ~4.0 on real
# record payloads).
#
# The setting alone is not safe on a small-window model. The transcript's decay
# ladder protects the last PROTECT_LAST_TURNS turns, and because turns alternate
# assistant/user that shields roughly the last TWO tool results — which the
# ladder then cannot decay no matter how far over budget it is. Six 400-record
# calls measured a 27,250-token floor: fine against a 200k model's 100k budget,
# larger than the whole 16k budget of a 32k model. So the configured value is a
# CEILING, and the window decides the rest.
_PREVIEW_BUDGET_SHARE = 0.15
_CHARS_PER_TOKEN = 4


def _inline_budget(organization_settings: Any, context_window_tokens: Any = None) -> int:
    """Characters of result the model may see inline.

    `mcp_result_inline_chars` clamped, then capped against what this model's
    transcript can actually hold. Clamped because a bad stored value here is
    the difference between a useless observation and one that fills the
    context window on its own.
    """
    try:
        cfg = organization_settings.get_config("mcp_result_inline_chars") if organization_settings else None
        budget = int(getattr(cfg, "value", _DEFAULT_INLINE_CHARS))
    except (TypeError, ValueError, AttributeError):
        budget = _DEFAULT_INLINE_CHARS
    budget = max(0, min(_MAX_INLINE_CHARS, budget))

    if not isinstance(context_window_tokens, int) or context_window_tokens <= 0:
        # No window to reason about — the operator's number stands. Guessing a
        # small window here would silently shrink every result on a provider
        # that simply doesn't report one.
        return budget
    try:
        from app.ai.agents.planner.transcript_bridge import transcript_budget_tokens

        transcript_budget = transcript_budget_tokens(
            type("_S", (), {"context_window_tokens": context_window_tokens})()
        )
    except Exception:
        return budget
    ceiling = int(transcript_budget * _PREVIEW_BUDGET_SHARE * _CHARS_PER_TOKEN)
    return min(budget, max(ceiling, 0))


def _fit_rows(rows: list, budget: int) -> tuple:
    """(leading records that fit in `budget` chars, whether any were left out).

    Measured per record rather than by slicing the serialized whole, so one
    pathological wide row can't collapse the sample to nothing — the first
    record always goes in, and `row_count` tells the model what it is missing.
    """
    import json

    if budget <= 0:
        return rows[:_MIN_PREVIEW_ROWS], len(rows) > _MIN_PREVIEW_ROWS
    used = 0
    for i, row in enumerate(rows):
        try:
            used += len(json.dumps(row, default=str)) + 1
        except Exception:
            used += len(str(row)) + 1
        if used > budget and i > 0:
            return rows[:i], True
    return rows, False


def _inline_preview(result_data: Any, table_rows: list, table_path: str, budget: int) -> tuple:
    """What goes inline for a tabular result.

    Returns (preview, preview_row_count, preview_truncated, is_full_payload).

    When the rows were extracted from inside an envelope, the extraction used
    to decide the inline copy too: everything outside the detected list — the
    rest of a wide record, sibling objects, the envelope's own fields — never
    reached the model even with 99% of the budget unspent, and a small sidecar
    list could shadow the actual answer. The detector is advisory for the file
    on disk; this makes it advisory inline as well: if the WHOLE payload fits
    the budget, inline the whole payload, and only fall back to leading records
    when it does not.
    """
    import json

    if table_path and budget > 0:
        try:
            serialized = json.dumps(result_data, default=str)
        except Exception:
            serialized = None
        if serialized is not None and len(serialized) <= budget:
            return result_data, len(table_rows), False, True
    fitted, truncated = _fit_rows(table_rows, budget)
    return fitted, len(fitted), truncated, False


def _register_same_turn(file: Any, runtime_ctx: dict, report: Any) -> None:
    """Make a just-written file visible to the tools that run after it.

    Two separate collections need it, and missing either one is silent:

    * ``runtime_ctx["excel_files"]`` — what create_data / write_csv / inspect_data
      hand to generated code.
    * ``report.files`` — what read_file resolves a session file id against
      (resolve_session_file). The association row IS written, so a later turn
      sees the file; but the collection was loaded once at run start and a Core
      insert into the secondary table doesn't touch it, and expire_on_commit is
      False so no commit refreshes it either. Without this append, read_file on
      a file materialized earlier in the SAME turn returns "not found".

    The update goes through ``set_committed_value``, NOT ``report.files.append``.
    Appending to the loaded collection marks it dirty, so SQLAlchemy emits its
    own INSERT for the association on the next flush — on top of the one above —
    and the run dies on ``UNIQUE constraint failed: report_file_association``.
    ``set_committed_value`` installs the value as already-persisted, which is
    exactly what it is. The presence check keeps this off reports loaded with
    ``noload(Report.files)``, where touching the attribute would trigger a lazy
    load and raise under async.
    """
    try:
        ef = runtime_ctx.get("excel_files")
        if isinstance(ef, list) and all(getattr(x, "id", None) != file.id for x in ef):
            ef.append(file)
    except Exception as e:
        logger.debug(f"execute_mcp: excel_files registration skipped: {e}")

    try:
        from sqlalchemy.orm.attributes import set_committed_value

        # `files` in the instance dict means selectinload ran; absent means
        # noload/lazy, and we leave it alone.
        loaded = getattr(report, "__dict__", {}).get("files") if report is not None else None
        if loaded is not None and all(getattr(x, "id", None) != file.id for x in loaded):
            set_committed_value(report, "files", list(loaded) + [file])
    except Exception as e:
        logger.debug(f"execute_mcp: report.files registration skipped: {e}")


_UPLOAD_DIR = "uploads/files"


def _upload_path(filename: str) -> str:
    """Path under the uploads dir, creating the dir if it is missing.

    The write used to assume the directory existed; when it didn't, every
    materialization raised ENOENT into the same swallow-and-warn handler as a
    genuine failure, and the run continued as though the tool had returned
    nothing worth saving.
    """
    import os

    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    return os.path.join(_UPLOAD_DIR, filename)


def _record_shape(rows: list) -> Dict[str, Any]:
    """Column names and types for the first rows of a table.

    Handed to the tool that consumes the artifact so it can write the reader
    from fact instead of inferring the schema from a 3-row preview.
    """
    columns: Dict[str, str] = {}
    for row in rows[:_SHAPE_SAMPLE_ROWS]:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if key in columns and columns[key] != "null":
                continue
            if value is None:
                columns.setdefault(key, "null")
            elif isinstance(value, bool):
                columns[key] = "bool"
            elif isinstance(value, int):
                columns[key] = "int"
            elif isinstance(value, float):
                columns[key] = "float"
            elif isinstance(value, (dict, list)):
                columns[key] = "nested"
            else:
                columns[key] = "str"
    trimmed = dict(list(columns.items())[:_SHAPE_MAX_COLUMNS])
    return {
        "row_count": len(rows),
        "column_count": len(columns),
        "columns": trimmed,
        "columns_truncated": len(columns) > len(trimmed),
    }


def _is_loopback_url(url: str) -> bool:
    """True when a URL points back at this instance (a localhost/loopback host).

    Such a connection is a self-call to our own /api/mcp; calling it over HTTP
    re-enters the app, so on SQLite we must release the agent's transaction
    first to avoid the single-writer deadlock.
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".localhost")


def _result_summary(tool_name: str, content_type: str, output: Dict[str, Any]) -> str:
    """Prose that tells the agent what it got and what to do with it next.

    Extracted from run_stream so the routing is testable on its own: which
    tool this names is the difference between the agent reading the records
    it asked for and generating pandas to re-fetch them.
    """
    # Route on `media` — what the artifact IS — rather than leaving the
    # choice to prose. Each media has exactly one right next tool, and
    # naming the wrong one is how a text result ends up in a codegen tool
    # that has no reader for it.
    summary = f"Executed '{tool_name}'"
    file_id = output.get("file_id")
    media = output.get("media")
    if file_id and content_type == "tabular":
        source = f" at '{output['tabular_path']}'" if output.get("tabular_path") else ""
        summary += (
            f" → saved as {output['file_name']}, file_id={file_id}."
            f" It holds {output['row_count']} records{source}."
        )
        # Whether the records above are the whole set decides the agent's
        # next move, so say which it is. Told only "saved to a file", it
        # spends a create_data round trip re-reading data it can already
        # see; told nothing about a cut, it answers from a partial list.
        if output.get("preview_truncated"):
            summary += (
                f" The first {output['preview_row_count']} are shown above —"
                " do NOT answer from those alone."
            )
            # When the server handed back its own cursor, paging the source
            # beats reading our copy: it is the only route that returns
            # records this call never fetched. `result_metadata` carries
            # whatever cursors/totals the envelope declared.
            if output.get("result_metadata"):
                summary += (
                    f" This response carried pagination metadata"
                    f" ({output['result_metadata']}) — to get records beyond"
                    f" this page, call '{tool_name}' again with the next"
                    " cursor/offset rather than re-reading the file."
                )
            summary += (
                f" To READ more of what was already fetched, use"
                f" read_file(file_id='{file_id}', offset=…) and page with the"
                " returned next_cursor; offsets are BYTE offsets into the"
                " JSON, so a window may start mid-record — that is fine for"
                " reading, not for parsing."
            )
        elif output.get("preview_is_full_payload"):
            summary += (
                " The COMPLETE response is shown above — the record list plus"
                " everything around it — so answer directly from it instead of"
                " re-reading the file."
            )
        elif output.get("tabular_path"):
            # Untruncated rows extracted from inside an envelope the budget
            # could not hold whole: the list is complete, the response is not.
            # Claiming "all shown" here is how a small sidecar list shadowing a
            # large record turned into a wrong answer.
            summary += (
                f" All records of that list are shown above, but the response"
                f" holds MORE outside '{output['tabular_path']}' that was NOT"
                f" inlined — if the answer may live outside the list, use"
                f" read_file(file_id='{file_id}') before answering."
            )
        else:
            summary += (
                " All of them are shown above — answer directly from them"
                " instead of re-reading the file."
            )
        summary += (
            f" To chart or aggregate the full set, use"
            f" create_data(source_file_ids=['{file_id}']);"
            f" use write_csv(source_file_ids=['{file_id}']) only if you need"
            " the table as a CSV file."
        )
        if output.get("candidate_paths"):
            summary += (
                " Other record lists in the same payload: "
                + ", ".join(output["candidate_paths"])
                + " — say so explicitly if you want one of those instead."
            )
    elif media == "text":
        summary += (
            f" → text saved as {output['file_name']}, file_id={file_id}."
            f" Read it with read_file(file_id='{file_id}') — page through it"
            " with offset/length. It has no table, so create_data and"
            " write_csv cannot load it unless it holds a regular,"
            " parseable pattern."
        )
    elif file_id and output.get("parse_skipped"):
        # Too big to analyze in-process, but the bytes are all on disk.
        # Say that plainly — an agent told only "no records found" will
        # assume the file is useless and go looking somewhere else.
        summary += (
            f" → saved as {output['file_name']}, file_id={file_id}."
            f" The response was {output.get('size_chars', 0):,} characters —"
            " too large to analyze here, so its structure was NOT inspected."
            " The file is complete JSON. To LOOK at it, use"
            f" read_file(file_id='{file_id}', offset=…) and page with the"
            " returned next_cursor. To analyze it, use"
            f" create_data(source_file_ids=['{file_id}']) or"
            f" write_csv(source_file_ids=['{file_id}']) and locate the"
            " record list yourself with"
            " pd.read_json(path, typ='series').to_dict(), or ask the tool"
            " for a narrower window / a page at a time."
        )
    elif file_id:
        summary += (
            f" → saved as {output['file_name']} ({media}), file_id={file_id}."
            f" No record list was found in it. Inspect it with"
            f" read_file(file_id='{file_id}'), or extract a table with"
            f" write_csv(source_file_ids=['{file_id}']) if you can see one."
        )
    elif output.get("materialization_error"):
        # Never let a failed write look like "there was nothing to save".
        summary += (
            f" → the result could NOT be saved to a file"
            f" ({output['materialization_error']}). Only the truncated preview"
            " above is available; do not assume a file exists."
        )
    elif output.get("row_count"):
        summary += f" → {output['row_count']} rows (inline)"
    else:
        summary += f" → {content_type} result"
    return summary


class ExecuteMCPTool(Tool):
    """Execute a tool on an MCP server or custom API endpoint."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="execute_mcp",
            description="""
            Purpose:
Execute a tool on a connected MCP server or custom API endpoint.
Returns the tool's output. EVERY successful call saves the FULL result to a file
and returns its `file_id` — tabular results as CSV, everything else as JSON or
text. As much of the result as fits the inline budget is returned in `preview`.
Read `preview_truncated` before deciding what to do next:
    - false → every detected record is above; when `preview_is_full_payload` is
      true the `preview` is the ENTIRE response verbatim. Answer from it
      directly; do not spend another call re-reading what you already have.
      (If `tabular_path` is set without `preview_is_full_payload`, the response
      carried more OUTSIDE that list — read_file if the answer may live there.)
    - true → `preview` holds the first `preview_row_count` of `row_count`
      records. Never answer from a partial list, and never rebuild the data
      from it — get the rest first.
Pick the next step by what you need, not by the result's type:
    - READ more records → read_file(file_id=…, offset=…), paging with the
      returned next_cursor. Byte-offset windows may start mid-record.
    - records this call never fetched → if the observation carries
      `result_metadata` with a cursor, call this tool again with it
    - chart or aggregate → create_data(source_file_ids=[file_id])
    - need it as a CSV → write_csv(source_file_ids=[file_id])
Never try to call this connection from generated Python — generated code has no
access to it.

Use when:
    - You need to fetch data from an external tool (Notion, Jira, Datadog, etc.)
    - You need to invoke an API endpoint to retrieve or submit data
    - If a tool's <mcp_tools> entry shows no <arg> elements (or the tool isn't
      listed), call search_mcps first for its schema; entries WITH <arg>
      elements are complete — call directly.

Argument typing — `arguments` is validated against the tool's real schema
before the call goes out, so match declared types exactly:
    - An arg typed "string" takes a STRING even when its content is JSON —
      serialize the JSON (vendors like monday and Jira use this shape for
      column/field maps).
    - An arg typed "integer" takes a NUMBER — a unix epoch is 1740787200,
      not "2026-03-01". An arg with enum= takes exactly one listed value;
      integer enums are numbers, not labels.
    - Nested <arg> elements = an OBJECT of that shape; <item> = an ARRAY of
      objects, not an array of strings.
    - Omit optional arguments you have no value for — never pass null or "".

Do not use when:
    - You need to query a SQL database (use create_data instead)
    - You need to read uploaded files (use inspect_data instead)
            """,
            category="both",
            version="1.0.0",
            input_schema=ExecuteMCPInput.model_json_schema(),
            output_schema=ExecuteMCPOutput.model_json_schema(),
            tags=["mcp", "tools", "api", "execution"],
            timeout_seconds=60,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ExecuteMCPInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ExecuteMCPOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ExecuteMCPInput(**tool_input)
        organization_settings = runtime_ctx.get("settings")

        # Feature gate check
        if organization_settings:
            enable_mcp = organization_settings.get_config("enable_mcp_tools")
            if enable_mcp and not enable_mcp.value:
                await log_tool_audit(
                    runtime_ctx,
                    action="tool.access_blocked_by_policy",
                    resource_type="report",
                    resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                    details={"tool": "execute_mcp", "policy": "enable_mcp_tools"},
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": {"success": False, "error_message": "MCP tools are disabled for this organization."},
                        "observation": {"summary": "execute_mcp blocked: enable_mcp_tools is disabled", "success": False},
                    },
                )
                return

        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Executing {data.tool_name}",
            "connection_id": data.connection_id,
        })

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        # The run's user — needed so per-user OAuth connections (custom_api /
        # mcp with auth_policy=user_required) resolve THIS user's access token
        # instead of falling back to the connection's system credentials (which,
        # for an oauth_app connection, carry no user token → the call goes out
        # unauthenticated and X returns 401).
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        if not db or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": "Missing database session or organization context."},
                    "observation": {"summary": "Missing context", "success": False},
                },
            )
            return

        # Validate connection belongs to org and tool is enabled
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_connection"})
        from sqlalchemy import select
        from app.models.connection import Connection
        from app.models.connection_tool import ConnectionTool
        from app.services.connection_service import ConnectionService

        # Resolve connection — only allow MCP/API connections linked to this report's data sources
        from sqlalchemy import or_
        report = runtime_ctx.get("report")
        allowed_conn_ids = set()
        if report:
            for ds in (report.data_sources or []):
                for conn in (ds.connections or []):
                    if conn.type in ("mcp", "custom_api"):
                        allowed_conn_ids.add(str(conn.id))

        conn_result = await db.execute(
            select(Connection).where(
                or_(
                    Connection.id == data.connection_id,
                    Connection.name == data.connection_id,
                ),
                Connection.organization_id == str(organization.id),
                Connection.type.in_(["mcp", "custom_api"]),
            )
        )
        connection = conn_result.scalars().first()

        # Verify connection is linked to this report's data sources
        if connection and allowed_conn_ids and str(connection.id) not in allowed_conn_ids:
            connection = None
        if not connection:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": f"Connection '{data.connection_id}' not found."},
                    "observation": {"summary": "Connection not found", "success": False},
                },
            )
            return

        # Emit connection name so the UI can show it during streaming
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "connection_resolved", "connection_name": connection.name})

        # Resolve per-user context forwarding (identity/membership → headers +
        # custom_metadata). Locked metadata fields clobber the model's values;
        # ai fields fill only where the model left a gap. Done here — before the
        # policy confirmation and the call — so the audited/confirmed payload is
        # exactly what the MCP server receives.
        from app.services.mcp_context_injection import (
            resolve_mcp_context,
            apply_metadata_injection,
        )
        forward_ctx = None
        try:
            forward_ctx = await resolve_mcp_context(db, connection, user, organization)
        except Exception as e:
            logger.warning(f"execute_mcp: context forwarding resolve failed: {e}")
        if forward_ctx is not None:
            if forward_ctx.blocking_missing:
                missing = ", ".join(forward_ctx.blocking_missing)
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": {"success": False, "error_message": f"Missing required user context: {missing}."},
                        "observation": {"summary": f"Blocked: missing required user context ({missing})", "success": False},
                    },
                )
                return
            if data.arguments is None:
                data.arguments = {}
            apply_metadata_injection(data.arguments, forward_ctx)

        # Check tool is enabled
        tool_result = await db.execute(
            select(ConnectionTool).where(
                ConnectionTool.connection_id == str(connection.id),
                ConnectionTool.name == data.tool_name,
            )
        )
        tool_record = tool_result.scalar_one_or_none()
        # Capture the tool's declared input schema up front so that, on any
        # downstream failure, we can hand the agent the *correct* argument shape
        # in the observation. Without this the agent only learns what was wrong
        # (e.g. "invalid search field") and keeps re-guessing argument names.
        tool_input_schema = getattr(tool_record, "input_schema", None) if tool_record else None
        # Hide admin-locked metadata fields from the schema echoed back on failure,
        # so the agent never sees (and never re-guesses) server-injected fields.
        if tool_input_schema:
            try:
                import json as _json
                from app.services.mcp_context_injection import filter_locked_from_schema
                _cfg = connection.config
                if isinstance(_cfg, str):
                    _cfg = _json.loads(_cfg)
                tool_input_schema = filter_locked_from_schema(tool_input_schema, _cfg or {})
            except Exception:
                pass
        if tool_record and not tool_record.is_enabled:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error_message": f"Tool '{data.tool_name}' is disabled."},
                    "observation": {"summary": f"Tool '{data.tool_name}' is disabled by admin", "success": False},
                },
            )
            return

        # Enforce the effective tool policy (user pref > agent overlay > default).
        # deny blocks, ask pauses the run for user approval, auto delegates the
        # decision to a small-model judge. Only applies to discovered
        # ConnectionTool rows (i.e. real MCP / custom API tools).
        if tool_record:
            policy_gate = self._enforce_policy(
                db, connection, tool_record, data, runtime_ctx, report
            )
            async for ev in policy_gate:
                if isinstance(ev, ToolEndEvent):
                    yield ev
                    return
                yield ev

        # Validate arguments against the tool's own schema BEFORE going to the
        # network. We already hold the schema locally, so a shape error costs
        # nothing to catch here — whereas the remote equivalent costs a full
        # round trip and comes back as a vendor string that usually doesn't name
        # the offending field. The failure payload carries the schema, so the
        # agent gets the mismatch and the correct shape in one observation.
        if tool_input_schema:
            from app.ai.tools.mcp_schema import validate_arguments

            ok, arg_errors = validate_arguments(data.arguments, tool_input_schema)
            if not ok:
                logger.info(
                    "execute_mcp: local schema validation rejected %s: %s",
                    data.tool_name, "; ".join(arg_errors),
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload=self._failure_payload(
                        data.tool_name,
                        "Arguments do not match the tool's input schema — "
                        + "; ".join(arg_errors),
                        tool_input_schema,
                    ),
                )
                return

        # Construct client and call tool
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "calling_tool"})

        try:
            # A configured MCP/API connection is ALWAYS called over its own wire.
            # We deliberately do NOT substitute a same-named BOW built-in tool for
            # the connection's call — that would run the wrong tool for an external
            # server (e.g. a Tableau or other-instance `create_report`) and skip
            # identity forwarding. Pass the run's user so per-user OAuth creds
            # resolve to their token (system creds carry none for oauth_app).
            service = ConnectionService()
            # Forward resolved identity headers (static + per-user injected).
            header_overrides = (
                {"headers": forward_ctx.headers}
                if forward_ctx is not None and forward_ctx.has_headers
                else None
            )
            client = await service.construct_client(
                db, connection, user, config_overrides=header_overrides
            )
            server_url = getattr(client, "server_url", "") or ""
            # Loopback connection (this instance's own /api/mcp): the HTTP
            # self-call re-enters the app and would deadlock SQLite's single
            # writer while we hold the agent session. Release our transaction
            # first — expire_on_commit=False keeps ORM objects usable afterwards.
            if _is_loopback_url(server_url):
                await self._release_db(db)
            logger.info(f"execute_mcp: Calling remote MCP: {server_url or '?'}")
            result = await client.acall_tool(data.tool_name, data.arguments)
            logger.info(f"execute_mcp: Remote call returned success={result.get('success')}, error={result.get('error')}")
        except BaseException as e:
            logger.error(f"execute_mcp: Tool call failed: {e}", exc_info=True)
            yield ToolEndEvent(
                type="tool.end",
                payload=self._failure_payload(data.tool_name, str(e), tool_input_schema),
            )
            return

        if not result.get("success"):
            yield ToolEndEvent(
                type="tool.end",
                payload=self._failure_payload(data.tool_name, result.get("error", "Unknown error"), tool_input_schema),
            )
            return

        # Handle result based on content type
        content_type = result.get("content_type", "json")
        result_data = result.get("data")

        output = {
            "success": True,
            "content_type": content_type,
            "connection_name": connection.name,
            "file_id": None,
            "file_name": None,
            "row_count": None,
            "preview": None,
            "error_message": None,
        }
        # Persist policy outcomes with the result so the UI (after rehydration)
        # and the planner's conversation digest can see them.
        policy_verdict = runtime_ctx.pop("_mcp_policy_verdict", None)
        if policy_verdict:
            output["policy_verdict"] = policy_verdict
        approval = runtime_ctx.pop("_mcp_policy_approval", None)
        if approval:
            output["approval"] = approval

        # Providers hand back a bare array, an envelope-wrapped table
        # ({"data": [...], "pages": {...}}), or a payload they simply labelled
        # "json" — so locate the rows ourselves instead of trusting the label.
        # Without this a wrapped table is saved as an opaque .json blob and the
        # rows never reach the analysis stack.
        table_rows, table_path = (None, "")
        if content_type in ("tabular", "json"):
            table_rows, table_path = find_table(result_data)
        elif content_type == "text":
            # A provider that labels its output "text" may still be handing back
            # CSV, NDJSON, or JSON its own parse missed. Reading it as prose
            # loses a real table, so sniff before believing the label.
            kind, sniffed_rows = parse_text_payload(result_data)
            if sniffed_rows:
                table_rows, table_path = sniffed_rows, ""
                output["parsed_from_text"] = kind

        # Invariant: a successful call leaves exactly ONE durable artifact plus a
        # pointer to it, and that artifact is the payload AS RECEIVED.
        #
        # It used to be a derived CSV whenever rows were spotted, which put a
        # heuristic on the critical path: whatever `find_table` picked was the
        # only thing that survived to disk, and everything it didn't pick — the
        # envelope's cursors, sibling lists, nested objects (pandas writes those
        # as Python reprs, which don't even parse back) — was gone. Saving the
        # response verbatim makes the detector advisory: `tabular_path` and
        # `candidate_paths` tell the consumer where the rows are, and a wrong
        # guess costs one key lookup instead of the data. A CSV is still one
        # write_csv call away for anyone who wants the file.
        inline_budget = _inline_budget(
            organization_settings,
            getattr(runtime_ctx.get("model"), "context_window_tokens", None),
        )

        if table_rows is not None:
            content_type = "tabular"
            output["content_type"] = content_type
            output["row_count"] = len(table_rows)
            preview, shown, rows_truncated, is_full = _inline_preview(
                result_data, table_rows, table_path, inline_budget
            )
            output["preview"] = preview
            output["preview_row_count"] = shown
            # State the cut explicitly. A silently-shortened list reads as the
            # complete result, and an agent that believes it has all 40 records
            # will answer from the 12 it can see.
            output["preview_truncated"] = rows_truncated
            if is_full:
                output["preview_is_full_payload"] = True
            if table_path:
                output["tabular_path"] = table_path
                # Keep the envelope's cursors/totals — they're how the agent
                # knows whether it has the whole result set.
                metadata = envelope_metadata(result_data, table_path)
                if metadata:
                    output["result_metadata"] = metadata
            # Runner-up row lists, so a wrong pick is visible and correctable
            # rather than silent.
            others = [p for p, _ in table_candidates(result_data) if p != table_path]
            if others:
                output["candidate_paths"] = others[:5]
            output["record_shape"] = _record_shape(table_rows)
        elif content_type == "text":
            text = result_data if isinstance(result_data, str) else str(result_data)
            limit = inline_budget or 3000
            if len(text) > limit:
                output["preview"] = text[:limit] + f"… [truncated, {len(text)} total chars]"
                output["preview_truncated"] = True
            else:
                output["preview"] = text
        else:
            import json
            limit = inline_budget or 3000
            try:
                preview_str = json.dumps(result_data, default=str)
                if len(preview_str) <= limit:
                    output["preview"] = result_data
                else:
                    # Truncated preview so the model can see the structure
                    output["preview"] = preview_str[:limit] + f"… [truncated, {len(preview_str)} total chars]"
                    output["preview_truncated"] = True
            except Exception:
                output["preview"] = str(result_data)[:limit]

        # A response too large to parse comes back as an unparsed string. It is
        # still JSON, and saying otherwise is worse than not checking at all:
        # the artifact lands as .txt, the consumer is told to read prose, and it
        # never finds records that are plainly in the file.
        if result.get("parse_skipped"):
            output["parse_skipped"] = True
            output["size_chars"] = result.get("size_chars")
            artifact_kind, artifact_payload = "json_text", result_data
        elif isinstance(result_data, str):
            artifact_kind, artifact_payload = "text", result_data
        else:
            artifact_kind, artifact_payload = "json", result_data
        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": f"materializing_{artifact_kind}"},
        )
        try:
            if artifact_kind == "text":
                file_record = await self._materialize_to_text(
                    artifact_payload, data.tool_name, runtime_ctx
                )
            elif artifact_kind == "json_text":
                # Already-serialized JSON: write the bytes through untouched
                # rather than round-tripping 8 MB through json.dump.
                file_record = await self._materialize_to_text(
                    artifact_payload, data.tool_name, runtime_ctx, extension="json",
                    content_type="application/json",
                )
            else:
                file_record = await self._materialize_to_json(
                    artifact_payload, data.tool_name, runtime_ctx
                )
            output["file_id"] = str(file_record.id)
            output["file_name"] = file_record.filename
            output["media"] = "json" if artifact_kind == "json_text" else artifact_kind
        except Exception as e:
            # Do NOT swallow this. An empty file_id reads exactly like "there
            # was nothing to save", so the agent moves on believing the data is
            # somewhere it isn't.
            logger.warning(f"execute_mcp: {artifact_kind} materialization failed: {e}")
            output["media"] = "none"
            output["materialization_error"] = str(e)
            if table_rows is not None and not output.get("preview_is_full_payload"):
                # The inline copy is now the ONLY copy, so spend the full budget
                # on it rather than the 10 rows this used to salvage. (A preview
                # already holding the whole payload is left alone — re-fitting
                # would swap the complete response for a subset of it.)
                fitted, rows_truncated = _fit_rows(table_rows, inline_budget or _MAX_INLINE_CHARS)
                output["preview"] = fitted
                output["preview_row_count"] = len(fitted)
                output["preview_truncated"] = rows_truncated

        # If the tool returned a file blob (e.g. a Drive download), materialize
        # it into a session File so the analysis stack can use it — same path as
        # read_mcp_resource / uploaded files. Sets session_file_id for the agent
        # to pass to inspect_data / create_data.
        if not output.get("file_id"):
            import base64
            from ._file_tool_common import attach_drive_file_to_session, ext_for_mime
            for b in (result.get("binaries") or []):
                if not b.get("blob_b64") or not ext_for_mime(b.get("mime_type")):
                    continue
                try:
                    raw = base64.b64decode(b["blob_b64"], validate=False)
                except Exception:
                    continue
                name = (str(b.get("uri") or data.tool_name).rstrip("/").split("/")[-1]) or data.tool_name
                sid = await attach_drive_file_to_session(
                    runtime_ctx, filename=name, content_bytes=raw, mime_type=b.get("mime_type"),
                )
                if sid:
                    output["file_id"] = sid
                    output["session_file_id"] = sid
                    break

        # Audit
        await log_tool_audit(
            runtime_ctx,
            action="tool.mcp_executed",
            resource_type="report",
            resource_id=str(report.id) if report else None,
            details={
                "tool": "execute_mcp",
                "connection_id": data.connection_id,
                "tool_name": data.tool_name,
                "content_type": content_type,
                "file_id": output.get("file_id"),
            },
        )

        summary = _result_summary(data.tool_name, content_type, output)

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": {
                    "summary": summary,
                    "content_type": content_type,
                    "file_id": output.get("file_id"),
                    # The artifact descriptor travels on the observation too, so
                    # it survives the planner's past-observation compaction —
                    # a later turn still knows what the file holds and how to
                    # read it without re-running the call.
                    "media": output.get("media"),
                    "file_name": output.get("file_name"),
                    "tabular_path": output.get("tabular_path"),
                    "candidate_paths": output.get("candidate_paths"),
                    "record_shape": output.get("record_shape"),
                    # Absence of tabular_path means "unknown", not "flat", when
                    # the payload was never parsed — the consumer must be told
                    # which it is or it will pick the wrong reader.
                    "parse_skipped": output.get("parse_skipped"),
                    "size_chars": output.get("size_chars"),
                    "materialization_error": output.get("materialization_error"),
                    "preview": output.get("preview"),
                    # Whether `preview` is the whole result or a leading slice.
                    # Without it the agent cannot tell a complete 12-record
                    # answer from the first 12 of 4,000.
                    "preview_truncated": output.get("preview_truncated"),
                    "preview_row_count": output.get("preview_row_count"),
                    # True when `preview` is the response verbatim (envelope
                    # included), not just the detected record list.
                    "preview_is_full_payload": output.get("preview_is_full_payload"),
                    "row_count": output.get("row_count"),
                    "success": True,
                },
            },
        )

    async def _enforce_policy(
        self, db, connection, tool_record, data, runtime_ctx: Dict[str, Any], report
    ) -> AsyncIterator[ToolEvent]:
        """Yield policy events for one call; a ToolEndEvent means 'blocked'.

        Completing without a ToolEndEvent means the call is approved and the
        caller proceeds to execute the tool.
        """
        import asyncio
        from uuid import uuid4
        from app.services.tool_policy_service import (
            ToolPolicyService,
            TOOL_POLICY_ALLOW,
            TOOL_POLICY_ASK,
            TOOL_POLICY_DENY,
            TOOL_POLICY_AUTO,
        )
        from app.ai.tools.schemas import ToolConfirmationEvent

        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        # Agents in this run that own the connection — their overlay applies.
        ds_ids = [
            str(ds.id)
            for ds in (getattr(report, "data_sources", None) or [])
            if any(str(c.id) == str(connection.id) for c in (ds.connections or []))
        ]
        policy_svc = ToolPolicyService()
        resolution = await policy_svc.resolve_for_run(
            db, tool=tool_record, data_source_ids=ds_ids, user=user
        )

        if not resolution.is_enabled:
            yield self._policy_end_event(
                data.tool_name,
                f"Tool '{data.tool_name}' is disabled for this agent.",
                blocked_by="disabled",
            )
            return

        effective = resolution.effective
        if effective == TOOL_POLICY_ALLOW:
            return

        if effective == TOOL_POLICY_DENY:
            await log_tool_audit(
                runtime_ctx,
                action="tool.access_blocked_by_policy",
                resource_type="report",
                resource_id=str(report.id) if report else None,
                details={
                    "tool": "execute_mcp", "tool_name": data.tool_name,
                    "connection_id": str(connection.id), "policy": "deny",
                    "user_policy": resolution.user_policy,
                },
            )
            yield self._policy_end_event(
                data.tool_name,
                f"Tool '{data.tool_name}' is denied by policy and cannot be executed. "
                "Continue the task without it.",
                blocked_by="deny",
            )
            return

        if effective == TOOL_POLICY_AUTO:
            yield ToolProgressEvent(type="tool.progress", payload={
                "stage": "auto_policy_review", "tool_name": data.tool_name,
            })
            # Release the session's transaction before the (multi-second) judge
            # LLM call — same reasoning as agent_v2._release_db_between_steps:
            # holding it starves the pool and, on SQLite, blocks all writers.
            await self._release_db(db)
            verdict = await self._auto_judge(db, connection, tool_record, data, runtime_ctx, report)
            yield ToolProgressEvent(type="tool.progress", payload={
                "stage": "auto_policy_decided",
                "approved": bool(verdict.approve),
                "reason": verdict.reason,
                "timing": False,
            })
            await log_tool_audit(
                runtime_ctx,
                action="tool.auto_policy_decision",
                resource_type="report",
                resource_id=str(report.id) if report else None,
                details={
                    "tool": "execute_mcp", "tool_name": data.tool_name,
                    "connection_id": str(connection.id),
                    "approved": bool(verdict.approve), "reason": verdict.reason,
                },
            )
            # Stash the verdict so run_stream persists it on the final tool
            # output (result_json) — the live progress event alone is lost when
            # the UI rehydrates the run from the DB.
            runtime_ctx["_mcp_policy_verdict"] = {
                "approved": bool(verdict.approve), "reason": verdict.reason or "",
            }
            if not verdict.approve:
                yield self._policy_end_event(
                    data.tool_name,
                    f"Automatic policy review declined this call: {verdict.reason or 'not approved'}. "
                    "You may ask the user to run it manually or continue without it.",
                    blocked_by="auto",
                    extra_output={
                        "policy_reason": verdict.reason,
                        "policy_verdict": {"approved": False, "reason": verdict.reason or ""},
                    },
                )
            return

        if effective == TOOL_POLICY_ASK:
            if not ToolPolicyService.is_interactive_run(runtime_ctx):
                await log_tool_audit(
                    runtime_ctx,
                    action="tool.access_blocked_by_policy",
                    resource_type="report",
                    resource_id=str(report.id) if report else None,
                    details={
                        "tool": "execute_mcp", "tool_name": data.tool_name,
                        "connection_id": str(connection.id), "policy": "ask",
                        "reason": "non_interactive_run",
                    },
                )
                yield self._policy_end_event(
                    data.tool_name,
                    f"Tool '{data.tool_name}' requires user approval, which is not "
                    "available in this context (scheduled/background run). Continue without it.",
                    blocked_by="ask",
                )
                return

            from app.ai.tools.confirmation import (
                register_confirmation,
                discard_confirmation,
            )
            from app.services.tool_confirmation_service import ToolConfirmationService

            confirmation_id = str(uuid4())
            head = runtime_ctx.get("head_completion")
            system = runtime_ctx.get("system_completion")
            # In-process future: a same-worker click wakes the run immediately.
            # It is only a fast path — the DB row below is the source of truth,
            # because the approval POST is load-balanced across uvicorn workers
            # and usually lands on a worker that has no such future.
            future = register_confirmation(confirmation_id, meta={
                "kind": "mcp_tool_policy",
                "user_id": str(user.id) if user else None,
                "connection_tool_id": str(tool_record.id),
                "completion_ids": [
                    str(c.id) for c in (head, system) if c is not None
                ],
                "tool_name": data.tool_name,
            })
            confirmations = ToolConfirmationService()
            await confirmations.create(
                db,
                confirmation_id=confirmation_id,
                organization_id=getattr(runtime_ctx.get("organization"), "id", None),
                report_id=str(report.id) if report else None,
                system_completion_id=str(system.id) if system is not None else None,
                head_completion_id=str(head.id) if head is not None else None,
                user_id=str(user.id) if user else None,
                connection_id=str(connection.id),
                connection_tool_id=str(tool_record.id),
                tool_name=data.tool_name,
                arguments=data.arguments or {},
                timeout_seconds=self._ASK_TIMEOUT_S,
            )
            # Release the session's transaction before blocking on the user —
            # the approval endpoint needs the DB writer (to persist a
            # remembered preference), and on SQLite an open transaction here
            # would deadlock it into a 500. Mirrors _release_db_between_steps.
            await self._release_db(db)
            response: Dict[str, Any] | None = None
            try:
                yield ToolConfirmationEvent(type="tool.confirmation", payload={
                    "kind": "mcp_tool_policy",
                    "confirmation_id": confirmation_id,
                    "tool_name": data.tool_name,
                    "connection_id": str(connection.id),
                    "connection_name": connection.name,
                    "connection_tool_id": str(tool_record.id),
                    "arguments": data.arguments or {},
                    "timeout_seconds": self._ASK_TIMEOUT_S,
                })
                sigkill = runtime_ctx.get("sigkill_event")
                waited = 0.0
                while waited < self._ASK_TIMEOUT_S:
                    if sigkill is not None and sigkill.is_set():
                        break
                    try:
                        response = await asyncio.wait_for(
                            asyncio.shield(future), timeout=self._ASK_POLL_S
                        )
                        break
                    except asyncio.TimeoutError:
                        # The click may have been answered on another worker —
                        # its decision is only visible in the DB.
                        response = await confirmations.poll_decision(confirmation_id)
                        if response is not None:
                            break
                        waited += self._ASK_POLL_S
                        # Keepalive so the ToolRunner idle watchdog doesn't kill
                        # the run while we wait for the user (timing=False keeps
                        # it out of the stage timings). Emitted on the original
                        # cadence, not on every (shorter) poll.
                        if waited % self._ASK_KEEPALIVE_S < self._ASK_POLL_S:
                            yield ToolProgressEvent(type="tool.progress", payload={
                                "stage": "awaiting_approval", "timing": False,
                                "remaining_seconds": max(0, int(self._ASK_TIMEOUT_S - waited)),
                            })
            finally:
                discard_confirmation(confirmation_id)
                if response is None:
                    # One last read: a click that landed inside the final poll
                    # window is already recorded, and honoring it beats telling
                    # the user their approval timed out.
                    response = await confirmations.poll_decision(confirmation_id)
                if response is None:
                    # Nothing decided (timeout or sigkill): stop advertising the
                    # row as answerable so a late click gets a clear 'expired'.
                    await confirmations.expire(db, confirmation_id)

            approved = bool(response and response.get("approved"))
            # Who decided. The DB-poll path only carries the resolver's id, but
            # may_respond restricts responding to the run's own user, so their
            # name is the correct display fallback whenever a decision exists.
            resolved_by_name = None
            if response is not None:
                resolved_by_name = response.get("resolved_by_name") or (
                    getattr(user, "name", None) if user else None
                )
            # Persist the user's decision on the tool output so the planner's
            # conversation digest (and the rehydrated UI) can see what the
            # user chose, not just that the call failed.
            runtime_ctx["_mcp_policy_approval"] = {
                "approved": approved,
                "remember": bool(response and response.get("remember")),
                "timed_out": response is None,
                "resolved_by_name": resolved_by_name,
            }
            await log_tool_audit(
                runtime_ctx,
                action="tool.approval_decision",
                resource_type="report",
                resource_id=str(report.id) if report else None,
                details={
                    "tool": "execute_mcp", "tool_name": data.tool_name,
                    "connection_id": str(connection.id),
                    "approved": approved,
                    "remembered": bool(response and response.get("remember")),
                    "timed_out": response is None,
                },
            )
            if not approved:
                reason = (
                    "the approval request timed out"
                    if response is None else "the user declined it"
                )
                remembered = bool(response and response.get("remember"))
                if remembered:
                    guidance = "The user saved 'always deny' for this tool — do not retry it."
                else:
                    # Deny once / timeout is scoped to this turn: a later
                    # explicit user request should retry (and re-prompt).
                    guidance = (
                        "Do not retry the same call in this turn; continue the "
                        "task without it or adjust your approach. If the user "
                        "explicitly asks for it again later, call it — they "
                        "will be prompted to approve."
                    )
                yield self._policy_end_event(
                    data.tool_name,
                    f"Tool '{data.tool_name}' was not executed because {reason}. {guidance}",
                    blocked_by="ask",
                    extra_output={"approval": runtime_ctx.get("_mcp_policy_approval")},
                )
                return
            yield ToolProgressEvent(type="tool.progress", payload={
                "stage": "approval_granted", "timing": False,
            })
            return

    _ASK_TIMEOUT_S: float = 240.0
    _ASK_KEEPALIVE_S: float = 15.0
    # How often the waiting run re-reads the confirmation row. Sets the worst-case
    # lag between the user's click and the run resuming when the approval POST is
    # handled by another worker (the same-worker future resolves instantly).
    _ASK_POLL_S: float = 3.0

    @staticmethod
    async def _release_db(db) -> None:
        """Commit the shared agent session so its connection (and SQLite's
        writer lock) is released while this tool blocks on a long await.
        expire_on_commit=False keeps loaded ORM objects usable afterwards."""
        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"execute_mcp: releasing db before policy wait failed: {e!r}")

    async def _auto_judge(self, db, connection, tool_record, data, runtime_ctx, report):
        """Run the small-model judge for an 'auto' policy call."""
        from app.ai.classifiers.tool_call_judge import ToolCallJudge, ToolCallVerdict

        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        try:
            from app.services.llm_service import LLMService

            small_model = await LLMService().get_default_model(
                db, organization, user, is_small=True
            )
        except Exception as e:
            logger.error(f"execute_mcp: failed to resolve small model for auto policy: {e}")
            small_model = None
        if small_model is None:
            return ToolCallVerdict(
                approve=False, confidence=0.0,
                reason="no LLM model available for automatic policy review",
            )

        task_context = None
        try:
            head = runtime_ctx.get("head_completion")
            prompt = getattr(head, "prompt", None)
            content = prompt.get("content") if isinstance(prompt, dict) else None
            title = getattr(report, "title", None)
            parts = [p for p in (title, content) if p]
            task_context = "\n".join(str(p) for p in parts) or None
        except Exception:
            pass

        judge = ToolCallJudge(small_model)
        return await judge.judge(
            tool_name=data.tool_name,
            tool_description=tool_record.description,
            connection_name=connection.name,
            arguments=data.arguments or {},
            task_context=task_context,
        )

    @staticmethod
    def _policy_end_event(tool_name: str, message: str, *, blocked_by: str,
                          extra_output: Dict[str, Any] | None = None) -> ToolEndEvent:
        output = {
            "success": False,
            "error_message": message,
            "blocked_by_policy": blocked_by,
        }
        if extra_output:
            output.update(extra_output)
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": {
                    "summary": message,
                    "success": False,
                    "blocked_by_policy": blocked_by,
                },
            },
        )

    @staticmethod
    def _failure_payload(tool_name: str, error: str, input_schema: Any) -> Dict[str, Any]:
        """Build a tool.end payload for a failed MCP call that includes the
        tool's declared input schema.

        The schema is carried as a structured field on both the output and the
        observation (so it survives planner past-observation compaction) and is
        summarized in prose so the agent sees, in one round-trip, exactly which
        arguments the tool accepts instead of re-guessing.
        """
        err = error or "Unknown error"
        summary = f"Tool '{tool_name}' failed: {err}"
        resolved = input_schema
        if input_schema:
            # Render the FULL shape, not one flat level: nested objects, array
            # item shape and enums are exactly where the agent goes wrong, and a
            # flattened "columnValues:string" hint tells it nothing about what
            # belongs inside. $refs are inlined first, since a bare
            # {"$ref": "#/$defs/X"} is unreadable at the point of use.
            try:
                from app.ai.tools.mcp_schema import resolve_refs, render_schema_xml

                resolved = resolve_refs(input_schema)
                args_xml = render_schema_xml(resolved)
                if args_xml:
                    summary += (
                        f". The full argument schema for '{tool_name}' is:\n{args_xml}\n"
                        "Retry with arguments matching these names and types exactly."
                    )
            except Exception:
                resolved = input_schema
        return {
            "output": {"success": False, "error_message": err, "input_schema": resolved},
            "observation": {"summary": summary, "success": False, "input_schema": resolved},
        }

    async def _materialize_to_text(
        self,
        text: str,
        tool_name: str,
        runtime_ctx: dict,
        extension: str = "txt",
        content_type: str = "text/plain",
    ):
        """Save a text result as a file.

        Text used to be the one branch that produced no artifact at all: the
        first 3000 characters became the preview and the rest was dropped on the
        floor. A long log, transcript or report is exactly the kind of result a
        follow-up tool needs in full, so it goes to disk like everything else.
        """
        from uuid import uuid4
        from app.models.file import File

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")

        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        unique_name = f"{uuid4()}_{safe_name}.{extension}"
        path = _upload_path(unique_name)

        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

        file = File(
            filename=f"{safe_name}.{extension}",
            path=path,
            content_type=content_type,
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
        )

        try:
            from app.services.file_preview import generate_file_preview
            file.preview = generate_file_preview(file)
        except Exception:
            pass

        async with db.begin_nested():
            db.add(file)
            await db.flush()

            if report:
                from app.models.report_file_association import report_file_association
                from sqlalchemy import insert
                await db.execute(
                    insert(report_file_association).values(
                        report_id=str(report.id),
                        file_id=str(file.id),
                    )
                )

        _register_same_turn(file, runtime_ctx, report)
        return file

    async def _materialize_to_json(self, data: Any, tool_name: str, runtime_ctx: dict):
        """Save a JSON result as a file so write_csv / create_data can read it."""
        import json
        from uuid import uuid4
        from app.models.file import File

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        # The agent loop puts the acting user under "user"; only some callers
        # set "current_user". Reading just the latter left user_id NULL, and
        # files.user_id is NOT NULL — so every materialization inside an agent
        # run died on the insert (CSV silently fell back to an inline preview,
        # JSON produced no file at all).
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")

        safe_name = tool_name.replace("/", "_").replace(" ", "_")
        unique_name = f"{uuid4()}_{safe_name}.json"
        path = _upload_path(unique_name)

        with open(path, "w") as f:
            json.dump(data, f, default=str)

        file = File(
            filename=f"{safe_name}.json",
            path=path,
            content_type="application/json",
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
        )

        # Same as the CSV path: without a preview the coder sees only a
        # filename and has to guess the file's shape (and its reader).
        try:
            from app.services.file_preview import generate_file_preview
            file.preview = generate_file_preview(file)
        except Exception:
            pass

        # Persist within a savepoint so a failure here rolls back cleanly
        # instead of poisoning the shared agent-execution transaction.
        async with db.begin_nested():
            db.add(file)
            # Flush first so file.id is populated before we link the association
            # (the id is assigned by a Python-side default at flush time).
            await db.flush()

            if report:
                from app.models.report_file_association import report_file_association
                from sqlalchemy import insert
                await db.execute(
                    insert(report_file_association).values(
                        report_id=str(report.id),
                        file_id=str(file.id),
                    )
                )

        _register_same_turn(file, runtime_ctx, report)
        return file

