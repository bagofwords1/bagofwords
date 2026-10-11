"""Every kind of conversation and dashboard data customers have.

Each kind is one conversation (sometimes one query inside a shared
conversation), keyed by a stable name. The key is what the golden files and
SUMMARY.md show instead of ids. Text is Hebrew on purpose: RTL and encoding
are part of what has to survive.

All rows here are written at MAIN_BASE_REVISION with SQLAlchemy Core; see
rows.py for why.
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import null as sa_null

from app.ee.encryption.types import encrypt_value
from tests.fixtures.dashboard_corpus.cast import Cast
from tests.fixtures.dashboard_corpus.rows import Rows, new_id

GOOD_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'חודש': ['ינואר', 'פברואר'], 'הכנסות': [120, 140]})\n"
)


def table_data(label: str, n: int = 2) -> dict:
    return {
        "rows": [{"חודש": f"{label} {i + 1}", "הכנסות": 100 + i} for i in range(n)],
        "columns": [{"field": "חודש"}, {"field": "הכנסות"}],
    }


def page_code(title: str) -> str:
    return (
        "function App() {\n"
        f"  return <div dir=\"rtl\"><h1>{title}</h1><BowChart index={{0}} /></div>;\n"
        "}\n"
    )


# A 1x1 PNG, enough for a thumbnail file the migration has to copy.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@dataclass
class Corpus:
    """Names <-> ids for everything the corpus wrote."""

    cast: Cast
    reports: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # id -> name
    tokens: dict[str, str] = field(default_factory=dict)  # conversation token -> report key
    ids: dict[str, str] = field(default_factory=dict)  # name -> id
    thumbnail_files: list[str] = field(default_factory=list)

    def name(self, name: str, row_id: str) -> str:
        self.names[str(row_id)] = name
        self.ids[name] = str(row_id)
        return str(row_id)


class Seeder:
    def __init__(self, rows: Rows, cast: Cast):
        self.rows = rows
        self.cast = cast
        self.corpus = Corpus(cast=cast)
        for key, person in cast.people.items():
            if person.user_id:
                self.corpus.name(f"user:{key}", person.user_id)
        for key, gid in cast.groups.items():
            self.corpus.name(f"group:{key}", gid)
        for key, pid in cast.projects.items():
            self.corpus.name(f"project:{key}", pid)
        for key, dsid in cast.data_sources.items():
            self.corpus.name(f"ds:{key}", dsid)
        self.corpus.name("org", cast.org_id)
        self.corpus.name("org:other", cast.other_org_id)
        # A fixed clock: creation order decides "latest", so it must not
        # depend on how fast the seed runs.
        self._t = datetime(2026, 3, 1, 9, 0, 0)
        rows.clock = self.tick
        # Every row the seed writes gets a stable name (table#n, in insertion
        # order) unless a more telling one is given with corpus.name().
        self._counts: dict[str, int] = {}

        def auto_name(table: str, row_id: str) -> None:
            self._counts[table] = self._counts.get(table, 0) + 1
            self.corpus.names.setdefault(row_id, f"{table}#{self._counts[table]}")

        rows.on_insert = auto_name

    @staticmethod
    def slug(*parts: str) -> str:
        """Slugs, tokens and paths derive from the corpus key: the golden files
        must not change between two builds of the same corpus."""
        return "-".join(re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in parts)

    def tick(self) -> datetime:
        self._t += timedelta(minutes=1)
        return self._t

    # ── conversations ────────────────────────────────────────────────

    def report(
        self,
        key: str,
        *,
        title: str,
        owner: str = "owner",
        data_sources: tuple[str, ...] = ("chinook",),
        conversation_token: bool = False,
        messages: bool = True,
        layout: bool = True,
        **cols: Any,
    ) -> str:
        org_id = self.cast.org_id
        t = self.tick()
        # set_visibility keeps the legacy status in step with the dashboard
        # surface: shared means 'published' (report_service.set_visibility).
        if cols.get("artifact_visibility", "none") != "none":
            cols.setdefault("status", "published")
        if cols.get("conversation_visibility", "none") != "none":
            conversation_token = True
        if conversation_token:
            cols.setdefault("conversation_share_token", self.slug(key, "token"))
            cols.setdefault("conversation_share_enabled", True)
        rid = self.rows.insert(
            "reports", title=title, slug=self.slug(key),
            user_id=self.cast.id(owner), organization_id=org_id,
            created_at=t, updated_at=t, last_activity_at=t, **cols,
        )
        self.corpus.reports[key] = rid
        self.corpus.name(key, rid)
        if conversation_token:
            self.corpus.tokens[cols["conversation_share_token"]] = key
        # Every report gets an empty active layout at creation
        # (report_service.create_report); viewer chat threads do not
        # (artifact_chat_service.resolve_chat_report).
        if layout:
            self.rows.insert("dashboard_layout_versions", report_id=rid, name="Default",
                             version=1, is_active=True, blocks=[], created_at=t, updated_at=t)
        for ds in data_sources:
            self.rows.insert("report_data_source_association", report_id=rid,
                             data_source_id=self.ds(ds))
        if messages:
            self.messages(key, owner)
        return rid

    def ds(self, key: str) -> str:
        return self.cast.data_sources[key]

    def messages(self, key: str, owner: str) -> None:
        rid = self.corpus.reports[key]
        t = self.tick()
        prompt = self.rows.insert(
            "completions", report_id=rid, role="user", message_type="table",
            prompt={"content": "תראה לי את ההכנסות לפי חודש"}, completion={},
            user_id=self.cast.id(owner), sigkill=t, created_at=t, updated_at=t,
        )
        answer = self.rows.insert(
            "completions", report_id=rid, role="system", parent_id=prompt,
            prompt={}, completion={"content": "הנה ההכנסות לפי חודש."},
            sigkill=t, turn_index=1, created_at=t + timedelta(seconds=5),
            updated_at=t + timedelta(seconds=5),
        )
        self.rows.insert("completion_blocks", completion_id=answer, source_type="final",
                         title="תשובה", status="completed", content="הנה ההכנסות לפי חודש.",
                         block_index=0, started_at=t, completed_at=t)
        self.corpus.name(f"{key}/msg/user", prompt)
        self.corpus.name(f"{key}/msg/answer", answer)

    def share(self, key: str, share_type: str, *, user: str = None, group: str = None) -> None:
        self.rows.insert(
            "report_shares", report_id=self.corpus.reports[key], share_type=share_type,
            user_id=self.cast.id(user) if user else None,
            group_id=self.cast.groups[group] if group else None,
        )

    # ── queries ──────────────────────────────────────────────────────

    def query(
        self,
        key: str,
        qkey: str,
        *,
        title: str,
        code: str = GOOD_CODE,
        data: Any = "default",
        step_status: str = "success",
        viz: bool = True,
        encrypted: bool = False,
        parameters: list | None = None,
        report_link: bool = True,
        widget_status: str = "draft",
        step_updated_at: datetime | None = None,
        owner: str = "owner",
        widget_report: str | None = None,
    ) -> dict:
        rid = self.corpus.reports[key]
        base = f"{key}/{qkey}"
        t = self.tick()
        # widget_report: the widget sits in another conversation than the
        # query (possible through POST /api/queries before 0.0.583).
        wid = self.rows.insert("widgets", title=title, slug=self.slug(base, "widget"),
                               report_id=self.corpus.reports[widget_report] if widget_report else rid,
                               status=widget_status, x=0, y=0, width=6, height=4,
                               created_at=t, updated_at=t)
        self.corpus.name(f"{base}/widget", wid)
        qid = self.rows.insert(
            "queries", title=title, widget_id=wid, report_id=rid if report_link else None,
            organization_id=self.cast.org_id, user_id=self.cast.id(owner),
            created_at=t, updated_at=t,
            # Left out when not given, so the model default ([]) applies.
            **({"parameters": parameters} if parameters is not None else {}),
        )
        self.corpus.name(base, qid)
        if data == "default":
            data = table_data(title)
        if encrypted and data is not None:
            data = encrypt_value(data)
        if data is None:
            # The daily purge writes SQL NULL (maintenance_service), not JSON null.
            data = sa_null()
        sid = self.rows.insert(
            "steps", title=f"{title} · ריצה", slug=self.slug(base, "step"),
            status=step_status, widget_id=wid, query_id=qid, code=code, data=data,
            prompt="", description="", type="table",
            created_at=t, updated_at=step_updated_at or t,
        )
        self.corpus.name(f"{base}/step", sid)
        self.rows.update("queries", qid, default_step_id=sid)
        out = {"widget": wid, "query": qid, "step": sid}
        if viz:
            vid = self.rows.insert("visualizations", title=title, status="success",
                                   report_id=rid, query_id=qid, view={"type": "bar_chart"},
                                   created_at=t, updated_at=t)
            self.corpus.name(f"{base}/viz", vid)
            out["viz"] = vid
        return out

    # ── dashboards ───────────────────────────────────────────────────

    def artifact(
        self,
        key: str,
        akey: str,
        *,
        title: str,
        versions: list[dict],
        mode: str = "page",
        owner: str = "owner",
        legacy_same_id: bool = False,
    ) -> str:
        """One dashboard with its versions, oldest first.

        A version dict takes: content (default: page code + viz of the given
        `viz` query keys), status, deleted, version (number), plus any
        artifact_versions column. `legacy_same_id` writes the artver01 shape:
        a parent whose id equals its single version's id.
        """
        rid = self.corpus.reports[key]
        base = f"{key}/{akey}"
        org_id, user_id = self.cast.org_id, self.cast.id(owner)
        t0 = self.tick()
        aid = new_id()
        if legacy_same_id:
            assert len(versions) == 1
        all_deleted = all(v.get("deleted") for v in versions)
        self.rows.insert(
            "artifacts", id=aid, report_id=rid, organization_id=org_id, created_by=user_id,
            mode=mode, title=title, created_at=t0, updated_at=t0,
            deleted_at=t0 if all_deleted else None,
        )
        self.corpus.name(base, aid)
        for n, spec in enumerate(versions, start=1):
            spec = dict(spec)
            t = self.tick()
            deleted = spec.pop("deleted", False)
            viz_keys = spec.pop("viz", [])
            content = spec.pop("content", None)
            if content is None:
                if mode == "doc":
                    content = {"markdown": f"# {title}", "visualization_ids": []}
                else:
                    content = {"code": page_code(title), "runtime_version": 1,
                               "visualization_ids": [self.corpus.ids[f"{key}/{v}/viz"] for v in viz_keys]}
            vid = aid if legacy_same_id else new_id()
            self.rows.insert(
                "artifact_versions", id=vid, artifact_id=aid, report_id=rid, user_id=user_id,
                organization_id=org_id, version=spec.pop("version", n), content=content,
                status=spec.pop("status", "completed"), created_at=t, updated_at=t,
                deleted_at=t if deleted else None, **spec,
            )
            self.corpus.name(f"{base}/v{n}", vid)
        return aid

    def simple_dash(self, key: str, *, title: str = "לוח מכירות", **report_cols) -> None:
        """A conversation with one query and a one-version page dashboard."""
        owner = report_cols.get("owner", "owner")
        self.report(key, title=title, **report_cols)
        self.query(key, "q_sales", title="הכנסות לפי חודש", owner=owner)
        self.artifact(key, "dash", title=title, owner=owner, versions=[{"viz": ["q_sales"]}])

    # ── other rows ───────────────────────────────────────────────────

    def file(self, name: str, *, filename: str, owner: str = "owner") -> str:
        fid = self.rows.insert("files", filename=filename, path=f"uploads/files/{self.slug(name)}.png",
                               content_type="image/png", user_id=self.cast.id(owner),
                               organization_id=self.cast.org_id)
        return self.corpus.name(name, fid)

    def connection_source(self, name: str, *, auth_policy: str, rls: bool = False) -> str:
        """A data source over an unreachable connection, in the state the
        viewer data policy reads. No route creates one without a licence and
        a live database, so it is written directly (same as the e2e/rbac
        viewer-run tests)."""
        org = self.cast.org_id
        cid = self.rows.insert("connections", name=f"{name} חיבור",
                               type="postgresql" if auth_policy == "system_only" else "powerbi",
                               config={"host": "localhost", "port": 5432, "database": "nope"},
                               organization_id=org, auth_policy=auth_policy,
                               **({"allowed_user_auth_modes": ["oauth"]}
                                  if auth_policy == "user_required" else {}))
        dsid = self.rows.insert("data_sources", name=name, organization_id=org, is_public=True)
        self.rows.insert("domain_connection", data_source_id=dsid, connection_id=cid)
        if rls:
            ctid = self.rows.insert("connection_tables", connection_id=cid, name="מכירות",
                                    kind="bow", columns=[{"name": "חודש"}], pks=[], fks=[],
                                    rls_enabled=True)
            self.rows.insert("datasource_tables", name="מכירות", datasource_id=dsid,
                             connection_table_id=ctid, is_active=True)
        self.cast.data_sources[name] = dsid
        self.corpus.name(f"ds:{name}", dsid)
        return dsid

    def viewer_result(self, key: str, qkey: str, viewer: str, *, fingerprint: str = "",
                      encrypted: bool = False, executed_as: str = "viewer") -> None:
        data = table_data(f"של {viewer}", 1)
        self.rows.insert(
            "step_user_results", step_id=self.corpus.ids[f"{key}/{qkey}/step"],
            user_id=self.cast.id(viewer), organization_id=self.cast.org_id,
            report_id=self.corpus.reports[key], status="success", executed_as=executed_as,
            params_fingerprint=fingerprint, data=encrypt_value(data) if encrypted else data,
            last_run_at=self.tick(),
        )


def seed_corpus(rows: Rows, cast: Cast, thumbnails_dir: str, written: list[str]) -> Corpus:
    """`written` collects the files the seed puts on disk, as they are
    written, so the caller can remove them even if seeding fails halfway."""
    s = Seeder(rows, cast)
    c = s.corpus
    c.thumbnail_files = written

    # ── א. structure ────────────────────────────────────────────────
    s.report("conv_no_dash", title="שיחה בלי דשבורד", artifact_visibility="internal")
    s.query("conv_no_dash", "q_sales", title="הכנסות לפי חודש")

    s.report("conv_one_dash", title="דשבורד אחד", artifact_visibility="internal")
    s.query("conv_one_dash", "q_sales", title="הכנסות לפי חודש")
    s.query("conv_one_dash", "q_customers", title="לקוחות לפי מדינה")
    s.artifact("conv_one_dash", "dash", title="לוח מכירות",
               versions=[{"viz": ["q_sales", "q_customers"]}])

    s.report("conv_three_dash", title="שלושה דשבורדים", artifact_visibility="internal")
    s.query("conv_three_dash", "q_sales", title="הכנסות לפי חודש")
    s.query("conv_three_dash", "q_stock", title="מלאי לפי מחסן")
    s.artifact("conv_three_dash", "dash_sales", title="לוח מכירות", versions=[{"viz": ["q_sales"]}])
    s.artifact("conv_three_dash", "deck", title="מצגת רבעונית", mode="slides",
               versions=[{"viz": ["q_sales"]}])
    s.artifact("conv_three_dash", "dash_stock", title="לוח מלאי", versions=[{"viz": ["q_stock"]}])

    s.report("dash_many_versions", title="הרבה גרסאות", artifact_visibility="internal")
    s.query("dash_many_versions", "q_sales", title="הכנסות לפי חודש")
    s.artifact("dash_many_versions", "dash", title="לוח מכירות", versions=[
        {"viz": ["q_sales"]}, {"viz": ["q_sales"]},
        {"viz": ["q_sales"], "status": "failed"}, {"viz": ["q_sales"]},
    ])

    for key, status, extra in (
        ("dash_newest_failed", "failed", {}),
        ("dash_newest_pending", "pending", {"content": {}}),
        # A stop lands before the code is written: the row stays empty
        # (create_artifact writes content only after rendering).
        ("dash_newest_stopped", "stopped", {"content": {}}),
    ):
        s.report(key, title=f"גרסה אחרונה {status}", artifact_visibility="internal")
        s.query(key, "q_sales", title="הכנסות לפי חודש")
        s.artifact(key, "dash", title="לוח מכירות",
                   versions=[{"viz": ["q_sales"]}, {"viz": ["q_sales"], "status": status, **extra}])

    s.report("artver01_chain", title="שרשרת מ-artver01", artifact_visibility="internal")
    s.query("artver01_chain", "q_sales", title="הכנסות לפי חודש")
    for n, number in enumerate((1, 2, 5), start=1):
        s.artifact("artver01_chain", f"legacy_{n}", title="לוח מכירות", legacy_same_id=True,
                   versions=[{"viz": ["q_sales"], "version": number}])

    s.report("dash_deleted_version", title="גרסה מחוקה", artifact_visibility="internal")
    s.query("dash_deleted_version", "q_sales", title="הכנסות לפי חודש")
    s.artifact("dash_deleted_version", "dash", title="לוח מכירות",
               versions=[{"viz": ["q_sales"]}, {"viz": ["q_sales"], "deleted": True}])

    s.report("dash_fully_deleted", title="דשבורד מחוק", artifact_visibility="internal")
    s.query("dash_fully_deleted", "q_sales", title="הכנסות לפי חודש")
    s.artifact("dash_fully_deleted", "dash_live", title="לוח חי", versions=[{"viz": ["q_sales"]}])
    s.artifact("dash_fully_deleted", "dash_gone", title="לוח שנמחק",
               versions=[{"viz": ["q_sales"], "deleted": True}, {"viz": ["q_sales"], "deleted": True}])

    # The published widget dashboards of the previous generation: migration
    # e7f8g9h0i1j2 turned status='published' into artifact_visibility='public'.
    s.report("legacy_widgets", title="דשבורד מהדור הקודם", status="published",
             artifact_visibility="public")
    w1 = s.query("legacy_widgets", "q_sales", title="הכנסות לפי חודש", widget_status="published")
    w2 = s.query("legacy_widgets", "q_stock", title="מלאי", widget_status="published")
    tw = s.rows.insert("text_widgets", report_id=c.reports["legacy_widgets"], content="<p>סיכום</p>",
                       x=0, y=0, width=12, height=1)
    s.rows.insert("dashboard_layout_versions", report_id=c.reports["legacy_widgets"], name="Published",
                  version=2, is_active=True, blocks=[
                      {"type": "text_widget", "text_widget_id": tw, "x": 0, "y": 0, "width": 12, "height": 1},
                      {"type": "widget", "widget_id": w1["widget"], "x": 0, "y": 1, "width": 6, "height": 4},
                      {"type": "widget", "widget_id": w2["widget"], "x": 6, "y": 1, "width": 6, "height": 4},
                  ])
    with s.rows.engine.begin() as conn:
        t = s.rows.table("dashboard_layout_versions")
        conn.execute(t.update().where(t.c.report_id == c.reports["legacy_widgets"],
                                      t.c.version == 1).values(is_active=False))

    s.report("shared_no_dash", title="משותף בלי דשבורד", artifact_visibility="internal")
    s.query("shared_no_dash", "q_sales", title="הכנסות לפי חודש", viz=False)

    s.report("slides_previews", title="מצגת עם תצוגות", artifact_visibility="internal")
    s.query("slides_previews", "q_sales", title="הכנסות לפי חודש")
    s.artifact("slides_previews", "deck", title="מצגת", mode="slides", versions=[{
        "content": {"code": "from pptx import Presentation", "runtime_version": 1,
                    "visualization_ids": [c.ids["slides_previews/q_sales/viz"]],
                    "preview_images": ["pptx_previews/deck/slide-1.png"]},
        "pptx_path": "pptx/deck.pptx",
    }])
    _thumbnail(s, "slides_previews/deck/v1", thumbnails_dir)

    s.report("doc_after_page", title="מסמך אחרי דשבורד", artifact_visibility="internal")
    s.query("doc_after_page", "q_sales", title="הכנסות לפי חודש")
    s.artifact("doc_after_page", "dash", title="לוח מכירות", versions=[{"viz": ["q_sales"]}])
    s.artifact("doc_after_page", "doc", title="סיכום כתוב", mode="doc", versions=[{}])

    s.report("doc_only", title="רק מסמך", artifact_visibility="internal")
    f_doc = s.file("doc_only/file", filename="תרשים.png")
    s.artifact("doc_only", "doc", title="מסמך", mode="doc", versions=[{
        "content": {"markdown": f"# מסמך\n\n{{{{file:{f_doc}}}}}", "visualization_ids": [],
                    "file_ids": [f_doc]},
    }])

    s.report("embedded_files", title="קבצים מוטמעים", artifact_visibility="internal")
    s.query("embedded_files", "q_sales", title="הכנסות לפי חודש")
    f_old = s.file("embedded_files/file_old", filename="לוגו-ישן.png")
    f_new = s.file("embedded_files/file_new", filename="לוגו.png")
    for fid in (f_old, f_new):
        s.rows.insert("report_file_association", report_id=c.reports["embedded_files"], file_id=fid)
    viz = [c.ids["embedded_files/q_sales/viz"]]
    s.artifact("embedded_files", "dash", title="לוח עם לוגו", versions=[
        {"deleted": True, "content": {"code": page_code("ישן"), "visualization_ids": viz,
                                      "files": [{"id": f_old, "content_type": "image/png", "filename": "לוגו-ישן.png"}]}},
        {"content": {"code": page_code("חדש"), "visualization_ids": viz,
                     "files": [{"id": f_new, "content_type": "image/png", "filename": "לוגו.png"}]}},
    ])

    s.report("live_resources", title="משאבים חיים", artifact_visibility="internal")
    s.query("live_resources", "q_sales", title="הכנסות לפי חודש")
    s.artifact("live_resources", "dash", title="טופס בקשות", versions=[{"content": {
        "code": page_code("טופס"), "runtime_version": 1, "sdk_version": 1,
        "visualization_ids": [c.ids["live_resources/q_sales/viz"]],
        "resource_requirements": {"collections": ["entries"]}}}])

    s.report("publication_pin", title="נעיצת פרסום", artifact_visibility="internal")
    s.query("publication_pin", "q_sales", title="הכנסות לפי חודש")
    pin_aid = s.artifact("publication_pin", "dash", title="לוח מכירות",
                         versions=[{"viz": ["q_sales"]}, {"viz": ["q_sales"]}])
    s.rows.insert("artifact_publications", artifact_id=pin_aid,
                  version_id=c.ids["publication_pin/dash/v1"], revision=1)

    # ── ב. conversation types and sharing ───────────────────────────
    s.simple_dash("vis_public", artifact_visibility="public")
    s.simple_dash("vis_internal", artifact_visibility="internal")
    s.simple_dash("vis_shared_user", artifact_visibility="shared")
    s.share("vis_shared_user", "artifact", user="direct_share")
    s.simple_dash("vis_shared_group", artifact_visibility="shared")
    s.share("vis_shared_group", "artifact", group="analysts")
    s.simple_dash("vis_none")

    s.simple_dash("conv_public", conversation_visibility="public")
    s.simple_dash("conv_shared_user", conversation_visibility="shared")
    s.share("conv_shared_user", "conversation", user="direct_share")
    s.simple_dash("surfaces_differ", artifact_visibility="internal", conversation_visibility="shared")
    s.share("surfaces_differ", "conversation", group="analysts")
    s.simple_dash("conv_enabled_mismatch", conversation_visibility="public",
                  conversation_share_enabled=False)

    s.simple_dash("archived_public", status="archived", artifact_visibility="public")
    s.simple_dash("archived_shared", status="archived", artifact_visibility="shared")
    s.share("archived_shared", "artifact", user="direct_share")
    s.simple_dash("report_deleted_at", artifact_visibility="public",
                  deleted_at=datetime(2026, 3, 2, 12, 0, 0))

    s.simple_dash("in_project_private", project_id=cast.projects["private"])
    s.simple_dash("in_project_org", project_id=cast.projects["org_wide"])
    # delete_project archives the reports it holds.
    s.simple_dash("in_project_deleted", project_id=cast.projects["deleted"], status="archived")

    s.simple_dash("bow_source", artifact_visibility="internal", bow_source_access={
        "version": 1, "source_id": "builtin:bow", "organization_id": cast.org_id,
        "report_ids": [], "scope_ids": None})

    s.simple_dash("chat_enabled", artifact_visibility="internal", artifact_chat_enabled=True,
                  artifact_chat_data_source_ids=[cast.data_sources["chinook"]])
    for viewer in ("chat_viewer", "member"):
        s.report(f"chat_enabled/thread_{viewer}", title="צ'אט צופה", owner=viewer, layout=False,
                 report_type="artifact_chat", forked_from_id=c.reports["chat_enabled"])

    s.simple_dash("test_report", report_type="test")
    s.simple_dash("training_mode", mode="training", artifact_visibility="internal")

    s.simple_dash("fork_done", owner="forker", forked_from_id=c.reports["vis_public"])
    # Hydration counts as live for 300s after the step's last update
    # (fork_service.FORK_HYDRATION_STALE_SECONDS). Far future / fixed past
    # keep both states true however long the build takes.
    for key, updated in (("fork_filling", datetime(2100, 1, 1)), ("fork_stale", datetime(2026, 3, 1))):
        s.report(key, title="fork שמתמלא", owner="forker", forked_from_id=c.reports["vis_public"])
        s.query(key, "q_sales", title="הכנסות לפי חודש", owner="forker", step_status="pending",
                code="", data={}, step_updated_at=updated)
        s.artifact(key, "dash", title="לוח מכירות", owner="forker", versions=[{"viz": ["q_sales"]}])
    s.simple_dash("fork_retired", owner="forker", status="archived",
                  forked_from_id=c.reports["vis_public"])

    s.simple_dash("auto_conv", artifact_visibility="internal", data_sources=())
    s.simple_dash("run_creator", artifact_visibility="internal", shared_run_identity="creator")

    s.simple_dash("run_viewer_results", artifact_visibility="internal")
    s.viewer_result("run_viewer_results", "q_sales", "has_results")
    s.viewer_result("run_viewer_results", "q_sales", "has_results", fingerprint="a" * 64)
    s.viewer_result("run_viewer_results", "q_sales", "direct_share")

    s.connection_source("מחסן אישי", auth_policy="user_required")
    s.simple_dash("user_required", artifact_visibility="internal", data_sources=("מחסן אישי",))
    s.viewer_result("user_required", "q_sales", "has_results")

    s.connection_source("מחסן RLS", auth_policy="system_only", rls=True)
    s.simple_dash("rls", artifact_visibility="internal", data_sources=("מחסן RLS",))

    s.report("identity_param", title="פרמטר זהות", artifact_visibility="internal")
    s.query("identity_param", "q_sales", title="הכנסות שלי", parameters=[{
        "name": "UserEmail", "type": "string", "label": "UserEmail", "default": None,
        "required": False, "source": "identity", "identity_binding": "viewer.email",
        "options": None, "options_source": None, "strict_options": False}])
    s.artifact("identity_param", "dash", title="הלוח שלי", versions=[{"viz": ["q_sales"]}])

    s.simple_dash("no_data_tab", artifact_visibility="internal", include_data_tab=False)

    s.connection_source("מחסן", auth_policy="system_only")
    s.simple_dash("multi_source", artifact_visibility="internal", data_sources=("chinook", "מחסן"))

    # ── ג. execution and data ───────────────────────────────────────
    s.report("orphans", title="שאריות מקוד ישן", artifact_visibility="internal")
    s.query("orphans", "q_sales", title="הכנסות לפי חודש")
    s.query("orphans", "q_no_report", title="שאילתה בלי שיחה", report_link=False, viz=False)
    t = s.tick()
    wid = s.rows.insert("widgets", title="ריצה בלי שאילתה", slug=s.slug("orphans/step_no_query", "widget"),
                        report_id=c.reports["orphans"], status="draft", x=0, y=0, width=6, height=4)
    c.name("orphans/step_no_query/widget", wid)
    c.name("orphans/step_no_query/step", s.rows.insert(
        "steps", title="ריצה בלי שאילתה", slug=s.slug("orphans/step_no_query", "step"), status="success",
        widget_id=wid, query_id=None, code=GOOD_CODE, data=table_data("ישן"),
        prompt="", description="", type="table", created_at=t, updated_at=t))
    s.artifact("orphans", "dash", title="לוח מכירות", versions=[{"viz": ["q_sales"]}])

    s.report("default_states", title="ריצות לא תקינות", artifact_visibility="internal")
    s.query("default_states", "q_ok", title="תקינה")
    s.query("default_states", "q_draft", title="טיוטה", step_status="draft", code="", data={})
    s.query("default_states", "q_error", title="שגיאה", step_status="error")
    s.query("default_states", "q_purged", title="נוקתה", data=None)
    s.artifact("default_states", "dash", title="לוח מצבים",
               versions=[{"viz": ["q_ok", "q_draft", "q_error", "q_purged"]}])

    s.report("viz_refs", title="הפניות לגרפים", artifact_visibility="internal")
    s.query("viz_refs", "q_live", title="גרף חי")
    s.query("viz_refs", "q_deleted", title="גרף שנמחק")
    with s.rows.engine.begin() as conn:
        for tbl, rid in (("visualizations", c.ids["viz_refs/q_deleted/viz"]),
                         ("queries", c.ids["viz_refs/q_deleted"])):
            tb = s.rows.table(tbl)
            conn.execute(tb.update().where(tb.c.id == rid).values(deleted_at=s.tick()))
    missing = new_id()
    c.name("viz_refs/missing_viz", missing)
    s.artifact("viz_refs", "dash", title="לוח עם הפניות", versions=[{"content": {
        "code": page_code("הפניות"), "runtime_version": 1,
        "visualization_ids": [c.ids["viz_refs/q_live/viz"], c.ids["viz_refs/q_deleted/viz"], missing]}}])

    s.report("options_sources", title="רשימות בחירה", artifact_visibility="internal")
    s.query("options_sources", "q_genres", title="ז'אנרים")
    s.query("options_sources", "q_by_id", title="שירים לפי ז'אנר", parameters=[{
        "name": "genre", "type": "string", "source": "input",
        "options_source": {"query_id": c.ids["options_sources/q_genres"], "value_column": "חודש"}}])
    s.query("options_sources", "q_by_title", title="אלבומים לפי ז'אנר", parameters=[{
        "name": "genre", "type": "string", "source": "input",
        "options_source": {"query_id": "ז'אנרים", "value_column": "חודש"}}])
    s.artifact("options_sources", "dash", title="לוח עם בחירה",
               versions=[{"viz": ["q_genres", "q_by_id", "q_by_title"]}])

    s.report("load_step", title="ריצה שטוענת ריצה", artifact_visibility="internal")
    s.query("load_step", "q_base", title="בסיס")
    s.query("load_step", "q_derived", title="נגזרת", code=(
        "def generate_df(ds_clients, excel_files, load_step):\n"
        "    return load_step('בסיס')\n"))
    s.artifact("load_step", "dash", title="לוח נגזר", versions=[{"viz": ["q_base", "q_derived"]}])

    c.name("entity_from_step", s.rows.insert(
        "entities", organization_id=cast.org_id, owner_id=cast.id("owner"), type="model",
        title="מודל הכנסות", slug=s.slug("entity_from_step"), code=GOOD_CODE,
        status="published", data=table_data("ישות"),
        source_step_id=c.ids["conv_one_dash/q_sales/step"]))

    s.report("thumbnail", title="תמונה ממוזערת", artifact_visibility="internal")
    s.query("thumbnail", "q_sales", title="הכנסות לפי חודש")
    s.artifact("thumbnail", "dash", title="לוח מכירות", versions=[{
        "viz": ["q_sales"], "screenshot_base64": base64.b64encode(TINY_PNG).decode()}])
    _thumbnail(s, "thumbnail/dash/v1", thumbnails_dir)

    s.simple_dash("cron", artifact_visibility="internal", cron_schedule="0 8 * * 1",
                  last_run_at=datetime(2026, 3, 1, 8, 0, 0), notification_subscribers=[
                      {"type": "user", "id": cast.id("member")},
                      {"type": "email", "address": "cfo@corpus.com"}])
    s.simple_dash("cron_legacy_empty", artifact_visibility="internal", cron_schedule="")
    s.simple_dash("cron_legacy_none", artifact_visibility="internal", cron_schedule="None")
    s.simple_dash("refresh_off", artifact_visibility="internal", refresh_on_view=False)

    s.simple_dash("sched_prompt", artifact_visibility="internal")
    c.name("sched_prompt/prompt", s.rows.insert(
        "scheduled_prompts", report_id=c.reports["sched_prompt"], user_id=cast.id("owner"),
        title="סיכום שבועי", prompt={"content": "תסכם את השבוע"}, cron_schedule="0 9 * * 0"))
    s.simple_dash("sched_prompt_spawn", artifact_visibility="internal")
    spid = c.name("sched_prompt_spawn/prompt", s.rows.insert(
        "scheduled_prompts", report_id=c.reports["sched_prompt_spawn"], user_id=cast.id("owner"),
        title="דוח יומי", prompt={"content": "דוח יומי"}, cron_schedule="0 7 * * *",
        spawn_new_report=True))
    s.simple_dash("sched_prompt_spawn/run_1", title="דוח יומי · ריצה", scheduled_prompt_id=spid)

    s.report("encrypted", title="נתונים מוצפנים", artifact_visibility="internal")
    s.query("encrypted", "q_sealed", title="מוצפנת", encrypted=True)
    s.query("encrypted", "q_plain", title="גלויה")
    s.artifact("encrypted", "dash", title="לוח מוצפן", versions=[{"viz": ["q_sealed", "q_plain"]}])
    s.viewer_result("encrypted", "q_sealed", "has_results", encrypted=True)

    # Legacy shapes the API allowed before 0.0.583 (a client could pass any
    # widget_id to POST /api/queries). Seeded last so the names of every row
    # above stay as they were. "widget first" and "query first" ownership
    # answer differently for the first; "latest step of the widget" and "of
    # the query" differ for the second.
    s.report("widget_elsewhere_home", title="שיחה פרטית של ה-widget")
    s.report("widget_elsewhere", title="widget משיחה אחרת", artifact_visibility="internal")
    s.query("widget_elsewhere", "q_sales", title="הכנסות לפי חודש", widget_report="widget_elsewhere_home")
    s.artifact("widget_elsewhere", "dash", title="לוח מכירות", versions=[{"viz": ["q_sales"]}])

    s.report("shared_widget", title="widget משותף", artifact_visibility="internal")
    first = s.query("shared_widget", "q_first", title="הכנסות לפי חודש")
    t = s.tick()
    c.name("shared_widget/q_second", s.rows.insert(
        "queries", title="הכנסות לפי רבעון", widget_id=first["widget"], report_id=c.reports["shared_widget"],
        organization_id=cast.org_id, user_id=cast.id("owner"), created_at=t, updated_at=t))
    s.artifact("shared_widget", "dash", title="לוח מכירות", versions=[{"viz": ["q_first"]}])

    # A widget with no conversation (widgets.report_id has been nullable in the
    # database since a2d893ef9090). Step-keyed checks found no conversation for
    # its step; query-keyed checks found its query's own report_id, if any.
    s.report("widget_without_report", title="widget בלי שיחה", artifact_visibility="internal")
    for qkey, report_id in (("q_own_report", c.reports["widget_without_report"]), ("q_no_report", None)):
        base = f"widget_without_report/{qkey}"
        t = s.tick()
        wid = c.name(f"{base}/widget", s.rows.insert(
            "widgets", title=qkey, slug=s.slug(base, "widget"), report_id=None, status="draft",
            x=0, y=0, width=6, height=4, created_at=t, updated_at=t))
        qid = c.name(base, s.rows.insert(
            "queries", title=qkey, widget_id=wid, report_id=report_id, organization_id=cast.org_id,
            user_id=cast.id("owner"), created_at=t, updated_at=t))
        sid = c.name(f"{base}/step", s.rows.insert(
            "steps", title=qkey, slug=s.slug(base, "step"), status="success", widget_id=wid, query_id=qid,
            code=GOOD_CODE, data=table_data(qkey), prompt="", description="", type="table",
            created_at=t, updated_at=t))
        s.rows.update("queries", qid, default_step_id=sid)

    return c


def _thumbnail(s: Seeder, version_name: str, thumbnails_dir: str) -> None:
    vid = s.corpus.ids[version_name]
    s.rows.update("artifact_versions", vid, thumbnail_path=f"thumbnails/{vid}.png")
    os.makedirs(thumbnails_dir, exist_ok=True)
    path = os.path.join(thumbnails_dir, f"{vid}.png")
    with open(path, "wb") as fh:
        fh.write(TINY_PNG)
    s.corpus.thumbnail_files.append(path)
