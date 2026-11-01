"""Dashboards as first-class entities: the existing dashboards

Revision ID: dashfc08
Revises: dashfc07
Create Date: 2026-10-20

Phase 9 of docs/design/dashboards-first-class.md. Every dashboard a
customer has becomes what its conversation made it: what someone could open
before the upgrade, they open after it, through the same link, with the
same rows and the same access.

For every conversation with dashboards:

- each dashboard gets the conversation's owner, project and viewer settings
  (run identity, Data tab, chat, refresh on view, last run);
- the dashboard the conversation's /r link shows (its newest dashboard or
  deck, else its newest doc) is that link's dashboard (legacy_report_id),
  and gets the conversation's schedule and subscribers;
- a conversation others could open (artifact_visibility public, internal or
  shared -- archived or not) publishes it: a ready release copied from its
  newest completed version and the rows the link shows, nothing run, its
  readers' own results copied along; with 'shared', the grants of people and
  groups still in the organization;
- a deleted (archived) conversation nobody else could open takes its
  dashboards with it, as deleting one does since phase 7;
- readers' chats point at the dashboard.

Conversation schedules also live in APScheduler's job table (report_{id});
those jobs are removed (kept in dashfc08_report_jobs for the downgrade), so
a schedule never runs twice: the dashboard's job is registered at startup.

Nothing that exists changes: the migration fills columns that were empty and
adds rows. Everything it does is recorded in dashfc08_undo, which makes a
second run add nothing and lets the downgrade restore the database exactly.
Encrypted columns are copied in SQL, as they are (inside alembic the licence
is not loaded, so the ORM would write them back in plaintext).

Written against the tables as they are when it runs (reflected), with no
application code: customers skip releases, and later code must not change
what this migration does.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import pickle
import re
import shutil
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc08"
down_revision: str | None = "dashfc07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

UNDO = "dashfc08_undo"
JOBS_BACKUP = "dashfc08_report_jobs"
SHARED = ("public", "internal", "shared")
NO_CRON = (None, "", "None")
UPLOADS = Path(__file__).resolve().parents[2] / "uploads"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ── tables ───────────────────────────────────────────────────────────────

class Tables:
    def __init__(self, conn):
        self.conn = conn
        self.names = set(sa.inspect(conn).get_table_names())
        self._cache = {}

    def __getitem__(self, name) -> sa.Table:
        if name not in self._cache:
            self._cache[name] = sa.Table(name, sa.MetaData(), autoload_with=self.conn)
        return self._cache[name]

    def has(self, name) -> bool:
        return name in self.names


def _undo_table(conn) -> sa.Table:
    meta = sa.MetaData()
    table = sa.Table(
        UNDO, meta,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("row_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),  # insert | update
        sa.Column("prior", sa.Text, nullable=True),          # update: the columns as they were (JSON)
        # _done asks it, row by row, what was already done: indexed, or a
        # large installation's upgrade grows with the square of its rows.
        sa.Index("ix_dashfc08_undo_lookup", "table_name", "row_id", "action"),
    )
    meta.create_all(conn, checkfirst=True)
    return table


class _Opaque:
    """Whatever a job's state names (the service, its trigger's timezone):
    not imported, not needed."""

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        pass


class _NoImportUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        return _Opaque


def _scheduled_by(job_state, report_id) -> str | None:
    """The user a conversation's APScheduler job runs as, read without
    importing app code (decision 12). set_report_schedule schedules a bound
    method: the state's args are the service instance, then the
    conversation, the user and the organization. None when unreadable."""
    try:
        state = _NoImportUnpickler(io.BytesIO(bytes(job_state))).load()
        args = [a for a in (state.get("args") or ()) if isinstance(a, str)]
    except Exception:  # an APScheduler state this code cannot read: counted, not judged
        return None
    if report_id not in args or args.index(report_id) + 1 >= len(args):
        return None
    return args[args.index(report_id) + 1]


def _jobs_backup_table(conn) -> sa.Table:
    meta = sa.MetaData()
    table = sa.Table(
        JOBS_BACKUP, meta,
        sa.Column("id", sa.Unicode(191), primary_key=True),
        sa.Column("next_run_time", sa.Float(25)),
        sa.Column("job_state", sa.LargeBinary, nullable=False),
    )
    meta.create_all(conn, checkfirst=True)
    return table


def _jsonable(value):
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    return value


def _from_jsonable(value):
    if isinstance(value, dict) and set(value) == {"__dt__"}:
        return datetime.fromisoformat(value["__dt__"])
    return value


# ── load_step resolution (frozen from app/services/dashboard_copy.py) ─────

class StepIndex:
    """Among a conversation's successful default steps, by id, then slug,
    then title (case-insensitive; on a title clash the newest wins)."""

    def __init__(self, steps):
        self._id, self._slug, self._title = {}, {}, {}
        self._title_count = Counter()
        for s in sorted(steps, key=lambda s: s["created_at"] or datetime.min):
            self._id[str(s["id"])] = s
            if s["slug"]:
                self._slug[s["slug"].lower()] = s
            if s["title"]:
                self._title[s["title"].lower()] = s
                self._title_count[s["title"].lower()] += 1

    def find(self, ref):
        ref = str(ref)
        if ref in self._id:
            return self._id[ref], "id"
        if ref.lower() in self._slug:
            return self._slug[ref.lower()], "slug"
        if ref.lower() in self._title:
            return self._title[ref.lower()], "title"
        return None, None

    def title_is_shared(self, ref) -> bool:
        return self._title_count[str(ref).lower()] > 1


def _load_step_literals(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    lines = code.splitlines(keepends=True)
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line)

    def offset(lineno, col):
        return starts[lineno - 1] + len(lines[lineno - 1].encode("utf-8")[:col].decode("utf-8", "ignore"))

    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_step"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            c = node.args[0]
            out.append((offset(c.lineno, c.col_offset), offset(c.end_lineno, c.end_col_offset), c.value))
    return sorted(out)


def _rewrite_load_step(code, replace):
    for start, end, value in reversed(_load_step_literals(code)):
        new = replace(value)
        if new is None or new == value:
            continue
        literal = code[start:end]
        i = next(n for n, ch in enumerate(literal) if ch in "'\"")
        quote = literal[i:i + 3] if literal[i:i + 3] in ("'''", '"""') else literal[i]
        code = f"{code[:start]}{literal[:i]}{quote}{new}{quote}{code[end:]}"
    return code


def _remap_ids(value, id_map):
    if value is None:
        return None
    raw = json.dumps(value, ensure_ascii=False)
    return json.loads(_UUID.sub(lambda m: id_map.get(m.group(0).lower(), m.group(0)), raw))


def _sql(table: sa.Table, values: dict) -> dict:
    """None as SQL NULL on JSON columns (the reflected JSON type would write
    the JSON literal 'null', which the rows never held)."""
    return {k: sa.null() if v is None and isinstance(table.c[k].type, sa.JSON) else v for k, v in values.items()}


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# ── the run ──────────────────────────────────────────────────────────────

class Migration:
    def __init__(self, conn):
        self.conn = conn
        self.t = Tables(conn)
        self.undo = _undo_table(conn)
        self.now = datetime.utcnow()
        self.report = Counter()
        self.scheduled_by_other: list[str] = []
        self.files_written: list[Path] = []

    # bookkeeping
    def _done(self, table, row_id, action) -> bool:
        return self.conn.execute(sa.select(self.undo.c.id).where(
            self.undo.c.table_name == table, self.undo.c.row_id == str(row_id),
            self.undo.c.action == action).limit(1)).first() is not None

    def _record(self, table, row_id, action, prior=None):
        self.conn.execute(self.undo.insert().values(
            table_name=table, row_id=str(row_id), action=action,
            prior=json.dumps({k: _jsonable(v) for k, v in prior.items()}, default=str) if prior is not None else None))

    def _update(self, table, row_id, values):
        """Set `values` on one row, recording the columns as they were."""
        tbl = self.t[table]
        cols = [tbl.c[k] for k in values]
        row = self.conn.execute(sa.select(*cols).where(tbl.c.id == row_id)).mappings().first()
        if row is None:
            return
        self._record(table, row_id, "update", dict(row))
        self.conn.execute(tbl.update().where(tbl.c.id == row_id).values(**_sql(tbl, values)))

    def _insert(self, table, values):
        self.conn.execute(self.t[table].insert().values(**_sql(self.t[table], values)))
        self._record(table, values["id"], "insert")

    def _copy_row(self, table, source_id, overrides):
        """Copy one row in SQL (every column as it is, encrypted ones too),
        with `overrides` in place of some columns."""
        tbl = self.t[table]
        def value(c):
            v = overrides[c.name]
            return (sa.null() if v is None else sa.literal(v, type_=c.type)).label(c.name)
        exprs = [value(c) if c.name in overrides else c for c in tbl.columns]
        self.conn.execute(tbl.insert().from_select([c.name for c in tbl.columns],
                                                   sa.select(*exprs).where(tbl.c.id == source_id)))
        self._record(table, overrides["id"], "insert")

    # the conversations
    def run(self):
        reports = self.t["reports"]
        artifacts = self.t["artifacts"]
        rows = self.conn.execute(
            sa.select(reports).where(sa.exists().where(artifacts.c.report_id == reports.c.id))
            .order_by(reports.c.created_at)
        ).mappings().all()
        shown_by_report = {}
        for report in rows:
            shown = self._conversation(report)
            if shown is not None:
                shown_by_report[str(report["id"])] = shown
        self._chats(shown_by_report)
        self._jobs()
        return self._summary()

    def _versions(self, report_id):
        versions, artifacts = self.t["artifact_versions"], self.t["artifacts"]
        return self.conn.execute(
            sa.select(versions.c.id, versions.c.artifact_id, versions.c.status, versions.c.created_at,
                      artifacts.c.mode, artifacts.c.deleted_at.label("artifact_deleted_at"))
            .join(artifacts, artifacts.c.id == versions.c.artifact_id)
            .where(versions.c.report_id == report_id, versions.c.deleted_at.is_(None))
            .order_by(versions.c.created_at.desc())
        ).mappings().all()

    def _shown(self, versions):
        """(artifact id, version id) the conversation's link shows: the
        artifact of its newest dashboard or deck version, else of its newest
        doc; published from that artifact's newest completed version (else
        the conversation's newest completed one of the same kind)."""
        live = [v for v in versions if v["artifact_deleted_at"] is None]
        pages = [v for v in live if v["mode"] != "doc"]
        pool = pages or live
        if not pool:
            return None, None
        artifact_id = pool[0]["artifact_id"]
        completed = [v for v in pool if v["artifact_id"] == artifact_id and v["status"] == "completed"]
        if not completed:
            completed = [v for v in pool if v["status"] == "completed"]
            if not completed:
                return artifact_id, None
            artifact_id = completed[0]["artifact_id"]
        return artifact_id, completed[0]["id"]

    def _conversation(self, report):
        rid = str(report["id"])
        artifacts = self.t["artifacts"]
        dashboards = self.conn.execute(
            sa.select(artifacts).where(artifacts.c.report_id == rid)).mappings().all()
        # A dashboard the new code made (it has an owner this migration did
        # not give it) is left exactly as it is: a database that ran a build
        # between phases 8 and 9 has some. The link chooses among the old ones.
        made_since = {str(d["id"]) for d in dashboards
                      if d["owner_id"] is not None and not self._done("artifacts", d["id"], "update")}
        dashboards = [d for d in dashboards if str(d["id"]) not in made_since]
        if all(self._done("artifacts", d["id"], "update") for d in dashboards):
            return self._recorded_shown(rid)
        shared = report["artifact_visibility"] in SHARED
        gone = report["status"] == "archived" or report["deleted_at"] is not None
        versions = [v for v in self._versions(rid) if str(v["artifact_id"]) not in made_since]
        shown_id, version_id = self._shown(versions)
        # A deleted conversation keeps only the dashboard its link shows, and
        # that only when it is published (shared, with a finished version):
        # its drafts go, as deleting a conversation does since phase 7.
        kept_shown = str(shown_id) if (shown_id and shared and version_id) else None

        def survives(d):
            return d["deleted_at"] is None and (not gone or str(d["id"]) == kept_shown)

        # The schedule goes to the dashboard the link shows -- unless that is a
        # deck, which never refreshes: then to the newest page dashboard,
        # which main's schedule refreshed with it (decided 2026-10-21).
        alive = {str(d["id"]) for d in dashboards if survives(d)}
        schedule_to = None
        if report["cron_schedule"] not in NO_CRON and alive:
            modes = {str(d["id"]): d["mode"] for d in dashboards}
            if shown_id and str(shown_id) in alive and modes.get(str(shown_id)) != "slides":
                schedule_to = str(shown_id)
            else:
                schedule_to = next((str(v["artifact_id"]) for v in versions
                                    if v["mode"] == "page" and str(v["artifact_id"]) in alive), None)

        settings = {
            "owner_id": report["user_id"], "project_id": report["project_id"],
            "shared_run_identity": report["shared_run_identity"] or "viewer",
            "include_data_tab": report["include_data_tab"] if report["include_data_tab"] is not None else True,
            "chat_enabled": bool(report["artifact_chat_enabled"]),
            "chat_data_source_ids": report["artifact_chat_data_source_ids"],
            "chat_model_id": report["artifact_chat_model_id"],
            "refresh_on_view": report["refresh_on_view"] if report["refresh_on_view"] is not None else True,
            "last_run_at": report["last_run_at"],
        }
        for d in dashboards:
            if self._done("artifacts", d["id"], "update"):
                continue
            values = dict(settings)
            if d["deleted_at"] is None and not survives(d):
                values["deleted_at"] = self.now  # decision 6
                self.report["deleted"] += 1
            elif str(d["id"]) == str(shown_id) and d["deleted_at"] is None:
                values["legacy_report_id"] = rid
                if shared and version_id:
                    # Moved with its grants (_grants, at publish); with nothing
                    # to publish it stays private -- a visibility without its
                    # grants would open it to the wrong people later.
                    values["visibility"] = report["artifact_visibility"]
            elif d["deleted_at"] is None:  # one already deleted is no draft
                self.report["drafts"] += 1
            if str(d["id"]) == schedule_to:
                values["cron_schedule"] = report["cron_schedule"].strip()
                values["notification_subscribers"] = report["notification_subscribers"]
                self.report["schedules_moved"] += 1
            self._update("artifacts", d["id"], values)
        if (report["cron_schedule"] not in NO_CRON and alive and schedule_to is None
                and not self._done("reports", rid, "update")):
            # Only decks to refresh: the schedule goes, as decision 7's do.
            self._update("reports", rid, {"cron_schedule": None})
            self.report["schedules_removed"] += 1

        live = bool(shown_id) and self._is_live(shown_id)
        if shared and live and version_id:
            self._publish(report, shown_id, version_id)
            if report["artifact_visibility"] == "shared":
                self._grants(rid, shown_id, report["organization_id"])
        elif live:
            self.report["drafts"] += 1
        return shown_id if live else None

    def _recorded_shown(self, rid):
        artifacts = self.t["artifacts"]
        row = self.conn.execute(sa.select(artifacts.c.id).where(artifacts.c.legacy_report_id == rid)).first()
        return str(row[0]) if row else None

    def _is_live(self, artifact_id):
        artifacts = self.t["artifacts"]
        return self.conn.execute(sa.select(artifacts.c.deleted_at).where(
            artifacts.c.id == artifact_id)).scalar_one_or_none() is None

    # sharing
    def _grants(self, rid, artifact_id, organization_id):
        shares = self.t["report_shares"]
        rows = self.conn.execute(sa.select(shares).where(
            shares.c.report_id == rid, shares.c.share_type == "artifact", shares.c.deleted_at.is_(None))
        ).mappings().all()
        for share in rows:
            if share["user_id"] and not self._member(share["user_id"], organization_id):
                self.report["shares_dropped"] += 1
                continue
            if share["group_id"] and not self._live_group(share["group_id"], organization_id):
                self.report["shares_dropped"] += 1
                continue
            if not share["user_id"] and not share["group_id"]:
                continue
            self._insert("artifact_shares", {
                "id": str(uuid.uuid4()), "artifact_id": artifact_id, "user_id": share["user_id"],
                "group_id": share["group_id"], "created_at": self.now, "updated_at": self.now})
            self.report["shares_copied"] += 1

    def _member(self, user_id, organization_id):
        m = self.t["memberships"]
        return self.conn.execute(sa.select(m.c.id).where(
            m.c.user_id == user_id, m.c.organization_id == organization_id, m.c.deleted_at.is_(None)
        ).limit(1)).first() is not None

    def _live_group(self, group_id, organization_id):
        g = self.t["groups"]
        return self.conn.execute(sa.select(g.c.id).where(
            g.c.id == group_id, g.c.organization_id == organization_id, g.c.deleted_at.is_(None)
        ).limit(1)).first() is not None

    # publishing
    def _deps(self, report_id, content):
        """The queries the link shows and the ones it needs without showing
        them (frozen from dashboard_copy.collect_dashboard_deps)."""
        queries, steps, viz = self.t["queries"], self.t["steps"], self.t["visualizations"]
        listed = [str(v) for v in ((content or {}).get("visualization_ids") or [])]
        rows = self.conn.execute(
            sa.select(queries.c.id, queries.c.title, queries.c.parameters, queries.c.deleted_at,
                      queries.c.created_at, queries.c.default_step_id,
                      steps.c.id.label("step_id"), steps.c.slug.label("step_slug"),
                      steps.c.title.label("step_title"), steps.c.code.label("step_code"),
                      steps.c.created_at.label("step_created_at"))
            .join(steps, steps.c.id == queries.c.default_step_id)
            .where(queries.c.report_id == report_id, steps.c.status == "success")
            .order_by(queries.c.created_at)
        ).mappings().all()
        by_id = {str(q["id"]): q for q in rows}
        served = [q for q in rows if q["deleted_at"] is None]
        charts_of = {}
        for v in self.conn.execute(sa.select(viz.c.id, viz.c.query_id).where(
                viz.c.query_id.in_(list(by_id) or [""]))).mappings():
            charts_of.setdefault(str(v["query_id"]), []).append(str(v["id"]))
        if listed:
            owners = {str(r[0]) for r in self.conn.execute(
                sa.select(viz.c.query_id).where(viz.c.id.in_(listed))).all()}
            shown = [q for q in served if str(q["id"]) in owners]
        else:
            shown = list(served)
        shown_ids = [str(q["id"]) for q in shown]

        def titles(qs):
            out = {}
            for q in qs:
                if q["title"]:
                    out[q["title"].lower()] = str(q["id"])
            return out
        shown_titles = titles(shown)
        other_titles = titles(q for q in served if str(q["id"]) not in set(shown_ids))
        served_ids = {str(q["id"]) for q in served}
        index = StepIndex({"id": q["step_id"], "slug": q["step_slug"], "title": q["step_title"],
                           "created_at": q["step_created_at"]} for q in rows)
        step_owner = {str(q["step_id"]): str(q["id"]) for q in rows}
        option_targets = {}

        def reached(q):
            for spec in _json(q["parameters"]) or []:
                src = spec.get("options_source") if isinstance(spec, dict) else None
                ref = str(src.get("query_id")) if isinstance(src, dict) and src.get("query_id") else None
                if ref:
                    target = ref if ref in served_ids else (
                        shown_titles.get(ref.lower()) or other_titles.get(ref.lower()))
                    if target:
                        option_targets[(str(q["id"]), ref)] = target
                        yield target
            for _, _, ref in _load_step_literals(q["step_code"] or ""):
                s, _ = index.find(ref)
                if s is not None:
                    yield step_owner[str(s["id"])]

        seen, hidden, todo = set(shown_ids), [], list(shown)
        while todo:
            for target in reached(todo.pop()):
                if target not in seen:
                    seen.add(target)
                    hidden.append(target)
                    todo.append(by_id[target])
        return [by_id[i] for i in [*shown_ids, *hidden]], option_targets, index

    def _publish(self, report, artifact_id, version_id):
        if self._done("artifact_releases", artifact_id, "published"):
            return
        versions = self.t["artifact_versions"]
        version = self.conn.execute(sa.select(versions).where(versions.c.id == version_id)).mappings().one()
        content = _json(version["content"]) or {}
        queries, option_targets, index = self._deps(str(report["id"]), content)
        release_id = str(uuid.uuid4())

        query_map, step_map, viz_map, slug_map = {}, {}, {}, {}
        for q in queries:
            query_map[str(q["id"])] = str(uuid.uuid4())
            new_step = str(uuid.uuid4())
            step_map[str(q["step_id"])] = new_step
            slug_map[new_step] = f"release-{uuid.uuid4().hex[:12]}"

        def new_ref(ref):
            s, how = index.find(ref)
            new_id = step_map.get(str(s["id"])) if s is not None else None
            if new_id is None:
                return None
            if how == "id":
                return new_id
            if how == "slug" or index.title_is_shared(ref):
                return slug_map[new_id]
            return None

        self._insert("artifact_releases", {
            "id": release_id, "artifact_id": artifact_id, "organization_id": report["organization_id"],
            "source_version_id": version_id, "content": {}, "status": "ready",
            "published_by": report["user_id"], "published_at": self.now,
            "bow_source_access": report["bow_source_access"], "theme_name": report["theme_name"],
            "theme_overrides": report["theme_overrides"], "created_at": self.now, "updated_at": self.now})

        qt, vt = self.t["queries"], self.t["visualizations"]
        for q in queries:
            old_qid, new_qid = str(q["id"]), query_map[str(q["id"])]
            params = _json(q["parameters"])
            if isinstance(params, list):
                params = json.loads(json.dumps(params))
                for spec in params:
                    src = spec.get("options_source") if isinstance(spec, dict) else None
                    if isinstance(src, dict) and src.get("query_id"):
                        old = option_targets.get((old_qid, str(src["query_id"])))
                        if old in query_map:
                            src["query_id"] = query_map[old]
                        else:
                            spec["options_source"] = None
            self._copy_row("queries", old_qid, {
                "id": new_qid, "report_id": None, "artifact_release_id": release_id, "widget_id": None,
                "default_step_id": None, "parameters": params, "created_at": self.now, "updated_at": self.now})
            old_sid, new_sid = str(q["step_id"]), step_map[str(q["step_id"])]
            overrides = {"id": new_sid, "slug": slug_map[new_sid], "query_id": new_qid, "widget_id": None,
                         "created_at": self.now, "updated_at": self.now}
            code = q["step_code"] or ""
            rewritten = _rewrite_load_step(code, new_ref) if code else code
            if rewritten != code:
                overrides["code"] = rewritten
            self._copy_row("steps", old_sid, overrides)
            self.conn.execute(qt.update().where(qt.c.id == new_qid).values(default_step_id=new_sid))
            for vrow in self.conn.execute(sa.select(vt.c.id).where(vt.c.query_id == old_qid)).all():
                new_vid = str(uuid.uuid4())
                viz_map[str(vrow[0])] = new_vid
                self._copy_row("visualizations", str(vrow[0]), {
                    "id": new_vid, "report_id": None, "artifact_release_id": release_id, "query_id": new_qid,
                    "created_at": self.now, "updated_at": self.now})
            self._reader_results(old_sid, new_sid)

        id_map = {k.lower(): v for m in (query_map, step_map, viz_map) for k, v in m.items()}
        new_content = _remap_ids(content, id_map)
        previews = {}
        if new_content.get("preview_images"):
            new_content["preview_images"], previews = self._copy_previews(new_content["preview_images"], release_id)
        thumbnail = None
        if report["bow_source_access"] or self._one_identitys_rows(report):
            pass  # as Publish: no picture of rows a reader may not get (served without a user)
        elif version["thumbnail_path"] in previews:
            thumbnail = previews[version["thumbnail_path"]]
        elif version["thumbnail_path"]:
            thumbnail = self._copy_file(UPLOADS / "thumbnails" / f"{version_id}.png",
                                        UPLOADS / "thumbnails" / f"release-{release_id}.png",
                                        f"thumbnails/release-{release_id}.png")
        pptx = self._copy_deck(version["pptx_path"], f"release-{release_id}")
        rel = self.t["artifact_releases"]
        self.conn.execute(rel.update().where(rel.c.id == release_id).values(
            **_sql(rel, {"content": new_content, "thumbnail_path": thumbnail, "pptx_path": pptx})))

        for name, column, target in (("report_data_source_association", "data_source_id",
                                      "artifact_release_data_source_association"),
                                     ("report_file_association", "file_id", "artifact_release_file_association")):
            if not self.t.has(name) or not self.t.has(target):
                continue
            src = self.t[name]
            # Legacy data-source links have no uniqueness constraint; a
            # release records membership once, regardless of repeated links.
            links = sa.select(src.c[column]).where(src.c.report_id == report["id"]).distinct()
            for (value,) in self.conn.execute(links).all():
                self.conn.execute(self.t[target].insert().values(artifact_release_id=release_id, **{column: value}))

        self._update("artifacts", artifact_id, {"published_release_id": release_id})
        self._record("artifact_releases", artifact_id, "published")
        self.report["published"] += 1

    def _reader_results(self, old_step, new_step):
        """Decision 9: readers keep the result they had (copied as it is)."""
        results = self.t["step_user_results"]
        for (rid,) in self.conn.execute(sa.select(results.c.id).where(results.c.step_id == old_step)).all():
            self._copy_row("step_user_results", rid, {
                "id": str(uuid.uuid4()), "step_id": new_step, "report_id": None,
                "created_at": self.now, "updated_at": self.now})
            self.report["reader_results_copied"] += 1

    def _one_identitys_rows(self, report) -> bool:
        """Frozen, conservative SQL of Publish's thumbnail rule (phase 5;
        app.services.viewer_data_policy / identity_taint): the conversation's
        rows are one identity's when a source has row-level security, when a
        personal connection is read as the viewer, or when a query takes an
        identity parameter; a conversation with no source of its own is judged
        by the organization's. (What the code loads from elsewhere is not read
        here: as main, which drew such pictures too.)"""
        t, rid = self.t, str(report["id"])
        if not t.has("report_data_source_association"):
            return False
        assoc = t["report_data_source_association"]
        ds_ids = [str(r[0]) for r in self.conn.execute(
            sa.select(assoc.c.data_source_id).where(assoc.c.report_id == rid)).all()]
        if not ds_ids and t.has("data_sources"):
            # No source of its own: it ran on the organization's -- all of them
            # count, as Publish's rule reads it (_org_fallback_ds_ids, no code).
            sources = t["data_sources"]
            ds_ids = [str(r[0]) for r in self.conn.execute(sa.select(sources.c.id).where(
                sources.c.organization_id == str(report["organization_id"]), sources.c.deleted_at.is_(None))).all()]
        if ds_ids and t.has("datasource_tables") and t.has("connection_tables"):
            dst, ct = t["datasource_tables"], t["connection_tables"]
            if self.conn.execute(sa.select(ct.c.id).select_from(dst.join(ct, ct.c.id == dst.c.connection_table_id))
                                 .where(dst.c.datasource_id.in_(ds_ids), dst.c.is_active.is_(True),
                                        dst.c.deleted_at.is_(None), ct.c.kind == "bow",
                                        ct.c.rls_enabled.is_(True), ct.c.deleted_at.is_(None))
                                 .limit(1)).first():
                return True
        if (ds_ids and (report["shared_run_identity"] or "viewer") == "viewer"
                and t.has("domain_connection") and t.has("connections")):
            dc, conns = t["domain_connection"], t["connections"]
            if self.conn.execute(sa.select(conns.c.id).select_from(dc.join(conns, conns.c.id == dc.c.connection_id))
                                 .where(dc.c.data_source_id.in_(ds_ids), conns.c.deleted_at.is_(None),
                                        conns.c.auth_policy != "system_only").limit(1)).first():
                return True
        queries = t["queries"]
        for (params,) in self.conn.execute(sa.select(queries.c.parameters).where(
                queries.c.report_id == rid, queries.c.parameters.isnot(None))).all():
            params = json.loads(params) if isinstance(params, str) else params
            if any(isinstance(p, dict) and p.get("source") in ("identity", "input_identity_default")
                   for p in (params or [])):
                return True
        return False

    # files (copied next to the originals inside uploads/, as Publish does)
    def _copy_file(self, source: Path, target: Path, relative: str):
        try:
            if not source.is_file():
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self.files_written.append(target)
            return relative
        except OSError as e:
            log.warning("dashfc08: file not copied %s: %s", source, e)
            return None

    def _copy_deck(self, path, stem):
        if not path or not str(path).lower().endswith(".pptx"):
            return None
        source = Path(path) if Path(path).is_absolute() else UPLOADS / str(path)
        try:
            source = source.resolve()
            source.relative_to(UPLOADS.resolve())
        except (ValueError, OSError):
            return None
        target = source.with_name(f"{stem}.pptx")
        return str(target) if self._copy_file(source, target, str(target)) else None

    def _copy_previews(self, paths, release_id):
        base = UPLOADS / "pptx_previews"
        out, copied = [], {}
        for p in paths:
            source = base / str(p).removeprefix("pptx_previews/")
            try:
                source.resolve().relative_to(base.resolve())
            except (ValueError, OSError):
                out.append(p)
                continue
            if source.suffix.lower() != ".png":
                out.append(p)
                continue
            new = f"pptx_previews/release-{release_id}/{source.name}"
            if self._copy_file(source, base / f"release-{release_id}" / source.name, new):
                copied[p] = new
                out.append(new)
            else:
                out.append(p)
        return out, copied

    # readers' chats
    def _chats(self, shown_by_report):
        reports = self.t["reports"]
        rows = self.conn.execute(sa.select(reports.c.id, reports.c.forked_from_id, reports.c.source_artifact_id)
                                 .where(reports.c.report_type == "artifact_chat")).mappings().all()
        for chat in rows:
            target = shown_by_report.get(str(chat["forked_from_id"]))
            if target and chat["source_artifact_id"] is None and not self._done("reports", chat["id"], "update"):
                self._update("reports", chat["id"], {"source_artifact_id": target})

    # conversation schedules' jobs
    def _jobs(self):
        if not self.t.has("apscheduler_jobs"):
            return
        jobs = self.t["apscheduler_jobs"]
        backup = _jobs_backup_table(self.conn)
        reports = self.t["reports"]
        artifacts = self.t["artifacts"]
        for job in self.conn.execute(sa.select(jobs).where(jobs.c.id.like("report\\_%", escape="\\"))).mappings().all():
            rid = job["id"][len("report_"):]
            report = self.conn.execute(sa.select(reports.c.user_id, reports.c.cron_schedule).where(
                reports.c.id == rid)).mappings().first()
            # Live ones only: a conversation whose dashboards were all deleted
            # has none to move its schedule to (decision 7).
            has_dashboard = self.conn.execute(sa.select(artifacts.c.id).where(
                artifacts.c.report_id == rid, artifacts.c.deleted_at.is_(None)).limit(1)).first() is not None
            if report is not None and report["cron_schedule"] not in NO_CRON and not has_dashboard:
                # Decision 7: the schedule goes with its job, so nothing says
                # it still runs (the list's icon, the schedule dialog).
                self._update("reports", rid, {"cron_schedule": None})
                self.report["widget_schedules_removed"] += 1
            runs_as = _scheduled_by(job["job_state"], rid)
            if report is not None and runs_as is not None and runs_as != str(report["user_id"]):
                self.scheduled_by_other.append(rid)
            if not self.conn.execute(sa.select(backup.c.id).where(backup.c.id == job["id"])).first():
                self.conn.execute(backup.insert().values(id=job["id"], next_run_time=job["next_run_time"],
                                                         job_state=job["job_state"]))
            self.conn.execute(jobs.delete().where(jobs.c.id == job["id"]))
            self.report["jobs_removed"] += 1

    def _summary(self):
        out = {k: self.report.get(k, 0) for k in (
            "published", "drafts", "deleted", "shares_copied", "shares_dropped", "schedules_moved",
            "schedules_removed",
            "jobs_removed", "widget_schedules_removed", "reader_results_copied")}
        out["schedules_set_by_someone_else"] = list(self.scheduled_by_other)
        return out


def migrate(conn) -> dict:
    """Convert every existing dashboard; safe to run again. Returns the counts
    it logs. If it fails, the database rolls back: the files it copied for
    releases that will not exist go too."""
    migration = Migration(conn)

    def remove_copied_files(*_):
        for path in migration.files_written:
            with contextlib.suppress(OSError):
                path.unlink()

    try:
        report = migration.run()
    except BaseException:
        remove_copied_files()
        raise
    # The upgrade's transaction is not over: a later revision, or the commit,
    # failing rolls this migration back too -- its files go with it.
    _until_committed(conn, remove_copied_files)
    log.info("dashfc08: %s", json.dumps(report))
    return report


def _until_committed(conn, on_rollback) -> None:
    """Call `on_rollback` if the connection's transaction is rolled back."""
    _when_settled(conn, on_rollback=on_rollback)


def _when_settled(conn, *, on_commit=None, on_rollback=None) -> None:
    """Call `on_commit` or `on_rollback` once, when the connection's
    transaction ends. (Listeners stay on this connection object, which lives
    as long as the upgrade: removing them while the event is being
    dispatched is not allowed.)"""
    state = {"settled": False}

    def settle(fn):
        def listener(_conn):
            if not state["settled"]:
                state["settled"] = True
                if fn is not None:
                    fn()
        return listener

    sa.event.listen(conn, "rollback", settle(on_rollback))
    sa.event.listen(conn, "commit", settle(on_commit))


def upgrade() -> None:
    migrate(op.get_bind())


# ── downgrade ────────────────────────────────────────────────────────────

def downgrade() -> None:
    restore(op.get_bind())


def restore(conn) -> None:
    """Undo the migration exactly -- in the database. The files it copied for
    releases stay: deleting them before the commit is known to have
    succeeded could leave restored releases pointing at nothing, while
    unused files with random names harm no one (2026-10-22 review)."""
    t = Tables(conn)
    if not t.has(UNDO):
        return
    undo = t[UNDO]
    entries = conn.execute(sa.select(undo).order_by(undo.c.id.desc())).mappings().all()

    # 1. Every column the migration set, back as it was (newest first), which
    #    also clears the dashboards' pointers to their releases.
    for e in entries:
        if e["action"] == "update" and t.has(e["table_name"]):
            table = t[e["table_name"]]
            prior = {k: _from_jsonable(v) for k, v in json.loads(e["prior"]).items()}
            conn.execute(table.update().where(table.c.id == e["row_id"]).values(**_sql(table, prior)))

    # 2. Every row it added, in an order the foreign keys allow (Postgres
    #    checks them; the copies point at each other).
    inserted: dict[str, list[str]] = {}
    for e in entries:
        if e["action"] == "insert":
            inserted.setdefault(e["table_name"], []).append(e["row_id"])
    if inserted.get("queries"):
        queries = t["queries"]
        conn.execute(queries.update().where(queries.c.id.in_(inserted["queries"])).values(default_step_id=None))
    # Since the upgrade the app may have pointed at the copied steps (readers'
    # runs, usage records, chats...): those let go first, as the app's own
    # release deletion does (dashboard_publish.delete_release_rows; Postgres
    # enforces these keys).
    copied_steps = inserted.get("steps") or []
    for i in range(0, len(copied_steps), 500):
        chunk = copied_steps[i:i + 500]
        for name, column in (("entities", "source_step_id"), ("completions", "step_id"),
                             ("tool_executions", "created_step_id")):
            if t.has(name):
                conn.execute(t[name].update().where(t[name].c[column].in_(chunk)).values(**{column: None}))
        for name in ("table_usage_events", "table_feedback_events", "step_user_results"):
            if t.has(name):
                conn.execute(t[name].delete().where(t[name].c.step_id.in_(chunk)))
    # The schedules this migration moved go with it: their dashboards' jobs.
    if t.has("apscheduler_jobs"):
        jobs = t["apscheduler_jobs"]
        moved = [e["row_id"] for e in entries if e["action"] == "update" and e["table_name"] == "artifacts"
                 and "cron_schedule" in json.loads(e["prior"] or "{}")]
        for i in range(0, len(moved), 500):
            conn.execute(jobs.delete().where(jobs.c.id.in_([f"dashboard_{a}" for a in moved[i:i + 500]])))
    for name in ("step_user_results", "artifact_shares", "visualizations", "steps", "queries"):
        ids = inserted.get(name) or []
        for i in range(0, len(ids), 500):
            conn.execute(t[name].delete().where(t[name].c.id.in_(ids[i:i + 500])))
    releases = t["artifact_releases"]
    for release_id in inserted.get("artifact_releases") or []:
        for name in ("artifact_release_data_source_association", "artifact_release_file_association"):
            if t.has(name):
                conn.execute(t[name].delete().where(t[name].c.artifact_release_id == release_id))
        conn.execute(releases.delete().where(releases.c.id == release_id))

    # 3. The conversations' schedule jobs, back.
    if t.has(JOBS_BACKUP) and t.has("apscheduler_jobs"):
        backup, jobs = t[JOBS_BACKUP], t["apscheduler_jobs"]
        for job in conn.execute(sa.select(backup)).mappings().all():
            if not conn.execute(sa.select(jobs.c.id).where(jobs.c.id == job["id"])).first():
                conn.execute(jobs.insert().values(**dict(job)))
    if t.has(JOBS_BACKUP):
        t[JOBS_BACKUP].drop(conn)
    t[UNDO].drop(conn)

