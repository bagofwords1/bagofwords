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
        # Validation loop: render in headless browser and fix errors if needed
        # ═══════════════════════════════════════════════════════════════════════
        max_validation_attempts = 3
        validation_result: Optional[ValidationResult] = None

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

        # Build content object (slides structure is parsed from HTML at export time)
        content: Dict[str, Any] = {
            "code": code,
            "visualization_ids": included_viz_ids,
        }

        # Update the pending artifact with content and mark as completed
        artifact.content = content
        artifact.status = "completed" if (validation_result and validation_result.success) else "completed"
        await db.commit()
        await db.refresh(artifact)

        # Generate thumbnail in background (truly non-blocking)
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

        output = CreateArtifactOutput(
            artifact_id=str(artifact.id),
            code=code,
            mode=data.mode,
            title=data.title,
            version=artifact.version,
        )

        # Build observation message - include screenshot context if available
        has_screenshot = validation_result and validation_result.screenshot_base64
        summary_msg = f"Created artifact '{data.title or 'Untitled'}' with {len(code)} characters of code"
        if has_screenshot:
            summary_msg += ". Screenshot of the rendered dashboard is attached for validation."

        observation: Dict[str, Any] = {
            "summary": summary_msg,
            "artifact_id": str(artifact.id),
            "mode": data.mode,
            "visualization_count": len(visualizations),
            "visualization_ids": included_viz_ids,
        }

        # Add validation info to observation
        if validation_result:
            observation["validation"] = {
                "success": validation_result.success,
                "errors": validation_result.errors if not validation_result.success else [],
            }
            # Add screenshot as images array for vision model consumption
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
        """Build the prompt for generating slides (pure HTML + vanilla JS)."""
        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        # Build previous artifacts context
        previous_artifacts_context = ""
        if previous_artifacts:
            previous_artifacts_context = "\n═══════════════════════════════════════════════════════════════════════════════\nPREVIOUS ARTIFACTS (for reference/iteration)\n═══════════════════════════════════════════════════════════════════════════════\n\n"
            for i, art in enumerate(previous_artifacts):
                previous_artifacts_context += f"**Artifact {i+1}: {art.get('title', 'Untitled')}** (ID: {art.get('id')})\n"
                if art.get('code'):
                    previous_artifacts_context += f"```html\n{art.get('code')}\n```\n\n"

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
  ]
}}
```

**CRITICAL - How to access data:**
- Use `column.field` to get the key for accessing row data: `row[column.field]`
- Use `column.headerName` for display labels in table headers
- Example: `rows.map(row => row[columns[0].field])` to get values for first column

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

{f"**Conversation History:**{chr(10)}{messages_context}" if messages_context else ""}

{previous_artifacts_context}
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

**DO NOT include:**
- Report IDs, UUIDs, or technical identifiers (e.g., "ID 0c6a0483-6876...")
- Branding badges or watermarks
- Footer credits or attribution text

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

