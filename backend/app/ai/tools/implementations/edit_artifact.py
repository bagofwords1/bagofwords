"""
edit_artifact tool - Surgically edit an existing artifact's code using search/replace diffs.

Instead of regenerating the entire dashboard from scratch, this tool loads the existing
code and applies targeted changes based on the user's edit instruction.
"""

import asyncio
import difflib
import json
import logging
import re
from typing import AsyncIterator, Dict, Any, Type, List, Optional, Tuple
from uuid import uuid4

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
    ToolConfirmationEvent,
)
from app.ai.tools.confirmation import wait_for_confirmation
from app.ai.tools.schemas.edit_artifact import EditArtifactInput, EditArtifactOutput
from app.ai.llm import LLM
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.models.query import Query
from app.dependencies import async_session_maker
from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_PROMPT

logger = logging.getLogger(__name__)

# ─── Diff markers ────────────────────────────────────────────────────────────
SEARCH_MARKER = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"

DIFF_MARKER_PATTERNS = [
    re.compile(r'^<{6,}\s*SEARCH\s*$', re.MULTILINE),
    re.compile(r'^={6,}\s*$', re.MULTILINE),
    re.compile(r'^>{6,}\s*REPLACE\s*$', re.MULTILINE),
]


def _normalize_text(text: str) -> str:
    """Normalize text for matching: CRLF→LF, strip trailing whitespace per line,
    strip stray markdown fences, normalize tabs to spaces."""
    # CRLF → LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip stray markdown fences that LLMs sometimes leak into SEARCH blocks
    text = re.sub(r'^```[\w]*\s*$', '', text, flags=re.MULTILINE)
    # Tabs → 2 spaces (consistent with typical JSX/React style)
    text = text.replace("\t", "  ")
    # Strip trailing whitespace per line
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text


def _find_closest_match(search_text: str, code: str, max_context: int = 200) -> Optional[str]:
    """Find the closest matching region in code for a failed SEARCH block.
    Returns a human-readable hint showing what the code actually contains."""
    search_lines = search_text.strip().split("\n")
    code_lines = code.split("\n")

    if not search_lines or not code_lines:
        return None

    # Use SequenceMatcher to find the best matching region
    best_ratio = 0.0
    best_start = 0
    search_len = len(search_lines)

    for i in range(max(1, len(code_lines) - search_len + 1)):
        candidate = code_lines[i:i + search_len]
        ratio = difflib.SequenceMatcher(
            None,
            "\n".join(search_lines),
            "\n".join(candidate),
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio < 0.4:
        return None

    # Show the closest match with line numbers
    match_end = min(best_start + search_len, len(code_lines))
    matched_lines = code_lines[best_start:match_end]
    numbered = [f"  {best_start + j + 1}: {ln}" for j, ln in enumerate(matched_lines[:15])]
    if len(matched_lines) > 15:
        numbered.append(f"  ... ({len(matched_lines) - 15} more lines)")

    return (
        f"Closest match ({best_ratio:.0%} similar) at lines {best_start + 1}-{match_end}:\n"
        + "\n".join(numbered)
    )


def _validate_diff_structure(diff_text: str) -> Tuple[bool, str]:
    """Validate that SEARCH/REPLACE markers are properly paired and ordered.

    Returns:
        Tuple of (is_valid, error_message)
    """
    lines = diff_text.split("\n")
    state = "idle"  # idle -> in_search -> in_replace -> idle
    block_count = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == SEARCH_MARKER:
            if state != "idle":
                return False, f"Line {i + 1}: Found SEARCH marker while inside a block (state={state})"
            state = "in_search"
        elif stripped == DIVIDER_MARKER and state == "in_search":
            state = "in_replace"
        elif stripped == REPLACE_MARKER:
            if state != "in_replace":
                return False, f"Line {i + 1}: Found REPLACE marker without matching SEARCH/divider (state={state})"
            state = "idle"
            block_count += 1

    if state != "idle":
        return False, f"Unclosed block at end of output (state={state})"

    return True, ""


def _parse_diff_blocks(diff_text: str) -> List[Tuple[str, str]]:
    """Parse well-formed SEARCH/REPLACE blocks from diff text.
    Assumes structure has already been validated."""
    blocks = []
    lines = diff_text.split("\n")
    state = "idle"
    search_lines: List[str] = []
    replace_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == SEARCH_MARKER and state == "idle":
            state = "in_search"
            search_lines = []
            replace_lines = []
        elif stripped == DIVIDER_MARKER and state == "in_search":
            state = "in_replace"
        elif stripped == REPLACE_MARKER and state == "in_replace":
            blocks.append(("\n".join(search_lines), "\n".join(replace_lines)))
            state = "idle"
        elif state == "in_search":
            search_lines.append(line)
        elif state == "in_replace":
            replace_lines.append(line)

    return blocks


def _try_match_and_replace(code: str, search: str, replace: str) -> Optional[str]:
    """Try to find search text in code and replace it. Returns modified code or None.

    Tries in order:
    1. Exact match
    2. Normalized match (trailing whitespace, tabs→spaces)
    """
    # 1. Exact match
    if search in code:
        return code.replace(search, replace, 1)

    # 2. Normalized match
    norm_code = _normalize_text(code)
    norm_search = _normalize_text(search)

    if norm_search in norm_code:
        start_idx = norm_code.index(norm_search)
        # Map back to original code via line positions
        norm_lines_before = norm_code[:start_idx].count("\n")
        orig_lines = code.split("\n")
        search_line_count = search.split("\n").__len__()

        orig_start = norm_lines_before
        orig_end = orig_start + search_line_count

        if orig_end <= len(orig_lines):
            orig_chunk = "\n".join(orig_lines[orig_start:orig_end])
            return code.replace(orig_chunk, replace, 1)

    return None


def apply_search_replace_diff(
    existing_code: str, diff_text: str
) -> Tuple[str, bool, int, List[str]]:
    """Apply search/replace diff blocks to existing code (Aider-style).

    Validates structure, normalizes inputs, and applies all-or-nothing.
    If ANY block fails to match, the original code is returned unchanged.

    Args:
        existing_code: The original code to modify
        diff_text: The LLM output containing SEARCH/REPLACE blocks

    Returns:
        Tuple of (modified_code, all_blocks_applied, num_blocks_found, failure_details)
    """
    # Normalize inputs
    diff_text = diff_text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Structural validation
    is_valid, error_msg = _validate_diff_structure(diff_text)
    if not is_valid:
        logger.warning(f"edit_artifact: Invalid diff structure: {error_msg}")
        return existing_code, False, 0, [f"Malformed diff structure: {error_msg}"]

    # 2. Parse blocks
    blocks = _parse_diff_blocks(diff_text)
    if not blocks:
        return existing_code, False, 0, []

    # 3. All-or-nothing apply on a copy
    modified = existing_code
    failure_details: List[str] = []

    for idx, (search, replace) in enumerate(blocks):
        result = _try_match_and_replace(modified, search, replace)
        if result is not None:
            modified = result
        else:
            # Block failed — collect diagnostic info
            search_preview = search[:100].replace("\n", "\\n")
            detail = f"Block {idx + 1}/{len(blocks)} failed to match. SEARCH ({len(search)} chars): {search_preview}..."
            closest = _find_closest_match(search, existing_code)
            if closest:
                detail += f"\n{closest}"
            failure_details.append(detail)
            logger.warning(f"edit_artifact: {detail}")

    if failure_details:
        # All-or-nothing: discard all changes, return original
        return existing_code, False, len(blocks), failure_details

    return modified, True, len(blocks), []


def sanitize_code_output(code: str, mode: str = "page") -> Tuple[str, List[str]]:
    """Final safety net: strip diff markers and validate basic structure.

    Returns:
        Tuple of (sanitized_code, warnings)
    """
    warnings: List[str] = []

    # Strip any remaining diff markers (should never be in artifact code)
    for pattern in DIFF_MARKER_PATTERNS:
        if pattern.search(code):
            warnings.append("Stripped leftover diff markers from code output.")
            code = pattern.sub('', code)

    # Strip stray markdown fences
    code = re.sub(r'^```[\w]*\s*$', '', code, flags=re.MULTILINE)

    # Clean up excessive blank lines left by stripping
    code = re.sub(r'\n{4,}', '\n\n\n', code)
    code = code.strip()

    # Validate basic structure for page mode
    if mode == "page":
        if code and '<script' not in code.lower() and len(code) > 100:
            warnings.append("Page mode code is missing <script> tag — may not render correctly.")

    # Size check: reject suspiciously small code
    if len(code) < 4000 and not code.strip():
        warnings.append("Code output is empty after sanitization.")

    return code, warnings


class EditArtifactTool(Tool):
    """Tool for surgically editing existing artifact code.

    This tool loads the existing code and applies targeted search/replace diffs
    based on the user's instruction. For small, focused changes only — if the
    change is too large for diffs, the tool returns a failure so the planner
    can route to create_artifact instead.
    """

    def __init__(self):
        # Reuse methods from CreateArtifactTool (same pattern as MCP wrapper)
        from app.ai.tools.implementations.create_artifact import CreateArtifactTool
        self._create_tool = CreateArtifactTool()

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_artifact",
            description=(
                "Apply small, surgical edits to an existing artifact using search/replace diffs. "
                "Best for: tweaking colors, adjusting layout, fixing bugs, adding a single component. "
                "NOT for large redesigns or full rewrites — use create_artifact for those. "
                "Prioritize using read_artifact before editing an artifact. "
                "If the edit is adding a new visualization, you MUST ADD it as a parameter to the tool. "
                "Requires artifact_id from a previous create_artifact or read_artifact result. "
                "Do NOT ask the user for artifact IDs - extract them from the conversation context."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditArtifactInput.model_json_schema(),
            output_schema=EditArtifactOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=120,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["artifact", "dashboard", "edit"],
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditArtifactInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditArtifactOutput

    def _build_edit_prompt(
        self,
        existing_code: str,
        edit_prompt: str,
        mode: str,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str = "",
        messages_context: str = "",
        report_title: Optional[str] = None,
        image_count: int = 0,
        original_spec: Optional[str] = None,
    ) -> str:
        """Build the prompt for editing existing artifact code."""

        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        if mode == "slides":
            return self._build_slides_edit_prompt(
                existing_code=existing_code,
                edit_prompt=edit_prompt,
                viz_json=viz_json,
                instructions_context=instructions_context,
                messages_context=messages_context,
                report_title=report_title,
                image_count=image_count,
            )

        images_context = ""
        if image_count > 0:
            images_context = f"\n**Attached Images:** {image_count} image(s) provided for visual reference. Use these to understand the design intent, branding, color schemes, or layout preferences the user wants to incorporate."

        original_spec_section = ""
        if original_spec:
            original_spec_section = f"""
═══════════════════════════════════════════════════════════════════════════════
ORIGINAL DASHBOARD SPEC (accumulated requirements from previous iterations)
═══════════════════════════════════════════════════════════════════════════════

{original_spec}

IMPORTANT: The above spec describes what the dashboard should already look like.
Preserve ALL of these requirements while applying the new edit below.
"""

        return f"""You are editing an existing React dashboard. Your job is to apply the user's requested change with surgical precision. Do not rewrite code that does not need to change. Preserve all existing functionality, styling, layout, event handlers, and responsive behavior unless the user explicitly asked to change it.

{SANDBOX_RUNTIME_PROMPT}

═══════════════════════════════════════════════════════════════════════════════
EXISTING DASHBOARD CODE
═══════════════════════════════════════════════════════════════════════════════

```
{existing_code}
```
{original_spec_section}
═══════════════════════════════════════════════════════════════════════════════
USER'S EDIT REQUEST
═══════════════════════════════════════════════════════════════════════════════

{edit_prompt}

{f"**Report Title:** {report_title}" if report_title else ""}
{images_context}
{f"**Organization Instructions:**{chr(10)}{instructions_context}" if instructions_context else ""}
{f"**Conversation History:**{chr(10)}{messages_context}" if messages_context else ""}

═══════════════════════════════════════════════════════════════════════════════
VISUALIZATION DATA (for reference if the edit involves data access)
═══════════════════════════════════════════════════════════════════════════════

{viz_json}

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

Output SEARCH/REPLACE blocks. SEARCH must match existing code exactly.

<<<<<<< SEARCH
(exact lines from existing code)
=======
(replacement lines)
>>>>>>> REPLACE

Rules:
- Output ONLY SEARCH/REPLACE blocks — not the full file.
- Include 2-3 lines of context in SEARCH for unambiguous matching.
- Order blocks top to bottom.
- Preserve existing code unless the user asked to change it.
- For NEW charts, use `<EChart option={{...}} height={{N}} />` — 'bow' theme handles styling. Do NOT repeat theme defaults.
- Do NOT output full code. Only SEARCH/REPLACE blocks. If the change feels too large for diffs, output nothing — the planner will use create_artifact instead.
- Omit className on globals (KPICard, SectionCard, FilterSelect) when using light mode — defaults handle it.

⚠️ **KEEP EDITS MINIMAL.** Do not rewrite code that doesn't need to change. Do not add verbose comments or unnecessary variables.

═══════════════════════════════════════════════════════════════════════════════
DATA ACCESS (applies to ALL edits)
═══════════════════════════════════════════════════════════════════════════════

- `useArtifactData()` returns `{{ report, visualizations }}` or `null` while loading
- Each viz: `{{ id, title, columns: [{{headerName, field}}], rows: [{{...}}], view, dataModel }}`
- Access values: `row[column.field]`, display labels: `column.headerName`
- **NEVER hardcode data** — ALL values from `data.visualizations[N].rows`

═══════════════════════════════════════════════════════════════════════════════
FILTERING (if the edit involves adding, modifying, or fixing filters)
═══════════════════════════════════════════════════════════════════════════════

Use the built-in `useFilters()` hook — do NOT reimplement filter logic manually:

  const {{ filterableColumns, filters, setFilter, resetFilters, filterRows }} = useFilters();

- `filterableColumns`: auto-detected categorical columns (array of `{{ field, unique_values }}`)
- `filterRows(rows)`: returns rows matching active filters — call on each viz's rows
- `setFilter(field, value)`: apply a filter; `null` to clear
- `resetFilters()`: clear all active filters
- Filter state is shared globally — `setFilter` in one component updates `filterRows` everywhere
- Render filter UI using `filterableColumns`. Place filter bar `sticky top-0 z-50`
- Charts that should NOT be filtered can use `viz.rows` directly without `filterRows`
- If a viz does not have the filtered column, its rows pass through unaffected

Apply the user's edit now:"""

    def _build_slides_edit_prompt(
        self,
        existing_code: str,
        edit_prompt: str,
        viz_json: str,
        instructions_context: str = "",
        messages_context: str = "",
        report_title: Optional[str] = None,
        image_count: int = 0,
    ) -> str:
        """Build edit prompt for slides mode (python-pptx code)."""

        images_context = ""
        if image_count > 0:
            images_context = f"\n**Attached Images:** {image_count} image(s) provided for visual reference. Use these to understand the design intent, branding, color schemes, or layout preferences the user wants to incorporate."

        return f"""You are editing existing python-pptx presentation code. Apply the user's requested change with surgical precision. Preserve all existing slide structure, styling, and data access unless the user explicitly asked to change it.

═══════════════════════════════════════════════════════════════════════════════
EXISTING PYTHON-PPTX CODE
═══════════════════════════════════════════════════════════════════════════════

```python
{existing_code}
```

═══════════════════════════════════════════════════════════════════════════════
USER'S EDIT REQUEST
═══════════════════════════════════════════════════════════════════════════════

{edit_prompt}

{f"**Report Title:** {report_title}" if report_title else ""}
{images_context}
{f"**Organization Instructions:**{chr(10)}{instructions_context}" if instructions_context else ""}
{f"**Conversation History:**{chr(10)}{messages_context}" if messages_context else ""}

═══════════════════════════════════════════════════════════════════════════════
VISUALIZATION DATA (for reference)
═══════════════════════════════════════════════════════════════════════════════

{viz_json}

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

Output SEARCH/REPLACE blocks to make targeted changes:

<<<<<<< SEARCH
(exact lines from existing code)
=======
(replacement lines)
>>>>>>> REPLACE

Rules:
- SEARCH must exactly match consecutive lines from the existing code.
- Include 2-3 lines of context around each change for unambiguous matching.
- Multiple blocks allowed, ordered top to bottom.
- NEVER output full code — only SEARCH/REPLACE blocks. This tool is for surgical edits only.

Apply the edit now:"""

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = EditArtifactInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"artifact_id": data.artifact_id, "title": "Editing artifact"})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_artifact"})

        # Get runtime context
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        db = runtime_ctx.get("db")
        context_hub = runtime_ctx.get("context_hub")
        organization_settings = runtime_ctx.get("settings")

        # Check privacy setting
        allow_llm_see_data = True
        if organization_settings:
            try:
                allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
            except Exception:
                allow_llm_see_data = True

        # Load the existing artifact
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_artifact"})
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
                    "output": {"success": False, "error": f"Failed to load artifact: {str(e)}"},
                    "observation": {
                        "summary": f"Failed to load artifact: {str(e)}",
                        "error": {"type": "db_error", "message": str(e)},
                    },
                },
            )
            return

        if not artifact:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": f"Artifact {data.artifact_id} not found"},
                    "observation": {
                        "summary": f"Artifact not found: {data.artifact_id}",
                        "error": {"type": "not_found", "message": f"No artifact with id {data.artifact_id}"},
                    },
                },
            )
            return

        # Extract existing code and viz_ids
        content = artifact.content or {}
        existing_code = content.get("code", "")
        existing_viz_ids = content.get("visualization_ids", [])

        if not existing_code:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": "Artifact has no code to edit"},
                    "observation": {
                        "summary": "Artifact has no code to edit",
                        "error": {"type": "no_code", "message": "The artifact's content has no code field."},
                    },
                },
            )
            return

        # Merge visualization IDs: existing + any new ones from input + auto-discovered
        merged_viz_ids = list(existing_viz_ids)
        if data.visualization_ids:
            for vid in data.visualization_ids:
                if vid not in merged_viz_ids:
                    merged_viz_ids.append(vid)

        # Auto-merge: pick up any report vizs created after the artifact being edited
        report_id = str(report.id) if report else None
        if report_id:
            try:
                async with async_session_maker() as fresh_db:
                    new_vizs = await fresh_db.execute(
                        select(Visualization.id).where(
                            Visualization.report_id == report_id,
                            Visualization.created_at > artifact.created_at,
                        )
                    )
                    for (vid,) in new_vizs.all():
                        vid_str = str(vid)
                        if vid_str not in merged_viz_ids:
                            merged_viz_ids.append(vid_str)
                            logger.info(f"edit_artifact: auto-merged viz {vid_str} (created after artifact)")
            except Exception as e:
                logger.warning(f"edit_artifact: auto-merge viz query failed: {e}")

        # Fetch all visualizations (batched query, same as create_artifact)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_visualizations"})

        visualizations: List[Dict[str, Any]] = []
        warnings: List[str] = []

        try:
            from app.models.step import Step
            # Use a fresh session to ensure we see recently committed vizs/steps
            async with async_session_maker() as fresh_db:
                result = await fresh_db.execute(
                    select(Visualization)
                    .options(
                        selectinload(Visualization.query).selectinload(Query.default_step),
                        selectinload(Visualization.query).selectinload(Query.steps),
                    )
                    .where(Visualization.id.in_(merged_viz_ids))
                )
                fetched_vizs = {str(v.id): v for v in result.scalars().all()}
        except Exception as e:
            logger.exception("Failed to batch-fetch visualizations for edit_artifact")
            fetched_vizs = {}
            warnings.append(f"Error fetching visualizations: {str(e)}")

        # Process each viz
        for viz_id in merged_viz_ids:
            viz = fetched_vizs.get(viz_id)
            if viz is None:
                warnings.append(f"Visualization {viz_id} not found")
                continue

            if report_id and str(viz.report_id) != report_id:
                warnings.append(f"Visualization {viz_id} does not belong to this report")
                continue

            # Get step with data
            step = None
            if viz.query and viz.query.default_step:
                step = viz.query.default_step
            elif viz.query and viz.query.steps:
                step = viz.query.steps[-1] if viz.query.steps else None

            step_status = step.status if step else None
            if step_status != "success":
                warnings.append(f"Visualization {viz_id} skipped: step status is '{step_status or 'unknown'}'")
                continue

            step_data = step.data if step else {}
            rows = (step_data.get("rows") or [])[:100] if step_data else []
            raw_columns = step_data.get("columns") or [] if step_data else []
            data_model = step.data_model if step else {}

            view_dict = viz.view or {}
            query_id = str(viz.query_id) if viz.query_id else None

            ventry = {
                "id": str(viz.id),
                "title": viz.title,
                "query_id": query_id,
                "view": self._create_tool._trim_none(view_dict),
                "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                "columns": raw_columns,
                "row_count": len(rows),
                "rows": rows,
                "dataModel": data_model or {},
            }
            visualizations.append(ventry)

        # Build viz profiles with truncated sample rows for edit (3 instead of 5)
        viz_profiles = [self._create_tool._build_viz_profile(v, allow_llm_see_data) for v in visualizations]
        # Truncate sample_rows to 3 for edit mode to save tokens
        for profile in viz_profiles:
            if "sample_rows" in profile:
                profile["sample_rows"] = profile["sample_rows"][:3]

        # Emit confirmation request and wait for user approval
        confirmation_id = str(uuid4())
        yield ToolConfirmationEvent(type="tool.confirmation", payload={
            "stage": "awaiting_confirmation",
            "confirmation_id": confirmation_id,
            "title": artifact.title or "Untitled Artifact",
            "mode": artifact.mode,
            "visualizations": [
                {"id": v["id"], "title": v["title"], "type": v.get("data_model_type", "")}
                for v in visualizations
            ],
        })
        confirmation = await wait_for_confirmation(confirmation_id, timeout=5.0)
        if not confirmation.get("approved", True):
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"error": "User cancelled"},
                "observation": {"error": "User cancelled artifact edit"},
            })
            return
        # Apply any user edits (e.g., updated title)
        if confirmation.get("title"):
            artifact.title = confirmation["title"]
            await db.commit()

        # Emit visualizations_resolved so badges persist after confirmation
        yield ToolProgressEvent(type="tool.progress", payload={
            "stage": "visualizations_resolved",
            "tool_name": "edit_artifact",
            "visualizations": [
                {"id": v["id"], "title": v["title"], "type": v.get("data_model_type", "")}
                for v in visualizations
            ],
        })

        # Build instruction context
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "building_context"})
        instruction_context_builder = runtime_ctx.get("instruction_context_builder") or (
            getattr(context_hub, "instruction_builder", None) if context_hub else None
        )
        instructions_context = ""
        try:
            if instruction_context_builder is not None:
                inst_section = await instruction_context_builder.build(categories=["dashboard", "visualization", "general"])
                instructions_context = inst_section.render() or ""
        except Exception:
            pass

        # Get conversation history context
        context_view = runtime_ctx.get("context_view")
        messages_context = ""
        try:
            _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
            messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        except Exception as e:
            logger.warning(f"Failed to extract messages context in edit_artifact: {e}")
            messages_context = ""

        # Load images attached to the head completion for vision-capable models
        head_completion = runtime_ctx.get("head_completion")
        head_completion_id = str(head_completion.id) if head_completion else None
        completion_images = await self._create_tool._load_completion_images(db, head_completion_id)

        # Validate model supports vision if images are present
        model = runtime_ctx.get("model")
        if completion_images and not getattr(model, "supports_vision", False):
            logger.info(f"Model doesn't support vision, skipping {len(completion_images)} completion images")
            completion_images = []

        # Build the edit prompt
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_edit"})

        prompt = self._build_edit_prompt(
            existing_code=existing_code,
            edit_prompt=data.edit_prompt,
            mode=artifact.mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            messages_context=messages_context,
            report_title=getattr(report, 'title', None) if report else None,
            image_count=len(completion_images),
            original_spec=artifact.generation_prompt,
        )

        # Stream LLM response
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_generating"})
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        buffer = ""

        async for chunk in llm.inference_stream(
            prompt,
            images=completion_images if completion_images else None,
            usage_scope="edit_artifact",
            usage_scope_ref_id=str(report.id) if report else None,
        ):
            buffer += chunk
            if len(buffer) % 100 == 0:
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={"stage": "generating", "chars": len(buffer), "timing": False}
                )

        # Apply the diff
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "applying_edit"})

        new_code, diff_applied, num_blocks, failure_details = apply_search_replace_diff(existing_code, buffer)

        if num_blocks == 0:
            # No diff blocks found — reject full rewrites, edit_artifact is for surgical diffs only.
            # The planner should use create_artifact for large changes.
            logger.warning("edit_artifact: No SEARCH/REPLACE blocks found. Change is too large for surgical edit.")
            new_code = existing_code
            diff_applied = False
            warnings.append(
                "This change is too large for edit_artifact (no surgical diffs could be produced). "
                "The artifact was NOT modified. Use create_artifact to rebuild the dashboard with this change instead."
            )

        elif not diff_applied:
            # Diff blocks found but some/all failed to match — return original unchanged (Aider-style)
            # Do NOT attempt _extract_code fallback here — that's what causes marker leakage
            new_code = existing_code
            logger.warning(
                f"edit_artifact: {num_blocks} diff blocks found but failed to apply. "
                f"Returning original code unchanged. Failures: {failure_details}"
            )
            warnings.append(
                f"Edit failed: {len(failure_details)} of {num_blocks} SEARCH/REPLACE block(s) could not be matched. "
                "The artifact was NOT modified. Review the failure details and retry with corrected SEARCH text."
            )
            for detail in failure_details:
                warnings.append(detail)

        # Output sanitization: strip any leaked diff markers, validate structure
        new_code, sanitize_warnings = sanitize_code_output(new_code, mode=artifact.mode)
        warnings.extend(sanitize_warnings)

        # Size guard: reject suspiciously small output
        if len(new_code) < 4000 and len(existing_code) >= 4000:
            logger.warning(
                f"edit_artifact: Output code ({len(new_code)} chars) is suspiciously small "
                f"compared to original ({len(existing_code)} chars). Keeping original."
            )
            warnings.append(
                f"Output code was only {len(new_code)} chars vs original {len(existing_code)} chars. "
                "This looks like a failed edit. The artifact was NOT modified."
            )
            new_code = existing_code
            diff_applied = False

        # Update title if provided
        new_title = data.title or artifact.title

        # Create a NEW artifact record (preserves version history for the frontend dropdown)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving_artifact"})

        included_viz_ids = [v["id"] for v in visualizations]
        new_version = artifact.version + 1

        # Accumulate generation_prompt: merge previous spec with current edit
        prev_spec = artifact.generation_prompt or ""
        accumulated_spec = f"{prev_spec}\n+ Edit (v{new_version}): {data.edit_prompt}".strip()

        new_artifact = Artifact(
            report_id=artifact.report_id,
            user_id=str(user.id) if user else artifact.user_id,
            organization_id=artifact.organization_id,
            title=new_title,
            mode=artifact.mode,
            content={"code": new_code, "visualization_ids": included_viz_ids},
            generation_prompt=accumulated_spec,
            version=new_version,
            status="completed",
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)

        # Page mode: take preview screenshot for planner reflection + generate thumbnail
        screenshot_base64: Optional[str] = None
        render_errors: list[str] = []
        if new_artifact.mode == "page":
            artifact_data = {
                "report": {
                    "id": str(report.id) if report else None,
                    "title": getattr(report, "title", None) if report else None,
                    "theme": getattr(report, "theme", None) if report else None,
                },
                "visualizations": visualizations,
            }
            thumbnail_html = self._create_tool._build_thumbnail_html(artifact_data, new_code, mode=new_artifact.mode)

            # Take preview screenshot (synchronous, ~3-5s) if model supports vision
            model = runtime_ctx.get("model")
            if allow_llm_see_data and model and getattr(model, "supports_vision", False):
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "capturing_preview"})
                screenshot_base64, render_errors = await self._create_tool._take_preview_screenshot(thumbnail_html)

            # Persist screenshot and render errors on artifact for later retrieval (read_artifact)
            if screenshot_base64 or render_errors:
                new_artifact.screenshot_base64 = screenshot_base64
                new_artifact.render_errors = render_errors or None
                await db.commit()

            # Generate thumbnail in background (for stored thumbnail, non-blocking)
            asyncio.create_task(
                self._create_tool._generate_thumbnail_background(
                    artifact_id=str(new_artifact.id),
                    html_content=thumbnail_html,
                    mode=new_artifact.mode,
                )
            )

        # Build output
        output = EditArtifactOutput(
            artifact_id=str(new_artifact.id),
            code=new_code,
            mode=new_artifact.mode,
            title=new_title,
            version=new_version,
            diff_applied=diff_applied,
        ).model_dump()

        # Add UI preview fields
        code_lines = new_code.count('\n') + 1 if new_code else 0
        output["artifact_preview"] = {
            "artifact_id": str(new_artifact.id),
            "title": new_title or "Untitled",
            "mode": new_artifact.mode,
            "version": new_version,
            "code_stats": {
                "chars": len(new_code),
                "lines": code_lines,
            },
            "visualization_ids": included_viz_ids,
            "visualization_count": len(visualizations),
            "diff_applied": diff_applied,
        }
        output["code_preview"] = {
            "language": "jsx" if new_artifact.mode == "page" else "python",
            "code": new_code,
            "collapsed_default": True,
        }

        # Build observation
        summary_msg = f"Edited artifact '{new_title or 'Untitled'}' (v{new_version})"
        if diff_applied:
            summary_msg += f" — applied {num_blocks} surgical edit(s)"
        else:
            summary_msg += " — fell back to full rewrite"
        if render_errors:
            summary_msg += f". RENDER FAILED with {len(render_errors)} error(s): {render_errors[0]}"
            if len(render_errors) > 1:
                summary_msg += f" (and {len(render_errors) - 1} more)"
            summary_msg += ". The dashboard code has a bug — use edit_artifact to fix the specific error."
        elif screenshot_base64:
            summary_msg += ". Screenshot of the rendered dashboard is attached — review it for visual correctness."

        observation: Dict[str, Any] = {
            "summary": summary_msg,
            "artifact_id": str(new_artifact.id),
            "mode": new_artifact.mode,
            "version": new_version,
            "diff_applied": diff_applied,
            "visualization_count": len(visualizations),
            "visualization_ids": included_viz_ids,
        }
        if render_errors:
            observation["render_errors"] = render_errors

        # Add preview screenshot for planner reflection (page mode)
        if screenshot_base64:
            observation["images"] = [{
                "data": screenshot_base64,
                "media_type": "image/png",
                "source_type": "base64",
            }]

        if warnings:
            observation["warnings"] = warnings

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            }
        )
