"""
edit_artifact tool - Surgically edit an existing artifact's code using search/replace diffs.

Instead of regenerating the entire dashboard from scratch, this tool loads the existing
code and applies targeted changes based on the user's edit instruction.
"""

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Dict, Any, Type, List, Optional, Tuple

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
from app.ai.tools.schemas.edit_artifact import EditArtifactInput, EditArtifactOutput
from app.ai.llm import LLM
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.models.query import Query
from app.dependencies import async_session_maker

logger = logging.getLogger(__name__)


def apply_search_replace_diff(existing_code: str, diff_text: str) -> Tuple[str, bool, int]:
    """Apply search/replace diff blocks to existing code.

    Args:
        existing_code: The original code to modify
        diff_text: The LLM output containing SEARCH/REPLACE blocks

    Returns:
        Tuple of (modified_code, all_blocks_applied, num_blocks_found)
    """
    # Parse SEARCH/REPLACE blocks
    blocks = re.findall(
        r'<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE',
        diff_text,
        re.DOTALL,
    )

    if not blocks:
        return existing_code, False, 0

    modified = existing_code
    all_applied = True

    for search, replace in blocks:
        # Try exact match first
        if search in modified:
            modified = modified.replace(search, replace, 1)
            continue

        # Try normalized match: strip trailing whitespace per line
        def normalize_lines(text: str) -> str:
            return "\n".join(line.rstrip() for line in text.split("\n"))

        normalized_code = normalize_lines(modified)
        normalized_search = normalize_lines(search)

        if normalized_search in normalized_code:
            # Find the position in normalized code, then apply to original
            # We need to find the corresponding region in the original code
            start_idx = normalized_code.index(normalized_search)

            # Count characters in original code up to the same line position
            norm_lines_before = normalized_code[:start_idx].count("\n")
            orig_lines = modified.split("\n")
            search_lines = search.split("\n")

            # Reconstruct the original text at that position
            orig_start_line = norm_lines_before
            orig_end_line = orig_start_line + len(search_lines)

            if orig_end_line <= len(orig_lines):
                orig_chunk = "\n".join(orig_lines[orig_start_line:orig_end_line])
                modified = modified.replace(orig_chunk, replace, 1)
                continue

        # This block failed to apply
        all_applied = False
        logger.warning(f"Search/replace block failed to match. Search text ({len(search)} chars): {search[:100]}...")

    return modified, all_applied, len(blocks)


class EditArtifactTool(Tool):
    """Tool for surgically editing existing artifact code.

    Instead of regenerating the entire dashboard, this tool loads the existing
    code and applies targeted search/replace diffs based on the user's instruction.
    Falls back to full rewrite if the diff cannot be applied.
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
                "Edit an existing dashboard or artifact by applying targeted changes to its code. "
                "Use this instead of create_artifact when modifying an existing artifact's layout, styling, "
                "filters, charts, or content. Preserves the existing design and only changes what is requested. "
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
        edit_instruction: str,
        mode: str,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str = "",
        messages_context: str = "",
        report_title: Optional[str] = None,
    ) -> str:
        """Build the prompt for editing existing artifact code."""

        viz_json = json.dumps(viz_profiles, indent=2, default=str)

        if mode == "slides":
            return self._build_slides_edit_prompt(
                existing_code=existing_code,
                edit_instruction=edit_instruction,
                viz_json=viz_json,
                instructions_context=instructions_context,
                messages_context=messages_context,
                report_title=report_title,
            )

        return f"""You are editing an existing React dashboard. Your job is to apply the user's requested change with surgical precision. Do not rewrite code that does not need to change. Preserve all existing functionality, styling, layout, event handlers, and responsive behavior unless the user explicitly asked to change it.

═══════════════════════════════════════════════════════════════════════════════
EXISTING DASHBOARD CODE
═══════════════════════════════════════════════════════════════════════════════

```
{existing_code}
```

═══════════════════════════════════════════════════════════════════════════════
USER'S EDIT REQUEST
═══════════════════════════════════════════════════════════════════════════════

{edit_instruction}

{f"**Report Title:** {report_title}" if report_title else ""}
{f"**Organization Instructions:**{chr(10)}{instructions_context}" if instructions_context else ""}
{f"**Conversation History:**{chr(10)}{messages_context}" if messages_context else ""}

═══════════════════════════════════════════════════════════════════════════════
VISUALIZATION DATA (for reference if the edit involves data access)
═══════════════════════════════════════════════════════════════════════════════

{viz_json}

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

Output your changes as one or more SEARCH/REPLACE blocks. Each block identifies an exact contiguous sequence of lines from the existing code (the SEARCH section) and provides the replacement lines (the REPLACE section). The SEARCH text must match the existing code exactly, including whitespace and indentation.

Format each block exactly like this:

<<<<<<< SEARCH
(exact lines from existing code to find)
=======
(replacement lines)
>>>>>>> REPLACE

Rules:
- Output ONLY the SEARCH/REPLACE blocks. Do not output the full file.
- The SEARCH section must be an exact, verbatim copy of consecutive lines from the existing code. Include enough surrounding context (2-3 lines before and after the change point) so the match is unambiguous.
- If adding entirely new code (e.g., a new component), use a SEARCH block that finds the insertion point (the lines immediately before where the new code should go) and a REPLACE block that includes those same lines plus the new code after them.
- If removing code, the REPLACE section should contain only the surrounding context lines without the removed code.
- You may output multiple SEARCH/REPLACE blocks if the change touches multiple locations.
- Order the blocks from top to bottom of the file.
- Never change visualization data access patterns (useArtifactData, column.field, etc.) unless the user asked for it.
- Preserve all existing ECharts configurations, responsive handling, resize observers, and event listeners unless the user asked to change them.
- If the user's request requires adding a new visualization or data source, use the visualization data profiles above to access it correctly via data.visualizations[N].
- If the edit is too large or fundamentally restructures the dashboard (e.g., "completely redesign this"), output the complete new code inside `<script type="text/babel">` and `</script>` tags instead of SEARCH/REPLACE blocks.

Apply the user's edit now:"""

    def _build_slides_edit_prompt(
        self,
        existing_code: str,
        edit_instruction: str,
        viz_json: str,
        instructions_context: str = "",
        messages_context: str = "",
        report_title: Optional[str] = None,
    ) -> str:
        """Build edit prompt for slides mode (python-pptx code)."""

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

{edit_instruction}

{f"**Report Title:** {report_title}" if report_title else ""}
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
- If the edit is too large, output the complete new code in a ```python``` code block instead.

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

        # Merge visualization IDs: existing + any new ones from input
        merged_viz_ids = list(existing_viz_ids)
        if data.visualization_ids:
            for vid in data.visualization_ids:
                if vid not in merged_viz_ids:
                    merged_viz_ids.append(vid)

        # Fetch all visualizations (batched query, same as create_artifact)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "loading_visualizations"})

        visualizations: List[Dict[str, Any]] = []
        warnings: List[str] = []
        report_id = str(report.id) if report else None

        try:
            from app.models.step import Step
            result = await db.execute(
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

        # Build instruction context
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

        # Build the edit prompt
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_edit"})

        prompt = self._build_edit_prompt(
            existing_code=existing_code,
            edit_instruction=data.edit_instruction,
            mode=artifact.mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            messages_context=messages_context,
            report_title=getattr(report, 'title', None) if report else None,
        )

        # Stream LLM response
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        buffer = ""

        async for chunk in llm.inference_stream(
            prompt,
            usage_scope="edit_artifact",
            usage_scope_ref_id=str(report.id) if report else None,
        ):
            buffer += chunk
            if len(buffer) % 100 == 0:
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={"stage": "generating", "chars": len(buffer)}
                )

        # Apply the diff
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "applying_edit"})

        new_code, diff_applied, num_blocks = apply_search_replace_diff(existing_code, buffer)

        if num_blocks == 0:
            # No diff blocks found — check if LLM output full code as fallback
            extracted = self._create_tool._extract_code(buffer, mode=artifact.mode)
            if extracted and extracted != buffer.strip():
                # LLM chose full rewrite
                new_code = extracted
                diff_applied = False
                logger.info(f"edit_artifact: No diff blocks found, using full rewrite fallback ({len(new_code)} chars)")
            else:
                # Neither diff nor full code — keep original
                logger.warning("edit_artifact: No diff blocks and no full code found in LLM output")
                new_code = existing_code
                diff_applied = False
                warnings.append("Could not parse edit from LLM output. The artifact was not modified.")

        elif not diff_applied:
            # Some blocks failed — try full rewrite fallback
            extracted = self._create_tool._extract_code(buffer, mode=artifact.mode)
            if extracted and extracted != buffer.strip():
                new_code = extracted
                logger.info(f"edit_artifact: Diff partially failed, using full rewrite fallback ({len(new_code)} chars)")
            else:
                # Use the partially-applied result
                logger.warning(f"edit_artifact: {num_blocks} diff blocks found but some failed to apply")
                warnings.append("Some edit blocks could not be matched exactly. The edit may be partially applied.")

        # Update title if provided
        new_title = data.title or artifact.title

        # Create a NEW artifact record (preserves version history for the frontend dropdown)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving_artifact"})

        included_viz_ids = [v["id"] for v in visualizations]
        new_version = artifact.version + 1

        new_artifact = Artifact(
            report_id=artifact.report_id,
            user_id=str(user.id) if user else artifact.user_id,
            organization_id=artifact.organization_id,
            title=new_title,
            mode=artifact.mode,
            content={"code": new_code, "visualization_ids": included_viz_ids},
            generation_prompt=data.edit_instruction,
            version=new_version,
            status="completed",
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)

        # Generate thumbnail in background (page mode only)
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

        observation: Dict[str, Any] = {
            "summary": summary_msg,
            "artifact_id": str(new_artifact.id),
            "mode": new_artifact.mode,
            "version": new_version,
            "diff_applied": diff_applied,
            "visualization_count": len(visualizations),
            "visualization_ids": included_viz_ids,
            "code": new_code,
        }

        if warnings:
            observation["warnings"] = warnings

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            }
        )
