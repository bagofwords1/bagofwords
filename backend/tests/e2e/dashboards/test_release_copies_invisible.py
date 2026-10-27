"""A published dashboard's copies are invisible on every conversation path (phase 2, part B).

Copies have no conversation and no widget. Every existing route that takes a
query, step or visualization id answers "not found" for them, for every kind
of reader in the corpus, and never serves their data or changes them. Only the
/d routes (phase 4) will serve them, under the dashboard's own sharing.

The routes are taken from the application itself, so a route added later is
covered without editing this file.
"""
import asyncio

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.release_copies import MARKER, add_release_copy, copy_rows
from tests.fixtures.dashboard_corpus.cast import ROLES

ID_PARAMS = {"query_id": "query", "step_id": "step", "visualization_id": "viz"}


def _run(coro):
    return asyncio.run(coro)


def _routes_taking_a_copy_id():
    """From the OpenAPI schema: FastAPI wraps included routers, so app.routes
    does not list the endpoints themselves. No conversation route is hidden
    from the schema (no include_in_schema=False under app/routes)."""
    import re
    import warnings

    from main import app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        paths = app.openapi()["paths"]
    for path, methods in sorted(paths.items()):
        names = set(re.findall(r"{(\w+)}", path))
        if names & set(ID_PARAMS):
            for method in sorted(methods):
                yield method.upper(), path, names


def _fill(path: str, names: set, copy: dict, corpus) -> str:
    # `ref`: the /d routes (phase 4) of the dashboard the copy was made for.
    # Its copy belongs to a release that is not the published one, so they
    # must refuse it like every other route.
    known = {**{p: copy[k] for p, k in ID_PARAMS.items()},
             "report_id": corpus.reports["conv_one_dash"], "ref": corpus.ids["conv_one_dash/dash"]}
    unknown = names - set(known)
    assert not unknown, f"{path}: no value for {sorted(unknown)}; teach this test what to pass"
    for name in names:
        path = path.replace("{" + name + "}", known[name])
    return path


@pytest.mark.e2e
def test_copies_are_not_found_on_any_conversation_route(corpus, corpus_rows):
    from fastapi.testclient import TestClient

    from main import app

    from tests.e2e.dashboards.dashboard_setup import publish

    cast = corpus.cast
    copy = _run(add_release_copy(artifact_id=corpus.ids["conv_one_dash/dash"],
                                 organization_id=cast.org_id, user_id=cast.id("owner")))
    # The dashboard has another, live release and is open to the whole
    # organization, so readers get past the /d door and the copy is refused
    # for what it is: not a row of the live release.
    publish(corpus.ids["conv_one_dash/dash"])
    corpus_rows.update("artifacts", corpus.ids["conv_one_dash/dash"], visibility="internal")
    before = _run(copy_rows(copy))
    client = TestClient(app, raise_server_exceptions=False)
    routes = list(_routes_taking_a_copy_id())
    assert len(routes) >= 10, routes  # the query, step and visualization routes exist
    import pathlib
    routes_dir = pathlib.Path(__file__).resolve().parents[3] / "app" / "routes"
    hidden = [p.name for p in routes_dir.rglob("*.py") if "include_in_schema=False" in p.read_text()]
    assert not hidden, f"routes hidden from the schema would escape this test: {hidden}"

    problems = []
    for method, path, names in routes:
        url = _fill(path, names, copy, corpus)
        for role in ROLES:
            person = cast.people[role]
            org = None if role == "anonymous" else (cast.other_org_id if role == "other_org" else cast.org_id)
            resp = client.request(method, url, headers=person.headers(org), json={})
            where = f"{role} {method} {path} -> {resp.status_code}"
            if resp.status_code < 400 or resp.status_code >= 500:
                problems.append(where)
            elif MARKER in resp.text:
                problems.append(f"{where} leaked the copy's data")
            elif role in ("owner", "admin") and resp.status_code not in (404, 422):
                # They can open the conversation, so anything but "not found"
                # means the route looked at the copy (422: the body was
                # refused before any lookup).
                problems.append(f"{where}, expected 404 for a reader who sees the conversation")
    assert not problems, "\n".join(problems)
    assert _run(copy_rows(copy)) == before, "a conversation route changed a copy"


@pytest.mark.e2e
def test_query_lists_never_include_copies(corpus):
    from fastapi.testclient import TestClient

    from main import app

    cast = corpus.cast
    copy = _run(add_release_copy(artifact_id=corpus.ids["conv_one_dash/dash"],
                                 organization_id=cast.org_id, user_id=cast.id("owner")))
    client = TestClient(app, raise_server_exceptions=False)
    problems = []
    for role in ROLES:
        person = cast.people[role]
        org = None if role == "anonymous" else (cast.other_org_id if role == "other_org" else cast.org_id)
        for params in ({}, {"report_id": corpus.reports["conv_one_dash"]}):
            resp = client.get("/api/queries", params=params, headers=person.headers(org))
            if resp.status_code >= 500 or copy["query"] in resp.text or MARKER in resp.text:
                problems.append(f"{role} /api/queries {params} -> {resp.status_code}")
    assert not problems, "\n".join(problems)


# Rows whose owner, as query-keyed checks see it, deliberately differs from
# before (decision 5): a query with no report_id of its own now reaches its
# widget's conversation. (widget_without_report/q_no_report reaches no one,
# as before; the gates now refuse it instead of skipping their check.)
DECISION_5 = {"orphans/q_no_report": "orphans"}


@pytest.mark.e2e
def test_ownership_answers_as_today_for_every_row(corpus, corpus_rows):
    """For every row in the corpus, owner_of gives what the code read before:
    step-keyed checks read `step.widget.report`, query-keyed checks read
    `query.report_id`, and the builder run and preview joined the query's
    widget. The only differences allowed are the decision-5 rows, and copies
    (new rows) belong to their release."""
    from app.dependencies import async_session_maker
    from app.services.row_owner import Owner, query_owner, step_owner

    cast = corpus.cast
    copy = _run(add_release_copy(artifact_id=corpus.ids["conv_one_dash/dash"],
                                 organization_id=cast.org_id, user_id=cast.id("owner")))
    with corpus_rows.engine.connect() as conn:
        steps = conn.execute(text(
            "SELECT s.id, s.widget_id, w.report_id AS w FROM steps s "
            "LEFT JOIN widgets w ON w.id = s.widget_id")).mappings().all()
        queries = conn.execute(text(
            "SELECT q.id, q.widget_id, q.report_id AS q, w.report_id AS w, q.artifact_release_id AS rel "
            "FROM queries q LEFT JOIN widgets w ON w.id = q.widget_id")).mappings().all()

    def report(value):
        return Owner("report", str(value)) if value else None

    name = corpus.names.get
    copies = {copy["query"], copy["step"]}

    async def compare():
        differences = {}
        async with async_session_maker() as db:
            for r in steps:
                before = report(r["w"])  # step.widget.report
                got = await step_owner(db, r["id"])
                if got != before:
                    differences[name(r["id"], r["id"])] = got
            for r in queries:
                before = report(r["q"])  # query.report_id
                got = await query_owner(db, r["id"])
                if got != before:
                    differences[name(r["id"], r["id"])] = got
                before_widget = report(r["w"])  # the builder paths' widget join
                got_widget = await query_owner(db, r["id"], widget_first=True)
                if got_widget != before_widget:
                    differences[f"{name(r['id'], r['id'])} (widget first)"] = got_widget
        return differences

    differences = _run(compare())
    copy_names = {name(i, i) for i in copies}
    expected = {k: report(corpus.reports[v]) for k, v in DECISION_5.items()}
    expected.update({n: Owner("release", copy["release"]) for n in copy_names})
    expected[f"{name(copy['query'], copy['query'])} (widget first)"] = Owner("release", copy["release"])
    assert differences == expected
