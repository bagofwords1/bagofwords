"""The copy engine: one dashboard version → a frozen copy owned by a release (phase 3).

Publish (phase 5) and the data migration (phase 9) copy a dashboard with
app/services/dashboard_copy.py. A copy must hold exactly what a viewer of the
dashboard's link sees today, plus the queries the dashboard needs without
showing them (filter option lists, load_step targets), with every reference
pointing at the copies. A reference left pointing at the draft renders an
empty chart without an error, so these tests look for any.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.dependencies import async_session_maker

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _run(coro):
    return asyncio.run(coro)


async def _copy(version_id: str):
    """Copy one version into a new release; return (release, result)."""
    from app.models.artifact import ArtifactVersion
    from app.models.artifact_release import ArtifactRelease
    from app.services.dashboard_copy import copy_dashboard

    async with async_session_maker() as db:
        version = await db.get(ArtifactVersion, version_id)
        release = ArtifactRelease(artifact_id=version.artifact_id, organization_id=version.organization_id,
                                  content={}, status="publishing")
        db.add(release)
        await db.flush()
        result = await copy_dashboard(db, version, release)
        await db.commit()
        return str(release.id), result


def _report_rows(conn, report_id):
    """Every column of a conversation's queries, steps, charts and versions."""
    out = {}
    for name, sql in (
        ("queries", "SELECT * FROM queries WHERE report_id = :r ORDER BY id"),
        ("steps", "SELECT s.* FROM steps s JOIN queries q ON q.id = s.query_id WHERE q.report_id = :r ORDER BY s.id"),
        ("visualizations", "SELECT * FROM visualizations WHERE report_id = :r ORDER BY id"),
        ("artifact_versions", "SELECT * FROM artifact_versions WHERE report_id = :r ORDER BY id"),
    ):
        out[name] = [dict(r) for r in conn.execute(text(sql), {"r": report_id}).mappings()]
    return json.dumps(out, default=str, sort_keys=True)


def _copied_rows(conn, release_id):
    q = [dict(r) for r in conn.execute(text("SELECT * FROM queries WHERE artifact_release_id = :p"),
                                       {"p": release_id}).mappings()]
    s = [dict(r) for r in conn.execute(text(
        "SELECT s.* FROM steps s JOIN queries q ON q.id = s.query_id WHERE q.artifact_release_id = :p"),
        {"p": release_id}).mappings()]
    v = [dict(r) for r in conn.execute(text("SELECT * FROM visualizations WHERE artifact_release_id = :p"),
                                       {"p": release_id}).mappings()]
    content = conn.execute(text("SELECT content FROM artifact_releases WHERE id = :p"), {"p": release_id}).scalar()
    return q, s, v, (json.loads(content) if isinstance(content, str) else content)


def _references_outside_the_copy(queries, steps, source_steps):
    """Every filter and load_step reference in the copies must resolve among
    the copies themselves: if the engine left a needed query behind, this is
    where it shows (independently of what the engine thinks it collected)."""
    from app.services.dashboard_copy import StepIndex, _load_step_literals

    ids = {q["id"] for q in queries}
    out = []
    for q in queries:
        for spec in (json.loads(q["parameters"]) if isinstance(q["parameters"], str) else q["parameters"]) or []:
            src = spec.get("options_source") if isinstance(spec, dict) else None
            if isinstance(src, dict) and src.get("query_id") and src["query_id"] not in ids:
                out.append(f"filter of {q['id']} points outside the copy: {src['query_id']}")
    index = StepIndex([_Row(s) for s in steps if s["status"] == "success"])
    for s in steps:
        for _, _, ref in _load_step_literals(s["code"] or ""):
            if index.find(ref)[0] is None and source_steps.find(ref)[0] is not None:
                out.append(f"load_step({ref!r}) in {s['id']} resolves in the draft, not in the copy")
    return out


def _source_step_index(conn, report_id):
    """load_step resolution in the draft: the conversation's successful default
    steps, deleted queries included (as LoadablesResolver searches)."""
    from app.services.dashboard_copy import StepIndex

    rows = conn.execute(text(
        "SELECT s.id, s.slug, s.title, s.created_at FROM steps s JOIN queries q ON q.default_step_id = s.id "
        "WHERE q.report_id = :r AND s.status = 'success'"), {"r": report_id}).mappings().all()
    return StepIndex([_Row(dict(r)) for r in rows])


class _Row:
    def __init__(self, row):
        self.__dict__.update(row)


async def _steps_with_different_data(step_map):
    """Compared through the ORM: Step.data is encrypted, so the stored bytes differ."""
    from app.models.step import Step

    # context_summary_json is left out: the Step model derives it from `data`
    # on write, so a copy of a legacy row without one gets one.
    fields = ("data", "view", "data_model", "applied_params", "status", "title")  # code: rewritten on purpose
    differ = []
    async with async_session_maker() as db:
        for old, new in step_map.items():
            a, b = await db.get(Step, old), await db.get(Step, new)
            changed = [f for f in fields if getattr(a, f) != getattr(b, f)]
            if changed:
                differ.append(f"{old} {changed}: {[(getattr(a, f), getattr(b, f)) for f in changed]}"[:300])
    return differ


def _shown_query_ids(test_client, cast, report_id, version_id):
    """What a viewer of the dashboard's link gets today: the source of truth."""
    resp = test_client.get(f"/api/r/{report_id}/queries", params={"artifact_id": version_id},
                           headers=cast.people["owner"].headers(cast.org_id))
    if resp.status_code != 200:
        return None  # not a dashboard the owner can open on its link (deleted conversation, another owner)
    return {q["id"] for q in resp.json()}


@pytest.mark.e2e
def test_every_dashboard_in_the_corpus_copies_completely(corpus, corpus_rows, test_client):
    """For every live dashboard version: the copy holds what the link shows
    (and the hidden queries it needs), no reference to a copied row's old id
    is left anywhere, the data is the same, and the draft is untouched."""
    cast = corpus.cast
    with corpus_rows.engine.connect() as conn:
        versions = conn.execute(text(
            "SELECT av.id, av.report_id FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
            "WHERE av.deleted_at IS NULL AND a.deleted_at IS NULL AND av.organization_id = :o ORDER BY av.id"),
            {"o": cast.org_id}).mappings().all()
    assert len(versions) > 30
    problems = []
    for v in versions:
        with corpus_rows.engine.connect() as conn:
            before = _report_rows(conn, v["report_id"])
        shown = _shown_query_ids(test_client, cast, v["report_id"], v["id"])
        release_id, result = _run(_copy(v["id"]))
        name = corpus.names.get(v["id"], v["id"])
        with corpus_rows.engine.connect() as conn:
            if _report_rows(conn, v["report_id"]) != before:
                problems.append(f"{name}: the draft changed")
            queries, steps, vizzes, content = _copied_rows(conn, release_id)
            source_steps = _source_step_index(conn, v["report_id"])
        copied_from = set(result.query_map)
        if shown is not None:
            if not shown <= copied_from:
                problems.append(f"{name}: shown queries not copied: {shown - copied_from}")
            extra = copied_from - shown - set(result.hidden_query_ids)
            if extra:
                problems.append(f"{name}: copied queries the link does not show: {extra}")
        old_ids = {k.lower() for k in (*result.query_map, *result.step_map, *result.viz_map)}
        blob = json.dumps([content, [q["parameters"] for q in queries], [s["code"] for s in steps]],
                          default=str, ensure_ascii=False)
        stale = {u.lower() for u in UUID.findall(blob)} & old_ids
        if stale:
            problems.append(f"{name}: references to the draft left in the copy: {sorted(stale)[:3]}")
        problems += [f"{name}: {p}" for p in _references_outside_the_copy(queries, steps, source_steps)]
        if any(q["report_id"] or q["widget_id"] for q in queries) or any(s["widget_id"] for s in steps) \
                or any(z["report_id"] for z in vizzes):
            problems.append(f"{name}: a copy still belongs to the conversation")
        for old_step in _run(_steps_with_different_data(result.step_map)):
            problems.append(f"{name}: step {old_step} copied with different data")
    assert not problems, "\n".join(problems)


@pytest.mark.e2e
def test_the_order_of_charts_and_deleted_charts_are_kept(corpus, corpus_rows):
    """The copy lists the same charts in the same order, and a deleted chart
    stays deleted instead of coming back."""
    release_id, result = _run(_copy(corpus.ids["refs_in_code/dash/v1"]))
    ids = corpus.ids
    with corpus_rows.engine.connect() as conn:
        _, _, vizzes, content = _copied_rows(conn, release_id)
        source = conn.execute(text("SELECT content FROM artifact_versions WHERE id = :v"),
                              {"v": ids["refs_in_code/dash/v1"]}).scalar()
    source = json.loads(source) if isinstance(source, str) else source
    assert content["visualization_ids"] == [result.viz_map[v] for v in source["visualization_ids"]]
    deleted_copy = result.viz_map[ids["refs_in_code/q_base/viz_deleted"]]
    assert [z["deleted_at"] is not None for z in vizzes if z["id"] == deleted_copy] == [True]


@pytest.mark.e2e
def test_filters_and_loaded_steps_point_at_the_copies(corpus, corpus_rows):
    ids = corpus.ids
    # options_source by id and by title → the copied option query's id
    release_id, result = _run(_copy(corpus.ids["options_sources/dash/v1"]))
    with corpus_rows.engine.connect() as conn:
        queries, _, _, _ = _copied_rows(conn, release_id)
    target = result.query_map[ids["options_sources/q_genres"]]
    for source_key in ("options_sources/q_by_id", "options_sources/q_by_title"):
        copy = next(q for q in queries if q["id"] == result.query_map[ids[source_key]])
        params = json.loads(copy["parameters"]) if isinstance(copy["parameters"], str) else copy["parameters"]
        assert params[0]["options_source"]["query_id"] == target, source_key

    # load_step by title (its target is also a chart there): copied, title kept
    release_id, result = _run(_copy(corpus.ids["load_step/dash/v1"]))
    assert ids["load_step/q_base"] in result.query_map
    with corpus_rows.engine.connect() as conn:
        _, steps, _, _ = _copied_rows(conn, release_id)
    assert any("load_step('בסיס')" in (s["code"] or "") for s in steps)

    # load_step by id and by slug, a query id in page code, an upper-case viz id,
    # and two queries with no chart that the dashboard still needs
    release_id, result = _run(_copy(corpus.ids["refs_in_code/dash/v1"]))
    with corpus_rows.engine.connect() as conn:
        queries, steps, _, content = _copied_rows(conn, release_id)
    hidden = {ids["refs_in_code/q_regions"], ids["refs_in_code/q_hidden"]}
    assert hidden <= set(result.hidden_query_ids), result.hidden_query_ids
    base_query = next(q for q in queries if q["id"] == result.query_map[ids["refs_in_code/q_base"]])
    base_params = json.loads(base_query["parameters"]) if isinstance(base_query["parameters"], str) \
        else base_query["parameters"]
    assert base_params[0]["options_source"]["query_id"] == result.query_map[ids["refs_in_code/q_regions"]]
    by_id = next(s for s in steps if s["id"] == result.step_map[ids["refs_in_code/q_by_id/step"]])
    by_slug = next(s for s in steps if s["id"] == result.step_map[ids["refs_in_code/q_by_slug/step"]])
    base_copy = next(s for s in steps if s["id"] == result.step_map[ids["refs_in_code/q_base/step"]])
    assert f"load_step('{base_copy['id']}')" in by_id["code"]
    assert f"load_step('{base_copy['slug']}')" in by_slug["code"]
    assert result.query_map[ids["refs_in_code/q_by_id"]] in content["code"]
    assert result.viz_map[ids["refs_in_code/q_by_slug/viz"]] in content["code"].lower()


@pytest.mark.e2e
def test_a_dashboard_without_a_chart_list_copies_what_its_link_shows(corpus, corpus_rows, test_client):
    """An old dashboard that never stored visualization_ids shows every query
    of its conversation with a successful run; that is what is copied."""
    cast = corpus.cast
    with corpus_rows.engine.connect() as conn:
        row = conn.execute(text(
            "SELECT av.id, av.report_id FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
            "WHERE av.deleted_at IS NULL AND a.mode = 'page' AND av.organization_id = :o "
            "AND (CAST(av.content AS TEXT) NOT LIKE '%visualization_ids%' "
            "OR CAST(av.content AS TEXT) LIKE '%\"visualization_ids\": []%')"),
            {"o": cast.org_id}).mappings().first()
    assert row is not None, "the corpus has no page dashboard without a chart list"
    shown = _shown_query_ids(test_client, cast, row["report_id"], row["id"])
    _, result = _run(_copy(row["id"]))
    assert shown and shown <= set(result.query_map)


@pytest.mark.e2e
def test_what_surrounds_the_dashboard_is_copied(corpus, corpus_rows):
    """Data sources, files and theme of the conversation; the thumbnail, deck
    and slide previews as files of the release's own."""
    from app.services.thumbnail_service import ThumbnailService

    ids = corpus.ids
    uploads = ThumbnailService.UPLOADS_DIR.parent
    deck = uploads / "pptx" / "corpus-deck.pptx"
    preview = uploads / "pptx_previews" / "corpus-deck" / "slide-1.png"
    for path, data in ((deck, b"PK deck"), (preview, b"\x89PNG slide")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    try:
        # Shaped like a real deck: the thumbnail is the first slide preview.
        # Two preview entries a hand-edited content could hold must not be copied.
        corpus_rows.update("artifact_versions", ids["slides_previews/deck/v1"], pptx_path=str(deck),
                           thumbnail_path="pptx_previews/corpus-deck/slide-1.png", content={
            "code": "from pptx import Presentation", "runtime_version": 1,
            "visualization_ids": [ids["slides_previews/q_sales/viz"]],
            "preview_images": ["pptx_previews/corpus-deck/slide-1.png", "../../app/main.py",
                               f"../thumbnails/{ids['slides_previews/deck/v1']}.png"]})
        release_id, _ = _run(_copy(ids["slides_previews/deck/v1"]))
        with corpus_rows.engine.connect() as conn:
            rel = conn.execute(text("SELECT * FROM artifact_releases WHERE id = :p"), {"p": release_id}).mappings().one()
        content = json.loads(rel["content"]) if isinstance(rel["content"], str) else rel["content"]
        assert rel["pptx_path"] and Path(rel["pptx_path"]) != deck and Path(rel["pptx_path"]).read_bytes() == b"PK deck"
        new_preview = content["preview_images"][0]
        assert new_preview != "pptx_previews/corpus-deck/slide-1.png"
        assert (uploads / new_preview).read_bytes() == b"\x89PNG slide"
        assert rel["thumbnail_path"] == new_preview
        assert content["preview_images"][1:] == ["../../app/main.py", f"../thumbnails/{ids['slides_previews/deck/v1']}.png"]
        release_dir = uploads / "pptx_previews" / f"release-{release_id}"
        assert sorted(p.name for p in release_dir.iterdir()) == ["slide-1.png"]
    finally:
        for path in (deck, preview):
            path.unlink(missing_ok=True)
        for leftover in [*uploads.glob("pptx/release-*.pptx"), *uploads.glob("pptx_previews/release-*/*"),
                         *uploads.glob("thumbnails/release-*.png")]:
            leftover.unlink(missing_ok=True)
        for folder in uploads.glob("pptx_previews/release-*"):
            folder.rmdir()

    release_id, _ = _run(_copy(ids["embedded_files/dash/v2"]))
    with corpus_rows.engine.connect() as conn:
        report = corpus.reports["embedded_files"]
        files = {r[0] for r in conn.execute(text("SELECT file_id FROM report_file_association WHERE report_id = :r"),
                                            {"r": report})}
        copied_files = {r[0] for r in conn.execute(text(
            "SELECT file_id FROM artifact_release_file_association WHERE artifact_release_id = :p"), {"p": release_id})}
        sources = {r[0] for r in conn.execute(text(
            "SELECT data_source_id FROM report_data_source_association WHERE report_id = :r"), {"r": report})}
        copied_sources = {r[0] for r in conn.execute(text(
            "SELECT data_source_id FROM artifact_release_data_source_association WHERE artifact_release_id = :p"),
            {"p": release_id})}
        theme = conn.execute(text("SELECT theme_name, theme_overrides FROM reports WHERE id = :r"),
                             {"r": report}).one()
        rel_theme = conn.execute(text("SELECT theme_name, theme_overrides FROM artifact_releases WHERE id = :p"),
                                 {"p": release_id}).one()
    assert files and copied_files == files
    assert sources and copied_sources == sources
    assert tuple(rel_theme) == tuple(theme)


@pytest.mark.e2e
def test_copying_twice_gives_two_independent_copies(corpus, corpus_rows):
    first_id, first = _run(_copy(corpus.ids["conv_one_dash/dash/v1"]))
    second_id, second = _run(_copy(corpus.ids["conv_one_dash/dash/v1"]))
    assert first_id != second_id
    assert not set(first.step_map.values()) & set(second.step_map.values())
    with corpus_rows.engine.connect() as conn:
        slugs = [r[0] for r in conn.execute(text(
            "SELECT s.slug FROM steps s JOIN queries q ON q.id = s.query_id WHERE q.artifact_release_id IN (:a, :b)"),
            {"a": first_id, "b": second_id})]
    assert len(slugs) == len(set(slugs)) >= 2


@pytest.mark.e2e
def test_the_engine_reads_nothing_outside_the_dashboards_conversation(corpus, corpus_rows):
    """A load_step or filter reference that matches a query of another
    conversation is not followed: the copy only takes what this conversation holds."""
    from app.models.artifact import ArtifactVersion
    from app.services.dashboard_copy import collect_dashboard_deps

    async def check():
        async with async_session_maker() as db:
            version = await db.get(ArtifactVersion, corpus.ids["load_step/dash/v1"])
            deps = await collect_dashboard_deps(db, version)
            from app.models.query import Query
            reports = {str(r[0]) for r in (await db.execute(
                select(Query.report_id).where(Query.id.in_(deps.all_query_ids)))).all()}
            assert reports == {corpus.reports["load_step"]}

    _run(check())


# ── edge cases, seeded into a corpus conversation for each test ────────────

from tests.e2e.dashboards.corpus_edits import add_dashboard as _add_dashboard  # noqa: E402
from tests.e2e.dashboards.corpus_edits import add_query as _add_query  # noqa: E402


def _loads(ref):
    return f"def generate_df(ds_clients, excel_files, load_step):\n    return load_step('{ref}')\n"


@pytest.mark.e2e
def test_load_step_edge_cases(corpus, corpus_rows):
    """A deleted query a step loads is copied still deleted; a title two steps
    share is pinned to the copy of the one the source resolves (the most
    recent); two queries loading each other do not loop."""
    from datetime import datetime

    rows = corpus_rows
    old = _add_query(rows, corpus, "conv_no_dash", "ישנה שנמחקה", chart=False, deleted=True)
    first = _add_query(rows, corpus, "conv_no_dash", "כפול", chart=False, step_created_at=datetime(2026, 3, 2))
    newest = _add_query(rows, corpus, "conv_no_dash", "כפול", chart=False, step_created_at=datetime(2026, 3, 5))
    a = _add_query(rows, corpus, "conv_no_dash", "מעגל א", chart=False, code=_loads("מעגל ב"))
    b = _add_query(rows, corpus, "conv_no_dash", "מעגל ב", chart=False, code=_loads("מעגל א"))
    twin_old = _add_query(rows, corpus, "conv_no_dash", "תאום", step_created_at=datetime(2026, 3, 2))
    twin_new = _add_query(rows, corpus, "conv_no_dash", "תאום", step_created_at=datetime(2026, 3, 6))
    loader = _add_query(rows, corpus, "conv_no_dash", "טוענת", code=(
        "def generate_df(ds_clients, excel_files, load_step):\n"
        "    load_step('ישנה שנמחקה'); load_step('מעגל א'); load_step('תאום')\n"
        "    return load_step('כפול')\n"))
    version = _add_dashboard(rows, corpus, "conv_no_dash", [loader["viz"], twin_old["viz"], twin_new["viz"]])

    release_id, result = _run(_copy(version))
    for name, row in (("deleted", old), ("newest", newest), ("cycle a", a), ("cycle b", b)):
        assert row["query"] in result.hidden_query_ids, name
    assert first["query"] not in result.query_map
    with corpus_rows.engine.connect() as conn:
        queries, steps, _, _ = _copied_rows(conn, release_id)
    by_id = {q["id"]: q for q in queries}
    assert by_id[result.query_map[old["query"]]]["deleted_at"] is not None
    from app.services.dashboard_copy import StepIndex

    loader_copy = next(s for s in steps if s["id"] == result.step_map[loader["step"]])
    copies = StepIndex([_Row(s) for s in steps if s["status"] == "success"])
    # Only the most recent "כפול" is copied, so the copy resolves the title to it
    assert copies.find("כפול")[0].id == result.step_map[newest["step"]]
    assert "load_step('ישנה שנמחקה')" in loader_copy["code"]  # a title only one step has stays
    # Both "תאום" are copied, so the title is pinned to the copy of the one the
    # draft loads (the most recent), by its slug
    twin_copy = next(s for s in steps if s["id"] == result.step_map[twin_new["step"]])
    assert f"load_step('{twin_copy['slug']}')" in loader_copy["code"]


@pytest.mark.e2e
def test_filter_edge_cases(corpus, corpus_rows):
    """A filter naming a title that a shown and a hidden query share takes the
    shown one, as the page does; a filter pointing into another conversation
    is dropped and nothing of that conversation is copied."""
    rows = corpus_rows
    shown_regions = _add_query(rows, corpus, "conv_no_dash", "אזורים")
    _add_query(rows, corpus, "conv_no_dash", "אזורים", chart=False)
    foreign = corpus.ids["conv_one_dash/q_sales"]
    user = _add_query(rows, corpus, "conv_no_dash", "מסננת", parameters=[
        {"name": "region", "type": "string", "source": "input",
         "options_source": {"query_id": "אזורים", "value_column": "x"}},
        {"name": "other", "type": "string", "source": "input",
         "options_source": {"query_id": foreign, "value_column": "x"}}])
    version = _add_dashboard(rows, corpus, "conv_no_dash", [user["viz"], shown_regions["viz"]])

    release_id, result = _run(_copy(version))
    assert foreign not in result.query_map
    with corpus_rows.engine.connect() as conn:
        queries, _, _, _ = _copied_rows(conn, release_id)
    copy = next(q for q in queries if q["id"] == result.query_map[user["query"]])
    params = json.loads(copy["parameters"]) if isinstance(copy["parameters"], str) else copy["parameters"]
    assert params[0]["options_source"]["query_id"] == result.query_map[shown_regions["query"]]
    assert params[1]["options_source"] is None
