"""Copy a dashboard: the rows it needs, with every reference rewritten.

Publish (phase 5) and the data migration (phase 9) copy one dashboard version
into an ArtifactRelease; Fork copies a whole conversation. Both go through the
same row copy and the same reference rewriting here
(docs/design/dashboards-first-class.md, phase 3).

A reference left pointing at the source renders an empty chart without an
error, so every kind a dashboard carries is rewritten:

- chart and query ids anywhere in the content (the chart list, page code,
  doc markdown, slides), in any letter case;
- a filter's `options_source.query_id`, whether it names the option query by
  id or by title, resolved the way the dashboard page resolves it;
- `load_step('<step id | slug | title>')` in step code, resolved the way
  LoadablesResolver resolves it: an id or a slug becomes the copy's, a title
  stays unless several steps share it, then it becomes the copy's slug so the
  copy loads the same step the source did.

A deleted chart, and a deleted query some step still loads, are copied still
deleted: the page receives the charts of the queries it shows, deleted ones
included, and load_step reaches deleted queries' steps, so the copy keeps
what works today without bringing anything back to life.
"""
import ast
import copy
import json
import logging
import re
import shutil
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.orm import defer, lazyload, selectinload

from app.core.path_safety import UnsafePathError, safe_join
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.visualization import Visualization

logger = logging.getLogger(__name__)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UPLOADS = Path(__file__).resolve().parents[2] / "uploads"


# ── resolution rules shared with the readers ───────────────────────────────

class StepIndex:
    """load_step resolution exactly as LoadablesResolver does it: among the
    conversation's successful default steps, by id, then slug, then title
    (case-insensitive; on a title clash the most recent step wins)."""

    def __init__(self, steps):
        self._id, self._slug, self._title = {}, {}, {}
        self._title_count = Counter()
        for s in sorted(steps, key=lambda s: s.created_at or datetime.min):
            self._id[str(s.id)] = s
            if s.slug:
                self._slug[s.slug.lower()] = s
            if s.title:
                self._title[s.title.lower()] = s
                self._title_count[s.title.lower()] += 1

    def find(self, ref: str):
        """(step, how) or (None, None); how is "id", "slug" or "title"."""
        ref = str(ref)
        if ref in self._id:
            return self._id[ref], "id"
        if ref.lower() in self._slug:
            return self._slug[ref.lower()], "slug"
        if ref.lower() in self._title:
            return self._title[ref.lower()], "title"
        return None, None

    def title_is_shared(self, ref: str) -> bool:
        return self._title_count[str(ref).lower()] > 1


def _load_step_literals(code: str):
    """(start, end, value) of every string literal passed first to load_step,
    found with the same AST walk extract_loadable_refs uses."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    lines = code.splitlines(keepends=True)
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line)

    def offset(lineno, col):  # ast columns count UTF-8 bytes
        return starts[lineno - 1] + len(lines[lineno - 1].encode("utf-8")[:col].decode("utf-8", "ignore"))

    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_step"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            c = node.args[0]
            out.append((offset(c.lineno, c.col_offset), offset(c.end_lineno, c.end_col_offset), c.value))
    return sorted(out)


def rewrite_load_step(code: str, replace: Callable[[str], str | None]) -> str:
    """Each load_step literal whose `replace(value)` is not None rewritten,
    keeping its quotes and prefix."""
    for start, end, value in reversed(_load_step_literals(code)):
        new = replace(value)
        if new is None or new == value:
            continue
        literal = code[start:end]
        i = next(n for n, ch in enumerate(literal) if ch in "'\"")
        quote = literal[i:i + 3] if literal[i:i + 3] in ("'''", '"""') else literal[i]
        code = f"{code[:start]}{literal[:i]}{quote}{new}{quote}{code[end:]}"
    return code


# ── what a dashboard needs ─────────────────────────────────────────────────

@dataclass
class DashboardDeps:
    """What one dashboard version needs from its conversation."""
    query_ids: list[str] = field(default_factory=list)         # the queries its link shows
    hidden_query_ids: list[str] = field(default_factory=list)  # needed, not shown: option lists, load_step targets
    missing_viz_ids: list[str] = field(default_factory=list)   # listed, but not a chart of a shown query
    # (query id, option ref) -> the option query the page resolves it to
    option_targets: dict = field(default_factory=dict)

    @property
    def all_query_ids(self) -> list[str]:
        return [*self.query_ids, *self.hidden_query_ids]


async def collect_dashboard_deps(db, version, *, include_pending: bool = False) -> DashboardDeps:
    """The queries a dashboard's link shows, and the ones it needs without showing.

    "Shows" is what GET /api/r/{report}/queries?artifact_id= returns today: the
    conversation's live queries with a successful default step that own a
    listed chart, or all of them when the version lists no charts. Then, until
    nothing new turns up, every query those reach: a filter's option query
    (the page looks among the shown queries, then among the rest the link
    serves) and every `load_step` target (StepIndex; a deleted query's step
    still loads). Only the version's own conversation is read; step data is
    not loaded.
    """
    report_id = str(version.report_id)
    listed = [str(v) for v in ((version.content or {}).get("visualization_ids") or [])]

    queries = (await db.execute(
        select(Query).options(
            lazyload("*"),
            selectinload(Query.default_step).options(lazyload("*"), defer(Step.data)),
            selectinload(Query.visualizations).options(lazyload("*")),
        ).join(Step, Step.id == Query.default_step_id)
        # `include_pending` (collab01): a joining editor's copies wait to run
        # with their credentials; the draft shows them waiting.
        .where(Query.report_id == report_id,
               Step.status.in_(("success", "pending")) if include_pending else Step.status == "success")
        .order_by(Query.created_at)
    )).scalars().unique().all()
    by_id = {str(q.id): q for q in queries}
    served = [q for q in queries if q.deleted_at is None]  # what the link serves at all

    if listed:
        owners = {str(r[0]) for r in (await db.execute(
            select(Visualization.query_id).where(Visualization.id.in_(listed)))).all()}
        shown = [q for q in served if str(q.id) in owners]
    else:
        shown = list(served)
    shown_ids = [str(q.id) for q in shown]
    charts = {str(v.id) for q in shown for v in q.visualizations}

    def titles(qs):
        out = {}
        for q in qs:  # later ones win, as in the page's title map
            if q.title:
                out[q.title.lower()] = str(q.id)
        return out
    shown_titles = titles(shown)
    other_titles = titles(q for q in served if str(q.id) not in set(shown_ids))
    served_ids = {str(q.id) for q in served}
    steps = StepIndex(q.default_step for q in queries)
    step_owner = {str(q.default_step_id): str(q.id) for q in queries}
    option_targets = {}

    def reached(q):
        for spec in q.parameters or []:
            src = spec.get("options_source") if isinstance(spec, dict) else None
            ref = str(src.get("query_id")) if isinstance(src, dict) and src.get("query_id") else None
            if ref:
                target = ref if ref in served_ids else (
                    shown_titles.get(ref.lower()) or other_titles.get(ref.lower()))
                if target:
                    option_targets[(str(q.id), ref)] = target
                    yield target
        for _, _, ref in _load_step_literals(getattr(q.default_step, "code", "") or ""):
            s, _ = steps.find(ref)
            if s is not None:
                yield step_owner[str(s.id)]

    seen, hidden, todo = set(shown_ids), [], list(shown)
    while todo:
        for target in reached(todo.pop()):
            if target not in seen:
                seen.add(target)
                hidden.append(target)
                todo.append(by_id[target])

    return DashboardDeps(
        query_ids=shown_ids, hidden_query_ids=hidden,
        missing_viz_ids=[v for v in listed if v not in charts],
        option_targets=option_targets,
    )


def load_step_closure(queries, start) -> list:
    """`start` plus every query of `queries` that their steps reach through
    load_step, transitively (StepIndex over the successful default steps,
    deleted queries included, as LoadablesResolver resolves them)."""
    eligible = [q for q in queries if q.default_step is not None and q.default_step.status == "success"]
    steps = StepIndex(q.default_step for q in eligible)
    owner = {str(q.default_step_id): q for q in eligible}
    seen = {str(q.id) for q in start}
    out, todo = list(start), list(start)
    while todo:
        q = todo.pop()
        for _, _, ref in _load_step_literals(getattr(q.default_step, "code", "") or ""):
            s, _ = steps.find(ref)
            target = owner.get(str(s.id)) if s is not None else None
            if target is not None and str(target.id) not in seen:
                seen.add(str(target.id))
                out.append(target)
                todo.append(target)
    return out


# ── copying rows ───────────────────────────────────────────────────────────

@dataclass
class Placement:
    """Where copies go: a conversation (Fork) or a release (Publish)."""
    report_id: str | None = None
    artifact_release_id: str | None = None
    # Fork gives each copied query a widget of the new conversation; a
    # release's copies have none.
    widget_for: Callable | None = None
    slug_prefix: str = "copy"


@dataclass
class CopyMaps:
    query_map: dict = field(default_factory=dict)  # old id -> new id
    step_map: dict = field(default_factory=dict)
    viz_map: dict = field(default_factory=dict)
    slug_map: dict = field(default_factory=dict)  # new step id -> its slug
    # Rewrites load_step calls in code the copy did not carry (Fork's withheld
    # code, written back by hydration) with the same rules.
    rewrite_code: Callable[[str], str] | None = None

    def ids(self) -> dict:
        return {k.lower(): v for m in (self.query_map, self.step_map, self.viz_map) for k, v in m.items()}


StepOverrides = Callable[[Step, Query], dict]
OptionTarget = Callable[[Query, str], str | None]

_STEP_FIELDS = ("title", "description", "type", "status", "status_reason", "prompt", "code", "data",
                "data_model", "view", "applied_params", "context_summary_json")


async def copy_query_rows(
    db,
    queries: list,
    *,
    placement: Placement,
    organization_id: str,
    user_id: str | None = None,
    step_overrides: StepOverrides | None = None,
    copy_source_refs: bool = True,
    option_target: OptionTarget | None = None,
) -> CopyMaps:
    """Copy each query, its default step and its charts, with the copies'
    filter references and load_step calls already pointing at the copies.

    `queries` must be loaded with default_step (data included) and
    visualizations. A deleted query or chart is copied still deleted.
    `user_id` replaces each query's owner (Fork: the forker); `step_overrides`
    returns step fields to replace (Fork's data policy); `option_target`
    resolves a filter reference to the source option query (default: by id,
    else by title among `queries`, later ones winning). Only default steps
    are copied; the step history stays behind.

    Ids and slugs are chosen before any row is written, so the rewritten
    references go in with the first INSERT (no UPDATE fires step hooks).
    """
    maps = CopyMaps()
    for q in queries:
        maps.query_map[str(q.id)] = str(uuid.uuid4())
        if q.default_step is not None:
            new_step = str(uuid.uuid4())
            maps.step_map[str(q.default_step.id)] = new_step
            maps.slug_map[new_step] = f"{placement.slug_prefix}-{uuid.uuid4().hex[:12]}"

    titles = {}
    for q in queries:
        if q.title:
            titles[q.title.lower()] = str(q.id)

    def default_target(query, ref):
        return ref if ref in maps.query_map else titles.get(ref.lower())

    target = option_target or default_target
    steps = StepIndex(q.default_step for q in queries
                      if q.default_step is not None and q.default_step.status == "success")

    def new_ref(ref):
        s, how = steps.find(ref)
        new_id = maps.step_map.get(str(s.id)) if s is not None else None
        if new_id is None:
            return None
        if how == "id":
            return new_id
        if how == "slug" or steps.title_is_shared(ref):
            return maps.slug_map[new_id]
        return None  # a title only one step has resolves the same in the copy

    maps.rewrite_code = lambda code: rewrite_load_step(code, new_ref)

    for old_query in queries:
        widget_id = await placement.widget_for(old_query) if placement.widget_for else None
        new_query = Query(
            id=maps.query_map[str(old_query.id)],
            title=old_query.title, description=old_query.description,
            parameters=(remap_parameters(old_query, maps.query_map, target) if old_query.parameters
                        else copy.deepcopy(old_query.parameters)),
            source_refs=copy.deepcopy(old_query.source_refs) if copy_source_refs else None,
            report_id=placement.report_id, artifact_release_id=placement.artifact_release_id,
            widget_id=widget_id,
            # Always the copied step, never the source's: a shared step would let
            # a run on one side overwrite the other's data.
            default_step_id=None,
            organization_id=organization_id, user_id=user_id or old_query.user_id,
            deleted_at=old_query.deleted_at,
        )
        db.add(new_query)
        await db.flush()

        old_step = old_query.default_step
        if old_step is not None:
            overrides = step_overrides(old_step, old_query) if step_overrides is not None else {}
            fields = {f: overrides[f] if f in overrides else copy.deepcopy(getattr(old_step, f))
                      for f in _STEP_FIELDS}
            if fields["code"]:
                fields["code"] = maps.rewrite_code(fields["code"])
            new_step_id = maps.step_map[str(old_step.id)]
            db.add(Step(id=new_step_id, **fields, slug=maps.slug_map[new_step_id],
                        widget_id=widget_id, query_id=str(new_query.id)))
            await db.flush()
            new_query.default_step_id = new_step_id

        for old_viz in old_query.visualizations:
            new_viz = Visualization(
                title=old_viz.title, status=old_viz.status, view=copy.deepcopy(old_viz.view),
                report_id=placement.report_id, artifact_release_id=placement.artifact_release_id,
                query_id=str(new_query.id), deleted_at=old_viz.deleted_at,
                # The chart's key across copies (collab01): who changed it
                # follows it into releases and other conversations.
                element_key=str(old_viz.element_key or old_viz.id),
            )
            db.add(new_viz)
            await db.flush()
            maps.viz_map[str(old_viz.id)] = str(new_viz.id)
    await db.flush()
    return maps


# ── rewriting references ───────────────────────────────────────────────────

def remap_ids(value, id_map: dict):
    """Every id of `id_map` (lower-case keys) anywhere in a JSON value, in any
    letter case, replaced by its copy. Other ids (files, sources) are left."""
    if value is None:
        return None
    raw = json.dumps(value, ensure_ascii=False)
    return json.loads(_UUID.sub(lambda m: id_map.get(m.group(0).lower(), m.group(0)), raw))


def remap_parameters(query, query_map: dict, option_target: OptionTarget):
    """A filter's option query repointed at its copy. A reference to nothing
    that was copied is dropped: the filter still works, it just offers no
    preset options (as before)."""
    out = copy.deepcopy(query.parameters)
    for spec in out if isinstance(out, list) else []:
        src = spec.get("options_source") if isinstance(spec, dict) else None
        if isinstance(src, dict) and src.get("query_id"):
            old = option_target(query, str(src["query_id"]))
            if old in query_map:
                src["query_id"] = query_map[old]
            else:
                spec["options_source"] = None
    return out


# ── one dashboard into a release ───────────────────────────────────────────

@dataclass
class CopyResult:
    query_map: dict
    step_map: dict
    viz_map: dict
    slug_map: dict
    hidden_query_ids: list = field(default_factory=list)
    missing_viz_ids: list = field(default_factory=list)


async def copy_dashboard(db, version, release) -> CopyResult:
    """Copy what `version` needs into `release`: the queries (shown and hidden),
    their default steps and charts, the rewritten content, the conversation's
    data sources, files, theme and monitoring access, and the thumbnail, deck
    and slide previews as files of the release's own. Data is copied as it
    is; nothing runs. The release row must exist and be fresh (one copy per
    release); its status is the caller's. Copied files are not removed if the
    caller rolls back."""
    deps = await collect_dashboard_deps(db, version)
    queries = {str(q.id): q for q in (await db.execute(
        select(Query).options(
            lazyload("*"), selectinload(Query.default_step).options(lazyload("*")),
            selectinload(Query.visualizations).options(lazyload("*")),
        ).where(Query.id.in_(deps.all_query_ids))
    )).scalars().unique().all()}
    maps = await copy_query_rows(
        db, [queries[i] for i in deps.all_query_ids],
        placement=Placement(artifact_release_id=str(release.id), slug_prefix="release"),
        organization_id=str(version.organization_id),
        option_target=lambda q, ref: deps.option_targets.get((str(q.id), ref)),
    )
    report = (await db.execute(
        select(Report).options(lazyload("*"), selectinload(Report.data_sources).options(lazyload("*")),
                               selectinload(Report.files).options(lazyload("*")))
        .where(Report.id == str(version.report_id))
    )).scalar_one()

    content = remap_ids(version.content or {}, maps.ids())
    copied_previews = {}
    if content.get("preview_images"):
        content["preview_images"], copied_previews = _copy_previews(content["preview_images"], str(release.id))
    release.content = content
    release.source_version_id = str(version.id)
    release.theme_name = report.theme_name
    release.theme_overrides = copy.deepcopy(report.theme_overrides)
    release.bow_source_access = copy.deepcopy(report.bow_source_access)
    release.pptx_path = _copy_deck(version.pptx_path, f"release-{release.id}")
    if version.thumbnail_path in copied_previews:
        # A deck's thumbnail is its first slide preview.
        release.thumbnail_path = copied_previews[version.thumbnail_path]
    elif version.thumbnail_path:
        from app.services.thumbnail_service import ThumbnailService
        release.thumbnail_path = ThumbnailService().copy_thumbnail(str(version.id), f"release-{release.id}")

    from app.models.artifact_release import (
        artifact_release_data_source_association as release_sources,
    )
    from app.models.artifact_release import (
        artifact_release_file_association as release_files,
    )
    for ds in report.data_sources:
        await db.execute(insert(release_sources).values(artifact_release_id=str(release.id),
                                                        data_source_id=str(ds.id)))
    for f in report.files:
        await db.execute(insert(release_files).values(artifact_release_id=str(release.id), file_id=str(f.id)))
    await db.flush()
    return CopyResult(query_map=maps.query_map, step_map=maps.step_map, viz_map=maps.viz_map,
                      slug_map=maps.slug_map, hidden_query_ids=deps.hidden_query_ids,
                      missing_viz_ids=deps.missing_viz_ids)


def _copy_deck(path: str | None, new_stem: str) -> str | None:
    """A copy of the .pptx next to the original inside uploads/; None when the
    original is not a deck on disk there."""
    if not path or not str(path).lower().endswith(".pptx"):
        return None
    try:
        source = Path(path) if Path(path).is_absolute() else _UPLOADS / str(path)
        source = safe_join(_UPLOADS, str(source.resolve().relative_to(_UPLOADS.resolve())))
        if not source.is_file():
            return None
        target = source.with_name(f"{new_stem}.pptx")
        shutil.copy2(source, target)
        return str(target)
    except (ValueError, OSError) as e:  # UnsafePathError is a ValueError
        logger.warning("deck not copied for %s: %s", new_stem, e)
        return None


def _copy_previews(paths: list, release_id: str, stem: str | None = None) -> tuple[list, dict]:
    """Slide previews copied to the release's own folder under
    uploads/pptx_previews, resolved like the preview route does (only PNGs
    inside that folder). A preview that is not one, or not on disk, keeps its
    path, which the route refuses or misses exactly as before."""
    base = _UPLOADS / "pptx_previews"
    out, copied = [], {}
    for p in paths:
        try:
            source = safe_join(base, str(p).removeprefix("pptx_previews/"))
            if source.suffix.lower() != ".png" or not source.is_file():
                raise FileNotFoundError(str(p))
            folder = stem or f"release-{release_id}"
            target = base / folder / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            new = f"pptx_previews/{folder}/{source.name}"
            copied[p] = new
            out.append(new)
        except (UnsafePathError, OSError):
            out.append(p)
    return out, copied


def remove_release_files(release_id: str, pptx_path: str | None = None) -> None:
    """The files copy_dashboard made for a release, removed: its thumbnail,
    deck and slide previews. Only the release's own names inside uploads/;
    anything else is left alone. Missing files are fine."""
    stem = f"release-{release_id}"
    candidates = [_UPLOADS / "thumbnails" / f"{stem}.png"]
    if pptx_path:
        try:
            deck = Path(pptx_path) if Path(pptx_path).is_absolute() else _UPLOADS / str(pptx_path)
            deck = safe_join(_UPLOADS, str(deck.resolve().relative_to(_UPLOADS.resolve())))
            if deck.name == f"{stem}.pptx":
                candidates.append(deck)
        except (ValueError, OSError):  # UnsafePathError is a ValueError
            pass
    for path in candidates:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("release file not removed %s: %s", path, e)
    shutil.rmtree(_UPLOADS / "pptx_previews" / stem, ignore_errors=True)
