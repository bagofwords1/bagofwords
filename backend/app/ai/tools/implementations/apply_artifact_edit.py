"""apply_artifact_edit — the mechanical (planner-authored) artifact edit path.

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
from app.ai.tools.schemas.apply_artifact_edit import ApplyArtifactEditInput, ApplyArtifactEditOutput
from app.ai.tools.implementations._artifact_refs import migrate_positional_viz_refs, viz_reference_errors
from app.models.artifact import Artifact

logger = logging.getLogger(__name__)


class ApplyArtifactEditTool(Tool):
    """Mechanical find/replace edits on a page artifact — no inner LLM."""

    def __init__(self):
        from app.ai.tools.implementations.create_artifact import CreateArtifactTool
        self._create_tool = CreateArtifactTool()

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="apply_artifact_edit",
            description=(
                "Apply exact find/replace edits YOU author to a page artifact — mechanical, no second "
                "model, atomic. Use when the current artifact code is in your context (from read_artifact "
                "this turn, or a create/edit result) and the change is precise: text, labels, colors, "
                "classNames, values, deleting/adding a self-contained section. Each `find` must match the "
                "code exactly once. The tool enforces the viz-reference and params-wiring contracts and "
                "render-validates before persisting; on any failure NOTHING is applied and the error tells "
                "you exactly what to correct — fix the ops and call again. Prefer edit_artifact instead "
                "when you have NOT read the current code or the change needs open-ended chart/data "
                "reasoning across the file."
            ),
            category="action",
            version="1.0.0",
            input_schema=ApplyArtifactEditInput.model_json_schema(),
            output_schema=ApplyArtifactEditOutput.model_json_schema(),
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
        return ApplyArtifactEditInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ApplyArtifactEditOutput

    def _fail(self, artifact, error_type: str, message: str, extra: Optional[Dict[str, Any]] = None) -> ToolEndEvent:
        obs: Dict[str, Any] = {
            "summary": f"apply_artifact_edit rejected for '{getattr(artifact, 'title', None) or 'artifact'}': {message} The artifact was NOT modified.",
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
        data = ApplyArtifactEditInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": "Apply artifact edit"})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")

        artifact = await db.get(Artifact, str(data.artifact_id))
        if artifact is None or (report is not None and str(artifact.report_id) != str(report.id)):
            yield self._fail(None, "not_found", f"Artifact {data.artifact_id} not found in this report.")
            return
        if artifact.mode != "page":
            yield self._fail(artifact, "wrong_mode", f"apply_artifact_edit only supports page artifacts (this one is '{artifact.mode}') — use edit_artifact or edit_doc.")
            return

        content = artifact.content or {}
        code = content.get("code", "") or ""
        existing_viz_ids = [str(v) for v in (content.get("visualization_ids") or [])]
        if not code.strip():
            yield self._fail(artifact, "no_code", "Artifact has no code to edit.")
            return

        # Legacy upgrade first, so planner-authored finds written against
        # vizById-style code match, and the persisted version is id-keyed.
        code, migrations = migrate_positional_viz_refs(code, existing_viz_ids)
        if migrations:
            logger.info(f"apply_artifact_edit: migrated {migrations} positional viz reference(s) for {artifact.id}")

        # Apply ops atomically: validate every op against the WORKING code in
        # order; the first failure rejects the whole batch.
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "applying_edits", "ops": len(data.edits)})
        from app.ai.tools.implementations.edit_artifact import _find_closest_match
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

        # Deterministic gates — hard, no repair (the planner corrects and retries).
        gate_errors: List[str] = viz_reference_errors(new_code, artifact_data)
        gate_errors += self._create_tool.params_wiring_errors(new_code, artifact_data)
        if gate_errors:
            yield self._fail(
                artifact, "contract_errors",
                f"{len(gate_errors)} contract error(s); first: {gate_errors[0]}",
                {"errors": gate_errors, "remediation": "Correct the ops to resolve each error and call apply_artifact_edit again."},
            )
            return

        # One render validation pass — no in-tool repair.
        screenshot_b64: Optional[str] = None
        render_errors: List[str] = []
        try:
            html = self._create_tool._build_thumbnail_html(artifact_data, new_code, mode="page")
            screenshot_b64, render_errors = await self._create_tool._take_preview_screenshot(html)
            fatal = self._create_tool.fatal_render_errors(render_errors)
            if fatal:
                yield self._fail(
                    artifact, "render_failed",
                    f"The edited code fails to render: {fatal[0]}",
                    {"render_errors": render_errors, "remediation": "Fix the ops (or use edit_artifact for an open-ended repair) and retry."},
                )
                return
        except Exception as e:
            logger.warning(f"apply_artifact_edit: render validation unavailable, persisting unvalidated: {e}")

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

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "artifact_id": str(new_artifact.id),
                    "version": new_artifact.version,
                    "applied_ops": len(data.edits),
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
