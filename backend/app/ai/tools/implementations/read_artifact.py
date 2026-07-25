"""
read_artifact tool - Read an existing artifact's code and metadata.

Use this to load previous artifact code into context before modifying with create_artifact.

Long-artifact support: full reads of artifacts above FULL_READ_MAX_CHARS return a
structural outline (with line numbers) instead of the whole code, and the planner
can then use range reads (offset/limit) or grep reads (grep_pattern with
before/after context) to pull only the regions it needs into context.
"""

import logging
import re
from typing import AsyncIterator, Dict, Any, Optional, Tuple, Type, List

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.read_artifact import ReadArtifactInput, ReadArtifactOutput
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.models.query import Query
from app.dependencies import async_session_maker
from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_OBSERVATION

logger = logging.getLogger(__name__)

# ─── Long-artifact windowing knobs ───────────────────────────────────────────
# Full reads at or below this size return the whole code (previous behavior).
# Above it, a full read returns a structural outline instead, and the planner
# is expected to follow up with range/grep reads.
FULL_READ_MAX_CHARS = 50_000
DEFAULT_RANGE_LIMIT = 400
GREP_LINE_CLIP = 500
OUTLINE_MAX_ENTRIES = 300


def _outline_patterns(mode: str) -> List[re.Pattern]:
    """Structural line patterns per artifact mode, used to build the outline."""
    if mode == "doc":
        return [re.compile(r"^#{1,6}\s+\S")]
    if mode == "slides":
        return [
            re.compile(r"^// Slide \d+"),
            re.compile(r"^\s*(?:def|class)\s+\w+"),
            re.compile(r"^\s*#\s*[-=─═*]{3,}"),
        ]
    # page (React/JSX dashboards)
    return [
        re.compile(r"^\s*(?:async\s+)?function\s+\w+"),
        re.compile(r"^\s*const\s+\w+\s*=\s*(?:async\s+)?(?:function\b|\()"),
        re.compile(r"^\s*const\s+\{[^}]*\}\s*=\s*use\w+"),
        re.compile(r"^\s*//\s*.*[-=─═*]{3,}"),
        re.compile(r"<(?:SectionCard|KPICard|EChart|FilterSelect|FilterSearch|FilterDateRange|BowFile)\b"),
        re.compile(r"^\s*ReactDOM\.createRoot"),
    ]


def build_outline(code: str, mode: str) -> str:
    """Build a line-numbered structural outline of the artifact code.

    Returns lines like `  123: function RevenueChart()` so the planner can
    navigate a long artifact and follow up with targeted range/grep reads.
    """
    patterns = _outline_patterns(mode)
    lines = code.split("\n")
    entries: List[str] = []
    for i, line in enumerate(lines):
        if any(p.search(line) for p in patterns):
            text = line.strip()
            if len(text) > 160:
                text = text[:160] + "…"
            entries.append(f"{i + 1:>6}: {text}")
            if len(entries) >= OUTLINE_MAX_ENTRIES:
                entries.append(f"     … outline truncated at {OUTLINE_MAX_ENTRIES} entries ({len(lines)} lines total)")
                break
    if not entries:
        # Nothing structural matched — show evenly spaced sample lines so the
        # planner still gets landmarks to range-read around.
        step = max(1, len(lines) // 40)
        for i in range(0, len(lines), step):
            text = lines[i].strip()
            if text:
                entries.append(f"{i + 1:>6}: {text[:160]}")
            if len(entries) >= 40:
                break
    return "\n".join(entries)


def slice_range(code: str, offset: int, limit: Optional[int]) -> Tuple[str, int, int, int, bool]:
    """Return (slice_text, start_line, end_line, total_lines, truncated) for a
    1-based line range read. The slice is verbatim (no line-number prefixes) so
    it can be quoted directly in edit_artifact SEARCH blocks."""
    lines = code.split("\n")
    total_lines = len(lines)
    limit = limit or DEFAULT_RANGE_LIMIT
    start = max(1, offset)
    if start > total_lines:
        return "", start, start - 1, total_lines, False
    end = min(total_lines, start + limit - 1)
    text = "\n".join(lines[start - 1:end])
    return text, start, end, total_lines, end < total_lines


def grep_code(
    code: str,
    pattern: str,
    ignore_case: bool = False,
    before: int = 0,
    after: int = 0,
    max_matches: int = 50,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Grep the artifact code line-by-line.

    Returns (matches, total_matches, truncated). Each match carries the verbatim
    matching line plus verbatim before/after context and 1-based line numbers.
    Raises re.error for an invalid pattern — callers surface that to the planner.
    """
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    lines = code.split("\n")
    matches: List[Dict[str, Any]] = []
    total = 0
    for i, line in enumerate(lines):
        if not rx.search(line):
            continue
        total += 1
        if len(matches) >= max_matches:
            continue
        ctx_start = max(0, i - before)
        clipped = line if len(line) <= GREP_LINE_CLIP else line[:GREP_LINE_CLIP]
        matches.append({
            "line_no": i + 1,
            "line": clipped,
            "before": lines[ctx_start:i],
            "after": lines[i + 1:i + 1 + after],
            "context_start_line": ctx_start + 1,
        })
    return matches, total, total > len(matches)


class ReadArtifactTool(Tool):
    """Tool to read an existing artifact's code and metadata."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_artifact",
            description=(
                "Read an existing artifact's content and metadata from the current report — dashboards, slides, AND documents (mode='doc'). "
                "For dashboards/slides the `code` field contains the JSX code; for docs it contains the document's full markdown. "
                "Use this to load previous artifact content into context before modifying with edit_artifact/create_artifact (dashboards) or edit_doc (documents), or when the user wants to inspect or analyze an existing artifact. "
                "ALWAYS use this before edit_doc so you can quote exact text for its find/replace edits. "
                "Use this also if the user is saying something is not working like filters or UI elements are not showing up - to check the existing code and visualizations for debugging. "
                "ALWAYS use this before editing an artifact (edit_artifact) to have a full view of the existing code, visualizations, and layout. "
                "If the user refers to a specific version of an artifact, ALWAYS load that version with this tool to have the correct code context for the edit. "
                "LONG ARTIFACTS: when the code is too large to return in full, a plain read returns a structural OUTLINE with line numbers instead of code. "
                "Then use `offset`/`limit` to read a specific line range, or `grep_pattern` (with `before`/`after` context lines) to find the exact region to edit. "
                "Both return VERBATIM code you can quote in edit_artifact SEARCH blocks or edit_doc find strings. "
                "Pass load_screenshot=true to include the last rendered preview screenshot in the observation — use this when debugging visual issues or when you need to see the current state before deciding how to edit. "
                "IMPORTANT: The artifact_id is found in previous create_artifact results shown as 'artifact_id: <uuid>' in the conversation. "
                "Do NOT ask the user for URLs or artifact IDs - extract the artifact_id from the conversation context."
            ),
            category="research",  # Must be research/action/both to be discovered by registry
            version="1.1.0",
            input_schema=ReadArtifactInput.model_json_schema(),
            output_schema=ReadArtifactOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["artifact", "dashboard", "read"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadArtifactInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadArtifactOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadArtifactInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"artifact_id": data.artifact_id})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "reading_artifact"})

        # Get context
        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
        report = context_hub.report if context_hub else runtime_ctx.get("report")

        if not db or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadArtifactOutput(
                        artifact_id=data.artifact_id,
                        mode="page",
                        code="",
                    ).model_dump(),
                    "observation": {
                        "summary": "Failed to read artifact: missing context",
                        "error": {"type": "context_error", "message": "Missing db or organization"},
                    },
                },
            )
            return

        # Fetch the artifact
        try:
            result = await db.execute(
                select(Artifact).where(
                    Artifact.id == data.artifact_id,
                    Artifact.organization_id == str(organization.id),
                )
            )
            artifact = result.scalar_one_or_none()
        except Exception as e:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadArtifactOutput(
                        artifact_id=data.artifact_id,
                        mode="page",
                        code="",
                    ).model_dump(),
                    "observation": {
                        "summary": f"Failed to read artifact: {str(e)}",
                        "error": {"type": "db_error", "message": str(e)},
                    },
                },
            )
            return

        if not artifact:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadArtifactOutput(
                        artifact_id=data.artifact_id,
                        mode="page",
                        code="",
                    ).model_dump(),
                    "observation": {
                        "summary": f"Artifact not found: {data.artifact_id}",
                        "error": {"type": "not_found", "message": f"No artifact with id {data.artifact_id}"},
                    },
                },
            )
            return

        # Extract code from content
        content = artifact.content or {}
        code = content.get("code", "")

        # Docs (mode='doc') store markdown, not code — return it as the "code"
        # payload so the planner can quote exact text for edit_doc find/replace.
        if artifact.mode == "doc":
            code = content.get("markdown", "")

        # For slides mode, concatenate all slide codes
        if artifact.mode == "slides" and "slides" in content:
            slides = content.get("slides", [])
            code = "\n\n".join(
                f"// Slide {i+1}: {s.get('title', 'Untitled')}\n{s.get('code', '')}"
                for i, s in enumerate(slides)
            )

        total_chars = len(code)
        total_lines = code.count("\n") + 1 if code else 0

        # ─── Resolve read mode: grep > range > full/outline ──────────────────
        read_mode = "full"
        code_view = code            # what goes into output.code / observation.code
        start_line: Optional[int] = None
        end_line: Optional[int] = None
        range_truncated = False
        matches: Optional[List[Dict[str, Any]]] = None
        grep_total: Optional[int] = None
        grep_truncated: Optional[bool] = None
        outline: Optional[str] = None
        window_note = ""

        if data.grep_pattern:
            read_mode = "grep"
            code_view = ""
            try:
                matches, grep_total, grep_truncated = grep_code(
                    code,
                    data.grep_pattern,
                    ignore_case=data.ignore_case,
                    before=data.before,
                    after=data.after,
                    max_matches=data.max_matches,
                )
            except re.error as e:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": ReadArtifactOutput(
                            artifact_id=str(artifact.id),
                            title=artifact.title,
                            mode=artifact.mode,
                            code="",
                            visualization_ids=content.get("visualization_ids", []),
                            version=artifact.version,
                            read_mode="grep",
                            total_lines=total_lines,
                            total_chars=total_chars,
                        ).model_dump(),
                        "observation": {
                            "summary": f"Invalid grep_pattern regex: {e}. Fix the pattern and retry.",
                            "error": {"type": "invalid_pattern", "message": str(e)},
                            "artifact_id": str(artifact.id),
                        },
                    },
                )
                return
            if grep_total == 0:
                # No matches — return the outline as a navigation aid so the
                # planner's next read is informed instead of another guess.
                outline = build_outline(code, artifact.mode)
                window_note = (
                    f"0 matches for pattern {data.grep_pattern!r}. "
                    "A structural outline (line numbers) is included — use it to pick an offset/limit range or a better pattern."
                )
            else:
                shown = len(matches)
                window_note = f"{grep_total} match(es) for pattern {data.grep_pattern!r}"
                if grep_truncated:
                    window_note += f", showing first {shown} (max_matches={data.max_matches}) — narrow the pattern for the rest"
                window_note += ". Matched lines and before/after context are verbatim — safe to quote in SEARCH blocks."

        elif data.offset is not None or data.limit is not None:
            read_mode = "range"
            offset = data.offset or 1
            code_view, start_line, end_line, total_lines, range_truncated = slice_range(
                code, offset, data.limit
            )
            if not code_view and start_line > total_lines:
                window_note = (
                    f"offset {start_line} is beyond the end of the code ({total_lines} lines total). "
                    "Retry with a smaller offset."
                )
            else:
                window_note = f"showing lines {start_line}-{end_line} of {total_lines}"
                if range_truncated:
                    window_note += f" — continue with offset={end_line + 1} for more"
                window_note += ". The slice is verbatim — safe to quote in SEARCH blocks."

        elif total_chars > FULL_READ_MAX_CHARS:
            read_mode = "outline"
            code_view = ""
            outline = build_outline(code, artifact.mode)
            window_note = (
                f"code is {total_chars:,} chars / {total_lines:,} lines — too large to return in full. "
                "A structural OUTLINE with line numbers is included instead. "
                "Use offset/limit to read a specific line range, or grep_pattern (with before/after) to find the exact region to edit."
            )

        # Extract visualization_ids if stored
        visualization_ids: List[str] = content.get("visualization_ids", [])

        # Check allow_llm_see_data privacy setting
        organization_settings = runtime_ctx.get("settings")
        allow_llm_see_data = True
        if organization_settings:
            try:
                allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
            except Exception:
                allow_llm_see_data = True

        # Fetch associated visualizations and build profiles
        viz_profiles: List[Dict[str, Any]] = []
        if visualization_ids:
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_visualizations"})
            try:
                from app.ai.tools.implementations.create_artifact import CreateArtifactTool
                create_tool = CreateArtifactTool()
                report_id = str(report.id) if report else None

                async with async_session_maker() as fresh_db:
                    result = await fresh_db.execute(
                        select(Visualization)
                        .options(
                            selectinload(Visualization.query).selectinload(Query.default_step),
                            selectinload(Visualization.query).selectinload(Query.steps),
                        )
                        .where(Visualization.id.in_(visualization_ids))
                    )
                    fetched_vizs = {str(v.id): v for v in result.scalars().all()}

                for viz_id in visualization_ids:
                    viz = fetched_vizs.get(viz_id)
                    if viz is None:
                        continue
                    if report_id and str(viz.report_id) != report_id:
                        continue

                    step = None
                    if viz.query and viz.query.default_step:
                        step = viz.query.default_step
                    elif viz.query and viz.query.steps:
                        step = viz.query.steps[-1] if viz.query.steps else None

                    if not step or step.status != "success":
                        continue

                    step_data = step.data if step else {}
                    rows = (step_data.get("rows") or [])[:100] if step_data else []
                    raw_columns = step_data.get("columns") or [] if step_data else []
                    data_model = step.data_model if step else {}
                    step_info = step_data.get("info") or {} if step_data else {}
                    column_info = step_info.get("column_info") or {}
                    view_dict = viz.view or {}

                    ventry = {
                        "id": str(viz.id),
                        "title": viz.title,
                        "query_id": str(viz.query_id) if viz.query_id else None,
                        "view": create_tool._trim_none(view_dict),
                        "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                        "columns": raw_columns,
                        "column_info": column_info,
                        "row_count": len(rows),
                        "rows": rows,
                        "dataModel": data_model or {},
                    }
                    viz_profiles.append(create_tool._build_viz_profile(ventry, allow_llm_see_data))
            except Exception as e:
                logger.warning(f"read_artifact: failed to fetch visualization profiles: {e}")

        # Build output
        output = ReadArtifactOutput(
            artifact_id=str(artifact.id),
            title=artifact.title,
            mode=artifact.mode,
            code=code_view,
            visualization_ids=visualization_ids,
            version=artifact.version,
            read_mode=read_mode,
            total_lines=total_lines,
            total_chars=total_chars,
            start_line=start_line,
            end_line=end_line,
            truncated=range_truncated,
            matches=matches,
            grep_total_matches=grep_total,
            grep_truncated=grep_truncated,
            outline=outline,
        ).model_dump()

        # Add UI preview fields (similar to describe_tables top_tables)
        output["artifact_preview"] = {
            "artifact_id": str(artifact.id),
            "title": artifact.title or "Untitled",
            "mode": artifact.mode,
            "version": artifact.version,
            "code_stats": {
                "chars": total_chars,
                "lines": total_lines,
            },
            "read_mode": read_mode,
            "visualization_ids": visualization_ids,
            "created_at": str(artifact.created_at) if artifact.created_at else None,
        }
        # Code for collapsible toggle (collapsed by default in UI).
        # For windowed reads show what the tool actually returned.
        if read_mode == "grep" and matches:
            preview_code = "\n\n".join(
                "\n".join([*m["before"], m["line"], *m["after"]]) for m in matches[:20]
            )
        elif read_mode in ("outline", "grep"):
            preview_code = outline or ""
        else:
            preview_code = code_view
        output["code_preview"] = {
            "language": "jsx",
            "code": preview_code,
            "collapsed_default": True,
        }

        # Build observation with the windowed view for context
        summary = f"Read artifact '{artifact.title or 'Untitled'}' ({artifact.mode}, v{artifact.version}, {total_chars:,} chars / {total_lines:,} lines, read_mode={read_mode})"
        if window_note:
            summary += f" — {window_note}"

        observation = {
            "summary": summary,
            "artifact_id": str(artifact.id),
            "title": artifact.title,
            "mode": artifact.mode,
            "read_mode": read_mode,
            "total_lines": total_lines,
            "total_chars": total_chars,
            "visualization_ids": visualization_ids,
            "visualization_profiles": viz_profiles,  # columns, sample rows (gated by allow_llm_see_data), data model
            "version": artifact.version,
            "runtime_environment": SANDBOX_RUNTIME_OBSERVATION,
        }
        # Available for 1 iteration; compacted by observation builder on next tool call
        if read_mode in ("full", "range"):
            observation["code"] = code_view
        if read_mode == "range":
            observation["start_line"] = start_line
            observation["end_line"] = end_line
            observation["truncated"] = range_truncated
        if matches is not None:
            observation["matches"] = matches
            observation["grep_total_matches"] = grep_total
            observation["grep_truncated"] = grep_truncated
        if outline is not None:
            observation["outline"] = outline

        # Include stored screenshot if requested, gated by privacy and vision support
        if data.load_screenshot:
            # Check model supports vision
            model = runtime_ctx.get("model")
            supports_vision = model and getattr(model, "supports_vision", False)

            if allow_llm_see_data and supports_vision and artifact.screenshot_base64:
                observation["images"] = [{
                    "data": artifact.screenshot_base64,
                    "media_type": "image/png",
                    "source_type": "base64",
                }]
                observation["summary"] += " (screenshot included)"

            # Include render errors if stored (useful even without screenshot)
            if artifact.render_errors:
                observation["render_errors"] = artifact.render_errors

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )
