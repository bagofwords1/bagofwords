import asyncio
from typing import AsyncIterator, Dict, Any, Type, List
from pydantic import BaseModel
from sqlalchemy import select
from app.models.dashboard_layout_version import DashboardLayoutVersion


from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    CreateDashboardInput,
    CreateDashboardOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from partialjson.json_parser import JSONParser
from app.ai.llm import LLM


class CreateDashboardTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_dashboard",
            description="Design a stunning dashboard or research report layout from available widgets and context.",
            category="action",
            version="1.0.0",
            input_schema=CreateDashboardInput.model_json_schema(),
            output_schema=CreateDashboardOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=True,
            required_permissions=[],
            is_active=True,
            tags=["dashboard", "report", "layout"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateDashboardInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDashboardOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateDashboardInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"report_title": data.report_title or ""})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Runtime context
        model = runtime_ctx.get("model")
        instruction_context_builder = runtime_ctx.get("instruction_context_builder") or (
            getattr(runtime_ctx.get("context_hub"), "instruction_builder", None) if runtime_ctx.get("context_hub") else None
        )
        previous_layout = await runtime_ctx.get("db").execute(select(DashboardLayoutVersion).where(DashboardLayoutVersion.report_id == str(runtime_ctx.get("report").id)).order_by(DashboardLayoutVersion.created_at.desc()))
        previous_layout = previous_layout.scalars().first()

        previous_messages = runtime_ctx.get("previous_messages") or ""
        observation_context = runtime_ctx.get("observation_context") or {}
        context_hub = runtime_ctx.get("context_hub")
        # Collect available queries (+ embedded visualizations) from ContextHub warm view
        queries: List[dict] = []
        visualizations: List[dict] = []

        # Helpers to compact noisy dicts
        def _trim_none(obj):
            try:
                if isinstance(obj, dict):
                    out = {}
                    for k, v in obj.items():
                        tv = _trim_none(v)
                        if tv is None:
                            continue
                        if isinstance(tv, (dict, list)) and len(tv) == 0:
                            continue
                        out[k] = tv
                    return out
                if isinstance(obj, list):
                    items = [_trim_none(v) for v in obj]
                    return [v for v in items if not (v is None or (isinstance(v, (dict, list)) and len(v) == 0))]
                return obj
            except Exception:
                return obj

        def _compact_view(view_dict):
            if not isinstance(view_dict, dict):
                return view_dict
            keep = {}
            if 'type' in view_dict:
                keep['type'] = view_dict.get('type')
            if 'encoding' in view_dict and view_dict.get('encoding') is not None:
                keep['encoding'] = view_dict.get('encoding')
            if 'variant' in view_dict and view_dict.get('variant'):
                keep['variant'] = view_dict.get('variant')
            if 'legendVisible' in view_dict:
                keep['legendVisible'] = view_dict.get('legendVisible')
            if 'xAxisVisible' in view_dict:
                keep['xAxisVisible'] = view_dict.get('xAxisVisible')
            if 'yAxisVisible' in view_dict:
                keep['yAxisVisible'] = view_dict.get('yAxisVisible')
            if 'style' in view_dict and isinstance(view_dict.get('style'), dict):
                keep['style'] = view_dict.get('style')
            if 'options' in view_dict and isinstance(view_dict.get('options'), dict):
                keep['options'] = view_dict.get('options')
            return _trim_none(keep)
        try:
            if context_hub is not None:
                # Use current warm cache (Agent maintains it); do not hit DB here
                view = context_hub.get_view()
                qsec = getattr(getattr(view, 'warm', None), 'queries', None)
                items = getattr(qsec, 'items', []) if qsec else []
                for it in (items or []):
                    qdict = {
                        "id": getattr(it, 'query_id', None),
                        "title": getattr(it, 'query_title', None),
                        "default_step_id": getattr(it, 'default_step_id', None),
                        "default_step_title": getattr(it, 'default_step_title', None),
                        "row_count": getattr(it, 'row_count', 0),
                        "columns": list(getattr(it, 'column_names', []) or []),
                        "data_model": getattr(it, 'data_model', None),
                        "visualizations": [],
                    }
                    vlist = []
                    for v in (getattr(it, 'visualizations', []) or []):
                        ventry = {
                            "id": getattr(v, 'id', None),
                            "title": getattr(v, 'title', None),
                            "status": getattr(v, 'status', None),
                            "view": _compact_view(getattr(v, 'view', None)),
                        }
                        vlist.append(ventry)
                        # also flatten for convenience in prompt
                        visualizations.append(ventry)
                    qdict["visualizations"] = vlist
                    queries.append(qdict)
        except Exception:
            queries = []
            visualizations = []
        # Also enrich from observation_context (created_visualization_ids) without DB lookups
        try:
            seen: set[str] = set([str(v.get("id")) for v in visualizations if v.get("id")])
            # Prefer explicit visualization_updates captured during agent streaming
            if isinstance(observation_context, dict):
                for vu in (observation_context.get("visualization_updates") or []):
                    if not isinstance(vu, dict):
                        continue
                    vid = str(vu.get("visualization_id") or "")
                    if not vid or vid in seen:
                        continue
                    vdata = vu.get("data") or {}
                    view = _compact_view(vdata.get("view") or {})
                    visualizations.append({
                        "id": vid,
                        "title": vdata.get("title"),
                        "status": vdata.get("status"),
                        "query_id": vdata.get("query_id"),
                        "type": (view or {}).get("type"),
                        "encoding": (view or {}).get("encoding"),
                        "view": view,
                    })
                    seen.add(vid)
                # Also scan tool_observations for created_visualization_ids as a catch-all
                for obs in (observation_context.get("tool_observations") or []):
                    try:
                        created = ((obs or {}).get("observation") or {}).get("created_visualization_ids") or []
                        for vid in created:
                            svid = str(vid)
                            if svid and svid not in seen:
                                visualizations.append({"id": svid})
                                seen.add(svid)
                    except Exception:
                        continue
        except Exception:
            visualizations = []
        instructions_context = ""
        try:
            if instruction_context_builder is not None:
                inst_section = await instruction_context_builder.build()
                instructions_context = inst_section.render() or ""
        except Exception:
            instructions_context = ""

        # Build designer prompt inline (deprecate dashboard_designer)
        def build_prompt() -> str:

            #prev_blocks = previous_layout.blocks if isinstance(previous_layout, DashboardLayoutVersion) else []
            #prev_blocks_count = len(prev_blocks) if isinstance(prev_blocks, list) else 0
            # Embed observations json (planner-style) for full grounding
            try:
                import json
                past_obs_json = json.dumps(observation_context.get("tool_observations") or [])
            except Exception:
                past_obs_json = "[]"
            try:
                import json
                last_obs_json = json.dumps(observation_context.get("last_observation") or None)
            except Exception:
                last_obs_json = "None"
            
            prev_blocks = previous_layout.blocks if previous_layout else []
            return f"""
SYSTEM
You are a world-class dashboard and research report designer. Create a STUNNING, narrative-driven presentation tailored to the user's goal and available visualizations. Output JSON ONLY that strictly matches the schema below.

GENERAL ORGANIZATION INSTRUCTIONS (MUST FOLLOW):
{instructions_context}

CONTEXT
- Report title (optional): {data.report_title or ''}
- User prompt: {data.prompt}
- Previous messages:
{previous_messages}
- Available queries:
{queries}
- Available visualizations:
{visualizations}

- Previous layout:
{prev_blocks}

OBSERVATIONS
<past_observations>{past_obs_json}</past_observations>
<last_observation>{last_obs_json}</last_observation>

OBJECTIVE
- Choose the best presentation mode:
  - "dashboard": polished business dashboard (product/sales/marketing/finance/ops) with KPIs, charts, and tables arranged as a mosaic story.
  - "research": long-form analysis with sections, titles, explanatory paragraphs, and supporting visuals.
- The result must be visually balanced, modern, and insightful.

GUIDELINES
- 12-column grid. All x, y, width, height are SMALL INTEGERS (grid units), not pixels. No overlaps.
- Typical sizes: charts height 8–12; KPI tiles 3–5; text height scales with width and content.
- Use text widgets to guide the reader: titles, subtitles, section intros, insights, summaries.
- Place text near related visuals; flow overview → drill-down; group related items side-by-side.
- Text widgets must be semantic HTML (h1, h2, h3, p, ul, li, a, table, etc.). No Markdown.
- Styling and view details must be included:
  - For text blocks, set view_overrides.variant to one of: "title" | "subtitle" | "paragraph" | "summary" when appropriate.
  - For data visualizations, set view_overrides with any of: variant (e.g., "area" or "smooth"), legendVisible, xAxisVisible, yAxisVisible, and style (colors, axis styles, title styles). Use integers/booleans/strings appropriately.
  - Prefer minimal, meaningful overrides that improve readability and visual balance.

EXPECTED JSON OUTPUT (strict):
{{
  "blocks": [
    // Ordered blocks (no placeholders). Ensure each has valid integers and no overlaps.
  ],
}}

Block types (exact fields):

// Visualization block
{{
  "type": "visualization",
  "visualization_id": "UUID",
  "x": int, "y": int, "width": int, "height": int,
  "view_overrides": {{
        "variant": string | null,
        "legendVisible": boolean | null,
        "xAxisVisible": boolean | null,
        "yAxisVisible": boolean | null,
        "style": {{
            "titleColor": string | null,
            "titleSize": int | null,
            "titleWeight": int | null,
            "axis": {{
                "xLabelColor": string | null,
                "xLineColor": string | null,
                "yLabelColor": string | null,
                "yLineColor": string | null,
                "gridLineColor": string | null
            }} | null
        }} | null,
        "options": object | null
    }} | null,
  "is_completed": True
  }}

// Text widget block
{{
  "type": "text_widget",
  "content": "<HTML>",
  "x": int, "y": int, "width": int, "height": int,
  "view_overrides": {{
    "variant": "title" | "subtitle" | "paragraph" | "summary",
    "style": object | null
  }} | null,
  "is_completed": True
}}

RULES
- Return ONLY JSON that matches the schema above.
- Ensure a compelling narrative and visually pleasing mosaic arrangement.
- Avoid overlaps; stick to the 12-column grid.
"""

        prompt = build_prompt()

        # Stream from LLM
        parser = JSONParser()
        final_layout: Dict[str, Any] = {"blocks": []}
        emitted_signatures: set[str] = set()

        def _block_signature(blk: Dict[str, Any]) -> str:
            try:
                btype = blk.get("type")
                x = int(blk.get("x", 0))
                y = int(blk.get("y", 0))
                w = int(blk.get("width", 0))
                h = int(blk.get("height", 0))
                if btype == "visualization":
                    return f"visualization:{blk.get('visualization_id')}:{x}:{y}:{w}:{h}"
                if btype == "text_widget":
                    content = blk.get("content", "")
                    ch = hash(content)
                    return f"text:{ch}:{x}:{y}:{w}:{h}"
                import json
                return f"other:{json.dumps(blk, sort_keys=True)}"
            except Exception:
                return repr(blk)

        # Use LLM wrapper for streaming (runtime_ctx["model"] is an LLMModel)
        llm = LLM(runtime_ctx.get("model"))
        buffer = ""
        async for chunk in llm.inference_stream(prompt):
            buffer += chunk
            try:
                result = parser.parse(buffer)
            except Exception:
                continue
            if not isinstance(result, dict):
                continue
            # Blocks: emit finalized new ones (require is_completed=True for all types)
            if isinstance(result.get("blocks"), list):
                for idx, blk in enumerate(result["blocks"]):
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("is_completed") is not True:
                        continue
                    normalized = dict(blk)
                    sig = _block_signature(normalized)
                    if sig in emitted_signatures:
                        continue
                    emitted_signatures.add(sig)

                    # Track in final layout and stream once
                    final_layout["blocks"].append(normalized)
                    yield ToolProgressEvent(type="tool.progress", payload={"stage": "block.completed", "block": normalized})

            # Ignore any other keys; schema expects only blocks

        # Final parse: include any blocks present in final buffer not yet emitted (require is_completed=True)
        try:
            result = parser.parse(buffer)
            if isinstance(result, dict) and isinstance(result.get("blocks"), list):
                for blk in result["blocks"]:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("is_completed") is not True:
                        continue
                    sig = _block_signature(blk)
                    if sig not in emitted_signatures:
                        final_layout["blocks"].append(blk)
                        emitted_signatures.add(sig)
        except Exception:
            pass

        output = CreateDashboardOutput(layout=final_layout, report_title=data.report_title)
        yield ToolEndEvent(type="tool.end", payload={"output": output.model_dump(), "observation": {"summary": "Dashboard designed", "layout": final_layout}})


