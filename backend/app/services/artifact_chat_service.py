"""Chat on the shared artifact page /r/{id}.

A viewer's conversation never touches the source report's transcript: each
(source report, viewer) pair gets one hidden child report with
report_type='artifact_chat' and forked_from_id=source. Because every listing,
search and agent surface already filters report_type == 'regular', these
threads are invisible everywhere except the /r/{id}/chat endpoints.

Scope model: the owner's allowlist (artifact_chat_data_source_ids; null =
the source report's attached roster) is intersected per message with the
agents the viewer can access themselves. The intersection becomes the chat
report's attached roster — a manual hard scope for the agent pipeline. An
empty intersection means "dashboard data only": no agents are attached, no
schema enters context, and the planner answers from the artifact's own
visualization data injected as platform context.
"""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association

logger = logging.getLogger(__name__)

# Rows per visualization injected into the data-only chat context. Keeps the
# prompt bounded on large dashboards while staying complete for typical ones.
MAX_CONTEXT_ROWS_PER_VIZ = 100


class ArtifactChatService:

    async def ensure_chat_access(self, db, report: Report, user) -> None:
        """Gate every /r/{id}/chat call. Raises 401/403/404.

        Order matters: the visibility check first (it 404s reports the caller
        may not even know exist), then auth, then the chat toggle, then org
        membership — chat runs the org's LLM and (optionally) its agents, so
        it stays an org-member surface even on public reports.
        """
        from app.services.report_service import ReportService

        await ReportService()._check_visibility(db, report, 'artifact_visibility', user)

        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        if not getattr(report, 'artifact_chat_enabled', False):
            raise HTTPException(status_code=403, detail="Chat is not enabled on this report")

        if not await self._is_org_member(db, user, report.organization_id):
            raise HTTPException(
                status_code=403,
                detail="Chat is available to members of this workspace only",
            )

    @staticmethod
    async def _is_org_member(db, user, organization_id) -> bool:
        from app.models.membership import Membership
        row = (await db.execute(
            select(Membership.id).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization_id),
            ).limit(1)
        )).scalar_one_or_none()
        return row is not None

    async def candidate_agent_ids(self, db, report: Report) -> list[str]:
        """The agents this report actually uses — the owner-side inheritance base.

        A manually-attached roster is authoritative. An Auto report (empty
        roster) has no attachment rows, but its runs still used concrete
        agents: recover them from the report's tool executions (connection_id /
        data_source_id arguments), falling back to the focused set. Never
        widens to "everything accessible" — that would share more than the
        dashboard ever used.
        """
        import json as _json

        roster = [str(ds.id) for ds in (report.data_sources or [])]
        if roster:
            return roster

        from app.models.agent_execution import AgentExecution
        from app.models.tool_execution import ToolExecution
        from app.models.data_source import DataSource

        rows = (await db.execute(
            select(ToolExecution.arguments_json)
            .join(AgentExecution, ToolExecution.agent_execution_id == AgentExecution.id)
            .where(AgentExecution.report_id == str(report.id))
        )).scalars().all()
        conn_ids: set[str] = set()
        ds_ids: set[str] = set()
        for raw in rows:
            try:
                args = raw if isinstance(raw, dict) else _json.loads(raw or "{}")
            except Exception:
                continue
            cid = args.get("connection_id")
            if cid:
                conn_ids.add(str(cid))
            for key in ("data_source_id",):
                if args.get(key):
                    ds_ids.add(str(args[key]))
            for key in ("data_source_ids", "agent_ids"):
                vals = args.get(key)
                if isinstance(vals, list):
                    ds_ids.update(str(v) for v in vals)
        if conn_ids:
            from app.models.domain_connection import domain_connection
            conn_rows = (await db.execute(
                select(domain_connection.c.data_source_id)
                .where(domain_connection.c.connection_id.in_(list(conn_ids)))
            )).scalars().all()
            ds_ids.update(str(x) for x in conn_rows)
        if ds_ids:
            valid = (await db.execute(
                select(DataSource.id).where(
                    DataSource.id.in_(list(ds_ids)),
                    DataSource.organization_id == str(report.organization_id),
                    DataSource.deleted_at.is_(None),
                )
            )).scalars().all()
            if valid:
                return [str(x) for x in valid]

        return [str(x) for x in (getattr(report, 'focused_data_source_ids', None) or [])]

    async def effective_agent_ids(self, db, report: Report, organization, user) -> list[str]:
        """owner allowlist ∩ viewer-accessible agents, both live.

        The owner side: artifact_chat_data_source_ids, or — when null — the
        agents the report actually uses (roster, or recovered from its runs
        for Auto reports; see candidate_agent_ids). Never Auto-wide.
        """
        from app.ai.tools.implementations.agent_focus_common import accessible_agents
        from app.services.data_source_service import DataSourceService

        allowed = getattr(report, 'artifact_chat_data_source_ids', None)
        if allowed is None:
            allowed_ids = set(await self.candidate_agent_ids(db, report))
        else:
            allowed_ids = {str(x) for x in allowed}
        if not allowed_ids:
            return []

        viewer_agents = await accessible_agents(db, organization, user)
        return [
            str(ds.id) for ds in viewer_agents
            if str(ds.id) in allowed_ids and DataSourceService.is_execution_live(ds)
        ]

    async def resolve_chat_report(self, db, source: Report, user, agent_ids: list[str]) -> Report:
        """Get-or-create this viewer's chat report and sync its roster.

        The roster is re-synced on every message so owner allowlist edits and
        viewer grant changes apply immediately. The chat report's empty roster
        is NOT Auto (resolve_run_agents special-cases report_type='artifact_chat').
        """
        chat_report = (await db.execute(
            select(Report).where(
                Report.report_type == 'artifact_chat',
                Report.forked_from_id == str(source.id),
                Report.user_id == str(user.id),
                Report.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()

        if chat_report is None:
            chat_report = Report(
                title=f"Chat: {source.title or 'Untitled report'}",
                slug=f"artifact-chat-{uuid.uuid4().hex[:12]}",
                status='draft',
                report_type='artifact_chat',
                mode='chat',
                model_id=source.model_id,
                user_id=str(user.id),
                organization_id=str(source.organization_id),
                forked_from_id=str(source.id),
            )
            db.add(chat_report)
            await db.flush()

        # Sync roster to the effective set (idempotent).
        current_rows = (await db.execute(
            select(report_data_source_association.c.data_source_id).where(
                report_data_source_association.c.report_id == str(chat_report.id)
            )
        )).all()
        current = {str(r[0]) for r in current_rows}
        target = set(agent_ids)
        for ds_id in target - current:
            await db.execute(report_data_source_association.insert().values(
                report_id=str(chat_report.id), data_source_id=ds_id,
            ))
        if current - target:
            await db.execute(report_data_source_association.delete().where(
                report_data_source_association.c.report_id == str(chat_report.id),
                report_data_source_association.c.data_source_id.in_(list(current - target)),
            ))
        await db.commit()
        return chat_report

    async def build_platform_context(self, db, source: Report, agent_ids: list[str]) -> dict:
        """The artifact-chat platform_context injected into the planner prompt.

        Always carries the dashboard's identity; in data-only mode (no agents)
        it also carries the artifact's visualization data — resolved LIVE from
        the source report's latest artifact, never copied, so it is exactly as
        fresh as the dashboard itself.
        """
        from app.models.artifact import Artifact
        from app.services.artifact_payload import collect_visualizations

        ctx: dict = {
            "source_report_title": source.title,
            "scope": "agents" if agent_ids else "data_only",
        }

        artifact = (await db.execute(
            select(Artifact)
            .where(Artifact.report_id == str(source.id), Artifact.deleted_at.is_(None))
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if artifact is None:
            return ctx

        ctx["artifact_title"] = artifact.title

        if not agent_ids:
            viz_payload = await collect_visualizations(db, artifact)
            ctx["visualizations"] = [
                {
                    "title": v.get("title"),
                    "columns": v.get("columns") or [],
                    "rows": (v.get("rows") or [])[:MAX_CONTEXT_ROWS_PER_VIZ],
                    "total_rows": len(v.get("rows") or []),
                }
                for v in viz_payload
            ]
        return ctx


artifact_chat_service = ArtifactChatService()
