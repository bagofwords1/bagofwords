"""The corpus really holds every kind it claims to, in the shape customers have.

A kind that silently failed to seed would make every later comparison pass
for the wrong reason, so each one is checked against the database itself.
"""
import pytest
from sqlalchemy import text

from app.ee.encryption.types import is_encrypted_envelope


def _one(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().all()


@pytest.mark.e2e
def test_every_kind_is_in_the_database(corpus, corpus_rows):
    ids = corpus.ids
    r = corpus.reports

    def report(key):
        return _one(conn, "SELECT * FROM reports WHERE id = :id", id=r[key])[0]

    def versions(key):
        return _one(conn, "SELECT av.*, a.mode, a.deleted_at AS artifact_deleted_at "
                          "FROM artifact_versions av JOIN artifacts a ON a.id = av.artifact_id "
                          "WHERE av.report_id = :id ORDER BY av.created_at", id=r[key])

    def step(name):
        return _one(conn, "SELECT * FROM steps WHERE id = :id", id=ids[name])[0]

    with corpus_rows.engine.connect() as conn:
        # א. structure
        assert versions("conv_no_dash") == []
        assert len({v["artifact_id"] for v in versions("conv_three_dash")}) == 3
        assert [v["status"] for v in versions("dash_many_versions")] == \
            ["completed", "completed", "failed", "completed"]
        assert versions("dash_newest_failed")[-1]["status"] == "failed"
        assert versions("dash_newest_pending")[-1]["status"] == "pending"
        assert versions("dash_newest_stopped")[-1]["status"] == "stopped"
        chain = versions("artver01_chain")
        assert len(chain) == 3 and all(v["id"] == v["artifact_id"] for v in chain)
        assert sorted(v["version"] for v in chain) == [1, 2, 5]
        dv = versions("dash_deleted_version")
        assert dv[1]["deleted_at"] is not None and dv[0]["deleted_at"] is None
        gone = [v for v in versions("dash_fully_deleted") if v["artifact_id"] == ids["dash_fully_deleted/dash_gone"]]
        assert gone and all(v["deleted_at"] and v["artifact_deleted_at"] for v in gone)
        assert versions("legacy_widgets") == []
        assert report("legacy_widgets")["status"] == "published"
        assert _one(conn, "SELECT count(*) AS n FROM widgets WHERE report_id = :id AND status = 'published'",
                    id=r["legacy_widgets"])[0]["n"] == 2
        assert versions("shared_no_dash") == [] and report("shared_no_dash")["artifact_visibility"] == "internal"
        deck = versions("slides_previews")[0]
        assert deck["mode"] == "slides" and deck["pptx_path"] and deck["thumbnail_path"]
        modes = [v["mode"] for v in versions("doc_after_page")]
        assert modes == ["page", "doc"]
        assert [v["mode"] for v in versions("doc_only")] == ["doc"]
        ef = versions("embedded_files")
        assert ef[0]["deleted_at"] and _content(ef[0])["files"] and _content(ef[1])["files"]
        assert _one(conn, "SELECT count(*) AS n FROM artifact_records")[0]["n"] == 3
        assert _one(conn, "SELECT count(*) AS n FROM artifact_publications WHERE artifact_id = :a",
                    a=ids["publication_pin/dash"])[0]["n"] == 1

        # ב. conversation types and sharing
        for key, vis in (("vis_public", "public"), ("vis_internal", "internal"),
                         ("vis_shared_user", "shared"), ("vis_shared_group", "shared"), ("vis_none", "none")):
            assert report(key)["artifact_visibility"] == vis
        assert _one(conn, "SELECT user_id FROM report_shares WHERE report_id = :id", id=r["vis_shared_user"])[0]["user_id"]
        assert _one(conn, "SELECT group_id FROM report_shares WHERE report_id = :id", id=r["vis_shared_group"])[0]["group_id"]
        assert report("conv_public")["conversation_share_token"]
        assert report("conv_enabled_mismatch")["conversation_share_enabled"] in (False, 0)
        assert report("archived_public")["status"] == "archived"
        assert report("report_deleted_at")["deleted_at"] is not None
        assert report("in_project_private")["project_id"] == corpus.cast.projects["private"]
        assert report("bow_source")["bow_source_access"]
        threads = _one(conn, "SELECT * FROM reports WHERE forked_from_id = :id AND report_type = 'artifact_chat'",
                       id=r["chat_enabled"])
        assert len(threads) == 2
        assert report("test_report")["report_type"] == "test"
        assert report("training_mode")["mode"] == "training"
        assert step("fork_filling/q_sales/step")["status"] == "pending"
        assert report("fork_retired")["status"] == "archived"
        assert _one(conn, "SELECT count(*) AS n FROM report_data_source_association WHERE report_id = :id",
                    id=r["auto_conv"])[0]["n"] == 0
        assert report("run_creator")["shared_run_identity"] == "creator"
        fps = _one(conn, "SELECT params_fingerprint FROM step_user_results WHERE report_id = :id",
                   id=r["run_viewer_results"])
        assert len(fps) == 3 and len({f["params_fingerprint"] for f in fps}) == 2
        assert _one(conn, "SELECT c.auth_policy FROM report_data_source_association rd "
                          "JOIN domain_connection dc ON dc.data_source_id = rd.data_source_id "
                          "JOIN connections c ON c.id = dc.connection_id WHERE rd.report_id = :id",
                    id=r["user_required"])[0]["auth_policy"] == "user_required"
        assert _one(conn, "SELECT ct.rls_enabled FROM report_data_source_association rd "
                          "JOIN domain_connection dc ON dc.data_source_id = rd.data_source_id "
                          "JOIN connection_tables ct ON ct.connection_id = dc.connection_id "
                          "WHERE rd.report_id = :id", id=r["rls"])[0]["rls_enabled"] in (True, 1)
        assert report("no_data_tab")["include_data_tab"] in (False, 0)

        # ג. execution and data
        assert _one(conn, "SELECT report_id FROM queries WHERE id = :id", id=ids["orphans/q_no_report"])[0]["report_id"] is None
        assert step("orphans/step_no_query/step")["query_id"] is None
        assert step("default_states/q_draft/step")["status"] == "draft"
        assert step("default_states/q_error/step")["status"] == "error"
        assert step("default_states/q_purged/step")["data"] is None
        assert _one(conn, "SELECT deleted_at FROM visualizations WHERE id = :id",
                    id=ids["viz_refs/q_deleted/viz"])[0]["deleted_at"] is not None
        assert ids["viz_refs/missing_viz"] in _content(versions("viz_refs")[0])["visualization_ids"]
        by_title = _json(_one(conn, "SELECT parameters FROM queries WHERE id = :id",
                              id=ids["options_sources/q_by_title"])[0]["parameters"])
        assert by_title[0]["options_source"]["query_id"] == "ז'אנרים"
        assert "load_step(" in step("load_step/q_derived/step")["code"]
        assert _one(conn, "SELECT source_step_id FROM entities WHERE id = :id",
                    id=ids["entity_from_step"])[0]["source_step_id"] == ids["conv_one_dash/q_sales/step"]
        assert versions("thumbnail")[0]["screenshot_base64"]
        assert report("cron")["cron_schedule"] and report("cron")["notification_subscribers"]
        assert report("cron_legacy_empty")["cron_schedule"] == ""
        assert report("cron_legacy_none")["cron_schedule"] == "None"
        assert report("refresh_off")["refresh_on_view"] in (False, 0)
        assert _one(conn, "SELECT spawn_new_report FROM scheduled_prompts WHERE id = :id",
                    id=ids["sched_prompt_spawn/prompt"])[0]["spawn_new_report"] in (True, 1)
        assert report("sched_prompt_spawn/run_1")["scheduled_prompt_id"] == ids["sched_prompt_spawn/prompt"]
        assert is_encrypted_envelope(_json(step("encrypted/q_sealed/step")["data"]))
        assert not is_encrypted_envelope(_json(step("encrypted/q_plain/step")["data"]))
        sealed = _one(conn, "SELECT data FROM step_user_results WHERE step_id = :id",
                      id=ids["encrypted/q_sealed/step"])[0]["data"]
        assert is_encrypted_envelope(_json(sealed))

        # legacy shapes from before 0.0.583
        elsewhere = _one(conn, "SELECT q.report_id AS q_report, w.report_id AS w_report, s.widget_id AS s_widget, "
                               "q.widget_id AS q_widget FROM queries q JOIN widgets w ON w.id = q.widget_id "
                               "JOIN steps s ON s.query_id = q.id WHERE q.id = :id",
                         id=ids["widget_elsewhere/q_sales"])[0]
        assert elsewhere["q_report"] == r["widget_elsewhere"]
        assert elsewhere["w_report"] == r["widget_elsewhere_home"]
        assert elsewhere["s_widget"] == elsewhere["q_widget"]
        shared = _one(conn, "SELECT id, default_step_id FROM queries WHERE widget_id = :w ORDER BY created_at",
                      w=ids["shared_widget/q_first/widget"])
        assert [q["id"] for q in shared] == [ids["shared_widget/q_first"], ids["shared_widget/q_second"]]
        assert shared[1]["default_step_id"] is None
        for qkey, own in (("q_own_report", r["widget_without_report"]), ("q_no_report", None)):
            row = _one(conn, "SELECT q.report_id AS q_report, w.report_id AS w_report FROM queries q "
                             "JOIN widgets w ON w.id = q.widget_id WHERE q.id = :id",
                       id=ids[f"widget_without_report/{qkey}"])[0]
            assert row["w_report"] is None and row["q_report"] == own


def _json(value):
    import json

    return json.loads(value) if isinstance(value, str) else value


def _content(version_row):
    return _json(version_row["content"])
