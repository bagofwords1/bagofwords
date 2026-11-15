"""Resolve the visualization ids a model hands a tool.

A visualization id is a 36-character UUID the model copies out of an earlier
tool result. Models miscopy it — an inserted or dropped character, a lost
tail, a stray ``viz_id:`` prefix — and every tool used to compare the string
exactly, so one wrong character failed the whole call with "not found" and
gave the model nothing to retry with.

``resolve_visualization_ids`` is the one place that turns what the model wrote
into ids that exist:

* an id is first read in its canonical spelling (case, braces, a label);
* an id that still names nothing is matched against the visualizations of
  THIS report only, and taken when exactly one is a near miss;
* anything left is reported per id, together with the report's usable
  visualizations, so the retry can copy an id from the error.

A correction never crosses reports: the candidates are the report's own rows,
so a near miss cannot bind someone else's chart.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select

logger = logging.getLogger(__name__)

# A near miss is at most this many single-character edits away. Two random
# UUIDs differ in about thirty of their 32 digits, so within one report a
# match this close identifies the id that was meant.
MAX_EDIT_DISTANCE = 2
# A cut-off id is taken when it is the start of exactly one candidate and at
# least this long (the first UUID group).
MIN_PREFIX_CHARS = 8
# How many of the report's visualizations an error lists for the retry.
MAX_AVAILABLE_LISTED = 30

_UUID_DIGITS = 32
_LABEL_RE = re.compile(r"^(?:(?:viz|visualization)(?:[_\s-]?id)?|id|uuid)\s*[:=]\s*", re.IGNORECASE)
_WRAPPERS = " \t\r\n`'\"{}[]()<>,;"


def normalize_viz_id(value: Any) -> str:
    """One spelling per id: the canonical UUID form, else trimmed lower case."""
    text = str(value if value is not None else "").strip(_WRAPPERS)
    text = _LABEL_RE.sub("", text).strip(_WRAPPERS)
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text.lower()


def _digits(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, or ``limit + 1`` as soon as it must exceed ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def closest_viz_id(requested: Any, candidates: Iterable[str]) -> Optional[str]:
    """The one candidate ``requested`` most plausibly meant, or None.

    None when nothing is close and when more than one candidate is: a guess
    between two charts is worse than an error that lists both.
    """
    key = _digits(normalize_viz_id(requested))
    if len(key) < MIN_PREFIX_CHARS:
        return None
    pool = list(dict.fromkeys(str(c) for c in candidates if c))
    if len(key) < _UUID_DIGITS - MAX_EDIT_DISTANCE:
        hits = [c for c in pool if _digits(c).startswith(key)]
    else:
        hits = [c for c in pool if _edit_distance(key, _digits(c), MAX_EDIT_DISTANCE) <= MAX_EDIT_DISTANCE]
    return hits[0] if len(hits) == 1 else None


def apply_id_corrections(text: Optional[str], corrections: Dict[str, str]) -> Optional[str]:
    """Rewrite the ids a model miscopied wherever else it wrote them.

    The model writes the same wrong id into the code (``vizById("…")``) or
    the markdown (``{{viz:…}}``) as into the id list, so correcting only the
    list would leave a reference to a chart that is not there.
    """
    if not text or not corrections:
        return text
    for wrong, right in corrections.items():
        if not wrong or wrong == right:
            continue
        # Whole ids only: a cut-off id is also the start of the id it was
        # corrected to, which must not be rewritten a second time.
        pattern = r"(?<![0-9A-Za-z-])" + re.escape(wrong) + r"(?![0-9A-Za-z-])"
        text = re.sub(pattern, lambda _m, _right=right: _right, text)
    return text


@dataclass
class VizIdResolution:
    """What ``resolve_visualization_ids`` made of the requested ids."""

    # One entry per distinct requested id, in request order: the id that
    # exists, or the normalized request when nothing matched.
    ids: List[str] = field(default_factory=list)
    # As the model wrote it -> the id used instead. Spelling-only changes
    # (case, braces) are included, so text can be rewritten with them.
    corrections: Dict[str, str] = field(default_factory=dict)
    # As the model wrote it -> the id used, for near-miss matches only.
    guessed: Dict[str, str] = field(default_factory=dict)
    # Requested ids (normalized) that name nothing in the report.
    unresolved: List[str] = field(default_factory=list)
    # The report's usable visualizations ({id, title}), newest first. None
    # until loaded: only a request with an id that did not match loads it
    # (see ``ensure_available``).
    available: Optional[List[Dict[str, str]]] = None

    def correction_notes(self) -> List[str]:
        return [
            f"Visualization id {wrong} matched no visualization in this report; used the closest one, {right}."
            for wrong, right in self.guessed.items()
        ]

    def unresolved_messages(self) -> List[str]:
        return [f"Visualization {vid} was not found in this report." for vid in self.unresolved]

    def remediation(self) -> str:
        if not self.available:
            return (
                "This report has no usable visualizations yet. Create the data first with "
                "create_data, then pass the viz_id it returns."
            )
        return (
            "Copy the id exactly from `available_visualizations` (this report's usable "
            "visualizations) and call the tool again."
        )


async def _report_catalog(db: Any, report_id: str) -> List[Dict[str, Any]]:
    """Every live visualization of the report with the status of its step.

    Columns only: the step's stored rows are not loaded.
    """
    from app.models.query import Query
    from app.models.step import Step
    from app.models.visualization import Visualization

    rows = (await db.execute(
        select(
            Visualization.id, Visualization.title, Visualization.query_id,
            Query.default_step_id, Step.status,
        )
        .select_from(Visualization)
        .outerjoin(Query, Query.id == Visualization.query_id)
        .outerjoin(Step, Step.id == Query.default_step_id)
        .where(
            Visualization.report_id == report_id,
            Visualization.artifact_release_id.is_(None),
            Visualization.deleted_at.is_(None),
        )
        .order_by(Visualization.created_at.desc(), Visualization.id)
    )).all()

    # A query with no default step is judged by its latest step, as the tools do.
    without_default = [str(r.query_id) for r in rows if r.query_id and not r.default_step_id]
    latest: Dict[str, Optional[str]] = {}
    if without_default:
        steps = (await db.execute(
            select(Step.query_id, Step.status)
            .where(Step.query_id.in_(without_default))
            .order_by(Step.created_at, Step.id)
        )).all()
        for step in steps:
            latest[str(step.query_id)] = step.status

    return [
        {
            "id": str(r.id),
            "title": r.title or "Untitled",
            "status": r.status if r.default_step_id else latest.get(str(r.query_id)),
        }
        for r in rows
    ]


def _usable(catalog: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    return [
        {"id": c["id"], "title": c["title"]} for c in catalog if c["status"] == "success"
    ][:MAX_AVAILABLE_LISTED]


async def ensure_available(db: Any, report_id: Optional[str], resolution: VizIdResolution) -> List[Dict[str, str]]:
    """The report's usable visualizations, loading them if the resolution did
    not need to (every id existed, but none could be used). Never raises."""
    if resolution.available is None:
        try:
            resolution.available = _usable(await _report_catalog(db, str(report_id))) if report_id else []
        except Exception:
            logger.exception("resolve_visualization_ids: report catalog lookup failed")
            resolution.available = []
    return resolution.available


def correct_code_viz_refs(code: Optional[str], payload_ids: Sequence[str]) -> Optional[str]:
    """Point a miscopied ``vizById("…")`` at the payload id it nearly spells.

    The id list can be right while the code carries the slip. A reference
    that is close to exactly one payload id is rewritten; anything else is
    left for the reference check to report.
    """
    if not code or not payload_ids:
        return code
    from app.ai.tools.implementations._artifact_refs import VIZ_BY_ID_RE

    known = {str(i) for i in payload_ids}
    fixes: Dict[str, str] = {}
    for ref in set(VIZ_BY_ID_RE.findall(code)):
        if ref in known:
            continue
        normalized = normalize_viz_id(ref)
        match = normalized if normalized in known else closest_viz_id(ref, known)
        if match:
            fixes[ref] = match
    return apply_id_corrections(code, fixes)


async def resolve_visualization_ids(
    db: Any,
    report_id: Optional[str],
    requested: Optional[Sequence[Any]],
    *,
    pool: Optional[Sequence[str]] = None,
) -> VizIdResolution:
    """Turn the ids a model wrote into ids of this report's visualizations.

    ``pool`` narrows what a near miss may match (an artifact's own ids, when
    the model names one to remove); exact ids are still looked up in the
    report. Never raises: when the lookup fails the ids come back normalized
    and the caller's own lookup reports what it finds.
    """
    resolution = VizIdResolution()
    raw_by_id: Dict[str, List[str]] = {}
    for value in requested or []:
        raw = str(value if value is not None else "")
        normalized = normalize_viz_id(raw)
        if not normalized:
            continue
        raw_by_id.setdefault(normalized, []).append(raw)
    if not raw_by_id:
        return resolution

    wanted = list(raw_by_id)
    known: Optional[set] = None
    if report_id:
        try:
            from app.models.visualization import Visualization
            known = {
                str(v) for v in (await db.execute(
                    select(Visualization.id).where(
                        Visualization.id.in_(wanted),
                        Visualization.report_id == str(report_id),
                    )
                )).scalars().all()
            }
        except Exception:
            logger.exception("resolve_visualization_ids: exact lookup failed")
            known = None

    catalog: List[Dict[str, Any]] = []
    if known is not None and any(vid not in known for vid in wanted):
        try:
            catalog = await _report_catalog(db, str(report_id))
        except Exception:
            logger.exception("resolve_visualization_ids: report catalog lookup failed")
            catalog = []
        resolution.available = _usable(catalog)

    candidates = [c["id"] for c in catalog]
    if pool is not None:
        allowed = {str(p) for p in pool}
        candidates = [c for c in candidates if c in allowed]

    for normalized in wanted:
        target = normalized
        if known is not None and normalized not in known:
            match = closest_viz_id(normalized, candidates)
            if match:
                target = match
                for raw in raw_by_id[normalized]:
                    resolution.guessed[raw] = match
            else:
                resolution.unresolved.append(normalized)
        for raw in raw_by_id[normalized]:
            if raw != target:
                resolution.corrections[raw] = target
        if target not in resolution.ids:
            resolution.ids.append(target)
    return resolution
