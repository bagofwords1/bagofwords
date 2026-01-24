import json
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
from app.ai.tools.schemas.create_dashboard import SemanticBlockOutput
from app.services.dashboard_layout_engine import DashboardBlockSpec, ColumnSpec, ContainerChrome, compute_layout
from partialjson.json_parser import JSONParser
from app.ai.llm import LLM
from app.dependencies import async_session_maker


def _semantic_to_engine_spec(block: Dict[str, Any]) -> DashboardBlockSpec:
    """Convert a parsed semantic block dict to DashboardBlockSpec for the layout engine."""
    block_type = block.get("type", "visualization")
    
    # Map children recursively for cards/sections
    children = None
    if block.get("children"):
        children = [_semantic_to_engine_spec(c) for c in block["children"]]
    
    # Map columns recursively for column_layout
    columns = None
    if block.get("columns") and block_type == "column_layout":
        columns = []
        for col in block["columns"]:
            col_children = [_semantic_to_engine_spec(c) for c in (col.get("children") or [])]
            columns.append(ColumnSpec(span=col.get("span", 6), children=col_children))
    
    # Map chrome for cards
    chrome = None
    if block.get("title") or block.get("subtitle"):
        chrome = ContainerChrome(
            title=block.get("title"),
            subtitle=block.get("subtitle"),
            showHeader=True,
            border=block.get("border", "soft"),
        )
    
    # Keep "text" as is for inline text blocks (AI-generated)
    # "text_widget" is legacy type with DB reference

    return DashboardBlockSpec(
        type=block_type,
        visualization_id=block.get("visualization_id"),
        content=block.get("content"),
        variant=block.get("variant"),
        children=children,
        columns=columns,
        chrome=chrome,
        role=block.get("role", "supporting_visual"),
        importance=block.get("importance", "secondary"),
        size=block.get("size", "medium"),
        section=block.get("section"),
        order=block.get("order", 0),
        view_overrides=block.get("view_overrides"),
    )


class CreateDashboardTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_dashboard",
            description="Design a stunning dashboard or research report layout from available widgets and context.",
            category="action",
            version="2.0.0",
            input_schema=CreateDashboardInput.model_json_schema(),
            output_schema=CreateDashboardOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=True,
            required_permissions=[],
            is_active=False,
            tags=["dashboard", "report", "layout"],
            allowed_modes=["chat", "deep"],
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
        instruction_context_builder = runtime_ctx.get("instruction_context_builder") or (
            getattr(runtime_ctx.get("context_hub"), "instruction_builder", None) if runtime_ctx.get("context_hub") else None
        )
        previous_layout = await runtime_ctx.get("db").execute(
            select(DashboardLayoutVersion)
            .where(DashboardLayoutVersion.report_id == str(runtime_ctx.get("report").id))
            .order_by(DashboardLayoutVersion.created_at.desc())
        )
        previous_layout = previous_layout.scalars().first()

        previous_messages = runtime_ctx.get("previous_messages") or ""
        observation_context = runtime_ctx.get("observation_context") or {}
        context_hub = runtime_ctx.get("context_hub")

        # Collect available visualizations with their ViewSchema
        visualizations: List[Dict[str, Any]] = []

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

        try:
            if context_hub is not None:
                view = context_hub.get_view()
                qsec = getattr(getattr(view, 'warm', None), 'queries', None)
                items = getattr(qsec, 'items', []) if qsec else []
                for it in (items or []):
                    for v in (getattr(it, 'visualizations', []) or []):
                        view_dict = getattr(v, 'view', None) or {}
                        ventry = {
                            "id": getattr(v, 'id', None),
                            "title": getattr(v, 'title', None),
                            "query_id": getattr(it, 'query_id', None),
                            "view": _trim_none(view_dict),
                            "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                            "columns": list(getattr(it, 'column_names', []) or []),
                            "row_count": getattr(it, 'row_count', 0),
                        }
                        visualizations.append(ventry)
        except Exception:
            visualizations = []

        # Enrich from observation_context
        try:
            seen: set[str] = set([str(v.get("id")) for v in visualizations if v.get("id")])
            if isinstance(observation_context, dict):
                for vu in (observation_context.get("visualization_updates") or []):
                    if not isinstance(vu, dict):
                        continue
                    vid = str(vu.get("visualization_id") or "")
                    if not vid or vid in seen:
                        continue
                    vdata = vu.get("data") or {}
                    view_dict = vdata.get("view") or {}
                    visualizations.append({
                        "id": vid,
                        "title": vdata.get("title"),
                        "query_id": vdata.get("query_id"),
                        "view": _trim_none(view_dict),
                        "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                        "columns": [],
                        "row_count": 0,
                    })
                    seen.add(vid)
        except Exception:
            pass

        # Build context strings
        instructions_context = ""
        mentions_context = "<mentions>No mentions for this turn</mentions>"
        entities_context = ""
        try:
            if instruction_context_builder is not None:
                inst_section = await instruction_context_builder.build(categories=["dashboard", "visualization", "general"])
                instructions_context = inst_section.render() or ""
            if context_hub is not None:
                view = context_hub.get_view()
                msec = getattr(getattr(view, 'static', None), 'mentions', None)
                if msec:
                    mentions_context = msec.render() or mentions_context
                esec = getattr(getattr(view, 'warm', None), 'entities', None)
                if esec:
                    entities_context = esec.render() or entities_context
        except Exception:
            instructions_context = ""

        # Build the semantic prompt
        def build_prompt() -> str:
            try:
                past_obs_json = json.dumps(observation_context.get("tool_observations") or [])
            except Exception:
                past_obs_json = "[]"
            try:
                last_obs_json = json.dumps(observation_context.get("last_observation") or None)
            except Exception:
                last_obs_json = "None"
            
            viz_json = json.dumps(visualizations, indent=2, default=str)

            return f"""SYSTEM
You are a world-class dashboard designer. Create STUNNING, modern dashboards with visual hierarchy and compelling data storytelling.

Output JSON ONLY. DO NOT include x, y, width, or height - the layout engine computes positions automatically.

ORGANIZATION INSTRUCTIONS:
{instructions_context}

        CONTEXT
- Report title: {data.report_title or 'Dashboard'}
- User prompt: {data.prompt}
        {mentions_context}
{entities_context}
- Previous messages:
{previous_messages}

AVAILABLE VISUALIZATIONS:
{viz_json}

OBSERVATIONS
<past_observations>{past_obs_json}</past_observations>
<last_observation>{last_obs_json}</last_observation>

═══════════════════════════════════════════════════════════════════════════════
BLOCK TYPES - Use these to build beautiful dashboards
═══════════════════════════════════════════════════════════════════════════════

1. TEXT BLOCK - For titles, descriptions, insights
{{
  "type": "text",
  "content": "<h2>Title</h2><p>Description text...</p>",
  "variant": "title" | "subtitle" | "paragraph" | "insight",
  "size": "full" | "medium",
  "is_completed": true
}}

2. VISUALIZATION BLOCK - Reference existing visualizations
{{
  "type": "visualization",
  "visualization_id": "UUID",
  "size": "xs" | "small" | "medium" | "large" | "full",
  "role": "kpi" | "hero" | "primary_visual" | "supporting_visual",
  "is_completed": true
}}

3. CARD BLOCK - Bordered container with title (wraps children)
{{
  "type": "card",
  "title": "Card Title",
  "subtitle": "Optional description",
  "children": [
    {{ ...visualization or text blocks... }}
  ],
  "size": "medium" | "large" | "full",
  "is_completed": true
}}

4. COLUMN LAYOUT - Side-by-side arrangement (spans must sum to 12)
{{
  "type": "column_layout",
  "columns": [
    {{ "span": 8, "children": [ ...blocks... ] }},
    {{ "span": 4, "children": [ ...blocks... ] }}
  ],
  "is_completed": true
}}

═══════════════════════════════════════════════════════════════════════════════
SIZE GUIDE (12-column grid)
═══════════════════════════════════════════════════════════════════════════════
- "xs": 2 cols (tiny)
- "small": 3 cols (KPI tiles - 4 fit per row)
- "medium": 6 cols (half width - 2 per row)
- "large": 8 cols (prominent)
- "full": 12 cols (full width)

═══════════════════════════════════════════════════════════════════════════════
ROLE GUIDE (affects default sizing)
═══════════════════════════════════════════════════════════════════════════════
- "kpi": Small metric card (size=small, height=4) - use for metric_card/count types
- "hero": Main focal chart (size=full, height=10)
- "primary_visual": Important chart (size=large, height=8)
- "supporting_visual": Secondary chart (size=medium, height=6)

═══════════════════════════════════════════════════════════════════════════════
DASHBOARD PATTERNS - Use these for professional layouts
═══════════════════════════════════════════════════════════════════════════════

PATTERN A: KPI Row + Hero Chart
{{
  "blocks": [
    {{"type": "text", "content": "<h1>Dashboard Title</h1><p>Overview description</p>", "size": "full", "is_completed": true}},
    {{"type": "visualization", "visualization_id": "kpi-1", "role": "kpi", "size": "small", "is_completed": true}},
    {{"type": "visualization", "visualization_id": "kpi-2", "role": "kpi", "size": "small", "is_completed": true}},
    {{"type": "visualization", "visualization_id": "kpi-3", "role": "kpi", "size": "small", "is_completed": true}},
    {{"type": "visualization", "visualization_id": "kpi-4", "role": "kpi", "size": "small", "is_completed": true}},
    {{"type": "card", "title": "Main Chart", "subtitle": "Detailed analysis", "children": [
      {{"type": "visualization", "visualization_id": "main-chart", "size": "full", "is_completed": true}}
    ], "size": "full", "is_completed": true}}
  ]
}}

PATTERN B: Side-by-Side Charts (8+4 split)
{{
  "blocks": [
    {{"type": "column_layout", "columns": [
      {{"span": 8, "children": [
        {{"type": "card", "title": "Traffic Overview", "subtitle": "Daily trends", "children": [
          {{"type": "visualization", "visualization_id": "line-chart", "size": "full", "is_completed": true}}
        ], "size": "full", "is_completed": true}}
      ]}},
      {{"span": 4, "children": [
        {{"type": "card", "title": "By Category", "children": [
          {{"type": "visualization", "visualization_id": "pie-chart", "size": "full", "is_completed": true}}
        ], "size": "full", "is_completed": true}}
      ]}}
    ], "is_completed": true}}
  ]
}}

PATTERN C: Cards in Columns (6+6 split)
{{
  "blocks": [
    {{"type": "column_layout", "columns": [
      {{"span": 6, "children": [
        {{"type": "card", "title": "Revenue", "children": [
          {{"type": "visualization", "visualization_id": "revenue-chart", "size": "full", "is_completed": true}}
        ], "size": "full", "is_completed": true}}
      ]}},
      {{"span": 6, "children": [
        {{"type": "card", "title": "Users", "children": [
          {{"type": "visualization", "visualization_id": "users-chart", "size": "full", "is_completed": true}}
        ], "size": "full", "is_completed": true}}
      ]}}
    ], "is_completed": true}}
  ]
}}

═══════════════════════════════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════════════════════════════
1. Return ONLY valid JSON with a "blocks" array
2. DO NOT include x, y, width, height - layout engine computes these
3. The ORDER of blocks in your output IS the layout order (top to bottom)
4. Wrap charts in cards with titles for professional look
5. Use column_layout for side-by-side comparisons (spans MUST sum to 12)
6. Place titles/descriptions BEFORE related visualizations
7. Every block MUST have "is_completed": true

OUTPUT FORMAT:
{{
  "blocks": [
    // Your blocks here in display order
  ]
}}
"""

        prompt = build_prompt()
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_layout"})

        # Stream from LLM
        parser = JSONParser()
        semantic_blocks: List[Dict[str, Any]] = []
        emitted_signatures: set[str] = set()

        def _block_signature(blk: Dict[str, Any]) -> str:
            try:
                btype = blk.get("type")
                if btype == "visualization":
                    return f"visualization:{blk.get('visualization_id')}"
                if btype in ("text", "text_widget"):
                    return f"text:{hash(blk.get('content', ''))}"
                if btype == "card":
                    return f"card:{blk.get('title', '')}:{hash(json.dumps(blk.get('children', []), sort_keys=True, default=str))}"
                if btype == "column_layout":
                    return f"columns:{hash(json.dumps(blk.get('columns', []), sort_keys=True, default=str))}"
                return f"other:{hash(json.dumps(blk, sort_keys=True, default=str))}"
            except Exception:
                return repr(blk)

        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        buffer = ""
        async for chunk in llm.inference_stream(
            prompt,
            usage_scope="create_dashboard",
            usage_scope_ref_id=None,
        ):
            buffer += chunk
            try:
                result = parser.parse(buffer)
            except Exception:
                continue
            if not isinstance(result, dict):
                continue

            # Collect completed semantic blocks
            if isinstance(result.get("blocks"), list):
                for blk in result["blocks"]:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("is_completed") is not True:
                        continue
                    sig = _block_signature(blk)
                    if sig in emitted_signatures:
                        continue
                    emitted_signatures.add(sig)
                    semantic_blocks.append(blk)
                    # Emit progress for UI feedback
                    yield ToolProgressEvent(
                        type="tool.progress",
                        payload={"stage": "semantic_block.received", "block_type": blk.get("type")}
                    )

        # Final parse for any remaining blocks
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
                        semantic_blocks.append(blk)
                        emitted_signatures.add(sig)
        except Exception:
            pass

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "computing_layout"})

        # Convert semantic blocks to engine specs and compute layout
        engine_specs: List[DashboardBlockSpec] = []
        for blk in semantic_blocks:
            try:
                spec = _semantic_to_engine_spec(blk)
                engine_specs.append(spec)
            except Exception:
                continue

        # Compute layout with x/y/width/height
        computed_layout = compute_layout(engine_specs)

        # Stream each computed block so the agent can persist them
        for block in computed_layout.get("blocks", []):
            yield ToolProgressEvent(
                type="tool.progress",
                payload={"stage": "block.completed", "block": block}
            )

        # Build output with both semantic and computed blocks
        semantic_outputs = []
        for blk in semantic_blocks:
            try:
                semantic_outputs.append(SemanticBlockOutput(
                    type=blk.get("type", "visualization"),
                    visualization_id=blk.get("visualization_id"),
                    content=blk.get("content"),
                    variant=blk.get("variant"),
                    role=blk.get("role"),
                    importance=blk.get("importance"),
                    size=blk.get("size"),
                    section=blk.get("section"),
                    order=blk.get("order"),
                    view_overrides=blk.get("view_overrides"),
                ))
            except Exception:
                continue

        output = CreateDashboardOutput(
            semantic_blocks=semantic_outputs,
            layout=computed_layout,
            report_title=data.report_title,
        )

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {
                    "summary": "Dashboard designed with semantic layout",
                    "layout": computed_layout,
                    "block_count": len(computed_layout.get("blocks", [])),
                    "semantic_block_count": len(semantic_blocks),
                }
            }
        )
