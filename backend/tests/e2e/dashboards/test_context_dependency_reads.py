"""A reader's dependency checks stay bounded as waiting charts fan out."""
from sqlalchemy import event
from sqlalchemy.engine import Engine

from tests.e2e.dashboards.corpus_edits import add_query
from tests.e2e.dashboards.dashboard_setup import own, share
from tests.e2e.dashboards.test_context_publish import (
    CUSTOMER, DRAFT_ROWS, LOADS_BASE, NEEDS_CUSTOMER, _finish, _post, _run, _status,
    drawn, spawned,  # noqa: F401
)


def test_waiting_dependencies_reuse_the_release_catalog_within_a_reader_run(
        corpus, corpus_rows, test_client, spawned):  # noqa: F811
    corpus_rows.update("queries", corpus.ids["load_step/q_base"], parameters=[CUSTOMER])
    corpus_rows.update("steps", corpus.ids["load_step/q_base/step"], code=NEEDS_CUSTOMER,
                       data=DRAFT_ROWS, applied_params={"customer_id": None})
    corpus_rows.update("steps", corpus.ids["load_step/q_derived/step"], code=LOADS_BASE)
    viz = [corpus.ids["load_step/q_base/viz"], corpus.ids["load_step/q_derived/viz"]]
    # Agent-authored queries have no create endpoint; use the corpus seeder.
    for index in range(6):
        viz.append(add_query(corpus_rows, corpus, "load_step", f"Dependent {index}", code=LOADS_BASE)["viz"])
    corpus_rows.update("artifact_versions", corpus.ids["load_step/dash/v1"],
                       content={"code": "function App() {}", "visualization_ids": viz})
    aid = own(corpus_rows, corpus, "load_step/dash")
    assert _post(test_client, corpus, aid).status_code == 202
    _finish(spawned)
    assert _status(test_client, corpus, aid)["published"]["status"] == "ready"
    assert share(test_client, corpus, aid, "internal").status_code == 200
    catalogs = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        # Full release catalog reads, irrespective of the caller's helper name.
        if (statement.lstrip().upper().startswith("SELECT") and "steps.slug" in statement
                and "steps.id = queries.default_step_id" in statement
                and "queries.artifact_release_id" in statement):
            catalogs.append(statement)

    event.listen(Engine, "before_cursor_execute", observe)
    try:
        for reader in ("member", "admin"):
            catalogs.clear()
            run = _run(test_client, corpus, aid, person=reader)
            assert run["steps_failed"] == 0 and run["steps_needing_input"] == len(viz), run
            # Discovery, ordering and wait propagation each need at most one
            # catalog, regardless of how many downstream charts share a base.
            assert len(catalogs) <= 3, f"{len(catalogs)} catalog reads for {len(viz)} steps"
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
