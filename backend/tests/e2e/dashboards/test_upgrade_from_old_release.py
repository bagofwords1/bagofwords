"""A customer on an old release upgrades straight to this one.

Customers skip releases, so the migration chain must carry a database from an
old release to head in one go. This starts at 0.0.560 (OLD_RELEASE_REVISION),
writes a shared conversation with a dashboard in that release's shape --
before artver01 split artifacts into parent + versions -- and upgrades.

Phase 0 only proves the path works and keeps the data; the dashboards
project's own migration adds its assertions here.
"""
import pytest
from sqlalchemy import text

from tests.fixtures.dashboard_corpus.alembic_at import (
    OLD_RELEASE_REVISION,
    alembic_pointed_at,
    current_revisions,
    scratch_database,
    script_directory,
)
from tests.fixtures.dashboard_corpus.rows import Rows


@pytest.mark.e2e
def test_old_release_database_upgrades_to_head(tmp_path):
    from sqlalchemy import create_engine

    from alembic import command

    with scratch_database(tmp_path) as url:
        with alembic_pointed_at(url) as cfg:
            command.upgrade(cfg, OLD_RELEASE_REVISION)

        rows = Rows(create_engine(url))
        try:
            org = rows.insert("organizations", name="לקוח ותיק")
            user = rows.insert("users", name="בעלים", email="owner@old.com", hashed_password="x")
            rows.insert("memberships", user_id=user, organization_id=org, role="admin")
            report = rows.insert("reports", title="דוח ותיק", slug="old-report", user_id=user,
                                 organization_id=org, artifact_visibility="internal")
            # 0.0.560 kept one artifacts row per version: title, mode, version
            # and content on the row itself.
            artifact = rows.insert("artifacts", report_id=report, user_id=user, organization_id=org,
                                   title="לוח מכירות", mode="page", version=3, status="completed",
                                   content={"code": "function App() {}", "visualization_ids": []})
        finally:
            rows.close()

        with alembic_pointed_at(url) as cfg:
            command.upgrade(cfg, "head")

        assert current_revisions(url) == (script_directory().get_current_head(),)
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                version = conn.execute(text(
                    "SELECT artifact_id, report_id, version FROM artifact_versions WHERE id = :id"
                ), {"id": artifact}).mappings().one()
                parent = conn.execute(text(
                    "SELECT title, mode, report_id FROM artifacts WHERE id = :id"
                ), {"id": version["artifact_id"]}).mappings().one()
                shared = conn.execute(text(
                    "SELECT artifact_visibility FROM reports WHERE id = :id"
                ), {"id": report}).scalar_one()
                # Phase 1 schema (dashfc01): the old dashboard takes the
                # conversation defaults, and nothing is published yet.
                dash = conn.execute(text(
                    "SELECT visibility, shared_run_identity, published_release_id FROM artifacts WHERE id = :id"
                ), {"id": version["artifact_id"]}).mappings().one()
                releases = conn.execute(text("SELECT count(*) FROM artifact_releases")).scalar_one()
        finally:
            engine.dispose()

        assert version["report_id"] == report and version["version"] == 3
        assert parent["title"] == "לוח מכירות" and parent["mode"] == "page"
        assert shared == "internal"
        assert dash["visibility"] == "none" and dash["shared_run_identity"] == "viewer"
        assert dash["published_release_id"] is None and releases == 0
