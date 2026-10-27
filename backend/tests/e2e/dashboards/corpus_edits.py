"""Add rows to a corpus conversation inside one test (the corpus is rebuilt per test)."""


def add_query(rows, corpus, report_key, title, *, code="def generate_df(ds_clients, excel_files):\n    return None\n",
              parameters=None, chart=True, deleted=False, step_created_at=None):
    from datetime import datetime

    from tests.fixtures.dashboard_corpus.rows import new_id

    report = corpus.reports[report_key]
    now = step_created_at or datetime(2026, 3, 2)
    widget = rows.insert("widgets", title=title, slug=f"edge-{new_id()[:12]}", report_id=report,
                         status="draft", x=0, y=0, width=6, height=4)
    query = rows.insert("queries", title=title, widget_id=widget, report_id=report,
                        organization_id=corpus.cast.org_id, user_id=corpus.cast.id("owner"),
                        deleted_at=now if deleted else None,
                        **({"parameters": parameters} if parameters is not None else {}))
    step = rows.insert("steps", title=title, slug=f"edge-{new_id()[:12]}", status="success", widget_id=widget,
                       query_id=query, code=code, data={"rows": [{"x": 1}], "columns": [{"field": "x"}]},
                       prompt="", description="", type="table", created_at=now, updated_at=now)
    rows.update("queries", query, default_step_id=step)
    viz = rows.insert("visualizations", title=title, status="success", report_id=report, query_id=query,
                      view={"type": "table"}) if chart else None
    return {"query": query, "step": step, "viz": viz}


def add_dashboard(rows, corpus, report_key, viz_ids):
    report = corpus.reports[report_key]
    artifact = rows.insert("artifacts", report_id=report, organization_id=corpus.cast.org_id,
                           created_by=corpus.cast.id("owner"), mode="page", title="edge")
    return rows.insert("artifact_versions", artifact_id=artifact, report_id=report,
                       user_id=corpus.cast.id("owner"), organization_id=corpus.cast.org_id, version=1,
                       status="completed", content={"code": "function App() {}", "visualization_ids": viz_ids})
