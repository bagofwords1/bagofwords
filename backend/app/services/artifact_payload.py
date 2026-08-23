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
        .where(Visualization.report_id == artifact.report_id)
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

    return {
        "report": {
            "id": str(report.id),
            "title": report.title,
            "theme": report.theme_name,
        },
        "visualizations": await collect_visualizations(db, artifact),
        "files": await collect_files(db, artifact),
        "current_user": None,
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


async def collect_params(db, artifact) -> dict[str, Any]:
    """Declared query parameters for the artifact's report, as ARTIFACT_DATA.params.

    Same aggregation as paramsPayload() in ArtifactFrame.vue: params sharing a
    name across queries collapse into ONE declaration whose query_ids lists
    every query it drives, so a single control moves all of them.

    Only the HTML export calls this. The PDF export deliberately ships without
    params — it is a flat render, and declaring controls that a sheet of paper
    cannot operate would only draw dead widgets.
    """
    from app.models.query import Query
    from app.models.visualization import Visualization
    from app.schemas.param_schema import parse_param_specs

    result = await db.execute(
        select(Query)
        .join(Visualization, Visualization.query_id == Query.id)
        .where(Visualization.report_id == artifact.report_id)
        .distinct()
    )
    queries = list(result.scalars().all())

    by_name: dict[str, dict[str, Any]] = {}
    options: dict[str, list[dict[str, Any]]] = {}

    for query in queries:
        for spec in parse_param_specs(query.parameters):
            entry = by_name.get(spec.name)
            if entry is None:
                entry = {
                    "name": spec.name,
                    "type": spec.type,
                    "label": spec.label,
                    "source": spec.source,
                    "default": spec.default,
                    "required": spec.required,
                    "options": spec.options,
                    "query_ids": [],
                }
                by_name[spec.name] = entry
            entry["query_ids"].append(str(query.id))

            if spec.name in options:
                continue
            if spec.options:
                options[spec.name] = _normalize_options(spec.options)
            elif spec.options_source:
                resolved = await _options_from_source(db, spec.options_source)
                if resolved:
                    options[spec.name] = resolved

    # Applied values start at each param's default, exactly as the live host
    # seeds them. Identity params carry no client value — the server binds
    # them per viewer, and an export has no viewer.
    values: dict[str, Any] = {}
    for name, entry in by_name.items():
        if entry["source"] == "identity":
            continue
        if entry["default"] is not None:
            values[name] = entry["default"]

    return {
        "declarations": list(by_name.values()),
        "values": values,
        "options": options,
        "ack": 0,
    }
