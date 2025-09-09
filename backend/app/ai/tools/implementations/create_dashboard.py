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
        # Prefer ContextHub widgets section (all report widgets + latest steps)
        widgets: List[dict] = []
        if context_hub is not None:
            await context_hub.refresh_warm()
            view = context_hub.get_view()
            widgets_section = getattr(getattr(view, 'warm', None), 'widgets', None)
            items = getattr(widgets_section, 'items', []) if widgets_section else []
            for it in (items or []):
                dm = it.data_model or {}
                widgets.append({
                    "id": it.widget_id,
                    "title": it.widget_title,
                    "type": (dm or {}).get("type"),
                    "data_model": dm,
                    "row_count": it.row_count,
                    "column_names": it.column_names,
                    "stats": it.stats,
                })
        # Fallback to observation_context if ContextHub widgets unavailable
        if not widgets and isinstance(observation_context, dict):
            try:
                seen: set[str] = set()
                for obs in (observation_context.get("tool_observations") or []):
                    if not isinstance(obs, dict):
                        continue
                    tool_name = str(obs.get("tool_name") or "").lower()
                    if tool_name not in ["create_widget", "create_and_execute_code", "execute_code", "execute_sql"]:
                        continue
                    obs_obj = obs.get("observation") or {}
                    tool_input = obs.get("tool_input") or {}
                    wid = str(obs_obj.get("widget_id") or obs.get("created_widget_id") or "")
                    data_model = (obs_obj.get("data_model") or {}) if isinstance(obs_obj, dict) else {}
                    entry = {
                        "id": wid or None,
                        "title": tool_input.get("widget_title"),
                        "type": (data_model or {}).get("type"),
                        "data_model": data_model if isinstance(data_model, dict) else {},
                        "summary": obs_obj.get("summary"),
                        "user_prompt": tool_input.get("user_prompt"),
                        "interpreted_prompt": tool_input.get("interpreted_prompt"),
                        "tool_name": tool_name,
                        "timestamp": obs.get("timestamp"),
                    }
                    key = entry["id"] or f"{tool_name}:{len(widgets)}"
                    if key in seen:
                        continue
                    seen.add(key)
                    widgets.append(entry)
            except Exception:
                widgets = []
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
You are a world-class dashboard and research report designer. Create a STUNNING, narrative-driven presentation tailored to the user's goal and available widgets. Output JSON ONLY that strictly matches the schema below.

GENERAL ORGANIZATION INSTRUCTIONS (MUST FOLLOW):
{instructions_context}

CONTEXT
- Report title (optional): {data.report_title or ''}
- User prompt: {data.prompt}
- Previous messages:
{previous_messages}
- Available widgets:
{widgets}

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
  - For data widgets, set view_overrides with any of: variant (e.g., "area" or "smooth"), legendVisible, xAxisVisible, yAxisVisible, and style (colors, axis styles, title styles). Use integers/booleans/strings appropriately.
  - Prefer minimal, meaningful overrides that improve readability and visual balance.

EXPECTED JSON OUTPUT (strict):
{{
  "blocks": [
    // Ordered blocks (no placeholders). Ensure each has valid integers and no overlaps.
  ],
}}

Block types (exact fields):

// Data widget block
{{
  "type": "widget",
  "widget_id": "UUID",
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
                if btype == "widget":
                    return f"widget:{blk.get('widget_id')}:{x}:{y}:{w}:{h}"
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
            # Blocks: emit only finalized new ones
            if isinstance(result.get("blocks"), list):
                for idx, blk in enumerate(result["blocks"]):
                    if not isinstance(blk, dict):
                        continue
                    # Only emit when LLM marks it completed
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

        # Final result (agent will have patched blocks during streaming)
        output = CreateDashboardOutput(layout=final_layout, report_title=data.report_title)
        yield ToolEndEvent(type="tool.end", payload={"output": output.model_dump(), "observation": {"summary": "Dashboard designed", "layout": final_layout}})


