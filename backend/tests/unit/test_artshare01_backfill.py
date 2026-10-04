"""artshare01 — dashboards move from report-level to per-artifact sharing.

The upgrade must not change who can see what: every existing artifact takes
its report's artifact_visibility, and every live report-level dashboard grant
(report_shares, share_type='artifact', user or group) lands on each live
artifact of that report. Conversation grants stay conversation-only. The
downgrade (production rollback) must leave the report-level data intact.

Run:
    cd backend
    TESTING=true uv run pytest tests/unit/test_artshare01_backfill.py --db=sqlite
"""
import os
import uuid

import sqlalchemy as sa


def _insert(conn, table: str, **values):
    """Insert a row, filling any other NOT NULL column without a default
    with a placeholder — this test only cares about the sharing columns."""
    for _, name, col_type, notnull, default, pk in conn.execute(sa.text(f"PRAGMA table_info({table})")):
        if name in values or not notnull or default is not None or pk:
            continue
        t = (col_type or "").upper()
        if "INT" in t or "BOOL" in t:
            values[name] = 0
        elif "JSON" in t:
            values[name] = "{}"
        elif "DATE" in t or "TIME" in t:
            values[name] = "2025-01-01 00:00:00"
        else:
            values[name] = f"x-{uuid.uuid4().hex[:8]}"
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    conn.execute(sa.text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), values)


def test_artshare01_preserves_access_and_round_trips(tmp_path):
    from alembic import command
    from alembic.config import Config
    from app.settings.config import settings

    db_url = f"sqlite:///{tmp_path}/artshare.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)

    # alembic/env.py resolves the URL from settings.TEST_DATABASE_URL under
    # TESTING — point it at our scratch file and restore afterwards.
    saved_setting = settings.TEST_DATABASE_URL
    saved_env = os.environ.get("TEST_DATABASE_URL")
    settings.TEST_DATABASE_URL = db_url
    os.environ["TEST_DATABASE_URL"] = db_url
    try:
        command.upgrade(cfg, "emailclaim01")
        engine = sa.create_engine(db_url)
        with engine.begin() as conn:
            _insert(conn, "reports", id="r_shared", artifact_visibility="shared",
                    conversation_visibility="shared", organization_id="o1", user_id="owner")
            _insert(conn, "reports", id="r_public", artifact_visibility="public",
                    conversation_visibility="none", organization_id="o1", user_id="owner")
            _insert(conn, "reports", id="r_private", artifact_visibility="none",
                    conversation_visibility="none", organization_id="o1", user_id="owner")
            for aid, rid, deleted in (
                ("a_sales", "r_shared", None),
                ("a_payroll", "r_shared", None),
                ("a_gone", "r_shared", "2025-06-01 00:00:00"),
                ("a_public", "r_public", None),
                ("a_private", "r_private", None),
            ):
                _insert(conn, "artifacts", id=aid, report_id=rid, organization_id="o1",
                        created_by="owner", mode="page", title=aid, deleted_at=deleted)
            for sid, uid, gid, share_type, deleted in (
                ("s_user", "u_viewer", None, "artifact", None),
                ("s_group", None, "g_team", "artifact", None),
                ("s_conv", "u_reader", None, "conversation", None),
                ("s_revoked", "u_former", None, "artifact", "2025-06-01 00:00:00"),
            ):
                _insert(conn, "report_shares", id=sid, report_id="r_shared", user_id=uid,
                        group_id=gid, share_type=share_type, deleted_at=deleted)

        command.upgrade(cfg, "artshare01")
        with engine.connect() as conn:
            visibility = dict(conn.execute(sa.text("SELECT id, visibility FROM artifacts")).all())
            grants = {
                (a, u, g) for a, u, g in conn.execute(sa.text(
                    "SELECT artifact_id, user_id, group_id FROM artifact_shares"
                ))
            }
            report_ids = dict(conn.execute(sa.text(
                "SELECT artifact_id, report_id FROM artifact_shares"
            )).all())

        assert visibility["a_sales"] == "shared"
        assert visibility["a_payroll"] == "shared"
        assert visibility["a_public"] == "public"
        assert visibility["a_private"] == "none"
        # Each live artifact of the shared report carries exactly the live
        # dashboard grants: no conversation grant, no revoked grant, nothing
        # on the deleted artifact.
        assert grants == {
            ("a_sales", "u_viewer", None), ("a_sales", None, "g_team"),
            ("a_payroll", "u_viewer", None), ("a_payroll", None, "g_team"),
        }
        assert set(report_ids.values()) == {"r_shared"}

        command.downgrade(cfg, "emailclaim01")
        with engine.connect() as conn:
            tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            artifact_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(artifacts)"))}
            report_rows = dict(conn.execute(sa.text("SELECT id, artifact_visibility FROM reports")).all())
            share_rows = conn.execute(sa.text("SELECT count(*) FROM report_shares")).scalar()
        assert "artifact_shares" not in tables
        assert "visibility" not in artifact_cols
        assert report_rows == {"r_shared": "shared", "r_public": "public", "r_private": "none"}
        assert share_rows == 4
    finally:
        settings.TEST_DATABASE_URL = saved_setting
        if saved_env is None:
            os.environ.pop("TEST_DATABASE_URL", None)
        else:
            os.environ["TEST_DATABASE_URL"] = saved_env
