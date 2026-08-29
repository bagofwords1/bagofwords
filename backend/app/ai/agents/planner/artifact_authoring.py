"""Artifact authoring reference for the planner.

Final phase-3 state of docs/design/artifact-iteration-and-filtering.md: the
PLANNER authors all artifact source itself — full code on create_artifact
(`code`), exact find/replace ops on edit_artifact — and the tools are
mechanical (apply, gate, render-validate, persist; no inner LLM). This module
assembles the static reference the planner needs to do that authoring. It is
stable across calls so provider-side prompt caching absorbs its size.
"""

from functools import lru_cache


_SLIDES_CONTRACT = """
═══════════════════════════════════════════════════════════════════════════════
SLIDES AUTHORING (mode='slides') — python-pptx script contract
═══════════════════════════════════════════════════════════════════════════════
For decks you author a complete python-pptx script (pass it as create_artifact.code).
The sandboxed namespace already provides: Presentation, Inches, Pt, Emu, RGBColor,
PP_ALIGN, MSO_ANCHOR, MSO_SHAPE, XL_CHART_TYPE, XL_LEGEND_POSITION,
CategoryChartData, ChartData — do NOT import anything. Data variables provided:
- `visualizations`: list of dicts with 'title', 'columns', 'rows'. Each entry of
  viz['columns'] is a DICT like {'field': 'Revenue', 'headerName': 'Revenue'} —
  use col['field'] as the row key, never pass the dict where a string is expected.
- `report`: {'id', 'title', 'theme'}; `image`/`image_ids` when files are embedded.
- `_pptx_output_path`: the script MUST end with prs.save(_pptx_output_path).
Guard nullish values; keep one block of statements per slide so later edits stay
textually local. Validation = the script executes and saves; on failure the tool
returns the exact exception — fix the script and call again.

═══════════════════════════════════════════════════════════════════════════════
MECHANICAL EDITS (edit_artifact) — op authoring rules
═══════════════════════════════════════════════════════════════════════════════
- Each op is {find, replace}; `find` must match the CURRENT code exactly once
  (whitespace included). Keep finds minimal but unique; extend with surrounding
  context when ambiguous. Ops apply in order, atomically — any failure applies
  nothing and returns the closest match to correct.
- Author edits against the code in <current_artifact>.<code> (or read_artifact
  when omitted for size). After a successful create/edit, the returned code is
  the new current state.
- Adding a viz: pass its id in visualization_ids AND add a section rendering
  vizById("<uuid>") in your ops. Removing one: pass remove_visualization_ids AND
  delete every reference in your ops. The viz-reference gate enforces both.
- Syntax is gated before anything renders: code that fails to parse fails the
  call (with the parser's line:col and a bracket-balance hint) and persists
  nothing. Write one statement per line and avoid deeply nested one-liners —
  balanced brackets are YOUR responsibility, count them in any dense expression
  you author.
"""


@lru_cache(maxsize=1)
def build_artifact_authoring_reference() -> str:
    """The full static authoring reference: page (JSX + runtime docs), slides
    (python-pptx contract), and mechanical-edit op rules."""
    # Lazy import: tools import planner-adjacent modules; keep module load light.
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    page_reference = CreateArtifactTool()._build_page_system_prompt()
    return (
        "═══════════════════════════════════════════════════════════════════════════════\n"
        "ARTIFACT AUTHORING REFERENCE — YOU write the artifact source\n"
        "═══════════════════════════════════════════════════════════════════════════════\n"
        "Artifacts are authored BY YOU, the planner: pass the complete source as\n"
        "create_artifact.code (JSX for page, python-pptx for slides) and author exact\n"
        "find/replace ops for edit_artifact. The tools are mechanical — they gate\n"
        "(viz references, params wiring), render-validate once, and persist; on failure\n"
        "they return precise errors and persist NOTHING. Your loop is the repair loop:\n"
        "fix the code/ops and call again. The reference below (written for a code\n"
        "author) is YOUR reference — 'the user message' there corresponds to the data\n"
        "and design intent you have in context.\n\n"
        + page_reference
        + _SLIDES_CONTRACT
    )
