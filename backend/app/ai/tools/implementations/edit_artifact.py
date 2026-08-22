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
import time
from pathlib import Path
from typing import AsyncIterator, Dict, Any, Type, List, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.edit_artifact import EditArtifactInput, EditArtifactOutput
from app.ai.tools.implementations._artifact_images import load_image_bytes
from app.ai.code_execution.pptx_executor import PptxPreviewService
from app.ai.llm import LLM
from app.ai.llm.types import Message, TextDeltaEvent
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.models.query import Query
from app.dependencies import async_session_maker
from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_PROMPT
from app.ai.prompt_language import build_language_directive

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

    # Fix double-brace pattern: function App() {\n{ ... }\n}
    code = re.sub(
        r'(function\s+\w+\s*\([^)]*\)\s*\{)\s*\n\s*\{',
        r'\1',
        code,
    )
    code = re.sub(
        r'\}\s*\n\s*\}\s*\n(\s*ReactDOM\.createRoot)',
        r'}\n\1',
        code,
    )

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
            allowed_modes=["chat"],
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
        organization_settings: Any = None,
        files: Optional[List[Dict[str, Any]]] = None,
        removed_vizs: Optional[List[Dict[str, str]]] = None,
        prev_render_errors: Optional[List[str]] = None,
    ) -> str:
        """Build the dynamic user prompt for editing existing artifact code.

        Page mode deliberately omits the conversation history and the
        accumulated spec: the planner already distills intent into
        edit_prompt, and after a diff edit the code IS the current spec.
        Re-sending both grew edit prompts unboundedly (38K→63K tokens by
        v11+ in production) and caused context-length failures. Static
        reference material lives in _build_edit_system_prompt.
        """

        viz_json = json.dumps(viz_profiles, indent=2, default=str)
        language_directive = build_language_directive(organization_settings)

        if mode == "slides":
            return self._build_slides_edit_prompt(
                existing_code=existing_code,
                edit_prompt=edit_prompt,
                viz_json=viz_json,
                instructions_context=instructions_context,
                messages_context=messages_context,
                report_title=report_title,
                image_count=image_count,
                language_directive=language_directive,
            )

        images_context = ""
        if image_count > 0:
            images_context = f"\n**Attached Images:** {image_count} image(s) provided for visual reference. Use these to understand the design intent, branding, color schemes, or layout preferences the user wants to incorporate."

        files_context = ""
        if files:
            lines = [
                f'- id="{f["id"]}"  type={f.get("content_type", "")}  name={f.get("filename", "")}'
                for f in files
            ]
            files_context = (
                "\n**Embedded Files (render with `<BowFile id=\"<id>\" />`):** These files are "
                "available on this artifact. Keep existing <BowFile> elements; add <BowFile> for any "
                "new id the edit asks to show. Images render inline; PDFs in a viewer. Available files:\n"
                + "\n".join(lines)
            )

        removed_section = ""
        if removed_vizs:
            removed_list = "\n".join(f'- "{rv.get("title", "Unknown")}" (id={rv.get("id")})' for rv in removed_vizs)
            removed_section = f"""
**REMOVED VISUALIZATIONS — delete their code:** The following visualizations have been REMOVED from this artifact's data payload:
{removed_list}
Delete every code section that references them: charts, KPI cards, tables, filters fed by their columns, and any derived computations. CRITICAL: `data.visualizations` is re-indexed after removal — its current order is exactly the VISUALIZATION DATA list below. Update every `viz[N]` index reference in the surviving code to match the new order.
"""

        render_errors_section = ""
        if prev_render_errors:
            errors_list = "\n".join(f"- {e}" for e in prev_render_errors[:5])
            render_errors_section = f"""
**KNOWN RENDER ERRORS in the current version:** The existing code produces these errors when rendered:
{errors_list}
If the edit request addresses them, these are the exact errors to fix. Either way, your edit must not reproduce them.
"""

        return f"""═══════════════════════════════════════════════════════════════════════════════
EDIT REQUEST (primary specification — follow exactly)
═══════════════════════════════════════════════════════════════════════════════

{edit_prompt}

{f"**Report Title:** {report_title}" if report_title else ""}
{images_context}
{files_context}
{f"**Organization Instructions:**{chr(10)}{instructions_context}" if instructions_context else ""}
{language_directive}
{removed_section}{render_errors_section}
EXISTING DASHBOARD CODE:

```
{existing_code}
```

VISUALIZATION DATA (current visualizations in data.visualizations order — for reference if the edit involves data access):

{viz_json}

(Note: `rows`/`sample_rows` above are a sample capped at 100 rows; `row_count` is the true dataset size the live dashboard receives.)

Apply the user's edit now:"""

    def _build_edit_system_prompt(self) -> str:
        """Static system prompt for dashboard edits.

        Only stable reference material — kept free of per-call state so
        provider prompt caching reuses it across every edit call.
        """
        return f"""You are editing an existing React dashboard. Apply the user's requested change with surgical precision. Do not rewrite code that does not need to change. Preserve all existing functionality, styling, layout, event handlers, and responsive behavior unless the user explicitly asked to change it.

═══════════════════════════════════════════════════════════════════════════════
REFERENCE — TOOLS, COMPONENTS & DATA
═══════════════════════════════════════════════════════════════════════════════

{SANDBOX_RUNTIME_PROMPT}

DATA ACCESS:
- `useArtifactData()` returns `{{ report, visualizations }}` or `null` while loading
- Each viz: `{{ id, title, columns: [{{headerName, field, dtype, unique_count}}], rows: [{{...}}], view, dataModel }}`
- Access values: `row[column.field]`, display labels: `column.headerName`
- Column metadata includes `dtype` (pandas type) and `unique_count` — use these for filter/format decisions
- **Sample vs full data:** `rows` in the user message are a SAMPLE (capped at 100) for editing context; at runtime the dashboard receives the FULL dataset — `row_count` rows. Never write workarounds for the sample size.
- **NEVER hardcode data** — ALL values from `data.visualizations[N].rows`
- **DEFENSIVE CODING**: Row values can be `null`/`undefined`. ALWAYS guard before string methods: `(val || '').includes('x')` or `String(val ?? '').toLowerCase()`. Never call `.includes()`, `.toLowerCase()`, `.startsWith()`, `.split()` on a potentially nullish value.

View hints — follow the viz config:
Inspect `view.aggregation`, `view.seriesStyles[i].aggregation`, and `view.defaultFilters` before rendering. These describe the author's intent for granular data:

- `view.aggregation` (`"sum"|"avg"|"count"|"min"|"max"`): aggregate rows with `reduce`/`Map` before rendering (e.g. metric cards, pies, heatmaps). Avoid reading `rows[0]` as the value when aggregation is set.
- `view.seriesStyles[i].aggregation`: per-series aggregation — apply when building each series in multi-series bar/line/area charts.
- `view.defaultFilters` (array of `{{column, operator, value}}`): seed these into `useFilters()` on first mount with `setFilter()` so the dashboard opens already filtered. Render the filtered rows when defaults are declared.

AVAILABLE COMPONENTS (convenience shortcuts — not requirements):
- `<KPICard>` — `className` replaces default theme (bg-white, border, text-slate-900). `titleClassName`/`subtitleClassName` replace text defaults. `style` for inline overrides. Theme to match the dashboard's color story.
- `<SectionCard>` — same theming props as KPICard. `className` replaces defaults.
- `<FilterSelect>` — portaled dropdown. `className` replaces default theme. Built-in search at 8+ options.
- `<FilterSearch>`, `<FilterDateRange>` — `className` replaces default theme.
- `fmt()`, `<LoadingSpinner>`
- All components are fully themeable. When the user's design calls for something these can't express — build custom React + Tailwind.
- **`viz` prop:** `<KPICard>` and `<SectionCard>` accept `viz={{viz[N]}}` — this renders a built-in "ⓘ" info popover (Data tab with rows, Code tab with the query). When adding new cards from a visualization, pass `viz={{viz[N]}}`. When an edit touches an existing card that lacks it, add `viz={{viz[N]}}` too. If the card renders FILTERED rows (`filterRows(viz[N].rows)`), also pass `rows={{<filtered rows>}}` so the popover's Data tab matches what's shown. If the card aggregates/derives its value, also pass `calc="<formula>"` (e.g. `calc="SUM(UnitPrice × Quantity) grouped by GenreName"`) — shown as a "Calculation" line in the popover. For CUSTOM markup (your own div tiles/charts/tables, not the prebuilt components), annotate each item's outer element with `data-bow-viz="N"` and `data-bow-calc="<formula>"` instead — a global overlay renders the same popover on those.

DATA-CAPABILITY CHECK — DO THIS FIRST, BEFORE GENERATING DIFFS:
Before producing any SEARCH/REPLACE blocks, verify the edit is achievable with the visualization data available. An edit that adds a filter/chart/KPI referencing a column that doesn't exist in any viz will silently break — surfacing the gap is far better than producing a broken diff.

1. Read the edit request and enumerate any new data-dependent elements: filters, chart axes/series, KPI values, groupings, sort keys.
2. For each new element, check VISUALIZATION DATA below: does the required column exist in the relevant viz's `columns` array?
3. Decide:
   - All required columns exist → proceed with the edit.
   - A required column is missing → **STOP. Do not emit diffs.** Output a single line starting with `DATA_GAP:` describing what's missing and which viz needs to be recreated. Example: `DATA_GAP: Cannot add customer filter — payments viz (id=...) lacks customer_id column. Recreate the payments query with customer_id projected, then retry edit_artifact.` The planner will handle recreation and come back.

FILTERING (if the edit involves adding, modifying, or fixing filters):

Use the built-in `useFilters()` hook — do NOT reimplement filter logic manually:

  const {{ filters, setFilter, resetFilters, filterRows }} = useFilters();

- `filters`: current state `{{ [field]: value }}`. Array = categorical selection, string = search text.
- `setFilter(field, value)`: set a filter; `null` or `""` to clear. Array for categorical, string for search.
- `resetFilters()`: clear all active filters
- `filterRows(rows, fieldMap?)`: returns filtered rows. Optional `fieldMap` remaps filter keys to viz-specific column names, e.g. `filterRows(rows, {{ country: 'CountryName' }})`.
- Array values (FilterSelect): exact match — row passes if value is in array
- String values (FilterSearch): case-insensitive substring match
- `{{ from, to }}` values (FilterDateRange): string comparison range
- Filter state is shared globally — `setFilter` in one component updates `filterRows` everywhere
- YOU choose which columns to filter using `dtype` and `unique_count` from column metadata — no auto-detection

FILTER FEASIBILITY (same as data-capability check, applied to filters):
- A filter only works on vizs whose `columns` include that field (directly or via `fieldMap`). Before wiring a global filter, list which vizs should be affected and confirm each has the column.
- If some participating vizs lack the column, that's a DATA_GAP — do not wire a filter that silently no-ops on those vizs. Surface the gap and stop.
- Never call `filterRows` on a viz that lacks the filter column "just in case" — silent pass-through is what makes dead filters invisible.

FILTER PLACEMENT — global vs local:
- **Global filter** (column in 2+ vizs after feasibility check): place in a top-level filter bar. Use `fieldMap` if column names differ across vizs.
- **Local filter** (column in only 1 viz): place INSIDE that viz's `<SectionCard>`.

FILTER DATA FLOW — CRITICAL:
- Every viz that passes the feasibility check for a filter MUST use `filterRows()` as its data source — for charts, tables, AND any KPI/summary derived from that viz.
- KPI cards summarizing filtered data MUST be computed from filtered rows, NEVER from raw `viz[N].rows`.

WHEN EDITING FILTERS: audit every data derivation in the existing code (useMemo, .map(), chart option builders, KPI computations). If it reads from viz[N].rows and the viz should be filtered (AND has the filter column), switch it to filterRows(viz[N].rows). Check useMemo dependencies — they must include the filtered result, not the raw viz object.

═══════════════════════════════════════════════════════════════════════════════
DESIGN GUIDANCE (for style/theme edits)
═══════════════════════════════════════════════════════════════════════════════

When the edit involves style or theme changes:
- Follow the user's style request exactly — if they say dark, flat, colorful, minimal, do that.
- Theme ALL components to match: use `className`, `titleClassName`, `subtitleClassName` on KPICard/SectionCard/FilterSelect to replace defaults. Don't leave white/slate defaults when the design calls for something else.
- Maintain a cohesive color story — edits to one section's colors should harmonize with the rest.
- If adding new visual elements, match the existing dashboard's design language unless the user asked to change it.

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
- For NEW charts, use `<EChart option={{...}} height={{N}} />` — supports ALL ECharts types. 'bow' theme handles base styling.
- Do NOT output full code. Only SEARCH/REPLACE blocks. If the change feels too large for diffs, output nothing — the planner will use create_artifact instead.

⚠️ **Implement the user's full request.** Don't skip requested changes to save tokens. Don't rewrite code the user didn't ask to change."""

    def _build_slides_edit_prompt(
        self,
        existing_code: str,
        edit_prompt: str,
        viz_json: str,
        instructions_context: str = "",
        messages_context: str = "",
        report_title: Optional[str] = None,
        image_count: int = 0,
        language_directive: str = "",
    ) -> str:
        """Build edit prompt for slides mode (python-pptx code)."""

        images_context = ""
        if image_count > 0:
            images_context = f"\n**Attached Images:** {image_count} image(s) provided for visual reference. Use these to understand the design intent, branding, color schemes, or layout preferences the user wants to incorporate."

        return f"""You are editing existing python-pptx presentation code. Apply the user's requested change with surgical precision. Preserve all existing slide structure, styling, and data access unless the user explicitly asked to change it.{language_directive}

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

Deck rules that still apply to an edit:
- **Keep the deck one visual system.** Reuse the palette, type scale, margins
  and motif already in the code. A new slide styled differently from its
  neighbours is worse than no new slide.
- **Titles state the finding, not the topic** — "EMEA drove all of Q3 growth",
  not "Revenue by Region". Under ~12 words.
- **One idea per slide**; at most 5 bullets of 2 lines. Detail goes in speaker
  notes (`slide.notes_slide`), not smaller body text.
- **Never invent a number** — figures must match the visualization data below.
- **Stay inside the canvas**: 13.333in x 7.5in, nothing within 0.5in of an
  edge, and check `top + height` on the last row of anything you add.
- **Guard chart data**: `CategoryChartData` raises on an empty category list,
  and that failure loses the whole deck — wrap new charts in `if rows:`.
- **On a dark background set chart label colors explicitly** (chart.font,
  category_axis/value_axis tick_labels) or they render near-black and vanish.
- **Images**: `image(file_id)` returns a fresh stream for
  `slide.shapes.add_picture(...)`; `image_ids` lists what is available. There is
  no filesystem access. Put a translucent scrim between a photo and any text
  over it.

Apply the edit now:"""

    async def _retry_failed_diff(
        self,
        edit_prompt: str,
        existing_code: str,
        failed_diff: str,
        failure_details: List[str],
        runtime_ctx: Dict[str, Any],
    ) -> Optional[str]:
        """One compact in-tool retry for SEARCH/REPLACE blocks that didn't match.

        Feeds the per-block failure diagnostics (with closest-match hints)
        back to the model so it can correct the SEARCH text against the real
        code. Returns the new diff text, or None if unavailable.
        """
        sigkill_event = runtime_ctx.get("sigkill_event")
        if sigkill_event and sigkill_event.is_set():
            return None

        failures_text = "\n\n".join(failure_details[:5])
        retry_prompt = f"""You produced SEARCH/REPLACE diff blocks for the edit request below, but some SEARCH blocks did not match the actual code. The artifact was NOT modified.

EDIT REQUEST:
{edit_prompt}

MATCH FAILURES (with closest-match hints from the real code):
{failures_text}

ACTUAL CURRENT CODE:
```
{existing_code}
```

YOUR PREVIOUS (failed) DIFF:
{failed_diff}

Re-emit corrected SEARCH/REPLACE blocks for the SAME edit. Copy SEARCH text exactly from the ACTUAL CURRENT CODE above (2-3 lines of context, byte-exact). Output ONLY the blocks:

<<<<<<< SEARCH
(exact lines from actual code)
=======
(replacement lines)
>>>>>>> REPLACE"""

        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        try:
            chunks: list[str] = []
            async for evt in llm.inference_stream_v2(
                messages=[Message(role="user", content=retry_prompt)],
                usage_scope="edit_artifact_diff_retry",
            ):
                if sigkill_event and sigkill_event.is_set():
                    return None
                if isinstance(evt, TextDeltaEvent):
                    chunks.append(evt.text)
            out = "".join(chunks)
            return out if out.strip() else None
        except Exception:
            logger.exception("edit_artifact: diff retry failed")
            return None

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = EditArtifactInput(**tool_input)
        # Repair budget: leave headroom under the runner's 300s hard timeout.
        _repair_deadline = time.monotonic() + 210

        yield ToolStartEvent(type="tool.start", payload={"artifact_id": data.artifact_id, "title": "Editing artifact"})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_artifact"})

        # Get runtime context
        sigkill_event = runtime_ctx.get("sigkill_event")
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
                select(Artifact)
                .options(lazyload("*"))
                .where(
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

        if artifact.mode == "doc":
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error": f"Artifact {data.artifact_id} is a document (mode='doc'). Use edit_doc to edit documents.",
                    },
                    "observation": {
                        "summary": f"Artifact {data.artifact_id} is a document — use edit_doc instead of edit_artifact.",
                        "error": {"type": "wrong_tool", "message": "edit_artifact only edits dashboards/slides; call edit_doc for documents."},
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

        # Merge embedded files: existing + any new ones from input (dedupe by id).
        existing_files = content.get("files", []) or []
        merged_files = list(existing_files)
        seen_file_ids = {str(f.get("id")) for f in merged_files if isinstance(f, dict)}
        new_file_ids = getattr(data, "file_ids", None) or []
        if new_file_ids:
            try:
                from app.models.file import File as _File
                frows = await db.execute(
                    select(_File).where(
                        _File.id.in_([str(x) for x in new_file_ids]),
                        _File.organization_id == str(organization.id) if organization else _File.organization_id.is_(None),
                    )
                )
                fetched = {str(f.id): f for f in frows.scalars().all()}
            except Exception as e:
                logger.warning(f"edit_artifact: failed to fetch files: {e}")
                fetched = {}
            for fid in new_file_ids:
                f = fetched.get(str(fid))
                if f is not None and str(f.id) not in seen_file_ids:
                    merged_files.append({
                        "id": str(f.id),
                        "content_type": f.content_type or "application/octet-stream",
                        "filename": f.filename,
                    })
                    seen_file_ids.add(str(f.id))

        # Merge visualization IDs: (existing − removed) + new. Removal is the
        # only way stale visualizations ever leave an artifact — without it,
        # replace-flows shipped both the old and the new viz forever.
        removed_viz_ids = {str(v) for v in (data.remove_visualization_ids or [])}
        merged_viz_ids = [v for v in existing_viz_ids if str(v) not in removed_viz_ids]
        if data.visualization_ids:
            for vid in data.visualization_ids:
                if vid not in merged_viz_ids and str(vid) not in removed_viz_ids:
                    merged_viz_ids.append(vid)

        # Auto-merge: pick up report vizs created during THIS turn that the
        # planner forgot to pass. Scoped to the current head completion —
        # the old version swept in anything created since the artifact
        # existed, which grafted unrelated explorations onto the dashboard
        # and broke replace-flows.
        report_id = str(report.id) if report else None
        head_completion = runtime_ctx.get("head_completion")
        _turn_started_at = getattr(head_completion, "created_at", None)
        if report_id and _turn_started_at is not None:
            try:
                async with async_session_maker() as fresh_db:
                    new_vizs = await fresh_db.execute(
                        select(Visualization.id).where(
                            Visualization.report_id == report_id,
                            Visualization.created_at > artifact.created_at,
                            Visualization.created_at >= _turn_started_at,
                        )
                    )
                    for (vid,) in new_vizs.all():
                        vid_str = str(vid)
                        if vid_str not in merged_viz_ids and vid_str not in removed_viz_ids:
                            merged_viz_ids.append(vid_str)
                            logger.info(f"edit_artifact: auto-merged viz {vid_str} (created this turn)")
            except Exception as e:
                logger.warning(f"edit_artifact: auto-merge viz query failed: {e}")

        # Resolve titles of removed vizs so the prompt can instruct deletion
        # of their code sections (the model needs names, not UUIDs).
        removed_viz_info: List[Dict[str, str]] = []
        if removed_viz_ids:
            try:
                async with async_session_maker() as fresh_db:
                    _removed_rows = await fresh_db.execute(
                        select(Visualization.id, Visualization.title).where(
                            Visualization.id.in_(list(removed_viz_ids))
                        )
                    )
                    _found_removed = {str(rid): (rtitle or "Untitled") for rid, rtitle in _removed_rows.all()}
            except Exception as e:
                logger.warning(f"edit_artifact: failed to resolve removed viz titles: {e}")
                _found_removed = {}
            for rid in removed_viz_ids:
                removed_viz_info.append({"id": rid, "title": _found_removed.get(rid, "Unknown")})

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
                        lazyload("*"),
                        selectinload(Visualization.query).options(
                            lazyload("*"),
                            selectinload(Query.default_step).options(lazyload("*")),
                            selectinload(Query.steps).options(lazyload("*")),
                        ),
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
            _all_rows = (step_data.get("rows") or []) if step_data else []
            rows = _all_rows[:100]
            raw_columns = step_data.get("columns") or [] if step_data else []
            data_model = step.data_model if step else {}
            step_info = step_data.get("info") or {} if step_data else {}
            column_info = step_info.get("column_info") or {}

            view_dict = viz.view or {}
            query_id = str(viz.query_id) if viz.query_id else None

            ventry = {
                "id": str(viz.id),
                "title": viz.title,
                "query_id": query_id,
                "view": self._create_tool._trim_none(view_dict),
                "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                "columns": raw_columns,
                "column_info": column_info,
                # row_count = TRUE dataset size; rows is a 100-row sample
                # (see create_artifact — same contract).
                "row_count": len(_all_rows),
                "sample_row_count": len(rows),
                "rows": rows,
                "dataModel": data_model or {},
                # Declared query parameters (see create_artifact — same contract)
                "parameters": list(getattr(viz.query, "parameters", None) or []) if viz.query else [],
            }
            visualizations.append(ventry)

        # Build viz profiles with truncated sample rows for edit (3 instead of 5)
        viz_profiles = [self._create_tool._build_viz_profile(v, allow_llm_see_data) for v in visualizations]
        # Truncate sample_rows to 3 for edit mode to save tokens
        for profile in viz_profiles:
            if "sample_rows" in profile:
                profile["sample_rows"] = profile["sample_rows"][:3]

        # Emit visualizations_resolved
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

        # Feed the edited version's known render errors forward so a repair
        # edit sees the exact failure instead of a planner paraphrase.
        prev_render_errors = list(getattr(artifact, "render_errors", None) or [])

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
            organization_settings=organization_settings,
            files=merged_files,
            removed_vizs=removed_viz_info,
            prev_render_errors=prev_render_errors,
        )
        # Static reference goes in the system prompt (cached provider-side);
        # slides keeps its single-prompt path.
        system_prompt = self._build_edit_system_prompt() if artifact.mode == "page" else None

        # Attach the broken version's screenshot when this edit is a repair —
        # gated on privacy + vision like every other screenshot attachment.
        _edit_images: List[Any] = list(completion_images) if completion_images else []
        _prev_screenshot = getattr(artifact, "screenshot_base64", None)
        if (
            prev_render_errors
            and _prev_screenshot
            and allow_llm_see_data
            and model
            and getattr(model, "supports_vision", False)
        ):
            from app.ai.llm.types import ImageInput as _ImageInput
            _edit_images.append(_ImageInput(data=_prev_screenshot, media_type="image/png", source_type="base64"))

        # Stream LLM response
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_generating"})
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        buffer = ""

        async for evt in llm.inference_stream_v2(
            messages=[Message(role="user", content=prompt)],
            system=system_prompt,
            images=_edit_images if _edit_images else None,
            usage_scope="edit_artifact",
            usage_scope_ref_id=str(report.id) if report else None,
        ):
            if sigkill_event and sigkill_event.is_set():
                break
            if isinstance(evt, TextDeltaEvent):
                buffer += evt.text
            if len(buffer) % 100 == 0:
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={"stage": "generating", "chars": len(buffer), "timing": False}
                )

        # Check sigkill after LLM generation
        if sigkill_event and sigkill_event.is_set():
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "artifact_id": str(artifact.id), "error": "Stopped by user"},
                    "observation": {"summary": "Artifact edit stopped by user", "artifact_id": str(artifact.id), "stopped": True},
                },
            )
            return

        # Detect DATA_GAP signal — LLM refused the edit because required columns are missing.
        # This must route the planner back to create_data, NOT to create_artifact.
        data_gap_match = re.search(r"^DATA_GAP:\s*(.+?)$", buffer, re.MULTILINE)
        if data_gap_match:
            gap_message = data_gap_match.group(1).strip()
            logger.info(f"edit_artifact: DATA_GAP detected — {gap_message}")
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "artifact_id": str(artifact.id),
                        "error": gap_message,
                    },
                    "observation": {
                        "summary": f"Edit blocked — data gap: {gap_message}",
                        "error": {
                            "type": "data_gap",
                            "message": gap_message,
                            "remediation": "Recreate the missing data via create_data with the required column(s) projected, then retry edit_artifact. Do NOT fall back to create_artifact — the gap is in the underlying data, not the artifact code.",
                        },
                        "artifact_id": str(artifact.id),
                        "mode": artifact.mode,
                        "version": artifact.version,
                        "diff_applied": False,
                    },
                },
            )
            return

        # Apply the diff
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "applying_edit"})

        new_code, diff_applied, num_blocks, failure_details = apply_search_replace_diff(existing_code, buffer)

        # One in-tool retry when blocks were produced but failed to match:
        # feed the failure diagnostics (with closest-match hints) back to the
        # model for corrected SEARCH text, instead of bouncing the failure to
        # a full outer planner iteration.
        diff_retry_used = False
        if num_blocks > 0 and not diff_applied and not (sigkill_event and sigkill_event.is_set()):
            yield ToolProgressEvent(
                type="tool.progress",
                payload={"stage": "retrying_diff", "failed_blocks": len(failure_details)},
            )
            _retry_buffer = await self._retry_failed_diff(
                edit_prompt=data.edit_prompt,
                existing_code=existing_code,
                failed_diff=buffer,
                failure_details=failure_details,
                runtime_ctx=runtime_ctx,
            )
            if _retry_buffer:
                diff_retry_used = True
                _code2, _applied2, _blocks2, _fail2 = apply_search_replace_diff(existing_code, _retry_buffer)
                if _blocks2 > 0 and _applied2:
                    new_code, diff_applied, num_blocks, failure_details = _code2, _applied2, _blocks2, _fail2

        edit_failed = False

        if num_blocks == 0:
            # No diff blocks found — reject full rewrites, edit_artifact is for surgical diffs only.
            # The planner should use create_artifact for large changes.
            logger.warning("edit_artifact: No SEARCH/REPLACE blocks found. Change is too large for surgical edit.")
            edit_failed = True
            warnings.append(
                "This change is too large for edit_artifact (no surgical diffs could be produced). "
                "The artifact was NOT modified. Use create_artifact to rebuild the dashboard with this change instead."
            )

        elif not diff_applied:
            # Diff blocks found but some/all failed to match — return original unchanged (Aider-style)
            # Do NOT attempt _extract_code fallback here — that's what causes marker leakage
            edit_failed = True
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

        if not edit_failed:
            # Output sanitization: strip any leaked diff markers, validate structure
            new_code, sanitize_warnings = sanitize_code_output(new_code, mode=artifact.mode)
            warnings.extend(sanitize_warnings)

            # Size guard: reject suspiciously small output
            if len(new_code) < 4000 and len(existing_code) >= 4000:
                logger.warning(
                    f"edit_artifact: Output code ({len(new_code)} chars) is suspiciously small "
                    f"compared to original ({len(existing_code)} chars). Keeping original."
                )
                edit_failed = True
                warnings.append(
                    f"Output code was only {len(new_code)} chars vs original {len(existing_code)} chars. "
                    "This looks like a failed edit. The artifact was NOT modified."
                )

        # If edit failed, return early with error — do NOT create a phantom artifact version
        if edit_failed:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "artifact_id": str(artifact.id),
                        "error": warnings[0] if warnings else "Edit failed",
                    },
                    "observation": {
                        "summary": f"Edit failed for artifact '{artifact.title or 'Untitled'}' (v{artifact.version}). " + (warnings[0] if warnings else ""),
                        "error": {
                            "type": "edit_failed",
                            "message": warnings[0] if warnings else "Edit failed",
                        },
                        "artifact_id": str(artifact.id),
                        "mode": artifact.mode,
                        "version": artifact.version,
                        "diff_applied": False,
                        "warnings": warnings,
                    },
                },
            )
            return

        # ═══════════════════════════════════════════════════════════════════════
        # Page mode: render-validate the edited code (and repair in-tool)
        # BEFORE persisting a new version. A version that doesn't render is
        # never persisted — the last good version stays live and the planner
        # gets a structured failure with the exact errors.
        # ═══════════════════════════════════════════════════════════════════════
        screenshot_base64: Optional[str] = None
        render_errors: list[str] = []
        render_repair_attempts = 0
        page_artifact_data: Optional[Dict[str, Any]] = None

        if artifact.mode == "page":
            page_artifact_data = {
                "report": {
                    "id": str(report.id) if report else None,
                    "title": getattr(report, "title", None) if report else None,
                    "theme": getattr(report, "theme", None) if report else None,
                },
                "visualizations": visualizations,
                # Anonymous on purpose — see create_artifact: validation must
                # exercise the current_user=null path.
                "current_user": None,
            }
            if merged_files:
                try:
                    page_artifact_data["files"] = await self._create_tool._build_file_datauris(db, merged_files)
                except Exception as e:
                    logger.warning(f"edit_artifact: failed to build file datauris for validation render: {e}")

            _validate_result: Optional[Dict[str, Any]] = None
            async for _item in self._create_tool._validate_and_repair_stream(
                new_code, page_artifact_data, "page", runtime_ctx, deadline_monotonic=_repair_deadline,
            ):
                if isinstance(_item, dict):
                    _validate_result = _item
                else:
                    yield _item

            if _validate_result is not None:
                if _validate_result["clean"]:
                    new_code = _validate_result["code"]
                    screenshot_base64 = _validate_result["screenshot"]
                    render_errors = list(_validate_result["errors"] or [])
                    render_repair_attempts = int(_validate_result["repair_attempts"] or 0)
                else:
                    _fatal = self._create_tool.fatal_render_errors(_validate_result["errors"] or [])
                    _first_error = _fatal[0] if _fatal else "unknown render error"
                    _attempts = int(_validate_result["repair_attempts"] or 0)
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": {
                                "success": False,
                                "artifact_id": str(artifact.id),
                                "error": f"Edited code failed render validation: {_first_error}",
                            },
                            "observation": {
                                "summary": (
                                    f"Edit rejected for artifact '{artifact.title or 'Untitled'}' (v{artifact.version}): "
                                    f"the edited code fails to render with {len(_fatal)} fatal error(s) after "
                                    f"{_attempts} in-tool repair attempt(s). The artifact was NOT modified — "
                                    f"the previous version remains live. First error: {_first_error}"
                                ),
                                "error": {
                                    "type": "render_validation_failed",
                                    "message": _first_error,
                                    "render_errors": _validate_result["errors"] or [],
                                    "repair_attempts": _attempts,
                                    "remediation": (
                                        "Retry edit_artifact with an edit_prompt that quotes the exact error(s) "
                                        "above and describes a simpler change, or rebuild via create_artifact if "
                                        "the edit fundamentally conflicts with the existing code."
                                    ),
                                },
                                "artifact_id": str(artifact.id),
                                "mode": artifact.mode,
                                "version": artifact.version,
                                "diff_applied": False,
                                "warnings": warnings,
                            },
                        },
                    )
                    return

        # Slides mode: execute the edited python-pptx code (repairing in-tool
        # on failure) BEFORE persisting anything — mirror of the page-mode
        # validate-before-persist above. A failed edit leaves the artifact
        # untouched instead of shipping a broken latest version. The deck is
        # built to a temp path and moved under the new version's id after
        # the row exists.
        _pptx_tmp_path: Optional[Path] = None
        _pptx_repair_attempts = 0
        if artifact.mode == "slides":
            import tempfile as _tempfile

            _pptx_tmp_path = Path(_tempfile.mkstemp(suffix=".pptx")[1])
            _pptx_tmp_path.unlink(missing_ok=True)
            _pptx_result: Optional[Dict[str, Any]] = None
            async for _item in self._create_tool._execute_and_repair_pptx(
                new_code,
                visualizations,
                {
                    "id": str(report.id) if report else None,
                    "title": getattr(report, "title", None) if report else None,
                    "theme": getattr(report, "theme", None) if report else None,
                },
                _pptx_tmp_path,
                await load_image_bytes(db, merged_files or []),
                runtime_ctx,
                deadline_monotonic=_repair_deadline,
            ):
                if isinstance(_item, dict):
                    _pptx_result = _item
                else:
                    yield _item

            if _pptx_result is None or not _pptx_result["ok"]:
                _error_msg = (_pptx_result or {}).get("error") or "unknown pptx execution error"
                _attempts = int((_pptx_result or {}).get("repair_attempts") or 0)
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": {
                            "success": False,
                            "artifact_id": str(artifact.id),
                            "error": f"Edited code failed PPTX execution: {_error_msg}",
                        },
                        "observation": {
                            "summary": (
                                f"Edit rejected for artifact '{artifact.title or 'Untitled'}' (v{artifact.version}): "
                                f"the edited code fails to build the presentation after "
                                f"{_attempts} in-tool repair attempt(s). The artifact was NOT modified — "
                                f"the previous version remains live. Error: {_error_msg}"
                            ),
                            "error": {
                                "type": "pptx_execution_failed",
                                "message": _error_msg,
                                "repair_attempts": _attempts,
                                "remediation": (
                                    "Retry edit_artifact with an edit_prompt that quotes the exact error "
                                    "above and describes a simpler change, or rebuild via create_artifact if "
                                    "the edit fundamentally conflicts with the existing code."
                                ),
                            },
                            "artifact_id": str(artifact.id),
                            "mode": artifact.mode,
                            "version": artifact.version,
                            "diff_applied": False,
                            "warnings": warnings,
                        },
                    },
                )
                return

            new_code = _pptx_result["code"]
            _pptx_repair_attempts = int(_pptx_result["repair_attempts"] or 0)

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
            content=(
                {"code": new_code, "visualization_ids": included_viz_ids, "files": merged_files}
                if merged_files
                else {"code": new_code, "visualization_ids": included_viz_ids}
            ),
            generation_prompt=accumulated_spec,
            version=new_version,
            status="completed",
        )
        # Page mode reached here only with a validated render — persist the
        # screenshot and (non-fatal) console errors captured during validation.
        if screenshot_base64 or render_errors:
            new_artifact.screenshot_base64 = screenshot_base64
            new_artifact.render_errors = render_errors or None
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)

        # Slides mode: the deck was already built (and repaired if needed)
        # BEFORE the version was persisted — move it under the new version's
        # id and render previews.
        if new_artifact.mode == "slides" and _pptx_tmp_path is not None:
            import shutil as _shutil

            uploads_dir = Path(__file__).parent.parent.parent.parent.parent / "uploads" / "pptx"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            out_path = uploads_dir / f"{new_artifact.id}.pptx"
            _shutil.move(str(_pptx_tmp_path), str(out_path))
            new_artifact.pptx_path = str(out_path)

            # A preview failure costs only the preview — the deck still opens.
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_previews"})
            try:
                previews = PptxPreviewService(logger=logger).generate_previews(
                    pptx_path=out_path,
                    artifact_id=str(new_artifact.id),
                )
                if previews:
                    new_artifact.content = {**new_artifact.content, "preview_images": previews}
                    new_artifact.thumbnail_path = previews[0]
            except Exception as e:
                logger.warning(f"edit_artifact: preview generation failed; deck still usable: {e}")

            await db.commit()
            await db.refresh(new_artifact)

        # Page mode: validation (and its screenshot) already ran before the
        # version was persisted — only the background thumbnail remains.
        if new_artifact.mode == "page" and page_artifact_data is not None:
            thumbnail_html = self._create_tool._build_thumbnail_html(page_artifact_data, new_code, mode=new_artifact.mode)
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
            if diff_retry_used:
                summary_msg += " (after one in-tool diff retry)"
        else:
            summary_msg += " — fell back to full rewrite"
        if new_artifact.mode == "page":
            if render_repair_attempts:
                summary_msg += f". Render validation passed after {render_repair_attempts} in-tool repair attempt(s)."
            else:
                summary_msg += ". Render validation passed."
            _console_warnings = [e for e in render_errors if e.startswith("[console.error]")]
            if _console_warnings:
                summary_msg += f" {len(_console_warnings)} non-fatal console error(s) were logged."
        elif new_artifact.mode == "slides":
            if _pptx_repair_attempts:
                summary_msg += f". PPTX execution passed after {_pptx_repair_attempts} in-tool repair attempt(s)."
            else:
                summary_msg += ". PPTX execution passed."
            _previews = (new_artifact.content or {}).get("preview_images") or []
            if _previews:
                summary_msg += f" Generated {len(_previews)} slide preview images."
            else:
                summary_msg += (
                    " The deck was built, but slide preview images could not be generated on this "
                    "server — the PPTX file is still downloadable."
                )
        if removed_viz_info:
            _removed_titles = ", ".join(rv.get("title", rv.get("id", "?")) for rv in removed_viz_info)
            summary_msg += f" Removed visualization(s): {_removed_titles}."

        # Screenshot attachment gated on privacy + vision (it shows the data)
        _attach_screenshot = bool(
            screenshot_base64 and allow_llm_see_data and model and getattr(model, "supports_vision", False)
        )
        if _attach_screenshot:
            summary_msg += " Screenshot of the rendered dashboard is attached — review it for visual correctness."

        observation: Dict[str, Any] = {
            "summary": summary_msg,
            "artifact_id": str(new_artifact.id),
            "mode": new_artifact.mode,
            "version": new_version,
            "diff_applied": diff_applied,
            "visualization_count": len(visualizations),
            "visualization_ids": included_viz_ids,
            "visualization_profiles": viz_profiles,  # columns, sample rows (gated by allow_llm_see_data), data model
        }
        if removed_viz_info:
            observation["removed_visualization_ids"] = [rv.get("id") for rv in removed_viz_info]
        if render_errors:
            observation["render_errors"] = render_errors
        if render_repair_attempts:
            observation["repair_attempts"] = render_repair_attempts
        if _pptx_repair_attempts:
            observation["repair_attempts"] = _pptx_repair_attempts

        # Attach the first slide preview for planner reflection — same gate as
        # the page-mode screenshot (the preview shows the data).
        if new_artifact.mode == "slides":
            _slides_previews = (new_artifact.content or {}).get("preview_images") or []
            _slides_preview_b64 = self._create_tool._first_preview_base64(_slides_previews)
            if _slides_preview_b64 and allow_llm_see_data and model and getattr(model, "supports_vision", False):
                observation["summary"] += " First slide preview is attached — review it for visual correctness."
                observation["images"] = [{
                    "data": _slides_preview_b64,
                    "media_type": "image/png",
                    "source_type": "base64",
                }]

        # Add preview screenshot for planner reflection (page mode)
        if _attach_screenshot:
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
