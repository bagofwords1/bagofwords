"""Server-side assembly of `window.ARTIFACT_DATA` for a page-mode artifact.

Anything that renders an artifact away from the browser — the PDF export, the
standalone HTML export — needs the same payload the live host builds in
frontend/components/dashboard/ArtifactFrame.vue: the report, its visualizations
in the artifact's own order, each one's stored rows, and any embedded files
inlined as data URIs.

This lives in one place on purpose. The visualization ORDERING in particular is
load-bearing and easy to get subtly wrong (see the comment on
`_ordered_visualizations`); a second, drifting copy of it would produce exports
where every chart binds to the wrong data while still looking plausible.
"""

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


def _ordered_visualizations(artifact, visualizations: list) -> list:
    """Order a report's visualizations the way the artifact's code indexes them.

    Dashboard code addresses visualizations BY INDEX (viz[0], viz[1], ...), and
    that index is defined by the artifact's ordered content["visualization_ids"].
    Mirror the live client (ArtifactFrame.vue): scope to this artifact's
    visualizations and order them by that list, appending any stragglers last.

    Without this, window.ARTIFACT_DATA would be a report-wide, unordered
    superset and every index-based lookup in the dashboard would bind to the
    wrong visualization — producing missing numbers and empty chart series.
    """
    viz_ids = (artifact.content or {}).get("visualization_ids") or []
    if not viz_ids:
        return visualizations

    viz_by_id = {str(v.id): v for v in visualizations}
    ordered = [viz_by_id[vid] for vid in viz_ids if vid in viz_by_id]
    ordered_id_set = set(viz_ids)
    ordered.extend(v for v in visualizations if str(v.id) not in ordered_id_set)
    return ordered


async def collect_visualizations(db, artifact) -> list[dict[str, Any]]:
    """Ordered visualizations with their stored rows, columns and data model."""
    from app.models.visualization import Visualization
    from app.models.query import Query
    from app.models.step import Step

    viz_stmt = (
        select(Visualization)
        .options(selectinload(Visualization.query))
        .where(
            Visualization.report_id == artifact.report_id,
            # Soft-deleted queries take their visualizations with them —
            # exports and thumbnails must not keep serving deleted data
            Visualization.deleted_at.is_(None),
        )
    )
    viz_result = await db.execute(viz_stmt)
    visualizations = _ordered_visualizations(artifact, list(viz_result.scalars().all()))

    viz_data: list[dict[str, Any]] = []
    for viz in visualizations:
        if not viz.query_id:
            continue
        query = await db.get(Query, viz.query_id)
        if not query:
            continue

        step = None
        if query.default_step_id:
            step = await db.get(Step, query.default_step_id)
        if not step:
            step_result = await db.execute(
                select(Step)
                .where(Step.query_id == query.id)
                .order_by(Step.created_at.desc())
                .limit(1)
            )
            step = step_result.scalar_one_or_none()

        viz_data.append({
            "id": str(viz.id),
            # Carried so an offline host can tell which visualizations a
            # declared query param is scoped to (declaration.query_ids).
            "query_id": str(viz.query_id),
            "parameters": query.parameters or [],
            "applied_params": getattr(step, "applied_params", None) or {},
            "title": viz.title or query.title or "Untitled",
            "view": viz.view or {},
            "rows": step.data.get("rows", []) if step and step.data else [],
            "columns": step.data.get("columns", []) if step and step.data else [],
            "dataModel": step.data_model or {} if step else {},
        })

    return viz_data


async def collect_files(db, artifact) -> list[dict[str, Any]]:
    """Embedded images/PDFs as data: URIs.

    The live viewer fetches these over an authenticated /files/{id}/content
    route that neither a headless browser nor a downloaded file can use;
    without inlining, every <BowFile> renders as a placeholder.
    """
    from app.ai.tools.implementations._artifact_images import build_file_datauris

    return await build_file_datauris(db, (artifact.content or {}).get("files") or [])


async def collect_artifact_payload(db, artifact) -> Optional[dict[str, Any]]:
    """Build the full ARTIFACT_DATA payload for a page-mode artifact.

    `current_user` is deliberately absent: identity is per-viewer and injected
    by the host at render time. Every caller here produces a shareable file
    with no viewer, so they set it to None and artifacts exercise their
    null-guard path — the same one the headless validation render uses.

    Returns None when the artifact's report no longer exists.
    """
    from app.models.report import Report

    report = await db.get(Report, artifact.report_id)
    if not report:
        return None

    visualizations = await collect_visualizations(db, artifact)
    return {
        "report": {
            "id": str(report.id),
            "title": report.title,
            "theme": report.theme_name,
        },
        "visualizations": [{k: v for k, v in viz.items() if k != "applied_params"} for viz in visualizations],
        "files": await collect_files(db, artifact),
        "current_user": None,
        # Which sandbox runtime generation to render with (themed kit at >= 11;
        # absent on rows created before it → legacy look and semantics).
        "runtime": {"version": int((artifact.content or {}).get("runtime_version") or 0)},
        "params": await collect_params(db, artifact, visualizations=visualizations),
    }


def _normalize_options(raw: list) -> list[dict[str, Any]]:
    """Coerce a static options list into the [{value, label}] the runtime reads."""
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, dict) and "value" in item:
            out.append({"value": item["value"], "label": str(item.get("label", item["value"]))})
        else:
            out.append({"value": item, "label": str(item)})
    return out


def build_params_payload(visualizations: list[dict[str, Any]]) -> dict[str, Any]:
    """Project existing query declarations into the iframe's parameter contract.

    Shared by generation previews and stored-artifact payloads. No queries are
    executed here; values reflect the stored snapshot, with declaration defaults
    only when the snapshot does not specify a value.
    """
    from app.schemas.param_schema import parse_param_specs

    declarations: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    options: dict[str, list[dict[str, Any]]] = {}
    for viz in visualizations:
        qid = str(viz.get("query_id") or viz.get("queryId") or "")
        vid = str(viz.get("id") or "")
        applied = viz.get("applied_params") or {}
        for spec in parse_param_specs(viz.get("parameters")):
            entry = declarations.setdefault(spec.name, {
                **spec.model_dump(), "query_ids": [], "visualization_ids": [],
            })
            if qid and qid not in entry["query_ids"]:
                entry["query_ids"].append(qid)
            if vid and vid not in entry["visualization_ids"]:
                entry["visualization_ids"].append(vid)
            if spec.source != "identity":
                value = applied.get(spec.name, spec.default) if spec.source == "input" else spec.default
                values.setdefault(spec.name, value)
            if spec.options:
                options.setdefault(spec.name, _normalize_options(spec.options))
    return {"declarations": list(declarations.values()), "values": values,
            "options": options, "ack": 0}


async def _options_from_source(db, source) -> list[dict[str, Any]]:
    """Resolve an options_source (another query's column) to stable choices.

    Mirrors resolveParamOptions in ArtifactFrame.vue: the choices come from
    that query's current default-step rows, deduped and order-preserving. They
    must never be derived from the currently filtered data, or picking a value
    would collapse the list it came from.
    """
    from app.models.query import Query
    from app.models.step import Step

    query = await db.get(Query, source.query_id)
    if not query:
        return []

    step = await db.get(Step, query.default_step_id) if query.default_step_id else None
    if not step:
        result = await db.execute(
            select(Step)
            .where(Step.query_id == query.id)
            .order_by(Step.created_at.desc())
            .limit(1)
        )
        step = result.scalar_one_or_none()
    if not step or not step.data:
        return []

    seen: set = set()
    out: list[dict[str, Any]] = []
    for row in step.data.get("rows", []) or []:
        if not isinstance(row, dict) or source.value_column not in row:
            continue
        value = row[source.value_column]
        marker = str(value)
        if marker in seen:
            continue
        seen.add(marker)
        label = row.get(source.label_column) if source.label_column else None
        out.append({"value": value, "label": str(label if label is not None else value)})
    return out


async def collect_params(db, artifact, *, visualizations: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Snapshot parameter context, scoped to this artifact's visualizations."""
    from app.schemas.param_schema import parse_param_specs

    if visualizations is None:
        visualizations = await collect_visualizations(db, artifact)
    included = set((artifact.content or {}).get("visualization_ids") or [])
    if included:
        visualizations = [v for v in visualizations if v["id"] in included]
    payload = build_params_payload(visualizations)
    for viz in visualizations:
        for spec in parse_param_specs(viz.get("parameters")):
            if spec.options_source:
                resolved = await _options_from_source(db, spec.options_source)
                if resolved:
                    payload["options"][spec.name] = resolved
    return payload
