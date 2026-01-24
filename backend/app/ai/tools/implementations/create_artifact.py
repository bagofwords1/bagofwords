import json
from typing import AsyncIterator, Dict, Any, Type, List

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.create_artifact import CreateArtifactInput, CreateArtifactOutput
from app.ai.llm import LLM
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.dependencies import async_session_maker


class CreateArtifactTool(Tool):
    """Tool for generating React-based artifact code for dashboards.

    This tool generates standalone React/JSX code that renders visualizations
    using ECharts, styled with Tailwind CSS, and transpiled in-browser via Babel.

    The generated code runs in a sandboxed iframe and receives visualization
    data via window.ARTIFACT_DATA.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_artifact",
            description=(
                "Compose multiple visualizations into a unified, interactive dashboard or slide presentation. "
                "Use this AFTER creating visualizations with create_data to combine them into a polished layout with "
                "KPI cards, charts, and responsive grids. Supports two modes: 'page' for interactive dashboards. "
                "Pass visualization_ids from previously created visualizations. "
                "IMPORTANT: Only use visualizations with successful step status (step.status == 'success'). "
                "Visualizations with failed or pending steps will be automatically excluded."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateArtifactInput.model_json_schema(),
            output_schema=CreateArtifactOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=120,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["artifact",  "dashboard", "slides"],
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateArtifactInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateArtifactOutput

    def _build_viz_profile(self, viz: Dict[str, Any], allow_llm_see_data: bool) -> Dict[str, Any]:
        """Build a privacy-aware profile of a visualization's data."""
        profile: Dict[str, Any] = {
            "id": viz.get("id"),
            "title": viz.get("title"),
            "chart_type": viz.get("data_model_type") or "table",
            "row_count": viz.get("row_count", 0),
            "columns": viz.get("columns", []),
        }

        # Include data model hints
        data_model = viz.get("dataModel") or {}
        if data_model:
            series = data_model.get("series", [])
            if series:
                profile["series_config"] = series[:3]  # First 3 series configs
            if data_model.get("group_by"):
                profile["group_by"] = data_model.get("group_by")

        # Include view configuration hints
        view = viz.get("view") or {}
        if view:
            inner_view = view.get("view") or view
            profile["view_config"] = {
                "type": inner_view.get("type"),
                "x": inner_view.get("x"),
                "y": inner_view.get("y"),
                "category": inner_view.get("category"),
                "value": inner_view.get("value"),
            }
            # Include palette if present
            palette = inner_view.get("palette") or {}
            if palette.get("colors"):
                profile["colors"] = palette.get("colors")[:5]

        # Include sample data if allowed
        if allow_llm_see_data:
            rows = viz.get("rows", [])
            if rows:
                profile["sample_rows"] = rows[:5]  # First 5 rows
                # Compute basic stats for numeric columns
                if rows and isinstance(rows[0], dict):
                    stats = {}
                    for col in viz.get("columns", []):
                        col_name = col if isinstance(col, str) else col.get("field", col.get("name"))
                        if col_name:
                            values = [r.get(col_name) for r in rows if r.get(col_name) is not None]
                            numeric_values = [v for v in values if isinstance(v, (int, float))]
                            if numeric_values:
                                stats[col_name] = {
                                    "min": min(numeric_values),
                                    "max": max(numeric_values),
                                    "sample_values": numeric_values[:3]
                                }
                            elif values:
                                unique = list(set(str(v) for v in values[:20]))
                                stats[col_name] = {
                                    "unique_count": len(unique),
                                    "sample_values": unique[:5]
                                }
                    if stats:
                        profile["column_stats"] = stats

        return profile

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateArtifactInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Artifact"})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

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

        instruction_context_builder = runtime_ctx.get("instruction_context_builder") or (
            getattr(context_hub, "instruction_builder", None) if context_hub else None
        )

        # Fetch visualizations by ID from database
        visualizations: List[Dict[str, Any]] = []
        warnings: List[str] = []
        included_viz_ids: List[str] = []

        # Build a lookup of query data from context_hub for enrichment
        query_data_lookup: Dict[str, Dict[str, Any]] = {}
        try:
            if context_hub is not None:
                view = context_hub.get_view()
                qsec = getattr(getattr(view, 'warm', None), 'queries', None)
                items = getattr(qsec, 'items', []) if qsec else []
                for it in (items or []):
                    query_id = getattr(it, 'query_id', None)
                    if query_id:
                        query_data_lookup[str(query_id)] = {
                            "columns": list(getattr(it, 'column_names', []) or []),
                            "row_count": getattr(it, 'row_count', 0),
                            "rows": list(getattr(it, 'rows', []) or [])[:100],
                            "dataModel": getattr(it, 'data_model', None) or {},
                        }
        except Exception:
            pass

        # Fetch and validate visualizations from DB
        report_id = str(report.id) if report else None
        for viz_id in data.visualization_ids:
            try:
                result = await db.execute(
                    select(Visualization).where(Visualization.id == viz_id)
                )
                viz = result.scalar_one_or_none()

                if viz is None:
                    warnings.append(f"Visualization {viz_id} not found")
                    continue

                # Validate viz belongs to the report
                if report_id and str(viz.report_id) != report_id:
                    warnings.append(f"Visualization {viz_id} does not belong to this report")
                    continue

                # Check if the associated step is successful
                step_status = None
                if viz.query and viz.query.default_step:
                    step_status = viz.query.default_step.status
                elif viz.query and viz.query.steps:
                    # Fallback to the latest step if no default_step
                    step_status = viz.query.steps[-1].status if viz.query.steps else None

                if step_status != "success":
                    warnings.append(f"Visualization {viz_id} skipped: step status is '{step_status or 'unknown'}' (not success)")
                    continue

                # Build visualization entry
                view_dict = viz.view or {}
                query_id = str(viz.query_id) if viz.query_id else None
                query_data = query_data_lookup.get(query_id, {}) if query_id else {}

                ventry = {
                    "id": str(viz.id),
                    "title": viz.title,
                    "query_id": query_id,
                    "view": self._trim_none(view_dict),
                    "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                    "columns": query_data.get("columns", []),
                    "row_count": query_data.get("row_count", 0),
                    "rows": query_data.get("rows", []),
                    "dataModel": query_data.get("dataModel", {}),
                }
                visualizations.append(ventry)
                included_viz_ids.append(str(viz.id))

            except Exception as e:
                warnings.append(f"Error fetching visualization {viz_id}: {str(e)}")

        # Build visualization profiles (privacy-aware)
        viz_profiles = [self._build_viz_profile(v, allow_llm_see_data) for v in visualizations]

        # Build instruction context
        instructions_context = ""
        try:
            if instruction_context_builder is not None:
                inst_section = await instruction_context_builder.build(categories=["dashboard", "visualization", "general"])
                instructions_context = inst_section.render() or ""
        except Exception:
            pass

        # Create artifact early with pending status so frontend can show it
        artifact = Artifact(
            report_id=str(report.id) if report else None,
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
            title=data.title or "Untitled Artifact",
            mode=data.mode,
            content={},  # Empty content initially
            generation_prompt=data.prompt,
            version=1,
            status="pending",
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)

        # Notify frontend that artifact is created (pending)
        yield ToolProgressEvent(
            type="tool.progress",
            payload={
                "stage": "artifact_created",
                "artifact_id": str(artifact.id),
                "status": "pending",
            }
        )

        # Build the prompt for generating React code
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_code"})

        prompt = self._build_prompt(
            user_prompt=data.prompt,
            title=data.title,
            mode=data.mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=getattr(report, 'title', None) if report else None,
            allow_llm_see_data=allow_llm_see_data,
        )

        # Stream from LLM
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        buffer = ""
        slides_detected = 0  # Track number of slides detected during streaming

        async for chunk in llm.inference_stream(
            prompt,
            usage_scope="create_artifact",
            usage_scope_ref_id=str(report.id) if report else None,
        ):
            buffer += chunk

            # For slides mode, detect new slides as they're generated
            if data.mode == "slides":
                # Count slide sections in buffer
                current_slides = buffer.count('<section class="slide"')
                if current_slides > slides_detected:
                    # New slide detected
                    for i in range(slides_detected, current_slides):
                        yield ToolProgressEvent(
                            type="tool.progress",
                            payload={
                                "stage": "slide_generated",
                                "slide_index": i,
                                "total_slides": current_slides
                            }
                        )
                    slides_detected = current_slides

            # Stream partial updates
            if len(buffer) % 100 == 0:  # Throttle updates
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={"stage": "generating", "chars": len(buffer)}
                )

        # Extract the code from the response
        code = self._extract_code(buffer, mode=data.mode)

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving_artifact"})

        # Build content object (slides structure is parsed from HTML at export time)
        content: Dict[str, Any] = {
            "code": code,
            "visualization_ids": included_viz_ids,
        }

        # Update the pending artifact with content and mark as completed
        artifact.content = content
        artifact.status = "completed"
        await db.commit()
        await db.refresh(artifact)

        output = CreateArtifactOutput(
            artifact_id=str(artifact.id),
            code=code,
            mode=data.mode,
            title=data.title,
            version=artifact.version,
        )

        observation: Dict[str, Any] = {
            "summary": f"Created artifact '{data.title or 'Untitled'}' with {len(code)} characters of code",
            "artifact_id": str(artifact.id),
            "mode": data.mode,
            "visualization_count": len(visualizations),
            "visualization_ids": included_viz_ids,
        }
        if warnings:
            observation["warnings"] = warnings

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": observation,
            }
        )

    def _trim_none(self, obj: Any) -> Any:
        """Remove None values and empty collections from nested structures."""
        try:
            if isinstance(obj, dict):
                out = {}
                for k, v in obj.items():
                    tv = self._trim_none(v)
                    if tv is None:
                        continue
                    if isinstance(tv, (dict, list)) and len(tv) == 0:
                        continue
                    out[k] = tv
                return out
            if isinstance(obj, list):
                items = [self._trim_none(v) for v in obj]
                return [v for v in items if not (v is None or (isinstance(v, (dict, list)) and len(v) == 0))]
            return obj
        except Exception:
            return obj

    def _build_slides_prompt(
        self,
        user_prompt: str,
        title: str | None,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str,
        report_title: str | None,
        allow_llm_see_data: bool,
    ) -> str:
        """Build the prompt for generating slides (pure HTML + vanilla JS)."""
        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        return f"""You are a world-class frontend developer and data visualization expert. Create a STUNNING, publication-quality slide presentation.

═══════════════════════════════════════════════════════════════════════════════
AVAILABLE (pre-loaded globally)
═══════════════════════════════════════════════════════════════════════════════

• **Tailwind CSS** - All utility classes available
  - Use modern design: rounded-xl, shadow-lg, backdrop-blur, gradients
  - Dark themes: bg-slate-900, text-white, text-slate-400
  - Flexbox, grid, spacing utilities

• **Vanilla JavaScript** - No frameworks needed
  - DOM manipulation: querySelector, classList, addEventListener
  - Access data via window.ARTIFACT_DATA

**DO NOT USE:** React, Babel, JSX, or any framework. Pure HTML + JS only.

═══════════════════════════════════════════════════════════════════════════════
DATA ACCESS
═══════════════════════════════════════════════════════════════════════════════

Data is available via `window.ARTIFACT_DATA`:
```javascript
const data = window.ARTIFACT_DATA;
const report = data.report;  // {{id, title, theme}}
const visualizations = data.visualizations;  // Array of viz objects
```

**Note:** A global loading spinner is shown until data arrives. You do NOT need to implement loading state.

Each visualization object contains:
- `id`, `title` - Identification
- `rows` - Array of data objects (the actual data)
- `columns` - Column definitions

═══════════════════════════════════════════════════════════════════════════════
YOUR VISUALIZATIONS
═══════════════════════════════════════════════════════════════════════════════

{viz_json}

{"(Full sample data included above)" if allow_llm_see_data else "(Data samples hidden for privacy - use column names and row_count to understand the data structure)"}

═══════════════════════════════════════════════════════════════════════════════
SLIDES MODE - PURE HTML PRESENTATION (NO REACT)
═══════════════════════════════════════════════════════════════════════════════

You are creating a **slide presentation** using PURE HTML + Tailwind CSS.
**DO NOT use React, Babel, or JSX.** Use vanilla JavaScript only.

**CRITICAL: Use these EXACT CSS classes for PPTX export compatibility:**

**Slide Container:**
```html
<section class="slide" data-slide="0" data-type="title">
  <!-- data-type: "title", "metrics", "bullets", "chart", "text" -->
</section>
```

**Title Slide (data-type="title"):**
```html
<h1 class="pptx-title">Main Title Here</h1>
<p class="pptx-subtitle">Subtitle or date here</p>
```

**Slide Heading (all other slides):**
```html
<h2 class="pptx-heading">Slide Heading</h2>
```

**Metrics/KPIs (data-type="metrics"):**
```html
<div class="pptx-metric">
  <div class="pptx-metric-value">$1.2M</div>
  <div class="pptx-metric-label">Total Revenue</div>
  <div class="pptx-metric-change">+15% vs last month</div>
</div>
```

**Bullet Points (data-type="bullets"):**
```html
<ul>
  <li class="pptx-bullet">First key point</li>
  <li class="pptx-bullet">Second key point</li>
</ul>
```

**Insights/Takeaways:**
```html
<p class="pptx-insight">Key insight or takeaway text</p>
```

**Code Snippets (data-type="code"):**
```html
<pre class="pptx-code">SELECT * FROM sales</pre>
```

**Chart Placeholders (data-type="chart"):**
```html
<div class="pptx-chart" data-chart-type="bar">
  <!-- Chart rendered by ECharts -->
</div>
```

**Navigation (vanilla JS):**
- Toggle `hidden` class to show/hide slides
- Arrow keys: ArrowRight/Space = next, ArrowLeft = previous
- Click navigation dots at bottom
- Navigation arrows on edges

**Design:**
- Dark background (bg-slate-900) with light text
- Each slide: `min-h-screen flex flex-col items-center justify-center`
- Large typography, high contrast
- One key insight per slide

**Slide count:** 4-8 slides depending on data.

**IMPORTANT:** Always include the pptx-* classes even if you add additional Tailwind classes.
Example: `<h1 class="pptx-title text-6xl font-bold text-white">Title</h1>`

═══════════════════════════════════════════════════════════════════════════════
DESIGN REQUEST
═══════════════════════════════════════════════════════════════════════════════

**Report Title:** {report_title or title or 'Dashboard'}
**Artifact Mode:** slides
**User Request:** {user_prompt}

{f"**Organization Instructions:**{chr(10)}{instructions_context}" if instructions_context else ""}

═══════════════════════════════════════════════════════════════════════════════
DESIGN PRINCIPLES
═══════════════════════════════════════════════════════════════════════════════

Create something BEAUTIFUL. Think:
- **Visual hierarchy** - What's the main story? Lead with it
- **Whitespace** - Let elements breathe, don't crowd
- **Color harmony** - Use a cohesive palette, accent colors for emphasis
- **Typography** - Clear hierarchy, readable sizes
- **Cards & containers** - Group related content, subtle shadows
- **Micro-interactions** - Hover states, smooth transitions
- **Data storytelling** - Choose chart types that reveal insights

Example slide patterns:
- Slide 1: Title with report name, date, key metric teaser
- Slide 2-3: Hero KPI cards with big numbers and trend indicators
- Slide 4-5: Full-width charts with key insights as subtitles
- Slide 6: Comparison or breakdown visualization
- Slide 7: Summary with key takeaways as bullet points

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

```html
<!-- Slides Container -->
<div id="slides-container" class="relative w-full h-screen overflow-hidden bg-slate-900">

  <!-- Slide 0: Title -->
  <section class="slide min-h-screen flex flex-col items-center justify-center px-8" data-slide="0" data-type="title">
    <h1 class="pptx-title text-6xl font-bold text-white text-center">Monthly Sales Report</h1>
    <p class="pptx-subtitle text-2xl text-slate-400 mt-4">January 2025 Performance Review</p>
  </section>

  <!-- Slide 1: Metrics -->
  <section class="slide min-h-screen flex flex-col items-center justify-center px-8 hidden" data-slide="1" data-type="metrics">
    <h2 class="pptx-heading text-4xl font-bold text-white mb-12">Key Metrics</h2>
    <div class="flex gap-8">
      <div class="pptx-metric text-center">
        <div class="pptx-metric-value text-5xl font-bold text-white">$1.2M</div>
        <div class="pptx-metric-label text-slate-400 mt-2">Total Revenue</div>
        <div class="pptx-metric-change text-green-400 text-sm mt-1">+15% vs last month</div>
      </div>
      <div class="pptx-metric text-center">
        <div class="pptx-metric-value text-5xl font-bold text-white">2,450</div>
        <div class="pptx-metric-label text-slate-400 mt-2">Orders</div>
        <div class="pptx-metric-change text-green-400 text-sm mt-1">+8%</div>
      </div>
    </div>
  </section>

  <!-- Slide 2: Key Points -->
  <section class="slide min-h-screen flex flex-col items-center justify-center px-8 hidden" data-slide="2" data-type="bullets">
    <h2 class="pptx-heading text-4xl font-bold text-white mb-8">Key Insights</h2>
    <ul class="space-y-4">
      <li class="pptx-bullet text-xl text-white">Revenue grew 15% compared to last quarter</li>
      <li class="pptx-bullet text-xl text-white">Customer retention improved by 12%</li>
      <li class="pptx-bullet text-xl text-white">New product line exceeded expectations</li>
    </ul>
    <p class="pptx-insight text-lg text-blue-400 mt-8 italic">Overall performance exceeded targets across all metrics</p>
  </section>

  <!-- Navigation Dots -->
  <div class="fixed bottom-8 left-1/2 -translate-x-1/2 flex gap-2">
    <button class="nav-dot w-3 h-3 rounded-full bg-white" data-goto="0"></button>
    <button class="nav-dot w-3 h-3 rounded-full bg-white/30" data-goto="1"></button>
    <button class="nav-dot w-3 h-3 rounded-full bg-white/30" data-goto="2"></button>
  </div>

  <!-- Arrow Navigation -->
  <button id="prev-btn" class="fixed left-4 top-1/2 -translate-y-1/2 text-white/50 hover:text-white text-4xl">&larr;</button>
  <button id="next-btn" class="fixed right-4 top-1/2 -translate-y-1/2 text-white/50 hover:text-white text-4xl">&rarr;</button>
</div>

<script>
  let currentSlide = 0;
  const slides = document.querySelectorAll('.slide');
  const dots = document.querySelectorAll('.nav-dot');
  const totalSlides = slides.length;

  function showSlide(index) {{
    slides.forEach((s, i) => s.classList.toggle('hidden', i !== index));
    dots.forEach((d, i) => {{
      d.classList.toggle('bg-white', i === index);
      d.classList.toggle('bg-white/30', i !== index);
    }});
    currentSlide = index;
  }}

  document.getElementById('prev-btn').onclick = () => showSlide(Math.max(0, currentSlide - 1));
  document.getElementById('next-btn').onclick = () => showSlide(Math.min(totalSlides - 1, currentSlide + 1));
  dots.forEach(d => d.onclick = () => showSlide(parseInt(d.dataset.goto)));

  document.addEventListener('keydown', (e) => {{
    if (e.key === 'ArrowRight' || e.key === ' ') showSlide(Math.min(totalSlides - 1, currentSlide + 1));
    if (e.key === 'ArrowLeft') showSlide(Math.max(0, currentSlide - 1));
  }});
</script>
```

REQUIREMENTS:
1. Output HTML code only (NO React/Babel - pure HTML + vanilla JS)
2. Use `<section class="slide">` for each slide with `data-slide="N"` attribute
3. First slide visible, others have `hidden` class
4. Include navigation: dots at bottom, arrow buttons on sides
5. Include vanilla JS for keyboard navigation (arrows, space)
6. Access data via `window.ARTIFACT_DATA` directly (no React hooks)
7. Each slide: `min-h-screen flex flex-col items-center justify-center`
8. Dark theme: bg-slate-900 background, white/slate text
9. Make it GORGEOUS - this is a presentation showcase

DO NOT use React, Babel, JSX, or any framework - PURE HTML + Tailwind + vanilla JavaScript only.

Now create the slide presentation:"""

    def _build_page_prompt(
        self,
        user_prompt: str,
        title: str | None,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str,
        report_title: str | None,
        allow_llm_see_data: bool,
    ) -> str:
        """Build the prompt for generating page/dashboard (React + ECharts)."""
        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        return f"""You are a world-class frontend developer and data visualization expert. Create a STUNNING, publication-quality dashboard.

═══════════════════════════════════════════════════════════════════════════════
AVAILABLE LIBRARIES (pre-loaded globally, do NOT import)
═══════════════════════════════════════════════════════════════════════════════

• **React 18** - `React`, `ReactDOM` available globally
  - Use hooks: useState, useEffect, useRef, useMemo, useCallback
  - Create beautiful, reusable components

• **ECharts 5** - `echarts` available globally
  - Full charting library: bar, line, area, pie, scatter, heatmap, radar, treemap, sunburst, gauge, funnel, sankey, etc.
  - Rich animations, tooltips, legends, gradients
  - Responsive with chart.resize()

• **Tailwind CSS** - All utility classes available
  - Use modern design: rounded-xl, shadow-lg, backdrop-blur, gradients
  - Dark/light themes, responsive grids, flexbox
  - Animations: animate-pulse, transition-all, hover effects

• **LoadingSpinner** - `<LoadingSpinner />` available globally
  - Props: `size` (number, default 24), `className` (string)
  - Inherits text color via currentColor
  - Use for loading states instead of building your own

═══════════════════════════════════════════════════════════════════════════════
DATA ACCESS
═══════════════════════════════════════════════════════════════════════════════

Data is available via `window.ARTIFACT_DATA`:
```javascript
const data = useArtifactData(); // React hook - returns null while loading
// data = {{ report: {{id, title, theme}}, visualizations: [...] }}
```

Each visualization object contains:
- `id`, `title` - Identification
- `rows` - Array of data objects (the actual data)
- `columns` - Column definitions
- `view` - Chart configuration hints
- `dataModel` - Series/axis configuration hints

═══════════════════════════════════════════════════════════════════════════════
YOUR VISUALIZATIONS
═══════════════════════════════════════════════════════════════════════════════

{viz_json}

{"(Full sample data included above)" if allow_llm_see_data else "(Data samples hidden for privacy - use column names and row_count to understand the data structure)"}

═══════════════════════════════════════════════════════════════════════════════
DESIGN REQUEST
═══════════════════════════════════════════════════════════════════════════════

**Report Title:** {report_title or title or 'Dashboard'}
**Artifact Mode:** page
**User Request:** {user_prompt}

{f"**Organization Instructions:**{chr(10)}{instructions_context}" if instructions_context else ""}

═══════════════════════════════════════════════════════════════════════════════
DESIGN PRINCIPLES
═══════════════════════════════════════════════════════════════════════════════

Create something BEAUTIFUL. Think:
- **Visual hierarchy** - What's the main story? Lead with it
- **Whitespace** - Let elements breathe, don't crowd
- **Color harmony** - Use a cohesive palette, accent colors for emphasis
- **Typography** - Clear hierarchy, readable sizes
- **Cards & containers** - Group related content, subtle shadows
- **Micro-interactions** - Hover states, smooth transitions
- **Data storytelling** - Choose chart types that reveal insights

Example design patterns:
- Hero KPI cards at top with large numbers and sparklines
- Main chart taking center stage with gradient backgrounds
- Supporting charts in a responsive grid
- Subtle animations on load
- Consistent border-radius and spacing

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

```
<script type="text/babel">
// Your React code here

function App() {{
  const data = useArtifactData();

  if (!data) {{
    return <LoadingState />;
  }}

  return (
    // Your gorgeous dashboard
  );
}}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
```

REQUIREMENTS:
1. Start with `<script type="text/babel">` and end with `</script>`
2. Use the `useArtifactData()` hook for reactive data access
3. Use `<LoadingSpinner size={{32}} />` for loading state (do NOT build your own spinner)
4. Initialize ECharts in useEffect, dispose on cleanup, handle resize
5. Make it responsive (works on mobile and desktop)
6. Make it GORGEOUS - this is a showcase piece

Example loading state:
```jsx
if (!data) {{
  return (
    <div className="flex items-center justify-center h-screen text-gray-400">
      <LoadingSpinner size={{32}} />
    </div>
  );
}}
```

Now create the dashboard:"""

    def _build_prompt(
        self,
        user_prompt: str,
        title: str | None,
        mode: str,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str,
        report_title: str | None,
        allow_llm_see_data: bool,
    ) -> str:
        """Build the prompt for generating artifact code. Dispatches to mode-specific builders."""
        if mode == "slides":
            return self._build_slides_prompt(
                user_prompt=user_prompt,
                title=title,
                viz_profiles=viz_profiles,
                instructions_context=instructions_context,
                report_title=report_title,
                allow_llm_see_data=allow_llm_see_data,
            )
        return self._build_page_prompt(
            user_prompt=user_prompt,
            title=title,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=report_title,
            allow_llm_see_data=allow_llm_see_data,
        )

    def _extract_code(self, response: str, mode: str = "page") -> str:
        """Extract the code from the LLM response.

        For 'page' mode: Extract React code from <script type="text/babel"> tags
        For 'slides' mode: Extract HTML content (everything after the JSON block)
        """
        if mode == "slides":
            return self._extract_slides_html(response)

        # Dashboard mode - extract React code from script tags
        start_marker = "<script type=\"text/babel\">"
        end_marker = "</script>"

        start_idx = response.find(start_marker)
        if start_idx == -1:
            # Try alternative markers
            start_marker = "<script type='text/babel'>"
            start_idx = response.find(start_marker)

        if start_idx != -1:
            end_idx = response.find(end_marker, start_idx)
            if end_idx != -1:
                return response[start_idx:end_idx + len(end_marker)]

        # If no script tags found, wrap the response
        code = response.strip()
        if not code.startswith("<script"):
            code = f'<script type="text/babel">\n{code}\n</script>'

        return code

    def _extract_slides_html(self, response: str) -> str:
        """Extract HTML content for slides mode."""
        import re

        # Try to find HTML code block
        html_match = re.search(r'```html?\s*([\s\S]*?)```', response)
        if html_match:
            return html_match.group(1).strip()

        # Look for the slides container div
        div_start = response.find('<div id="slides-container"')
        if div_start == -1:
            div_start = response.find('<div class="')

        if div_start != -1:
            # Find the matching closing and script
            script_end = response.rfind('</script>')
            if script_end != -1:
                return response[div_start:script_end + 9].strip()
            return response[div_start:].strip()

        # Fallback: return the response as-is
        return response.strip()

