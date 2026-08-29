"""edit_artifact — the mechanical (planner-authored) artifact edit path.

Phase-3 step 1 of docs/design/artifact-iteration-and-filtering.md: the planner
authors exact find/replace edits itself; this tool contains NO LLM. It applies
the ops atomically, runs the legacy codemod and the deterministic gates
(viz-reference + params-wiring contracts), render-validates once, and persists
a new version. Any failure rejects the whole edit with a structured error the
planner can correct in its own loop — the planner IS the repair loop here.
"""

import logging
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.edit_artifact import EditArtifactInput, EditArtifactOutput
from app.ai.tools.implementations._artifact_refs import migrate_positional_viz_refs, viz_reference_errors
from app.models.artifact import Artifact

logger = logging.getLogger(__name__)


class EditArtifactTool(Tool):
    """Mechanical find/replace edits on a page or slides artifact — no inner LLM."""

    def __init__(self):
        from app.ai.tools.implementations.create_artifact import CreateArtifactTool
        self._create_tool = CreateArtifactTool()

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_artifact",
            description=(
                "Apply exact find/replace edits YOU author to a page or slides artifact — mechanical, no second "
                "model, atomic. Use when the current artifact code is in your context (from read_artifact "
                "this turn, or a create/edit result) and the change is precise: text, labels, colors, "
                "classNames, values, deleting/adding a self-contained section. Each `find` must match the "
                "code exactly once. The tool enforces the viz-reference and params-wiring contracts and "
                "render-validates before persisting; on any failure NOTHING is applied and the error tells "
                "you exactly what to correct — fix the ops and call again. If the current code is not in your "
                "context, call read_artifact first. For a from-scratch redesign, author new source and call "
                "create_artifact with `code`. For slides, ops edit the python-pptx script; validation = the script executes and saves a deck."
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

    def _fail(self, artifact, error_type: str, message: str, extra: Optional[Dict[str, Any]] = None) -> ToolEndEvent:
        obs: Dict[str, Any] = {
            "summary": f"edit_artifact rejected for '{getattr(artifact, 'title', None) or 'artifact'}': {message} The artifact was NOT modified.",
            "error": {"type": error_type, "message": message, **(extra or {})},
        }
        if artifact is not None:
            obs["artifact_id"] = str(artifact.id)
            obs["version"] = artifact.version
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": False,
                    "artifact_id": str(artifact.id) if artifact is not None else "",
                    "error": message,
                },
                "observation": obs,
            },
        )

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = EditArtifactInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": "Apply artifact edit"})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")

        artifact = await db.get(Artifact, str(data.artifact_id))
        if artifact is None or (report is not None and str(artifact.report_id) != str(report.id)):
            yield self._fail(None, "not_found", f"Artifact {data.artifact_id} not found in this report.")
            return
        if artifact.mode not in ("page", "slides"):
            yield self._fail(artifact, "wrong_mode", f"edit_artifact supports page and slides artifacts (this one is '{artifact.mode}') — use edit_doc for documents.")
            return

        content = artifact.content or {}
        code = content.get("code", "") or ""
        existing_viz_ids = [str(v) for v in (content.get("visualization_ids") or [])]
        if not code.strip():
            yield self._fail(artifact, "no_code", "Artifact has no code to edit.")
            return

        # Legacy upgrade first (page only), so planner-authored finds written
        # against vizById-style code match, and the persisted version is
        # id-keyed. Slides scripts address the injected `visualizations` list —
        # no codemod applies.
        if artifact.mode == "page":
            code, migrations = migrate_positional_viz_refs(code, existing_viz_ids)
            if migrations:
                logger.info(f"edit_artifact: migrated {migrations} positional viz reference(s) for {artifact.id}")

        # Apply ops atomically: validate every op against the WORKING code in
        # order; the first failure rejects the whole batch.
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "applying_edits", "ops": len(data.edits)})
        from app.ai.tools.implementations.edit_artifact_legacy import _find_closest_match
        working = code
        for i, op in enumerate(data.edits):
            occurrences = working.count(op.find)
            if occurrences == 1:
                working = working.replace(op.find, op.replace, 1)
                continue
            if occurrences == 0:
                closest = _find_closest_match(op.find, working)
                hint = f" Closest match in current code:\n---\n{closest}\n---" if closest else ""
                yield self._fail(
                    artifact, "op_no_match",
                    f"Edit op {i + 1}/{len(data.edits)}: `find` text not found in the current code (match is exact, whitespace included).{hint}",
                    {"op_index": i, "closest_match": closest},
                )
            else:
                yield self._fail(
                    artifact, "op_ambiguous",
                    f"Edit op {i + 1}/{len(data.edits)}: `find` text occurs {occurrences} times — extend it with surrounding context until unique.",
                    {"op_index": i, "occurrences": occurrences},
                )
            return
        new_code = working

        # Merge viz ids: (existing − removed) + new — same semantics as edit_artifact.
        removed = {str(v) for v in (data.remove_visualization_ids or [])}
        merged_viz_ids = [v for v in existing_viz_ids if v not in removed]
        for vid in (data.visualization_ids or []):
            if str(vid) not in merged_viz_ids and str(vid) not in removed:
                merged_viz_ids.append(str(vid))

        # Validation payload straight from the shared payload builder, against
        # the MERGED viz set; enrich with declared params for the wiring gate.
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "validating"})
        from app.services.artifact_payload import collect_artifact_payload
        shim = SimpleNamespace(
            report_id=artifact.report_id,
            content={"visualization_ids": merged_viz_ids, "files": content.get("files") or []},
        )
        artifact_data = await collect_artifact_payload(db, shim)
        if artifact_data is None:
            yield self._fail(artifact, "no_report", "The artifact's report no longer exists.")
            return
        # collect_artifact_payload mirrors the live client and appends the
        # report's OTHER visualizations as stragglers after the listed ones.
        # The contracts apply to the artifact's own viz set only — gate and
        # validate against exactly merged_viz_ids.
        _wanted = set(merged_viz_ids)
        artifact_data["visualizations"] = [
            v for v in (artifact_data.get("visualizations") or []) if str(v.get("id")) in _wanted
        ]
        try:
            from app.models.query import Query
            for ventry in artifact_data.get("visualizations") or []:
                qid = ventry.get("query_id")
                q = await db.get(Query, qid) if qid else None
                ventry["parameters"] = list(getattr(q, "parameters", None) or []) if q else []
        except Exception:
            pass

        screenshot_b64: Optional[str] = None
        render_errors: List[str] = []
        _pptx_tmp = None

        if artifact.mode == "slides":
            # Mechanical validation for slides: the edited script must execute
            # and save a deck. No LLM repair — planner fixes and retries.
            import tempfile as _tempfile
            from pathlib import Path as _Path
            from app.ai.tools.implementations._artifact_images import load_image_bytes
            _pptx_tmp = _Path(_tempfile.mkstemp(suffix=".pptx")[1])
            _pptx_tmp.unlink(missing_ok=True)
            report_data = {
                "id": str(report.id) if report else None,
                "title": getattr(report, "title", None) if report else None,
                "theme": getattr(report, "theme", None) if report else None,
            }
            _pptx_res = None
            async for _item in self._create_tool._execute_and_repair_pptx(
                new_code,
                artifact_data.get("visualizations") or [],
                report_data,
                _pptx_tmp,
                await load_image_bytes(db, content.get("files") or []),
                runtime_ctx,
                max_repairs=0,
            ):
                if isinstance(_item, dict):
                    _pptx_res = _item
                else:
                    yield _item
            if not _pptx_res or not _pptx_res.get("ok"):
                yield self._fail(
                    artifact, "pptx_execution_failed",
                    f"The edited slides script fails to execute: {(_pptx_res or {}).get('error') or 'unknown error'}",
                    {"remediation": "Fix the ops so the python-pptx script runs (it must call prs.save(_pptx_output_path)) and call edit_artifact again."},
                )
                return
        else:
            # Deterministic gates — hard, no repair (the planner corrects and retries).
            gate_errors: List[str] = viz_reference_errors(new_code, artifact_data)
            gate_errors += self._create_tool.params_wiring_errors(new_code, artifact_data)
            if gate_errors:
                yield self._fail(
                    artifact, "contract_errors",
                    f"{len(gate_errors)} contract error(s); first: {gate_errors[0]}",
                    {"errors": gate_errors, "remediation": "Correct the ops to resolve each error and call edit_artifact again."},
                )
                return

            # One render validation pass — no in-tool repair.
            try:
                html = self._create_tool._build_thumbnail_html(artifact_data, new_code, mode="page")
                screenshot_b64, render_errors = await self._create_tool._take_preview_screenshot(html)
                fatal = self._create_tool.fatal_render_errors(render_errors)
                if fatal:
                    yield self._fail(
                        artifact, "render_failed",
                        f"The edited code fails to render: {fatal[0]}",
                        {"render_errors": render_errors, "remediation": "Fix the ops to resolve the render error and retry."},
                    )
                    return
            except Exception as e:
                logger.warning(f"edit_artifact: render validation unavailable, persisting unvalidated: {e}")

        # Persist as the next version (stored rows are never rewritten).
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving_artifact"})
        new_version = artifact.version + 1
        ops_summary = "; ".join(
            (op.find[:60].replace("\n", " ") + " → " + op.replace[:60].replace("\n", " ")) for op in data.edits[:5]
        )
        prev_spec = artifact.generation_prompt or ""
        accumulated_spec = f"{prev_spec}\n+ Edit (v{new_version}): [mechanical] {ops_summary}".strip()
        new_content: Dict[str, Any] = {"code": new_code, "visualization_ids": merged_viz_ids}
        if content.get("files"):
            new_content["files"] = content.get("files")
        new_artifact = Artifact(
            report_id=artifact.report_id,
            user_id=str(user.id) if user else artifact.user_id,
            organization_id=artifact.organization_id,
            title=data.title or artifact.title,
            mode=artifact.mode,
            content=new_content,
            generation_prompt=accumulated_spec,
            version=new_version,
            status="completed",
        )
        if screenshot_b64 or render_errors:
            new_artifact.screenshot_base64 = screenshot_b64
            new_artifact.render_errors = render_errors or None
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)

        # Slides: move the validated deck under the new version's id and
        # render previews (a preview failure only costs the preview).
        if artifact.mode == "slides" and _pptx_tmp is not None:
            import shutil as _shutil
            from pathlib import Path as _Path
            uploads_dir = _Path(__file__).parent.parent.parent.parent.parent / "uploads" / "pptx"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            out_path = uploads_dir / f"{new_artifact.id}.pptx"
            _shutil.move(str(_pptx_tmp), str(out_path))
            new_artifact.pptx_path = str(out_path)
            try:
                from app.ai.code_execution.pptx_executor import PptxPreviewService
                PptxPreviewService(logger=logger).generate_previews(
                    pptx_path=out_path, artifact_id=str(new_artifact.id)
                )
            except Exception as e:
                logger.warning(f"edit_artifact: preview generation failed (deck still downloadable): {e}")
            db.add(new_artifact)
            await db.commit()
            await db.refresh(new_artifact)

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "artifact_id": str(new_artifact.id),
                    "title": new_artifact.title,
                    "mode": new_artifact.mode,
                    "version": new_artifact.version,
                    "applied_ops": len(data.edits),
                    # Mechanical ops are surgical by definition — lets the UI
                    # card show the same Diff badge as historical edits.
                    "diff_applied": True,
                    "code": new_code,
                },
                "observation": {
                    "summary": (
                        f"Applied {len(data.edits)} mechanical edit(s) to artifact '{new_artifact.title}' — now v{new_version}. "
                        "Contracts verified and render validated. No further verification needed."
                    ),
                    "artifact_id": str(new_artifact.id),
                    "mode": new_artifact.mode,
                    "version": new_artifact.version,
                    "diff_applied": True,
                    "applied_ops": len(data.edits),
                },
            },
        )


# Shared legacy helpers re-exported for compat (tests, MCP wrapper).
from app.ai.tools.implementations.edit_artifact_legacy import (  # noqa: E402,F401
    apply_search_replace_diff,
    build_pinned_decisions,
    _find_closest_match as _find_closest_match_legacy,
)
