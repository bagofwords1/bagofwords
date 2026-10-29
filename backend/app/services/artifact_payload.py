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

# Sentinel for "no reader in scope": the in-app/owner renders (PDF, thumbnail,
# in-app HTML export, edit previews) bake the stored snapshot as before. Pass
# `viewer=` (a User, or None for an anonymous share-link reader) to resolve
# every step's rows through viewer_data_policy.resolve_step_data instead — the
# exact code path the public JSON view (ReportService.get_public_step) uses,
# so a public export can never embed rows that view withholds. Parity is
# pinned by tests/e2e/test_public_output_parity.py.
UNSCOPED: Any = object()


async def _resolve_rows(db, step, report, viewer) -> tuple[dict, bool]:
    """(data, withheld) for `step` as `viewer` may see it."""
    if step is None:
        return {}, False
    if viewer is UNSCOPED:
        return step.data or {}, False
    from app.services.viewer_data_policy import resolve_step_data

    resolution = await resolve_step_data(db, step, report, viewer)
    return resolution.data or {}, resolution.withheld


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


async def _viz_steps(db, artifact) -> list[tuple[Any, Any, Any]]:
    """(visualization, query, step) for the artifact, in its index order."""
    from app.models.visualization import Visualization
    from app.models.query import Query
    from app.models.step import Step

    # A published dashboard's release (dashboard_data.ReleaseRender) owns its
    # copies; a version reads its conversation's charts.
    release_id = getattr(artifact, "artifact_release_id", None)
    owner = (Visualization.artifact_release_id == release_id if release_id
             else Visualization.report_id == artifact.report_id)
    # The rows one reader may see, when the caller knows the reader (a /d
    # export); otherwise the stored snapshot.
    rows_for = getattr(artifact, "rows_for", None)
    viz_stmt = (
        select(Visualization)
        .options(selectinload(Visualization.query))
        .where(
            owner,
            # Soft-deleted queries take their visualizations with them —
            # exports and thumbnails must not keep serving deleted data
            Visualization.deleted_at.is_(None),
        )
    )
    viz_result = await db.execute(viz_stmt)
    visualizations = _ordered_visualizations(artifact, list(viz_result.scalars().all()))

    out: list[tuple[Any, Any, Any]] = []
    for viz in visualizations:
        if not viz.query_id:
            continue
        query = await db.get(Query, viz.query_id)
        if not query:
            continue

        step = None
        if rows_for is not None:
            # For a reader, the step their step route serves (successful only).
            from app.services.dashboard_data import default_step
            step = await default_step(db, query)
        elif query.default_step_id:
            step = await db.get(Step, query.default_step_id)
        if not step and rows_for is None:
            step_result = await db.execute(
                select(Step)
                .where(Step.query_id == query.id)
                .order_by(Step.created_at.desc())
                .limit(1)
            )
            step = step_result.scalar_one_or_none()
        out.append((viz, query, step))
    return out


async def collect_visualizations(db, artifact, *, viewer: Any = UNSCOPED) -> list[dict[str, Any]]:
    """Ordered visualizations with their stored rows, columns and data model.

    With `viewer` given, rows and applied params are what that reader may see
    (see UNSCOPED)."""
    from app.models.report import Report

    report = (await db.get(Report, artifact.report_id)
              if viewer is not UNSCOPED and getattr(artifact, "report_id", None) else None)
    # A published dashboard's render (dashboard_data.ReleaseRender) carries the
    # rows its one reader may see; it decides them itself.
    rows_for = getattr(artifact, "rows_for", None)

    viz_data: list[dict[str, Any]] = []
    for viz, query, step in await _viz_steps(db, artifact):
        if rows_for is not None and step is not None:
            seen = await rows_for(db, step, query)
            data, applied = seen["data"], seen["applied_params"] or {}
            withheld = seen["withheld"]
        else:
            data, withheld = await _resolve_rows(db, step, report, viewer)
            applied = getattr(step, "applied_params", None) or {}
            if viewer is not UNSCOPED:
                from app.services.viewer_data_policy import redact_applied_params

                applied = redact_applied_params(applied, query.parameters, withheld=withheld) or {}
        entry = {
            "id": str(viz.id),
            # Carried so an offline host can tell which visualizations a
            # declared query param is scoped to (declaration.query_ids).
            "query_id": str(viz.query_id),
            "parameters": query.parameters or [],
            "applied_params": applied,
            "title": viz.title or query.title or "Untitled",
            "view": viz.view or {},
            "rows": data.get("rows", []) if data else [],
            "columns": data.get("columns", []) if data else [],
            "dataModel": step.data_model or {} if step else {},
        }
        if viewer is not UNSCOPED:
            # Reader-scoped callers (the chat context, the public export) must
            # be able to tell "this reader may not see these rows" from "the
            # query returned nothing" — an empty `rows` means both.
            entry["withheld"] = withheld
        viz_data.append(entry)

    return viz_data


async def any_rows_withheld(db, artifact, viewer: Any) -> bool:
    """Whether `viewer` is denied any of the rows this artifact draws on.

    For outputs that were rendered once with the stored snapshot and cannot be
    filtered afterwards (a generated .pptx deck and its PDF conversion): if the
    public JSON view would withhold even one of their queries from this
    reader, the file must not be served to them at all.
    """
    if viewer is UNSCOPED:
        return False
    from app.models.report import Report

    report = await db.get(Report, artifact.report_id)
    for _viz, _query, step in await _viz_steps(db, artifact):
        if step is None:
            continue
        _, withheld = await _resolve_rows(db, step, report, viewer)
        if withheld:
            return True
    return False


# What a viewer's resolved rows are, relative to the stored snapshot:
ROWS_SNAPSHOT = "snapshot"   # exactly the stored rows (owner, plain content)
ROWS_PERSONAL = "personal"   # at least one query serves the viewer's OWN run
ROWS_WITHHELD = "withheld"   # at least one query has nothing for this viewer


async def viewer_rows_state(db, artifact, viewer: Any) -> str:
    """Classify what `viewer` gets for the queries this artifact renders.

    A file rendered once from the stored rows (a generated .pptx deck) is only
    correct for a reader whose rows ARE the stored rows. A signed-in viewer who
    ran an identity-scoped query themselves resolves to their own
    step_user_results slice instead — for them the baked deck holds someone
    else's rows even though nothing is "withheld". Callers re-render
    (ROWS_PERSONAL) or refuse (ROWS_WITHHELD); ROWS_SNAPSHOT may be served
    as stored.

    Scoped to the artifact's own visualization_ids when it lists them — those
    are the only rows a deck's code is handed.
    """
    if viewer is UNSCOPED:
        return ROWS_SNAPSHOT
    from app.models.report import Report
    from app.services.viewer_data_policy import resolve_step_data

    report = await db.get(Report, artifact.report_id)
    included = set((artifact.content or {}).get("visualization_ids") or [])
    state = ROWS_SNAPSHOT
    for viz, _query, step in await _viz_steps(db, artifact):
        if step is None or (included and str(viz.id) not in included):
            continue
        resolution = await resolve_step_data(db, step, report, viewer)
        if resolution.withheld:
            return ROWS_WITHHELD
        vr = resolution.viewer_result or {}
        if vr.get("status") == "success":
            state = ROWS_PERSONAL
    return state


async def collect_files(db, artifact) -> list[dict[str, Any]]:
    """Embedded images/PDFs as data: URIs.

    The live viewer fetches these over an authenticated /files/{id}/content
    route that neither a headless browser nor a downloaded file can use;
    without inlining, every <BowFile> renders as a placeholder.
    """
    from app.ai.tools.implementations._artifact_images import build_file_datauris

    return await build_file_datauris(db, (artifact.content or {}).get("files") or [])


async def collect_artifact_payload(db, artifact, *, viewer: Any = UNSCOPED) -> Optional[dict[str, Any]]:
    """Build the full ARTIFACT_DATA payload for a page-mode artifact.

    `current_user` is deliberately absent: identity is per-viewer and injected
    by the host at render time. Every caller here produces a shareable file
    with no viewer, so they set it to None and artifacts exercise their
    null-guard path — the same one the headless validation render uses.

    Returns None when the artifact's report no longer exists.
    """
    from app.models.report import Report

    report_payload = getattr(artifact, "report_payload", None)  # a release has no conversation
    if report_payload is None:
        report = await db.get(Report, artifact.report_id)
        if not report:
            return None
        report_payload = {"id": str(report.id), "title": report.title, "theme": report.theme_name}

    visualizations = await collect_visualizations(db, artifact, viewer=viewer)
    return {
        "report": report_payload,
        "visualizations": [{k: v for k, v in viz.items() if k != "applied_params"} for viz in visualizations],
        "files": await collect_files(db, artifact),
        "current_user": None,
        # Which sandbox runtime generation to render with (themed kit at >= 11;
        # absent on rows created before it → legacy look and semantics).
        "runtime": {"version": int((artifact.content or {}).get("runtime_version") or 0)},
        "params": await collect_params(db, artifact, visualizations=visualizations, viewer=viewer),
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


async def _options_from_source(db, source, viewer: Any = UNSCOPED, rows_for=None,
                               release_id=None) -> list[dict[str, Any]]:
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
    if release_id and str(query.artifact_release_id or "") != str(release_id):
        return []  # a release reads its own copies only

    if rows_for is not None:
        from app.services.dashboard_data import default_step
        step = await default_step(db, query)
    else:
        step = await db.get(Step, query.default_step_id) if query.default_step_id else None
    if not step and rows_for is None:
        result = await db.execute(
            select(Step)
            .where(Step.query_id == query.id)
            .order_by(Step.created_at.desc())
            .limit(1)
        )
        step = result.scalar_one_or_none()
    if step is not None and rows_for is not None:
        # The option list is rows too: a /d reader gets the ones they may see.
        data = (await rows_for(db, step, query))["data"]
    elif viewer is not UNSCOPED:
        # Option lists are another query's rows — same withholding applies.
        from app.models.report import Report

        report = await db.get(Report, query.report_id) if query.report_id else None
        data, _ = await _resolve_rows(db, step, report, viewer)
    else:
        data = (step.data or {}) if step else {}
    if not data:
        return []

    seen: set = set()
    out: list[dict[str, Any]] = []
    for row in data.get("rows", []) or []:
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


async def collect_params(
    db, artifact, *, visualizations: Optional[list[dict[str, Any]]] = None, viewer: Any = UNSCOPED,
) -> dict[str, Any]:
    """Snapshot parameter context, scoped to this artifact's visualizations."""
    from app.schemas.param_schema import parse_param_specs

    if visualizations is None:
        visualizations = await collect_visualizations(db, artifact, viewer=viewer)
    included = set((artifact.content or {}).get("visualization_ids") or [])
    if included:
        visualizations = [v for v in visualizations if v["id"] in included]
    payload = build_params_payload(visualizations)
    for viz in visualizations:
        for spec in parse_param_specs(viz.get("parameters")):
            if spec.options_source:
                resolved = await _options_from_source(db, spec.options_source, viewer,
                                                      getattr(artifact, "rows_for", None),
                                                      getattr(artifact, "artifact_release_id", None))
                if resolved:
                    payload["options"][spec.name] = resolved
    return payload
