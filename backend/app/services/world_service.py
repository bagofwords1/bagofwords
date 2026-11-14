"""Read-only projections of existing resources. No graph store or inferred edges.

Every page authorizes its objects before projecting names, links or counts.
Large collections page independently; report internals are expanded explicitly.
Never serialize ORM objects wholesale here (credentials/results/private traces).
"""

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import lazyload, selectinload

from app.errors import AppError, ErrorCode
from app.models.data_source import DataSource
from app.services.data_source_service import DataSourceService
from app.services.folder_service import can_read_folder_output, load_report_folders


def key(kind, value):
    return f"{kind}:{value}"


class WorldService:
    PAGE = 500

    async def build(
        self, db, user, org, *, section="overview", folder_id=None, agent_id=None, report_id=None, offset=0
    ):
        self.db, self.user, self.org = db, user, org
        self.nodes, self.edges, self.more = {}, {}, []
        self.ds_service = DataSourceService()
        # Folder knowledge has its own authorization, independent of the agent roster.
        if section == "folder":
            await self.folder_knowledge(folder_id)
            return self.page()
        agents = await self.ds_service.get_data_sources(db, user, org, show_all=True)
        self.agent_ids = {str(a.id) for a in agents}
        for a in agents:
            self.node("agent", a.id, a.name, description=a.description, url=f"/agents/{a.id}", expandable=True)
        if agent_id and agent_id not in self.agent_ids:
            raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND)
        if section in {"overview", "reports", "tasks"}:
            from app.routes.folders import list_folders

            folders = (await list_folders(db, user, org))["folders"]
            for folder in folders:
                self.node(
                    "folder", folder["id"], folder["name"], url=f"/agents/folders/{folder['id']}", expandable=True
                )
            for folder in folders:
                self.edge(key("folder", folder["parent_id"]), key("folder", folder["id"]), "contains")
            for agent in agents:
                self.edge(key("folder", agent.folder_id), key("agent", agent.id), "contains")
        if section == "overview":
            await self.connections()
        elif section == "instructions":
            await self.instructions(offset)
        elif section == "evals":
            await self.evals(offset)
        elif section == "files":
            await self.files(offset)
        elif section == "tasks":
            await self.tasks(offset)
        elif section == "projects":
            await self.projects(offset)
        elif section == "reports":
            await self.reports(offset)
        elif section == "artifacts":
            await self.artifacts(offset)
        elif section == "memory":
            await self.memories()
        elif section == "agent" and agent_id:
            await self.agent(agent_id, offset)
        elif section == "report" and report_id:
            await self.report(report_id, offset)
        return self.page()

    def page(self):
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values()), "next": self.more}

    async def folder_knowledge(self, folder_id):
        from app.core.permission_resolver import resolve_permissions
        from app.models.folder import Folder
        from app.routes.folders import folder_item, knowledge_items

        rights = await resolve_permissions(self.db, self.user.id, self.org.id)
        if not folder_id or not rights.has_resource_permission("folder", folder_id, "browse"):
            raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND)
        folder = await self.db.scalar(
            select(Folder).where(Folder.id == folder_id, Folder.organization_id == self.org.id)
        )
        if folder is None:
            raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND)
        access = folder_item(folder, rights)
        source = self.node("folder", folder.id, folder.name, url=f"/agents/folders/{folder.id}", expandable=True)
        for kind, node_type in (("instructions", "instruction"), ("files", "file"), ("evals", "eval")):
            if "manage_" + kind not in access["capabilities"] and (kind == "evals" or not access["can_query"]):
                continue
            items = await knowledge_items(folder_id, kind, db=self.db, user=self.user, org=self.org)
            for item in items:
                target = self.node(node_type, item["id"], item["name"])
                self.edge(source, target, "includes")

    def node(self, kind, id, label, *, description=None, url=None, expandable=False, **meta):
        id = str(id)
        self.nodes[key(kind, id)] = {
            "id": key(kind, id),
            "resource_id": id,
            "type": kind,
            "label": (label or kind)[:240],
            "description": (description or "")[:1600],
            "url": url,
            "expandable": expandable,
            "meta": meta,
        }
        return key(kind, id)

    def edge(self, source, target, relation):
        # Both endpoints must have passed this page's own access checks.
        if source not in self.nodes or target not in self.nodes:
            return
        id = f"{source}|{relation}|{target}"
        self.edges[id] = {"id": id, "source": source, "target": target, "relation": relation}

    def continuation(self, section, offset, **scope):
        self.more.append({"section": section, "offset": offset + self.PAGE, **scope})

    async def rows(self, model, *conditions, offset=0):
        return list(
            (
                await self.db.execute(
                    select(model)
                    .options(lazyload("*"))
                    .where(model.deleted_at.is_(None), *conditions)
                    .order_by(model.created_at.desc(), model.id)
                    .offset(offset)
                    .limit(self.PAGE + 1)
                )
            ).scalars()
        )

    async def connections(self):
        from app.models.connection import Connection
        from app.models.domain_connection import domain_connection

        rows = (
            await self.db.execute(
                select(Connection.id, Connection.name, Connection.type, domain_connection.c.data_source_id)
                .join(domain_connection, domain_connection.c.connection_id == Connection.id)
                .where(
                    domain_connection.c.data_source_id.in_(self.agent_ids),
                    Connection.organization_id == self.org.id,
                    Connection.deleted_at.is_(None),
                )
            )
        ).all()
        for id, name, type, aid in rows:
            self.node("connection", id, name, source_type=type)
            self.edge(key("agent", aid), key("connection", id), "connected_to")

    async def instructions(self, offset):
        from app.services.instruction_service import InstructionService

        result = await InstructionService().get_instructions(
            self.db, self.org, self.user, skip=offset, limit=self.PAGE, light=True, live=None
        )
        for i in result["items"]:
            self.node(
                "instruction",
                i.id,
                i.title or i.preview,
                description=i.description or i.preview,
                url=f"/agents/instructions/{i.id}",
                status=i.status,
                load_mode=i.load_mode,
            )
            for a in i.data_sources:
                self.edge(key("agent", a.id), key("instruction", i.id), "has_instruction")
        if offset + self.PAGE < result["total"]:
            self.continuation("instructions", offset)

    async def evals(self, offset):
        from app.core.eval_scope import eval_agent_scope
        from app.models.eval import TestSuite

        unscoped, allowed = await eval_agent_scope(self.db, self.user.id, self.org.id)
        conditions = [TestSuite.organization_id == self.org.id, TestSuite.owner_folder_id.is_(None)]
        if not unscoped:
            conditions.append(TestSuite.data_source_id.in_(allowed & self.agent_ids))
        rows = await self.rows(TestSuite, *conditions, offset=offset)
        for suite in rows[: self.PAGE]:
            self.node("eval", suite.id, suite.name, description=suite.description)
            self.edge(key("agent", suite.data_source_id), key("eval", suite.id), "includes")
        if len(rows) > self.PAGE:
            self.continuation("evals", offset)

    async def files(self, offset):
        from app.models.data_source_file_association import data_source_file_association as assoc
        from app.models.file import File
        from app.services.file_access_service import visible_files_clause

        clause = await visible_files_clause(self.db, self.user, self.org)
        rows = await self.rows(File, File.organization_id == self.org.id, clause, offset=offset)
        for f in rows[: self.PAGE]:
            self.node("file", f.id, f.filename, source_type=f.source_kind)
        links = (
            await self.db.execute(
                select(assoc).where(
                    assoc.c.file_id.in_([f.id for f in rows[: self.PAGE]]), assoc.c.data_source_id.in_(self.agent_ids)
                )
            )
        ).all()
        for r in links:
            self.edge(key("agent", r.data_source_id), key("file", r.file_id), "includes")
        if len(rows) > self.PAGE:
            self.continuation("files", offset)

    async def projects(self, offset):
        from app.models.project import Project
        from app.models.project import project_data_source_association as assoc
        from app.services.project_service import project_service

        ids = await project_service.get_visible_project_ids(self.db, self.user, self.org)
        rows = await self.rows(Project, Project.id.in_(ids), offset=offset)
        for p in rows[: self.PAGE]:
            self.node("project", p.id, p.name, description=p.description, url=f"/projects/{p.id}")
        for r in (await self.db.execute(select(assoc).where(assoc.c.project_id.in_(ids)))).all():
            self.edge(key("project", r.project_id), key("agent", r.data_source_id), "includes")
        if len(rows) > self.PAGE:
            self.continuation("projects", offset)

    async def tasks(self, offset):
        from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
        from app.models.report import Report
        from app.models.scheduled_prompt import ScheduledPrompt
        from app.services.bow_source_access import visible_reports_clause
        from app.services.file_access_service import user_can_view_report

        rights = await resolve_permissions(self.db, self.user.id, self.org.id)
        reports = select(Report.id).where(
            Report.organization_id == self.org.id,
            Report.deleted_at.is_(None),
            await visible_reports_clause(self.db, self.org.id, self.user),
        )
        conditions = [ScheduledPrompt.report_id.in_(reports)]
        if FULL_ADMIN not in rights.org_permissions:
            conditions.append(ScheduledPrompt.user_id == self.user.id)
        rows = await self.rows(ScheduledPrompt, *conditions, offset=offset)
        for task in rows[: self.PAGE]:
            report = await self.db.get(Report, task.report_id, options=[lazyload("*")])
            if not report or not await user_can_view_report(self.db, self.user, self.org, report):
                continue
            source = self.node("report", report.id, report.title, url=f"/reports/{report.id}", expandable=True)
            target = self.node("task", task.id, task.title or report.title, url="/automations")
            self.edge(source, target, "contains")
            for agent_id in task.data_source_ids or []:
                self.edge(target, key("agent", agent_id), "uses_agent")
            for folder_id in task.agent_folder_ids or []:
                self.edge(target, key("folder", folder_id), "uses_folder")
        if len(rows) > self.PAGE:
            self.continuation("tasks", offset)

    async def reports(self, offset):
        from app.models.report import Report
        from app.services.bow_source_access import visible_reports_clause
        from app.services.file_access_service import user_can_view_report
        from app.services.report_service import ReportService

        # Narrow in SQL, then check CONVERSATION visibility: a dashboard share
        # alone must not reveal its private conversation or execution history.
        visible = await ReportService()._visible_to_user_clause(self.db, self.user, self.org)
        rows = await self.rows(
            Report,
            Report.organization_id == self.org.id,
            Report.status != "archived",
            or_(Report.user_id == self.user.id, visible),
            await visible_reports_clause(self.db, self.org.id, self.user),
            offset=offset,
        )
        for r in rows[: self.PAGE]:
            if await can_read_folder_output(self.db, r, self.user) and await user_can_view_report(
                self.db, self.user, self.org, r
            ):
                self.node("report", r.id, r.title, url=f"/reports/{r.id}", expandable=True)
                for folder_id in await load_report_folders(self.db, r):
                    self.edge(key("report", r.id), key("folder", folder_id), "uses_folder")
        await self.report_agent_edges()
        if len(rows) > self.PAGE:
            self.continuation("reports", offset)

    async def report_agent_edges(self):
        from app.models.report_data_source_association import report_data_source_association as assoc

        ids = [n["resource_id"] for n in self.nodes.values() if n["type"] == "report"]
        for r in (await self.db.execute(select(assoc).where(assoc.c.report_id.in_(ids)))).all():
            self.edge(key("report", r.report_id), key("agent", r.data_source_id), "uses_agent")

    async def artifacts(self, offset):
        from app.models.report import Report
        from app.services.bow_source_access import visible_reports_clause
        from app.services.dashboard_list import ListQuery, list_dashboards
        from app.services.file_access_service import user_can_view_report

        for tab in ("my", "shared"):
            query = ListQuery(filter=tab, page=offset // self.PAGE + 1, limit=self.PAGE)
            result = await list_dashboards(self.db, self.user, self.org, query)
            for a in result["dashboards"]:
                self.node(
                    "artifact",
                    a["id"],
                    a["title"],
                    url=f"/d/{a['slug'] or a['id']}",
                    mode=a["mode"],
                    published=a["published"],
                )
                if a.get("report_id"):
                    report = (
                        await self.db.execute(
                            select(Report)
                            .options(lazyload("*"))
                            .where(
                                Report.id == a["report_id"],
                                await visible_reports_clause(self.db, self.org.id, self.user),
                                Report.organization_id == self.org.id,
                                Report.deleted_at.is_(None),
                            )
                        )
                    ).scalar_one_or_none()
                    if (
                        report
                        and await can_read_folder_output(self.db, report, self.user)
                        and await user_can_view_report(self.db, self.user, self.org, report)
                    ):
                        self.node("report", report.id, report.title, url=f"/reports/{report.id}", expandable=True)
                        self.edge(key("report", report.id), key("artifact", a["id"]), "produced")
            if result["meta"]["total"] > offset + self.PAGE and not self.more:
                self.continuation("artifacts", offset)
        await self.report_agent_edges()

    async def memories(self):
        from app.models.organization_settings import OrganizationSettings
        from app.services.memory_service import is_memory_enabled, memory_service

        settings = (
            await self.db.execute(
                select(OrganizationSettings).where(OrganizationSettings.organization_id == self.org.id)
            )
        ).scalar_one_or_none()
        if not is_memory_enabled(settings):
            return
        entries = await memory_service.active_entries(self.db, str(self.org.id), str(self.user.id))
        for m in entries:
            self.node("memory", m.id, m.text, description=m.text, private=True, handle=m.handle)
            for tag in m.tags or []:
                if tag.startswith("agent:"):
                    self.edge(key("memory", m.id), tag, "remembers")

    async def agent(self, agent_id, offset):
        from app.core.permission_resolver import resolve_permissions
        from app.models.agent_list import AgentList
        from app.models.connection_tool import ConnectionTool
        from app.models.data_source_connection_tool import DataSourceConnectionTool
        from app.services.tool_policy_service import ToolPolicyService, resolve_effective_policy

        permissions = await resolve_permissions(self.db, str(self.user.id), str(self.org.id))
        can_manage = permissions.has_resource_permission(
            "data_source", agent_id, "manage"
        ) or permissions.has_org_permission("manage_connections")
        await self.connections()
        result = await self.ds_service.get_data_source_schema_paginated(
            self.db,
            agent_id,
            self.org,
            page=offset // self.PAGE + 1,
            page_size=self.PAGE,
            current_user=self.user,
            restrict_to_active=not can_manage,
            exclude_file_source_types=False,
        )
        for t in result.tables:
            kind = "table"
            from app.schemas.data_source_registry import get_entry

            # File catalogs are still projected as resources, never file bytes.
            try:
                entry = get_entry(t.connection_type)
                if entry.data_shape == "files":
                    kind = "file"
            except (KeyError, ValueError, AttributeError, TypeError):
                pass
            tid = t.id or f"{agent_id}:{t.connection_id}:{t.name}"
            self.node(
                kind,
                tid,
                t.name,
                url=f"/agents/{agent_id}/tables",
                source_type=t.connection_type,
                columns=len(t.columns),
            )
            self.edge(key("agent", agent_id), key(kind, tid), "includes")
            self.edge(key("connection", t.connection_id), key(kind, tid), "provides")
        if result.total_pages > offset // self.PAGE + 1:
            self.continuation("agent", offset, agent_id=agent_id)
        ds = (
            await self.db.execute(
                select(DataSource)
                .options(lazyload("*"), selectinload(DataSource.connections).options(lazyload("*")))
                .where(DataSource.id == agent_id)
            )
        ).scalar_one()
        tools = await self.rows(
            ConnectionTool, ConnectionTool.connection_id.in_([c.id for c in ds.connections]), offset=offset
        )
        overlays = {
            str(o.connection_tool_id): o
            for o in (
                await self.db.execute(
                    select(DataSourceConnectionTool).where(DataSourceConnectionTool.data_source_id == agent_id)
                )
            ).scalars()
        }
        prefs = await ToolPolicyService().get_user_preferences(self.db, self.user.id, [t.id for t in tools])
        for t in tools[: self.PAGE]:
            overlay = overlays.get(str(t.id))
            if not (overlay.is_enabled if overlay else t.is_enabled):
                continue
            policy = resolve_effective_policy(overlay.policy if overlay else t.policy, prefs.get(str(t.id)))
            if policy == "deny":
                continue
            self.node("tool", t.id, t.name, description=t.description, url=f"/agents/{agent_id}/tools", policy=policy)
            self.edge(key("agent", agent_id), key("tool", t.id), "includes")
            self.edge(key("connection", t.connection_id), key("tool", t.id), "provides")
        lists = await self.rows(
            AgentList, AgentList.data_source_id == agent_id, AgentList.organization_id == self.org.id, offset=offset
        )
        for item in lists[: self.PAGE]:
            self.node(
                "list", item.id, item.name, description=item.description, url=f"/agents/{agent_id}/lists/{item.id}"
            )
            self.edge(key("agent", agent_id), key("list", item.id), "includes")
        if (len(tools) > self.PAGE or len(lists) > self.PAGE) and not self.more:
            self.continuation("agent", offset, agent_id=agent_id)
        # Only link references whose source AND target passed access checks.
        await self.instructions(0)
        self.more = [m for m in self.more if m["section"] != "instructions"]
        from app.models.instruction_reference import InstructionReference

        ids = [n["resource_id"] for n in self.nodes.values() if n["type"] == "instruction"]
        refs = (
            await self.db.execute(select(InstructionReference).where(InstructionReference.instruction_id.in_(ids)))
        ).scalars()
        kinds = {"datasource_table": "table", "connection_tool": "tool", "instruction": "instruction"}
        for r in refs:
            if r.object_type in kinds:
                self.edge(key("instruction", r.instruction_id), key(kinds[r.object_type], r.object_id), "references")

    async def report(self, report_id, offset):
        from app.models.agent_execution import AgentExecution
        from app.models.completion import Completion
        from app.models.query import Query
        from app.models.report import Report
        from app.models.step import Step
        from app.services.bow_source_access import assert_read
        from app.services.file_access_service import user_can_view_report

        report = (
            await self.db.execute(
                select(Report)
                .options(lazyload("*"))
                .where(Report.id == report_id, Report.organization_id == self.org.id, Report.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if (
            report is None
            or not await can_read_folder_output(self.db, report, self.user)
            or not await user_can_view_report(self.db, self.user, self.org, report)
        ):
            raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND)
        await assert_read(self.db, getattr(report, "bow_source_access", None), self.user)
        self.node("report", report.id, report.title, url=f"/reports/{report.id}", expandable=True)
        await self.report_agent_edges()
        queries = await self.rows(Query, Query.report_id == report_id, offset=offset)
        for q in queries[: self.PAGE]:
            self.node("query", q.id, q.title, description=q.description, url=f"/reports/{report_id}")
            self.edge(key("report", report_id), key("query", q.id), "contains")
        steps = await self.rows(
            Step,
            Step.query_id.in_(select(Query.id).where(Query.report_id == report_id, Query.deleted_at.is_(None))),
            offset=offset,
        )
        for s in steps[: self.PAGE]:
            self.node("step", s.id, s.title, url=f"/reports/{report_id}", status=s.status)
            self.edge(key("query", s.query_id), key("step", s.id), "has_step")
            self.edge(key("report", report_id), key("step", s.id), "contains")
        # Lineage only names tables that the user's current catalog permits.
        # A historical usage event is not an access grant to its source.
        from app.models.table_usage_event import TableUsageEvent

        usage = list(
            (
                await self.db.execute(
                    select(TableUsageEvent)
                    .options(lazyload("*"))
                    .where(
                        TableUsageEvent.org_id == self.org.id,
                        TableUsageEvent.step_id.in_([s.id for s in steps[: self.PAGE]]),
                        TableUsageEvent.data_source_id.in_(self.agent_ids),
                    )
                )
            ).scalars()
        )
        for aid in sorted({str(u.data_source_id) for u in usage}):
            await self.agent(aid, 0)
        for u in usage:
            self.edge(key("step", u.step_id), key("table", u.datasource_table_id), "uses_table")
        completions = await self.rows(Completion, Completion.report_id == report_id, offset=offset)
        for c in completions[: self.PAGE]:
            self.node("completion", c.id, f"{c.role} · {c.turn_index}", url=f"/reports/{report_id}", status=c.status)
            self.edge(key("report", report_id), key("completion", c.id), "contains")
        executions = []
        from app.core.console_access import resolve_console_scope

        try:
            scope = await resolve_console_scope(self.db, self.org, self.user)
            await scope.assert_report_visible(self.db, report_id)
        except HTTPException as e:
            if e.status_code not in (403, 404):
                raise
        else:
            executions = await self.rows(
                AgentExecution,
                AgentExecution.report_id == report_id,
                AgentExecution.organization_id == self.org.id,
                offset=offset,
            )
            for e in executions[: self.PAGE]:
                self.node("execution", e.id, f"{e.status} · {str(e.created_at)[:16]}", status=e.status)
                self.edge(key("report", report_id), key("execution", e.id), "has_execution")
                self.edge(key("completion", e.completion_id), key("execution", e.id), "executed_by")
        if any(len(rows) > self.PAGE for rows in (queries, steps, completions, executions)):
            self.continuation("report", offset, report_id=report_id)
