import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, Any, Type, List, Optional

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating artifact code via headless browser."""
    success: bool
    errors: List[str] = field(default_factory=list)
    screenshot_base64: Optional[str] = None
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.create_artifact import CreateArtifactInput, CreateArtifactOutput
from app.ai.llm import LLM
from app.ai.llm.types import ImageInput
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.dependencies import async_session_maker
from app.services.thumbnail_service import ThumbnailService
from app.ai.code_execution.pptx_executor import PptxCodeExecutor, PptxPreviewService
from sqlalchemy import desc


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
                "Create or update artifacts (dashboards, pages, slide presentations) from visualizations. "
                "Requires visualization_ids from create_data results in the conversation. "
                "Modes: 'page' for interactive dashboards with KPI cards, charts, and responsive grids; "
                "'slides' for presentation decks (exportable to PPTX). "
                "To update an existing artifact, provide existing_artifact_id - the previous layout and code will be used as a base. "
                "Only visualizations with successful step status are included."
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

    # Path to the sandbox HTML file (relative to project root)
    # __file__ -> implementations -> tools -> ai -> app -> backend -> project_root
    SANDBOX_HTML_PATH = Path(__file__).parent.parent.parent.parent.parent.parent / "frontend" / "public" / "artifact-sandbox.html"

    # Validation-specific script to inject (replaces message-based data loading)
    VALIDATION_SCRIPT = """
    <script>
      // ===========================================
      // Validation Mode Overrides
      // ===========================================
      (function() {
        // Inject artifact data directly (no message passing needed in validation)
        window.ARTIFACT_DATA = __ARTIFACT_DATA_JSON__;

        // Track errors for validation
        window.__ARTIFACT_ERRORS__ = [];

        // Augment existing error handler to track errors
        var originalOnError = window.onerror;
        window.onerror = function(msg, url, lineNo, columnNo, error) {
          window.__ARTIFACT_ERRORS__.push({
            type: 'error',
            message: msg,
            line: lineNo,
            column: columnNo,
            stack: error ? error.stack : null
          });
          if (originalOnError) {
            return originalOnError(msg, url, lineNo, columnNo, error);
          }
          return false;
        };

        window.addEventListener('unhandledrejection', function(event) {
          window.__ARTIFACT_ERRORS__.push({
            type: 'unhandledrejection',
            message: event.reason ? event.reason.message || String(event.reason) : 'Unknown rejection'
          });
        });

        // Signal when render is complete
        window.__ARTIFACT_RENDER_COMPLETE__ = false;

        // Hide global loader immediately since we have data
        var loader = document.getElementById('global-loader');
        if (loader) loader.classList.add('hidden');
      })();
    </script>
    """

    async def _generate_thumbnail_background(
        self,
        artifact_id: str,
        html_content: str,
        mode: str = "page",
    ) -> None:
        """Generate thumbnail in background and update artifact.

        Runs independently with its own database session.
        """
        try:
            thumbnail_service = ThumbnailService()
            thumbnail_path = await thumbnail_service.generate_thumbnail(
                artifact_id=artifact_id,
                html_content=html_content,
                mode=mode,
            )
            if thumbnail_path:
                # Use a fresh database session for the background update
                async with async_session_maker() as db:
                    from sqlalchemy import update
                    from app.models.artifact import Artifact
                    stmt = update(Artifact).where(Artifact.id == artifact_id).values(thumbnail_path=thumbnail_path)
                    await db.execute(stmt)
                    await db.commit()
        except Exception as e:
            logger.warning(f"Failed to generate thumbnail for artifact {artifact_id}: {e}")

    def _build_validation_html(self, artifact_data: dict, code: str, mode: str = "page") -> str:
        """Build HTML for validation by reading sandbox file and injecting validation code.

        Args:
            artifact_data: The data to inject as window.ARTIFACT_DATA
            code: The LLM-generated artifact code
            mode: 'page' for React dashboards, 'slides' for pure HTML presentations

        Returns:
            Complete HTML string ready for headless browser rendering
        """
        data_json = json.dumps(artifact_data, default=str)

        # Slides mode: pure HTML + Tailwind (no React/Babel)
        # Use string replacement instead of f-string to avoid JSON escaping issues
        if mode == "slides":
            slides_template = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    html, body { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
    .slide { transition: opacity 0.3s ease-in-out; }
  </style>
</head>
<body class="bg-slate-900">
  <script>
    window.ARTIFACT_DATA = __ARTIFACT_DATA_JSON__;
    window.__ARTIFACT_ERRORS__ = [];
    window.onerror = function(msg, url, lineNo, columnNo, error) {
      window.__ARTIFACT_ERRORS__.push({
        type: 'error',
        message: msg,
        line: lineNo,
        column: columnNo,
        stack: error ? error.stack : null
      });
      return false;
    };
    window.addEventListener('unhandledrejection', function(event) {
      window.__ARTIFACT_ERRORS__.push({
        type: 'unhandledrejection',
        message: event.reason ? event.reason.message || String(event.reason) : 'Unknown rejection'
      });
    });
    window.__ARTIFACT_RENDER_COMPLETE__ = false;
    setTimeout(function() {
      window.__ARTIFACT_RENDER_COMPLETE__ = true;
    }, 500);
  </script>

  __LLM_GENERATED_CODE__
</body>
</html>"""
            return slides_template.replace("__ARTIFACT_DATA_JSON__", data_json).replace("__LLM_GENERATED_CODE__", code)

        # Page mode: Read the sandbox HTML file for React/Babel
        try:
            sandbox_html = self.SANDBOX_HTML_PATH.read_text()
        except FileNotFoundError:
            logger.error(f"Sandbox HTML not found at {self.SANDBOX_HTML_PATH}")
            raise

        # Prepare the validation script with data injected
        validation_script = self.VALIDATION_SCRIPT.replace("__ARTIFACT_DATA_JSON__", data_json)

        # Insert validation script after <body> tag
        html = sandbox_html.replace("<body>", f"<body>\n{validation_script}")

        # Replace the LLM_GENERATED_CODE placeholder with actual code
        html = html.replace("<!-- LLM_GENERATED_CODE -->", code)

        # Add render complete signal at the end
        render_complete_script = """
    <script>
      // Mark render complete after a short delay to allow React to mount
      setTimeout(function() {
        window.__ARTIFACT_RENDER_COMPLETE__ = true;
      }, 100);
    </script>
    """
        html = html.replace("</body>", f"{render_complete_script}</body>")

        return html

    async def _validate_artifact(
        self,
        code: str,
        mode: str,
        visualizations: List[Dict[str, Any]],
        report: Any,
        allow_llm_see_data: bool = True,
    ) -> ValidationResult:
        """Validate artifact code by rendering in a headless browser.

        Args:
            code: The generated artifact code
            mode: 'page' or 'slides'
            visualizations: List of visualization data dicts
            report: The report object (for building ARTIFACT_DATA)
            allow_llm_see_data: If False, skip screenshot capture for privacy

        Returns:
            ValidationResult with success status, errors, and optional screenshot
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed, skipping artifact validation")
            return ValidationResult(
                success=True,
                errors=["Playwright not installed - validation skipped"]
            )

        # Build the artifact data structure
        artifact_data = {
            "report": {
                "id": str(report.id) if report else None,
                "title": getattr(report, 'title', None) if report else None,
                "theme": getattr(report, 'theme', None) if report else None,
            },
            "visualizations": visualizations,
        }

        # Build the HTML to render using the sandbox file (mode-aware)
        html = self._build_validation_html(artifact_data, code, mode=mode)

        errors: List[str] = []
        screenshot_base64: Optional[str] = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 720})

                # Capture console errors
                def handle_console(msg):
                    if msg.type == "error":
                        errors.append(f"Console error: {msg.text}")

                page.on("console", handle_console)

                # Capture page errors
                def handle_page_error(error):
                    errors.append(f"Page error: {str(error)}")

                page.on("pageerror", handle_page_error)

                # Load the HTML content directly (no network request needed)
                await page.set_content(html, wait_until="networkidle")

                # Wait for render to complete (with timeout)
                try:
                    await page.wait_for_function(
                        "window.__ARTIFACT_RENDER_COMPLETE__ === true",
                        timeout=10000
                    )
                except Exception as e:
                    errors.append(f"Render timeout: {str(e)}")

                # Give React/ECharts a bit more time to fully render
                await asyncio.sleep(1.0)

                # Collect any errors captured by our error handlers
                captured_errors = await page.evaluate("window.__ARTIFACT_ERRORS__")
                for err in captured_errors:
                    err_msg = err.get("message", "Unknown error")
                    if err.get("line"):
                        err_msg += f" (line {err.get('line')})"
                    errors.append(err_msg)

                # Take screenshot only if allow_llm_see_data is True (privacy setting)
                if allow_llm_see_data:
                    screenshot_bytes = await page.screenshot(type="png", full_page=False)
                    screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

                await browser.close()

        except Exception as e:
            logger.exception("Error during artifact validation")
            errors.append(f"Validation error: {str(e)}")

        return ValidationResult(
            success=len(errors) == 0,
            errors=errors,
            screenshot_base64=screenshot_base64,
        )

    async def _fix_code(
        self,
        code: str,
        errors: List[str],
        mode: str,
        runtime_ctx: Dict[str, Any],
        prompt_context: Dict[str, Any],
        screenshot_base64: Optional[str] = None,
    ) -> str:
        """Attempt to fix code errors using the same prompt with error context.

        Args:
            code: The broken code
            errors: List of error messages
            mode: 'page' or 'slides'
            runtime_ctx: Runtime context for LLM access
            prompt_context: Context needed to rebuild the original prompt
                (user_prompt, title, viz_profiles, instructions_context,
                 report_title, allow_llm_see_data, messages_context, previous_artifacts)
            screenshot_base64: Optional screenshot of the broken render for visual context

        Returns:
            Fixed code string
        """
        error_text = "\n".join(f"- {e}" for e in errors[:5])  # Limit to first 5 errors

        # Rebuild the original prompt with full context
        base_prompt = self._build_prompt(
            user_prompt=prompt_context["user_prompt"],
            title=prompt_context["title"],
            mode=mode,
            viz_profiles=prompt_context["viz_profiles"],
            instructions_context=prompt_context["instructions_context"],
            report_title=prompt_context["report_title"],
            allow_llm_see_data=prompt_context["allow_llm_see_data"],
            messages_context=prompt_context.get("messages_context", ""),
            previous_artifacts=prompt_context.get("previous_artifacts"),
        )

        # Build screenshot context if available
        screenshot_context = ""
        if screenshot_base64:
            screenshot_context = "\n\nA screenshot of the current broken render is attached. Use it to understand visual issues like layout problems, missing elements, or rendering errors."

        # Append error context and the broken code
        fix_prompt = f"""{base_prompt}

═══════════════════════════════════════════════════════════════════════════════
CRITICAL: FIX THESE ERRORS
═══════════════════════════════════════════════════════════════════════════════

The previous code attempt had the following runtime errors that MUST be fixed:

{error_text}{screenshot_context}

Previous broken code:
```
{code}
```

Fix these errors while keeping the same design and functionality. Output the corrected code now:"""

        # Use the same model for fixes
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)

        # Build image input if screenshot is available and model supports vision
        images: Optional[List[ImageInput]] = None
        model = runtime_ctx.get("model")
        if screenshot_base64 and model and getattr(model, "supports_vision", False):
            images = [ImageInput(data=screenshot_base64, media_type="image/png", source_type="base64")]

        try:
            response = await llm.inference(
                fix_prompt,
                images=images,
                usage_scope="create_artifact_fix",
                usage_scope_ref_id=None,
            )
            return self._extract_code(response, mode=mode)
        except Exception as e:
            logger.exception("Error fixing code")
            # Return original code if fix fails
            return code

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

        # Early validation: fail immediately if no visualization_ids provided
        if not data.visualization_ids or len(data.visualization_ids) == 0:
            yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Artifact"})
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error": "No visualization_ids provided. At least one visualization is required to create an artifact.",
                    },
                    "observation": {
                        "summary": "Failed to create artifact: no visualization_ids provided",
                        "error": {
                            "type": "validation_error",
                            "message": "visualization_ids is required and must contain at least one visualization ID. Create visualizations using create_data first, then use their IDs here.",
                        },
                    },
                },
            )
            return

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

        # Get conversation history context (similar to create_data.py)
        context_view = runtime_ctx.get("context_view")
        messages_context = ""
        try:
            _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
            messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        except Exception:
            messages_context = ""

        # Fetch previous artifacts for the same report and mode (for iterative refinement)
        previous_artifacts: List[Dict[str, Any]] = []
        try:
            if report:
                result = await db.execute(
                    select(Artifact)
                    .where(Artifact.report_id == str(report.id))
                    .where(Artifact.mode == data.mode)
                    .where(Artifact.status == "completed")
                    .order_by(desc(Artifact.created_at))
                    .limit(3)
                )
                prev_artifacts = result.scalars().all()
                for art in prev_artifacts:
                    artifact_info = {
                        "id": str(art.id),
                        "title": art.title,
                        "created_at": str(art.created_at) if art.created_at else None,
                        "code": (art.content or {}).get("code", "")[:2000],  # Limit code length
                    }
                    previous_artifacts.append(artifact_info)
        except Exception:
            previous_artifacts = []

        # Fetch visualizations by ID from database
        visualizations: List[Dict[str, Any]] = []
        warnings: List[str] = []
        included_viz_ids: List[str] = []

        # Fetch and validate visualizations from DB
        from app.models.query import Query
        from app.models.step import Step
        report_id = str(report.id) if report else None
        for viz_id in data.visualization_ids:
            try:
                # Eagerly load query -> default_step and steps to avoid async lazy loading issues
                result = await db.execute(
                    select(Visualization)
                    .options(
                        selectinload(Visualization.query).selectinload(Query.default_step),
                        selectinload(Visualization.query).selectinload(Query.steps),
                    )
                    .where(Visualization.id == viz_id)
                )
                viz = result.scalar_one_or_none()

                if viz is None:
                    warnings.append(f"Visualization {viz_id} not found")
                    continue

                # Validate viz belongs to the report
                if report_id and str(viz.report_id) != report_id:
                    warnings.append(f"Visualization {viz_id} does not belong to this report")
                    continue

                # Get the step with data (prefer default_step, fallback to latest step)
                step = None
                if viz.query and viz.query.default_step:
                    step = viz.query.default_step
                elif viz.query and viz.query.steps:
                    step = viz.query.steps[-1] if viz.query.steps else None

                # Check if the associated step is successful
                step_status = step.status if step else None
                if step_status != "success":
                    warnings.append(f"Visualization {viz_id} skipped: step status is '{step_status or 'unknown'}' (not success)")
                    continue

                # Get data directly from step (like frontend does)
                step_data = step.data if step else {}
                rows = (step_data.get("rows") or [])[:100] if step_data else []
                raw_columns = step_data.get("columns") or [] if step_data else []
                data_model = step.data_model if step else {}

                # Normalize columns to list of strings (may be objects with field/colId/headerName)
                columns = []
                for c in raw_columns:
                    if isinstance(c, str):
                        columns.append(c)
                    elif isinstance(c, dict):
                        col_name = c.get("field") or c.get("colId") or c.get("headerName") or c.get("name")
                        if col_name:
                            columns.append(col_name)
                    else:
                        columns.append(str(c))

                # Build visualization entry
                view_dict = viz.view or {}
                query_id = str(viz.query_id) if viz.query_id else None

                ventry = {
                    "id": str(viz.id),
                    "title": viz.title,
                    "query_id": query_id,
                    "view": self._trim_none(view_dict),
                    "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                    "columns": columns,
                    "row_count": len(rows),
                    "rows": rows,
                    "dataModel": data_model or {},
                }

                # Debug logging
                logger.info(f"Visualization {viz.title}: {len(rows)} rows, {len(columns)} columns: {columns[:5] if columns else 'none'}")
                if rows:
                    logger.info(f"  Sample row keys: {list(rows[0].keys())[:5] if isinstance(rows[0], dict) else 'not a dict'}")

                visualizations.append(ventry)
                included_viz_ids.append(str(viz.id))

            except Exception as e:
                warnings.append(f"Error fetching visualization {viz_id}: {str(e)}")

        # Early failure: if no valid visualizations were resolved, fail like create_data does with tables
        if not visualizations:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error": "No valid visualizations found. All requested visualization_ids were either not found, don't belong to this report, or have non-success step status.",
                    },
                    "observation": {
                        "summary": "Failed to create artifact: no valid visualizations resolved",
                        "error": {
                            "type": "no_valid_visualizations",
                            "message": "None of the requested visualization_ids could be used. Ensure visualizations exist, belong to this report, and have successful step status.",
                            "requested_ids": data.visualization_ids,
                            "warnings": warnings,
                        },
                    },
                },
            )
            return

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

        # Store prompt context for potential fix iterations
        prompt_context = {
            "user_prompt": data.prompt,
            "title": data.title,
            "viz_profiles": viz_profiles,
            "instructions_context": instructions_context,
            "report_title": getattr(report, 'title', None) if report else None,
            "allow_llm_see_data": allow_llm_see_data,
            "messages_context": messages_context,
            "previous_artifacts": previous_artifacts,
        }

        prompt = self._build_prompt(
            user_prompt=data.prompt,
            title=data.title,
            mode=data.mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=prompt_context["report_title"],
            allow_llm_see_data=allow_llm_see_data,
            messages_context=messages_context,
            previous_artifacts=previous_artifacts,
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

        # ═══════════════════════════════════════════════════════════════════════
        # Mode-specific processing: slides uses python-pptx, page uses browser validation
        # ═══════════════════════════════════════════════════════════════════════

        validation_result: Optional[ValidationResult] = None
        pptx_path: Optional[str] = None
        preview_images: List[str] = []

        if data.mode == "slides":
            # ═══════════════════════════════════════════════════════════════════
            # SLIDES MODE: Execute python-pptx code and generate previews
            # ═══════════════════════════════════════════════════════════════════
            yield ToolProgressEvent(
                type="tool.progress",
                payload={"stage": "executing_pptx_code"}
            )

            try:
                # Prepare data for execution
                report_data = {
                    "id": str(report.id) if report else None,
                    "title": getattr(report, "title", None) if report else None,
                    "theme": getattr(report, "theme", None) if report else None,
                }

                # Setup output path
                uploads_dir = Path(__file__).parent.parent.parent.parent.parent / "uploads" / "pptx"
                uploads_dir.mkdir(parents=True, exist_ok=True)
                output_path = uploads_dir / f"{artifact.id}.pptx"

                # Execute the python-pptx code
                executor = PptxCodeExecutor(logger=logger)
                result_path, output_log = executor.execute_pptx_code(
                    code=code,
                    visualizations=visualizations,
                    report=report_data,
                    output_path=output_path,
                )

                pptx_path = str(result_path)
                validation_result = ValidationResult(success=True)

                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={"stage": "generating_previews"}
                )

                # Generate preview images
                preview_service = PptxPreviewService(logger=logger)
                preview_images = preview_service.generate_previews(
                    pptx_path=result_path,
                    artifact_id=str(artifact.id),
                )

            except Exception as e:
                logger.error(f"PPTX execution failed: {e}")
                validation_result = ValidationResult(
                    success=False,
                    errors=[str(e)],
                )

        else:
            # ═══════════════════════════════════════════════════════════════════
            # PAGE MODE: Validate in headless browser
            # ═══════════════════════════════════════════════════════════════════
            max_validation_attempts = 3

            for attempt in range(max_validation_attempts):
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={
                        "stage": "validating",
                        "attempt": attempt + 1,
                        "max_attempts": max_validation_attempts,
                    }
                )

                validation_result = await self._validate_artifact(
                    code=code,
                    mode=data.mode,
                    visualizations=visualizations,
                    report=report,
                    allow_llm_see_data=allow_llm_see_data,
                )

                if validation_result.success:
                    # Validation passed
                    break

                if attempt < max_validation_attempts - 1:
                    # Try to fix the code
                    yield ToolProgressEvent(
                        type="tool.progress",
                        payload={
                            "stage": "fixing_errors",
                            "attempt": attempt + 1,
                            "errors": validation_result.errors[:3],  # Show first 3 errors
                        }
                    )
                    code = await self._fix_code(
                        code=code,
                        errors=validation_result.errors,
                        mode=data.mode,
                        runtime_ctx=runtime_ctx,
                        prompt_context=prompt_context,
                        screenshot_base64=validation_result.screenshot_base64,
                    )

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving_artifact"})

        # Build content object
        content: Dict[str, Any] = {
            "code": code,
            "visualization_ids": included_viz_ids,
        }

        # Add slides-specific content
        if data.mode == "slides" and preview_images:
            content["preview_images"] = preview_images

        # Update the pending artifact with content and mark as completed
        artifact.content = content
        artifact.status = "completed" if (validation_result and validation_result.success) else "failed"

        # Set pptx_path for slides mode
        if pptx_path:
            artifact.pptx_path = pptx_path

        await db.commit()
        await db.refresh(artifact)

        # Generate thumbnail in background (for page mode or as fallback)
        if data.mode == "page":
            artifact_data = {
                "report": {
                    "id": str(report.id) if report else None,
                    "title": getattr(report, "title", None) if report else None,
                    "theme": getattr(report, "theme", None) if report else None,
                },
                "visualizations": visualizations,
            }
            thumbnail_html = self._build_validation_html(artifact_data, code, mode=data.mode)
            asyncio.create_task(
                self._generate_thumbnail_background(
                    artifact_id=str(artifact.id),
                    html_content=thumbnail_html,
                    mode=data.mode,
                )
            )
        elif preview_images:
            # For slides mode, use the first preview image as thumbnail
            first_preview = Path(__file__).parent.parent.parent.parent.parent / "uploads" / preview_images[0]
            if first_preview.exists():
                artifact.thumbnail_path = preview_images[0]
                await db.commit()

        output = CreateArtifactOutput(
            artifact_id=str(artifact.id),
            code=code,
            mode=data.mode,
            title=data.title,
            version=artifact.version,
        )

        # Build observation message
        has_screenshot = validation_result and validation_result.screenshot_base64
        summary_msg = f"Created artifact '{data.title or 'Untitled'}' with {len(code)} characters of code"
        if data.mode == "slides" and preview_images:
            summary_msg += f". Generated {len(preview_images)} slide preview images."
        elif has_screenshot:
            summary_msg += ". Screenshot of the rendered dashboard is attached for validation."

        observation: Dict[str, Any] = {
            "summary": summary_msg,
            "artifact_id": str(artifact.id),
            "mode": data.mode,
            "visualization_count": len(visualizations),
            "visualization_ids": included_viz_ids,
        }

        # Add slides-specific info
        if data.mode == "slides":
            if preview_images:
                observation["preview_images"] = preview_images
                observation["slide_count"] = len(preview_images)
            if pptx_path:
                observation["pptx_path"] = pptx_path

        # Add validation info to observation
        if validation_result:
            observation["validation"] = {
                "success": validation_result.success,
                "errors": validation_result.errors if not validation_result.success else [],
            }
            # Add screenshot as images array for vision model consumption (page mode only)
            if validation_result.screenshot_base64:
                observation["images"] = [{
                    "data": validation_result.screenshot_base64,
                    "media_type": "image/png",
                    "source_type": "base64",
                }]

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
        messages_context: str = "",
        previous_artifacts: List[Dict[str, Any]] | None = None,
    ) -> str:
        """Build the prompt for generating slides using python-pptx code."""
        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        return f"""You are an expert at creating professional presentations using python-pptx.
Generate python-pptx code to create a polished slide deck.

═══════════════════════════════════════════════════════════════════════════════
AVAILABLE IN NAMESPACE (do not import - already provided)
═══════════════════════════════════════════════════════════════════════════════

Python-pptx classes and FUNCTIONS:
- Presentation, Inches, Pt, Emu, RGBColor
- PP_ALIGN, MSO_ANCHOR, MSO_SHAPE
- XL_CHART_TYPE, XL_LEGEND_POSITION
- CategoryChartData, ChartData

⚠️ CRITICAL: Inches, Pt, Emu are FUNCTIONS, not methods!
   ✅ CORRECT: Inches(1), Pt(24), Emu(914400)
   ❌ WRONG: 1.inches, 24.pt, value.inches

Data variables:
- visualizations: List[Dict] - each has 'title', 'columns', 'rows'
- report: Dict with 'id', 'title', 'theme'

Output:
- _pptx_output_path: str - path where you MUST save the presentation

═══════════════════════════════════════════════════════════════════════════════
YOUR VISUALIZATIONS
═══════════════════════════════════════════════════════════════════════════════

{viz_json}

{"(Full sample data included above)" if allow_llm_see_data else "(Data samples hidden for privacy - use column names and row_count)"}

═══════════════════════════════════════════════════════════════════════════════
TASK
═══════════════════════════════════════════════════════════════════════════════

**Report Title:** {report_title or title or 'Presentation'}
**User Request:** {user_prompt}

{f"**Organization Instructions:** {instructions_context}" if instructions_context else ""}

═══════════════════════════════════════════════════════════════════════════════
PYTHON-PPTX QUICK REFERENCE
═══════════════════════════════════════════════════════════════════════════════

**Setup (16:9 widescreen):**
```python
prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
```

**Add blank slide with dark background:**
```python
slide = prs.slides.add_slide(prs.slide_layouts[6])  # Blank layout
background = slide.background
fill = background.fill
fill.solid()
fill.fore_color.rgb = RGBColor(15, 23, 42)  # slate-900
```

**Add text box:**
```python
txBox = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
tf = txBox.text_frame
tf.word_wrap = True
p = tf.paragraphs[0]
p.text = "Title Text"
p.font.size = Pt(44)
p.font.bold = True
p.font.color.rgb = RGBColor(255, 255, 255)
p.alignment = PP_ALIGN.CENTER
```

**Add BAR CHART (CRITICAL - use this for charts):**
```python
chart_data = CategoryChartData()
chart_data.categories = ['Q1', 'Q2', 'Q3', 'Q4']
chart_data.add_series('Revenue', (1.2, 1.5, 1.8, 2.1))

x, y, cx, cy = Inches(1), Inches(2), Inches(11), Inches(5)
chart = slide.shapes.add_chart(
    XL_CHART_TYPE.BAR_CLUSTERED, x, y, cx, cy, chart_data
).chart

# Style the chart
chart.has_legend = True
chart.legend.position = XL_LEGEND_POSITION.BOTTOM
chart.legend.include_in_layout = False
plot = chart.plots[0]
plot.has_data_labels = True
```

**Other chart types:**
- XL_CHART_TYPE.COLUMN_CLUSTERED - vertical bars
- XL_CHART_TYPE.LINE - line chart
- XL_CHART_TYPE.PIE - pie chart
- XL_CHART_TYPE.AREA - area chart

**Dark background (slate-900 = RGB(15, 23, 42)):**
```python
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_THEME_COLOR
background = slide.background
fill = background.fill
fill.solid()
fill.fore_color.rgb = RGBColor(15, 23, 42)
```

**Access visualization data:**
```python
viz = visualizations[0]
columns = viz['columns']  # e.g. ['AlbumTitle', 'Revenue', 'UnitsSold']
rows = viz['rows']        # list of dicts like {{'AlbumTitle': 'Greatest Hits', 'Revenue': 1500.0}}

# Get categories and values for a chart:
categories = [str(row[columns[0]]) for row in rows]  # First column as labels
values = [float(row[columns[1]]) if row[columns[1]] else 0 for row in rows]  # Second column as values

# IMPORTANT: columns[i] returns a string like 'Revenue', then use that to index into row
# row[columns[1]] is the same as row['Revenue'] if columns[1] == 'Revenue'
```

═══════════════════════════════════════════════════════════════════════════════
DESIGN PHILOSOPHY - CREATE BEAUTIFUL, PROFESSIONAL SLIDES
═══════════════════════════════════════════════════════════════════════════════

**COLOR STRATEGY - Be Topic-Specific:**
Choose colors that feel designed for THIS topic. If your colors would work for any presentation, you haven't made specific enough choices.

Structure: One DOMINANT color (60-70% visual weight), 1-2 supporting tones, one accent.

Example palettes (pick one that fits the topic):
- **Midnight Executive**: Navy (0,31,63), Steel (119,136,153), Gold accent (212,175,55)
- **Forest & Moss**: Deep green (34,87,76), Sage (138,154,91), Cream (245,245,220)
- **Coral Energy**: Coral (255,127,80), Teal (0,128,128), Sand (244,232,214)
- **Ocean Depths**: Deep blue (0,51,102), Aqua (0,180,180), Pearl (240,248,255)
- **Sunset Warm**: Burgundy (128,0,32), Orange (255,140,0), Cream (255,253,240)
- **Modern Minimal**: Charcoal (54,69,79), Light gray (220,220,220), Teal accent (0,150,136)

**LAYOUT VARIETY - Never Repeat:**
Every slide MUST have visual elements - charts, shapes, or decorative elements. NO text-only slides.

Vary layouts between:
- Two-column (text left, chart right or vice versa)
- Full-width chart with title above
- KPI cards in a row (3-4 metric boxes)
- Chart with callout boxes for key insights
- Split layout with accent shape dividers

**TYPOGRAPHY:**
- Titles: 36-44pt bold, interesting positioning (not always centered)
- Body text: 18-24pt, LEFT-aligned (never center-align body text)
- KPI numbers: 48-72pt bold for impact
- Use font color contrast: white on dark, dark on light accents

**VISUAL ELEMENTS TO ADD:**
- Accent shapes: rectangles, rounded rectangles for backgrounds
- Divider lines or shapes between sections
- Colored boxes behind KPI numbers
- Subtle shape overlays for visual interest

**COMMON MISTAKES TO AVOID:**
- ⚠️ Using `value.inches` instead of `Inches(value)` - Inches/Pt/Emu are FUNCTIONS!
- Repeating the same layout across slides (VARY IT!)
- Center-aligning body text (use LEFT alignment)
- Using only blue without topic-specific reasoning
- Creating text-only slides without visual elements
- Accent lines directly under titles (hallmark of generic slides)
- Cramming too much data - limit charts to top 8-10 items

**TECHNICAL REQUIREMENTS:**
1. Define `generate_slides(visualizations, report)` returning a Presentation
2. Use 16:9 widescreen: Inches(13.333) x Inches(7.5)
3. Create REAL charts with slide.shapes.add_chart() + CategoryChartData
4. Use visualization data from the visualizations list
5. Margins: start shapes at Inches(0.75) to Inches(1) from edges

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT - Example with Design Principles Applied
═══════════════════════════════════════════════════════════════════════════════

```python
def generate_slides(visualizations, report):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # Color palette - choose colors that fit the topic
    PRIMARY = RGBColor(0, 51, 102)      # Deep blue
    SECONDARY = RGBColor(0, 128, 128)   # Teal
    ACCENT = RGBColor(255, 140, 0)      # Orange accent
    BG_DARK = RGBColor(15, 23, 42)      # Dark background
    TEXT_LIGHT = RGBColor(255, 255, 255)
    TEXT_MUTED = RGBColor(148, 163, 184)

    def set_background(slide, color=BG_DARK):
        bg = slide.background
        fill = bg.fill
        fill.solid()
        fill.fore_color.rgb = color

    def add_accent_shape(slide, left, top, width, height, color):
        shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
        shape.fill.solid()
        shape.fill.fore_color.rgb = color
        shape.line.fill.background()
        return shape

    # ═══════════════════════════════════════════════════════════════
    # SLIDE 1: Title with accent shape
    # ═══════════════════════════════════════════════════════════════
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_background(slide)

    # Accent shape behind title
    add_accent_shape(slide, Inches(0), Inches(2.5), Inches(5), Inches(2.5), PRIMARY)

    title_box = slide.shapes.add_textbox(Inches(0.75), Inches(3), Inches(12), Inches(1.5))
    tf = title_box.text_frame
    p = tf.paragraphs[0]
    p.text = report.get('title', 'Presentation')
    p.font.size = Pt(48)
    p.font.bold = True
    p.font.color.rgb = TEXT_LIGHT

    # ═══════════════════════════════════════════════════════════════
    # SLIDE 2: KPI Cards Row (if we have numeric data)
    # ═══════════════════════════════════════════════════════════════
    if visualizations and visualizations[0].get('rows'):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        set_background(slide)

        viz = visualizations[0]
        rows = viz.get('rows', [])
        columns = viz.get('columns', [])

        # Create 3 KPI cards across the slide
        card_width = Inches(3.5)
        card_height = Inches(2.5)
        start_x = Inches(1)
        card_y = Inches(2.5)
        gap = Inches(0.5)

        for i, col in enumerate(columns[:3]):
            if i >= 3:
                break
            x = start_x + i * (card_width + gap)

            # Card background
            card = add_accent_shape(slide, x, card_y, card_width, card_height, PRIMARY)

            # Value (large number)
            val = rows[0].get(col, 0) if rows else 0
            val_box = slide.shapes.add_textbox(x + Inches(0.3), card_y + Inches(0.5), card_width - Inches(0.6), Inches(1.2))
            tf = val_box.text_frame
            p = tf.paragraphs[0]
            p.text = "{{:,.0f}}".format(float(val)) if isinstance(val, (int, float)) else str(val)
            p.font.size = Pt(36)
            p.font.bold = True
            p.font.color.rgb = TEXT_LIGHT

            # Label
            label_box = slide.shapes.add_textbox(x + Inches(0.3), card_y + Inches(1.7), card_width - Inches(0.6), Inches(0.6))
            tf = label_box.text_frame
            p = tf.paragraphs[0]
            p.text = col
            p.font.size = Pt(14)
            p.font.color.rgb = TEXT_MUTED

    # ═══════════════════════════════════════════════════════════════
    # SLIDE 3: Chart with title (different layout)
    # ═══════════════════════════════════════════════════════════════
    if visualizations:
        viz = visualizations[0]
        columns = viz.get('columns', [])
        rows = viz.get('rows', [])

        if len(columns) >= 2 and rows:
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            set_background(slide)

            # Title on left side
            title_box = slide.shapes.add_textbox(Inches(0.75), Inches(0.5), Inches(5), Inches(1))
            tf = title_box.text_frame
            p = tf.paragraphs[0]
            p.text = viz.get('title', 'Data Analysis')
            p.font.size = Pt(32)
            p.font.bold = True
            p.font.color.rgb = TEXT_LIGHT

            # Extract data
            col_label = columns[0]
            col_value = columns[1]
            categories = [str(row.get(col_label, ''))[:20] for row in rows[:8]]
            values = [float(row.get(col_value, 0) or 0) for row in rows[:8]]

            # Chart (full width below title)
            chart_data = CategoryChartData()
            chart_data.categories = categories
            chart_data.add_series(col_value, tuple(values))

            chart = slide.shapes.add_chart(
                XL_CHART_TYPE.BAR_CLUSTERED,
                Inches(0.75), Inches(1.5), Inches(11.833), Inches(5.5),
                chart_data
            ).chart
            chart.has_legend = False

    return prs

# Execute and save
prs = generate_slides(visualizations, report)
prs.save(_pptx_output_path)
```

Create a beautiful, varied presentation following these design principles. Each slide should look DIFFERENT from the others. Use visual elements, accent shapes, and thoughtful color choices:"""

    def _build_page_prompt(
        self,
        user_prompt: str,
        title: str | None,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str,
        report_title: str | None,
        allow_llm_see_data: bool,
        messages_context: str = "",
        previous_artifacts: List[Dict[str, Any]] | None = None,
    ) -> str:
        """Build the prompt for generating page/dashboard (React + ECharts)."""
        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        # Build previous artifacts context
        previous_artifacts_context = ""
        if previous_artifacts:
            previous_artifacts_context = "\n═══════════════════════════════════════════════════════════════════════════════\nPREVIOUS ARTIFACTS (for reference/iteration)\n═══════════════════════════════════════════════════════════════════════════════\n\nThe user may want to modify or build upon these existing artifacts. Reference them if the user asks to change, update, or improve something:\n\n"
            for i, art in enumerate(previous_artifacts):
                previous_artifacts_context += f"**Artifact {i+1}: {art.get('title', 'Untitled')}** (ID: {art.get('id')})\n"
                if art.get('code'):
                    previous_artifacts_context += f"```jsx\n{art.get('code')}\n```\n\n"

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
DATA ACCESS - CRITICAL RULES
═══════════════════════════════════════════════════════════════════════════════

Data is available via `window.ARTIFACT_DATA`:
```javascript
const data = useArtifactData(); // React hook - returns null while loading
// data = {{ report: {{id, title, theme}}, visualizations: [...] }}
```

Each visualization object has this EXACT structure:
```js
{{
  id: "uuid-string",
  title: "Visualization Title",
  columns: [
    {{ "headerName": "AlbumId", "field": "AlbumId" }},
    {{ "headerName": "Album Title", "field": "AlbumTitle" }},
    {{ "headerName": "Total Revenue", "field": "total_revenue" }}
  ],
  rows: [
    {{ "AlbumId": 253, "AlbumTitle": "Battlestar Galactica", "total_revenue": 35.82 }},
    {{ "AlbumId": 251, "AlbumTitle": "The Office", "total_revenue": 31.84 }},
    // ... more rows
  ],
  view: {{ /* chart config hints */ }},
  dataModel: {{ /* series/axis config */ }}
}}
```

**CRITICAL - How to access data:**
- Use `column.field` to get the key for accessing row data: `row[column.field]`
- Use `column.headerName` for display labels in table headers
- Example: `rows.map(row => row[columns[0].field])` to get values for first column

**⚠️ CRITICAL: NEVER HARDCODE DATA**
- You MUST use `useArtifactData()` to access ALL data
- NEVER write literal/hardcoded values like `const data = [{{name: "Product A", value: 100}}]`
- NEVER use placeholder or example data in the output code
- ALL chart data, KPI values, labels, and metrics MUST come from `data.visualizations[N].rows`
- If the data structure is unclear, access it dynamically from the visualization objects

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

{f"**Conversation History:**{chr(10)}{messages_context}" if messages_context else ""}

{previous_artifacts_context}
═══════════════════════════════════════════════════════════════════════════════
DESIGN PRINCIPLES
═══════════════════════════════════════════════════════════════════════════════

**Style: Minimalist, Clean, Professional**

Create a polished, executive-ready dashboard. Think:
- Narrative is key. Use context (messages history, instructions) to create the outline/layut of the report
- You can show data in different angles, but don't make it redundant and noisy
- **Minimalism first** - Less is more. Remove visual clutter, no unnecessary decorations
- **Generous whitespace** - Let elements breathe, use padding liberally
- **Clean typography** - Simple, readable fonts. No fancy headers or badges
- **Subtle containers** - Light borders or shadows, not heavy cards
- **Beautiful, colorful charts** - Use vibrant but harmonious color palettes for data visualization
- **Professional feel** - Like a Bloomberg terminal or modern analytics platform
- **Data-focused** - The data is the star, UI should support not distract
**Color Guidelines for Charts:**
- Use rich, vibrant colors: blues (#3B82F6, #60A5FA), greens (#10B981, #34D399), purples (#8B5CF6), oranges (#F59E0B)
- Apply smooth gradients for area charts and backgrounds
- Ensure sufficient contrast for readability
- Use color consistently across related metrics

**DO NOT include:**
- Report IDs, UUIDs, or technical identifiers (e.g., "ID 0c6a0483-6876...")
- Branding badges or watermarks (e.g., "Built with ECharts", "Powered by React")
- Decorative headers like "Light, minimal dashboard • ECharts + React"
- Unnecessary icons or emoji
- Footer credits or attribution text
- Theme metadata or configuration blocks

Example design patterns:
- Clean KPI cards with large numbers and subtle trend indicators
- Full-width charts with minimal chrome
- Responsive grid with consistent spacing
- Smooth, subtle animations on load

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
2. Use the `useArtifactData()` hook for reactive data access - NEVER hardcode any data values
3. Use `<LoadingSpinner size={{32}} />` for loading state (do NOT build your own spinner)
4. Initialize ECharts in useEffect, dispose on cleanup, handle resize
5. Make it responsive (works on mobile and desktop)
6. Style: Minimalist, clean, professional - no branding badges or decorative headers -- BUT NOT BORING AND TEMPLATE LIKE!
7. Charts must be beautiful with vibrant, harmonious colors, gradients, and smooth animations
8. ALL displayed values must come from data.visualizations[N].rows - no placeholder data

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

**FINAL REMINDERS:**
- Extract ALL values from `data.visualizations` - never write literal numbers or strings
- Keep it minimal and professional - no decorative text, badges, or branding
- Use beautiful, colorful charts with vibrant palettes

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
        messages_context: str = "",
        previous_artifacts: List[Dict[str, Any]] | None = None,
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
                messages_context=messages_context,
                previous_artifacts=previous_artifacts,
            )
        return self._build_page_prompt(
            user_prompt=user_prompt,
            title=title,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=report_title,
            allow_llm_see_data=allow_llm_see_data,
            messages_context=messages_context,
            previous_artifacts=previous_artifacts,
        )

    def _extract_code(self, response: str, mode: str = "page") -> str:
        """Extract the code from the LLM response.

        For 'page' mode: Extract React code from <script type="text/babel"> tags
        For 'slides' mode: Extract python-pptx code from python code blocks
        """
        if mode == "slides":
            return self._extract_slides_python(response)

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

    def _extract_slides_python(self, response: str) -> str:
        """Extract python-pptx code for slides mode."""
        import re

        # Try to find Python code block
        python_match = re.search(r'```python\s*([\s\S]*?)```', response)
        if python_match:
            return python_match.group(1).strip()

        # Try generic code block
        code_match = re.search(r'```\s*([\s\S]*?)```', response)
        if code_match:
            return code_match.group(1).strip()

        # Look for function definition as start marker
        func_start = response.find('def generate_slides')
        if func_start != -1:
            # Find the prs.save() call at the end
            save_end = response.rfind('prs.save(')
            if save_end != -1:
                # Include the full save line
                end_idx = response.find(')', save_end)
                if end_idx != -1:
                    return response[func_start:end_idx + 1].strip()
            return response[func_start:].strip()

        # Fallback: return the response as-is
        return response.strip()

