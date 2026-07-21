
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, lazyload, noload
from app.models.report import Report
from app.schemas.report_schema import ReportCreate, ReportSchema, ReportUpdate
from app.schemas.data_source_schema import DataSourceReportSchema
from app.services.widget_service import WidgetService
from app.core.telemetry import telemetry
from app.schemas.widget_schema import WidgetSchema
from app.schemas.step_schema import StepSchema
from app.schemas.user_schema import UserSchema
from app.models.file import File
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.report_data_source_association import report_data_source_association
from app.models.report_file_association import report_file_association
from fastapi import HTTPException

import uuid
from sqlalchemy import select, or_, func, cast, delete, case, String as SAString
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from logging import getLogger
import asyncio

from app.core.scheduler import scheduler, cron_dow_to_apscheduler, claim_scheduled_run
from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.ee.audit.service import audit_service
from app.models.visualization import Visualization
from app.models.query import Query
from app.models.step import Step
from app.models.scheduled_prompt import ScheduledPrompt
from app.core.otel import get_tracer

logger = getLogger(__name__)
tracer = get_tracer(__name__)

class ReportService:

    def __init__(self):
        self.widget_service = WidgetService()
    
    async def _check_visibility(
        self,
        db: AsyncSession,
        report: Report,
        visibility_field: str,
        user=None,
    ) -> None:
        """Check if a user can access a report based on its visibility setting.

        visibility_field: 'artifact_visibility' or 'conversation_visibility'
        Raises 401 if login needed, 403 if denied, or passes silently if allowed.
        """
        from app.models.membership import Membership
        from app.models.report_share import ReportShare

        visibility = getattr(report, visibility_field, 'none') or 'none'

        if visibility == 'none':
            # Only owner can access
            if user is None or str(user.id) != str(report.user_id):
                raise HTTPException(status_code=404, detail="Not found")
            return

        if visibility == 'public':
            return  # Anyone can view

        if visibility == 'internal':
            if user is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            stmt = select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == report.organization_id,
            )
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=403, detail="Access denied")
            return

        if visibility == 'shared':
            if user is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            # Owner always has access
            if str(user.id) == str(report.user_id):
                return
            share_type = 'artifact' if visibility_field == 'artifact_visibility' else 'conversation'
            stmt = select(ReportShare).where(
                ReportShare.report_id == report.id,
                ReportShare.user_id == user.id,
                ReportShare.share_type == share_type,
                ReportShare.deleted_at.is_(None),
            )
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=403, detail="Access denied")
            return

    async def _emit_share_event(
        self, db, *, report, share_type, visibility, shared_user_ids, current_user,
    ) -> None:
        """Emit a silent report_shared / artifact_shared (or the unshared/off
        counterpart) event when sharing changes. share_type ∈ {conversation,
        artifact}; visibility ∈ {none, shared, internal, public}."""
        from app.services.session_event_service import SessionEventService
        from app.ai.context.session_events import (
            REPORT_SHARED, REPORT_UNPUBLISHED, ARTIFACT_SHARED, ARTIFACT_UNSHARED,
        )
        is_conv = (share_type == 'conversation')
        if visibility and visibility != 'none':
            # Friendly "shared with" label.
            if visibility == 'shared':
                names = []
                if shared_user_ids:
                    from app.models.user import User as _User
                    rows = (await db.execute(
                        select(_User.name, _User.email).where(_User.id.in_([str(u) for u in shared_user_ids]))
                    )).all()
                    names = [(n or e) for (n, e) in rows]
                shared_with = names or ['specific people']
            elif visibility == 'public':
                shared_with = ['anyone with the link']
            elif visibility == 'internal':
                shared_with = ['the workspace']
            else:
                shared_with = []
            kind = REPORT_SHARED if is_conv else ARTIFACT_SHARED
            meta = {"visibility": visibility, "shared_with": shared_with, "share_type": share_type}
            if not is_conv:
                meta["title"] = report.title or "Artifact"
            await SessionEventService.emit_safe(
                db, report=report, kind=kind, user=current_user, commit=False, meta=meta,
            )
        else:
            kind = REPORT_UNPUBLISHED if is_conv else ARTIFACT_UNSHARED
            content = ("Conversation sharing was turned off" if is_conv
                       else f'"{report.title or "Artifact"}" was made private')
            await SessionEventService.emit_safe(
                db, report=report, kind=kind, user=current_user, commit=False,
                content=content, meta={"visibility": "none", "share_type": share_type},
            )

    async def set_visibility(
        self,
        db: AsyncSession,
        report_id: str,
        share_type: str,
        visibility: str,
        shared_user_ids: list[str] | None,
        current_user: User,
        organization: Organization,
    ) -> dict:
        """Set visibility for artifact or conversation sharing.

        share_type: 'artifact' or 'conversation'
        visibility: 'none', 'shared', 'internal', 'public'
        shared_user_ids: list of user IDs (required when visibility == 'shared')
        """
        from app.models.report_share import ReportShare

        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        field = 'artifact_visibility' if share_type == 'artifact' else 'conversation_visibility'
        setattr(report, field, visibility)

        # Sync legacy fields for backward compatibility
        if share_type == 'artifact':
            report.status = 'published' if visibility != 'none' else 'draft'
        elif share_type == 'conversation':
            if visibility != 'none':
                report.conversation_share_enabled = True
                if not report.conversation_share_token:
                    report.conversation_share_token = uuid.uuid4().hex
            else:
                report.conversation_share_enabled = False

        # Update shares list
        newly_added_user_ids: list[str] = []
        if visibility == 'shared' and shared_user_ids is not None:
            # Snapshot who already had this share so we only notify *new* recipients.
            existing_rows = (await db.execute(
                select(ReportShare.user_id).where(
                    ReportShare.report_id == report_id,
                    ReportShare.share_type == share_type,
                    ReportShare.deleted_at.is_(None),
                )
            )).all()
            existing_uids = {str(r[0]) for r in existing_rows}
            # Remove existing shares for this type
            await db.execute(
                delete(ReportShare).where(
                    ReportShare.report_id == report_id,
                    ReportShare.share_type == share_type,
                )
            )
            # Add new shares
            for uid in shared_user_ids:
                share = ReportShare(
                    report_id=report_id,
                    user_id=uid,
                    share_type=share_type,
                )
                db.add(share)
            newly_added_user_ids = [str(u) for u in shared_user_ids if str(u) not in existing_uids]
        elif visibility != 'shared':
            # Clear shares if moving away from shared mode
            await db.execute(
                delete(ReportShare).where(
                    ReportShare.report_id == report_id,
                    ReportShare.share_type == share_type,
                )
            )

        await db.commit()
        await db.refresh(report)

        # Silent session event: conversation/artifact sharing changed. Covers
        # both share_types via the same set_visibility path.
        try:
            await self._emit_share_event(
                db, report=report, share_type=share_type, visibility=visibility,
                shared_user_ids=shared_user_ids, current_user=current_user,
            )
        except Exception:
            pass

        # Notify-first: the durable in-app notification is the canonical record of
        # "shared with you" — created here on the share grant itself (email stays
        # the explicit opt-in action). Non-fatal: sharing must not depend on it.
        if newly_added_user_ids:
            try:
                from app.services.inbox_service import inbox_service
                await inbox_service.notify_share(
                    db, report=report, share_type=share_type,
                    user_ids=newly_added_user_ids, actor_user=current_user,
                )
            except Exception:
                logger.warning("share in-app notification failed", exc_info=True)

        # Telemetry
        try:
            await telemetry.capture(
                "report_visibility_changed",
                {
                    "report_id": str(report.id),
                    "share_type": share_type,
                    "visibility": visibility,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.visibility_changed",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={
                    "title": report.title,
                    "share_type": share_type,
                    "visibility": visibility,
                },
            )
        except Exception:
            pass

        return {
            "share_type": share_type,
            "visibility": visibility,
            "shared_user_ids": shared_user_ids or [],
            "conversation_share_token": report.conversation_share_token if share_type == 'conversation' and visibility != 'none' else None,
        }

    async def get_shares(
        self,
        db: AsyncSession,
        report_id: str,
        share_type: str,
    ) -> list[dict]:
        """Get list of users a report is shared with for a given share_type."""
        from app.models.report_share import ReportShare

        stmt = (
            select(ReportShare)
            .options(selectinload(ReportShare.user))
            .where(
                ReportShare.report_id == report_id,
                ReportShare.share_type == share_type,
                ReportShare.deleted_at.is_(None),
            )
        )
        result = await db.execute(stmt)
        shares = result.scalars().all()

        return [
            {
                "id": str(s.id),
                "user_id": str(s.user_id),
                "user_name": s.user.name if s.user else None,
                "user_email": s.user.email if s.user else None,
                "share_type": s.share_type,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in shares
        ]

    async def _detect_app_version(self, db: AsyncSession, report_id: str) -> str:
        """Detect app version for routing decisions based on agent execution data."""
        from app.models.agent_execution import AgentExecution
        from app.models.completion import Completion
        
        # Check if there are any agent executions for this report
        ae_query = select(AgentExecution).join(
            Completion, AgentExecution.completion_id == Completion.id
        ).where(
            Completion.report_id == report_id,
            Completion.role == 'system'
        ).order_by(AgentExecution.created_at.desc()).limit(1)
        
        result = await db.execute(ae_query)
        latest_execution = result.scalar_one_or_none()
        
        if latest_execution and latest_execution.bow_version:
            return latest_execution.bow_version
        
        # Fallback: check if any system completions exist (indicating it has AI interactions)
        completion_query = select(Completion).where(
            Completion.report_id == report_id,
            Completion.role == 'system'
        ).limit(1)
        
        completion_result = await db.execute(completion_query)
        has_system_completions = completion_result.scalar_one_or_none() is not None
        
        # If it has system completions but no agent executions, it's legacy
        if has_system_completions:
            return "0.0.189"  # Legacy version
        
        # New reports default to current version
        from app.settings.config import settings
        return settings.version
        

    async def get_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> ReportSchema:
        # Load only the relationships this method serializes (user,
        # external_platform, data_sources+connections, shares). Report's
        # mapper-level lazy="selectin" would otherwise hydrate the entire
        # graph — every step version's data JSON, every artifact version,
        # the chat history — on every report open. The summary counts below
        # are computed with COUNT queries instead of loading the rows.
        result = await db.execute(
            select(Report)
            .options(
                lazyload("*"),
                # UserSchema serializes external_user_mappings — load it, the
                # top-level wildcard turned User's own eager defaults off too.
                selectinload(Report.user).selectinload(User.external_user_mappings),
                selectinload(Report.external_platform),
                selectinload(Report.shares).options(lazyload("*")),
                selectinload(Report.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.connections).options(lazyload("*")),
                ),
            )
            .filter(Report.id == report_id)
        )
        report = result.unique().scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Per-user starred state (same source of truth as the list view)
        from app.models.report_star import ReportStar
        star_result = await db.execute(
            select(ReportStar.id).where(
                ReportStar.report_id == report.id,
                ReportStar.user_id == current_user.id,
                ReportStar.deleted_at.is_(None),
            )
        )
        is_starred = star_result.scalar_one_or_none() is not None

        user_schema = UserSchema.from_orm(report.user)

        # Lifecycle-filter the attached data sources before serializing. The
        # association is a creation-time snapshot, so agents disabled (or moved
        # back to draft/development) afterwards would otherwise keep surfacing
        # in the report's agent panel and prompt-box selection — places the
        # active-agents selector would never offer them.
        from app.services.data_source_service import DataSourceService
        live_data_sources = await DataSourceService().filter_live_data_sources(
            db, report.data_sources, current_user, organization
        )

        # Detect app version for routing
        app_version = await self._detect_app_version(db, report.id)

        report_schema = ReportSchema(
            id=report.id,
            title=report.title,
            status=report.status,
            slug=report.slug,
            report_type=report.report_type,
            user=user_schema,
            cron_schedule=report.cron_schedule,
            created_at=report.created_at,
            updated_at=report.updated_at,
            last_activity_at=report.last_activity_at,
            last_run_at=report.last_run_at,
            app_version=app_version,
            data_sources=live_data_sources,
            external_platform=report.external_platform,
            theme_name=report.theme_name,
            theme_overrides=report.theme_overrides,
            mode=getattr(report, "mode", "chat"),
            # Report-level LLM override (null = user/org default resolves at run time)
            model_id=getattr(report, "model_id", None),
            # Agent focus (subset of attached agents whose full schema is in context)
            focused_data_source_ids=getattr(report, "focused_data_source_ids", None) or [],
            # Conversation sharing
            conversation_share_enabled=bool(getattr(report, "conversation_share_enabled", False)),
            conversation_share_token=getattr(report, "conversation_share_token", None),
            # Sharing visibility
            artifact_visibility=getattr(report, "artifact_visibility", "none") or "none",
            conversation_visibility=getattr(report, "conversation_visibility", "none") or "none",
            artifact_shared_user_ids=[
                str(s.user_id) for s in (report.shares or [])
                if s.share_type == 'artifact' and s.deleted_at is None
            ],
            conversation_shared_user_ids=[
                str(s.user_id) for s in (report.shares or [])
                if s.share_type == 'conversation' and s.deleted_at is None
            ],
            # Scheduled rerun notification subscribers
            notification_subscribers=getattr(report, "notification_subscribers", None),
            # Per-user starred state
            is_starred=is_starred,
            # Trigger provenance (⚡ indicator)
            webhook_id=getattr(report, "webhook_id", None),
            # Scheduled-run provenance (🕐 indicator)
            scheduled_prompt_id=getattr(report, "scheduled_prompt_id", None),
        )
        # Summary counts (for auto-opening sidebar) — COUNT queries, not
        # len(relationship): loading report.queries would drag in every step
        # version's data via Query.steps' selectin cascade.
        from app.models.artifact import Artifact
        qc_result = await db.execute(
            select(func.count(Query.id)).where(
                Query.report_id == report.id,
                Query.deleted_at.is_(None),
            )
        )
        report_schema.query_count = qc_result.scalar() or 0
        ac_result = await db.execute(
            select(func.count(Artifact.id)).where(
                Artifact.report_id == report.id,
                Artifact.deleted_at.is_(None),
            )
        )
        report_schema.artifact_count = ac_result.scalar() or 0
        sp_result = await db.execute(
            select(func.count(ScheduledPrompt.id)).where(
                ScheduledPrompt.report_id == report.id,
                ScheduledPrompt.is_active.is_(True),
                ScheduledPrompt.deleted_at.is_(None),
            )
        )
        sp_count = sp_result.scalar() or 0
        report_schema.has_scheduled_prompts = sp_count > 0
        report_schema.scheduled_prompt_count = sp_count

        # Instruction count
        from app.models.instruction import Instruction
        from app.models.agent_execution import AgentExecution
        ic_result = await db.execute(
            select(func.count(Instruction.id))
            .join(AgentExecution, Instruction.agent_execution_id == AgentExecution.id)
            .where(
                AgentExecution.report_id == report.id,
                Instruction.deleted_at == None,
            )
        )
        report_schema.instruction_count = ic_result.scalar() or 0

        # Webhook count (active, non-deleted)
        from app.models.webhook import Webhook
        wh_result = await db.execute(
            select(func.count(Webhook.id)).where(
                Webhook.report_id == report.id,
                Webhook.deleted_at == None,
                Webhook.is_active == True,
            )
        )
        report_schema.webhook_count = wh_result.scalar() or 0

        # Enrich fork lineage
        await self._enrich_fork_lineage(db, report, report_schema)
        return report_schema

    async def create_report(self, db: AsyncSession, report_data: ReportCreate, current_user: User, organization: Organization) -> ReportSchema:
        # Extract widget data and remove it from report_data
        widget_data = report_data.widget
        del report_data.widget
        file_uuids = report_data.files or []
        del report_data.files
        data_source_ids = report_data.data_sources or []
        del report_data.data_sources

        # Create the report object
        report = Report(**report_data.dict())
        # Ensure a default theme is set for new reports
        if getattr(report, 'theme_name', None) in (None, ''):
            report.theme_name = 'default'
        report.user_id = current_user.id
        report.organization_id = organization.id
        await self._set_slug_for_report(db, report)
        db.add(report)
        
        # Flush to get report.id without committing (stays in same transaction)
        await db.flush()
        
        # Refresh to initialize relationships for async access
        await db.refresh(report, ["files", "data_sources"])

        # Create dashboard layout in the same transaction
        empty_layout = DashboardLayoutVersion(
            report_id=report.id,
            name="",
            version=1,
            is_active=True,
            theme_name=report.theme_name,
            theme_overrides=report.theme_overrides or {},
            blocks=[],
        )
        db.add(empty_layout)

        # Associate files only if there are any (skip unnecessary query)
        if file_uuids:
            file_result = await db.execute(select(File).filter(File.id.in_(file_uuids)))
            files = file_result.scalars().all()
            report.files.extend(files)

        # Associate data sources only if there are any (skip unnecessary query)
        if data_source_ids:
            ds_result = await db.execute(
                select(DataSource)
                .options(
                    selectinload(DataSource.connections),
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.files),
                )
                .filter(
                    DataSource.id.in_(data_source_ids),
                    DataSource.organization_id == organization.id,
                )
            )
            data_sources = ds_result.scalars().all()
            # Access gate: only attach data sources the creator is actually
            # allowed to see. Report attachments are a trusted snapshot consumed
            # downstream without re-checking, so an unfiltered attach here lets a
            # user pin a private source they have no access to and then query it.
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            data_sources = await ds_service.filter_user_visible_data_sources(
                db, list(data_sources), current_user, organization
            )
            # Don't attach user_required sources the creator can't actually use
            # (no personal creds, no system fallback) — they'd only break
            # create/inspect-data tools at run time. Mirrors the per-execution
            # filtering in build_rich_context / get_context.
            data_sources, _skipped_unconnected = await ds_service.filter_user_usable_data_sources(
                db, list(data_sources), current_user
            )
            report.data_sources.extend(data_sources)

            # Snapshot data-source-attached files into the report so they
            # flow through report.files into FilesContextBuilder and the
            # planner. Dedup against files explicitly passed in file_uuids.
            existing_ids = {str(f.id) for f in report.files}
            for ds in data_sources:
                for f in ds.files:
                    if str(f.id) not in existing_ids:
                        report.files.append(f)
                        existing_ids.add(str(f.id))

        # Single commit for the entire transaction
        await db.commit()
        
        # Final refresh to load all relationships needed by ReportSchema
        await db.refresh(report, ["user", "widgets", "dashboard_layout_versions"])

        # Fire-and-forget telemetry (non-blocking)
        try:
            await telemetry.capture(
                "report_created",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                    "count_files": len(file_uuids),
                    "count_data_sources": len(data_source_ids)
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        return ReportSchema.from_orm(report).copy(update={"user": UserSchema.from_orm(current_user)})

    async def update_report(self, db: AsyncSession, report_id: str, report_data: ReportUpdate, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report_data.title:
            report.title = report_data.title
        if report_data.status:
            report.status = report_data.status
            # Sync artifact_visibility with legacy status field
            if report_data.status == 'published':
                report.artifact_visibility = 'public'
            elif report_data.status == 'draft':
                report.artifact_visibility = 'none'
        # Persist theme updates if present in payload
        if hasattr(report_data, 'theme_name') and report_data.theme_name is not None:
            report.theme_name = report_data.theme_name
        if hasattr(report_data, 'theme_overrides') and report_data.theme_overrides is not None:
            report.theme_overrides = report_data.theme_overrides
        # Persist mode update if present in payload
        if hasattr(report_data, 'mode') and report_data.mode is not None:
            # Block training mode if enable_training_mode or allow_llm_see_data is disabled
            if report_data.mode == 'training':
                # Reuse the settings already eager-loaded on the request-scoped
                # organization instead of issuing another query.
                org_settings = organization.settings
                if org_settings:
                    # Check enable_training_mode flag
                    enable_training_mode = org_settings.get_config("enable_training_mode")
                    training_mode_disabled = False
                    if enable_training_mode is not None:
                        if hasattr(enable_training_mode, 'value'):
                            training_mode_disabled = enable_training_mode.value is False
                        elif isinstance(enable_training_mode, dict):
                            training_mode_disabled = enable_training_mode.get('value') is False
                    else:
                        # Default to disabled if not set
                        training_mode_disabled = True
                    if training_mode_disabled:
                        raise HTTPException(
                            status_code=400,
                            detail="Training mode is not enabled for this organization"
                        )
                # Per-agent authorization: entering training mode is the
                # agent-admin capability, not an org-wide one. The actor must be
                # able to manage instructions on EVERY agent (data source) the
                # report is attached to — via full_admin / org-level
                # manage_instructions, or a per-data_source `manage` grant (which
                # implies manage_instructions). A plain member (view only) on the
                # agent is denied, even if they manage some other agent.
                from app.core.permission_resolver import resolve_permissions
                if report_data.data_sources is not None:
                    training_ds_ids = [str(x) for x in report_data.data_sources]
                else:
                    training_ds_ids = [str(ds.id) for ds in (report.data_sources or [])]
                resolved = await resolve_permissions(
                    db, str(current_user.id), str(organization.id)
                )
                can_train = resolved.has_org_permission('manage_instructions') or (
                    bool(training_ds_ids)
                    and all(
                        resolved.has_resource_permission('data_source', ds, 'manage_instructions')
                        for ds in training_ds_ids
                    )
                )
                if not can_train:
                    raise HTTPException(
                        status_code=403,
                        detail="You need manage access on this agent to enter training mode",
                    )
            report.mode = report_data.mode
        # Persist report-level LLM override if present in payload.
        #   None          -> field omitted, leave the current value untouched
        #   "" (empty)    -> clear the override back to user/org default
        #   <model id>    -> set, after strict validation (the user setting it
        #                    must be able to use that model, mirroring the
        #                    per-user default write path)
        if hasattr(report_data, 'model_id') and report_data.model_id is not None:
            _old_model_id = report.model_id
            if report_data.model_id == "":
                report.model_id = None
            else:
                from app.services.llm_service import LLMService
                await LLMService().validate_model_for_user(
                    db, organization, current_user, report_data.model_id
                )
                report.model_id = report_data.model_id
            # Silent session event when the conversation's model override actually
            # changed. This is the explicit user pick (dropdown persist) — the
            # right llm_changed signal, unlike the Auto router varying per turn.
            if report.model_id != _old_model_id:
                try:
                    from app.models.llm_model import LLMModel
                    from app.services.session_event_service import SessionEventService
                    _new_model = await db.get(LLMModel, report.model_id) if report.model_id else None
                    await SessionEventService.emit_report_model_changed(
                        db, report=report, old_model_id=_old_model_id,
                        new_model=_new_model, user=current_user, commit=False,
                    )
                except Exception:
                    pass
        # Replace data_sources associations if provided
        if hasattr(report_data, 'data_sources') and report_data.data_sources is not None:
            # Snapshot the user-VISIBLE scope before the change — never name a
            # source the acting user can't access.
            _old_visible = await self._report_visible_data_source_names(db, report, current_user, organization)
            await self.set_data_sources_for_report(db, report, report_data.data_sources, current_user, organization)
            try:
                # Only emit for genuine mid-conversation edits: a brand-new
                # report's setup/hydration PUT (which fires before any user turn)
                # must not log a scope change. Compare against the report's ACTUAL
                # (already access-filtered) new associations so removals the
                # server's access gate made are never attributed to the user, and
                # only user-visible sources are ever named.
                if await self._report_has_user_turn(db, report.id):
                    _new = await self._report_data_source_names(db, report.id)
                    _old_ids, _new_ids = set(_old_visible), set(_new)
                    _added = [_new[i] for i in (_new_ids - _old_ids)]
                    _removed = [_old_visible[i] for i in (_old_ids - _new_ids)]
                    if _added or _removed:
                        from app.services.session_event_service import SessionEventService
                        from app.ai.context.session_events import AGENT_SCOPE_CHANGED
                        await SessionEventService.emit_safe(
                            db, report=report, kind=AGENT_SCOPE_CHANGED, user=current_user, commit=False,
                            meta={"added": _added, "removed": _removed, "kind": "data_source"},
                        )
            except Exception:
                pass

        # Agent focus: the subset of attached agents whose FULL schema renders.
        #   None  -> field omitted, leave the current focus untouched
        #   []    -> clear the focus (revert to auto roster/seed behavior)
        #   [ids] -> set focus; ids are intersected with the currently-attached
        #            agents (an id that isn't attached is silently dropped rather
        #            than erroring, so a stale UI selection never breaks the save)
        if hasattr(report_data, 'focused_data_source_ids') and report_data.focused_data_source_ids is not None:
            attached_ids = {str(ds.id) for ds in (report.data_sources or [])}
            focus = [str(x) for x in report_data.focused_data_source_ids if str(x) in attached_ids]
            report.focused_data_source_ids = focus or None
            db.add(report)

        #await self._set_slug_for_report(db, report)

        await db.commit()
        await db.refresh(report)
        # Telemetry: report publish status changed
        try:
            await telemetry.capture(
                "report_published" if report.status == "published" else "report_unpublished",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.updated",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "status": report.status},
            )
        except Exception:
            pass

        return report

    async def _rerun_target_steps(self, db: AsyncSession, query_ids: list[str]) -> tuple[list[tuple[str, str]], int]:
        """Resolve which step a report rerun must re-execute for each query:
        the same step the dashboard renders (mirrors
        query_service.get_default_step_for_query, which must stay in sync) —
        the query's default step, else the latest success step, else the
        latest step by widget. Returns (deduped (step_id, code) tuples,
        count of queries that resolved to no step at all) without hydrating
        step payloads; the two common lookups are batched so the round-trip
        count doesn't grow with dashboard size."""
        if not query_ids:
            return [], 0
        query_rows = (await db.execute(
            select(Query.id, Query.default_step_id, Query.widget_id)
            .where(Query.id.in_([str(q) for q in query_ids]))
        )).all()

        default_ids = [r[1] for r in query_rows if r[1]]
        steps_by_id: dict[str, str] = {}
        if default_ids:
            for sid, code in (await db.execute(
                select(Step.id, Step.code).where(Step.id.in_(default_ids))
            )).all():
                steps_by_id[str(sid)] = code

        targets: list[tuple[str, str]] = []
        seen_steps: set[str] = set()
        unresolved = len(query_ids) - len(query_rows)  # ids with no Query row

        def _add(step_id, code) -> None:
            step_id = str(step_id)
            # Two queries can resolve to the same step (shared widget with no
            # default_step_id); run it once, not once per query.
            if step_id not in seen_steps:
                seen_steps.add(step_id)
                targets.append((step_id, code))

        for qid, default_step_id, widget_id in query_rows:
            if default_step_id and str(default_step_id) in steps_by_id:
                _add(default_step_id, steps_by_id[str(default_step_id)])
                continue
            # Rare fallback (no or dangling default step): latest success
            # step for the query's widget, else latest step.
            step_row = None
            for only_success in (True, False):
                stmt = select(Step.id, Step.code).where(Step.widget_id == widget_id)
                if only_success:
                    stmt = stmt.where(Step.status == 'success')
                step_row = (await db.execute(stmt.order_by(Step.created_at.desc()).limit(1))).first()
                if step_row:
                    break
            if step_row:
                _add(step_row[0], step_row[1])
            else:
                unresolved += 1
                logger.warning(f"No step found for query {qid}; counting as failed")
        return targets, unresolved

    async def rerun_report_steps(
        self,
        db: AsyncSession,
        report_id: str,
        current_user: User,
        organization: Organization,
        artifact_id: str | None = None,
        notify_subscribers: bool = False,
    ) -> dict:
        logger.info(f"Executing report rerun for report_id: {report_id}")
        # Load the report without the mapper-level selectin cascade — a rerun
        # only needs identity/notification columns plus the data sources and
        # files that step code executes against. The full graph (every step
        # version's data JSON, completions, artifact versions) must stay cold.
        result = await db.execute(
            select(Report)
            .options(
                lazyload("*"),
                selectinload(Report.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.connections).options(lazyload("*")),
                ),
                selectinload(Report.files).options(lazyload("*")),
            )
            .filter(Report.id == report_id)
        )
        report = result.unique().scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # What a report renders is defined by its artifact: the requested one
        # (interactive refresh of a selected dashboard) or the latest
        # non-deleted one. Superseded artifact versions live on as rows, so
        # collecting across all of them would rerun queries the dashboard no
        # longer shows. (Dashboard-layout visualization blocks are deprecated
        # and no longer consulted.)
        from app.models.artifact import Artifact
        artifact_stmt = (
            select(Artifact.content)
            .where(
                Artifact.report_id == str(report_id),
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        if artifact_id:
            # Explicit target may be any mode — docs refresh their embedded vizs too.
            artifact_stmt = artifact_stmt.where(Artifact.id == str(artifact_id))
        else:
            # Default rerun follows the latest DASHBOARD; a newer doc must not
            # silently change which queries a report rerun refreshes.
            artifact_stmt = artifact_stmt.where(Artifact.mode.in_(("page", "slides")))
        artifact_row = (await db.execute(artifact_stmt)).first()
        content = artifact_row[0] if artifact_row else None
        viz_ids = list(dict.fromkeys(
            str(v) for v in ((content or {}).get("visualization_ids") or []) if v
        ))

        query_ids: list[str] = []
        if viz_ids:
            viz_result = await db.execute(
                select(Visualization.query_id).where(
                    Visualization.id.in_(viz_ids),
                    Visualization.deleted_at.is_(None),
                )
            )
            query_ids = list(dict.fromkeys(str(q) for (q,) in viz_result.all() if q))

        steps_total = 0
        steps_succeeded = 0
        steps_failed = 0

        if query_ids:
            # Build data-source clients once for the whole run instead of per
            # step — client construction resolves credentials (and for ODBC
            # sources pays a connection handshake) per data source.
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            db_clients = {}
            for data_source in report.data_sources:
                try:
                    ds_clients = await ds_service.construct_clients(db, data_source, current_user=current_user)
                    db_clients.update(ds_clients)
                except Exception as e:
                    # Steps that need this source fail (and are counted) at
                    # execution time; other sources' steps still run.
                    logger.warning(f"Failed to construct clients for data source {data_source.id}: {e}; continuing")

            # Resolve org context once — every step shares it.
            org_settings = await organization.get_settings(db) if organization else None

            # Queries that resolved to no runnable step still count: their
            # charts cannot refresh, and a green "refreshed" over stale data
            # is the bug this rerun rewrite exists to fix.
            targets, unrunnable = await self._rerun_target_steps(db, query_ids)
            for step_id, code in targets:
                if not code or not str(code).strip():
                    logger.warning(f"Step code is empty for step {step_id}; counting as failed")
                    steps_total += 1
                    steps_failed += 1
                    continue
                steps_total += 1
                try:
                    logger.info(f"Rerunning step {step_id} (report {report_id})")
                    # Run as the report run's user (interactive caller, or the
                    # schedule creator for scheduled runs) so user_required
                    # connections resolve their creds / owner-admin fallback.
                    await self.widget_service.step_service.rerun_step(
                        db, step_id, current_user=current_user,
                        report=report, db_clients=db_clients,
                        organization=organization, organization_settings=org_settings,
                    )
                    steps_succeeded += 1
                except Exception as e:
                    steps_failed += 1
                    logger.warning(f"Failed to rerun step {step_id}: {e}; continuing")
            if unrunnable > 0:
                steps_total += unrunnable
                steps_failed += unrunnable
        else:
            # Legacy fallback for pre-artifact reports: rerun last step for
            # each published widget
            published_widgets = await self.widget_service.get_published_widgets_for_report(db, report_id)
            for widget in published_widgets:
                steps_total += 1
                try:
                    logger.info(f"Running widget {widget.id} for report {report_id}")
                    await self.widget_service.run_widget_step(db, widget, current_user, organization)
                    steps_succeeded += 1
                except Exception as e:
                    steps_failed += 1
                    logger.warning(f"Failed to run widget {widget.id}: {e}; continuing")
                    continue

        # Update last_run_at timestamp on the already-loaded ORM model
        report.last_run_at = datetime.utcnow()
        await db.commit()

        # Regenerate the artifact thumbnail in background — only when some
        # step actually produced fresh data (it boots a headless browser).
        if steps_succeeded > 0:
            from app.services.thumbnail_service import ThumbnailService
            thumbnail_service = ThumbnailService()
            asyncio.create_task(thumbnail_service.regenerate_for_report(report_id))

        # Notify subscribers — scheduled runs only (interactive Refresh must
        # not spam "your scheduled report ran"), and only when something ran.
        if notify_subscribers and steps_succeeded > 0 and report.notification_subscribers:
            from app.services.notification_service import notification_service
            from app.settings.config import settings as app_settings
            from app.dependencies import _locale_from_org
            report_url = f"{app_settings.bow_config.base_url}/r/{report_id}"
            # notify-first: durable in-app row for user subscribers (collapsed per
            # report so repeated runs refresh one entry). Email follows.
            try:
                from app.services.inbox_service import inbox_service
                user_ids = [str(s.get("id")) for s in report.notification_subscribers
                            if s.get("type") == "user" and s.get("id")]
                if user_ids:
                    await inbox_service.notify_users(
                        db, organization_id=str(report.organization_id), user_ids=user_ids,
                        source="schedule", type="scheduled_run",
                        title=f'"{report.title or "Untitled"}" ran',
                        body="Your scheduled report ran.",
                        link=f"/reports/{report_id}",
                        subject={"kind": "report", "report_id": str(report_id)},
                        group_key=f"schedule:{report_id}",
                    )
            except Exception:
                logger.warning("scheduled-report in-app notification failed", exc_info=True)
            asyncio.create_task(
                notification_service.send_scheduled_report_results(
                    report_id=report_id,
                    report_title=report.title or "Untitled Report",
                    subscribers=report.notification_subscribers,
                    report_url=report_url,
                    locale=_locale_from_org(organization),
                )
            )

        logger.info(
            f"Completed report rerun for report_id: {report_id} "
            f"({steps_succeeded}/{steps_total} steps succeeded, {steps_failed} failed)"
        )
        if steps_total == 0:
            message = "No report steps to rerun"
        elif steps_failed == 0:
            message = f"Reran {steps_succeeded} report step{'s' if steps_succeeded != 1 else ''}"
        else:
            message = f"Reran {steps_succeeded}/{steps_total} report steps ({steps_failed} failed)"
        return {
            "message": message,
            "steps_total": steps_total,
            "steps_succeeded": steps_succeeded,
            "steps_failed": steps_failed,
            "last_run_at": report.last_run_at,
        }

    async def _delete_scheduled_prompts_for_reports(self, db: AsyncSession, report_ids: list[str]) -> None:
        """Soft-delete any active scheduled prompts attached to the given reports and
        remove their APScheduler jobs. Used when reports are archived so that nested
        scheduled tasks no longer fire."""
        if not report_ids:
            return

        result = await db.execute(
            select(ScheduledPrompt)
            .filter(ScheduledPrompt.report_id.in_(report_ids))
            .filter(ScheduledPrompt.deleted_at == None)
        )
        scheduled_prompts = result.scalars().all()
        if not scheduled_prompts:
            return

        now = datetime.utcnow()
        for sp in scheduled_prompts:
            sp.deleted_at = now
            try:
                scheduler.remove_job(job_id=f"scheduled_prompt_{sp.id}")
            except JobLookupError:
                pass
            except Exception:
                logger.warning(f"Failed to remove scheduler job for scheduled prompt {sp.id}", exc_info=True)

        logger.info(f"Deleted {len(scheduled_prompts)} scheduled prompt(s) for archived report(s): {report_ids}")

    async def archive_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        report.status = 'archived'
        await self._delete_scheduled_prompts_for_reports(db, [str(report.id)])
        await db.commit()
        await db.refresh(report)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.archived",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title},
            )
        except Exception:
            pass

        return report

    async def publish_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report.status == 'published':
            report.status = 'draft'
            report.artifact_visibility = 'none'
        else:
            report.status = 'published'
            report.artifact_visibility = 'public'

        await db.commit()
        await db.refresh(report)
        # Telemetry: report publish status changed
        try:
            await telemetry.capture(
                "report_published" if report.status == "published" else "report_unpublished",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.published" if report.status == "published" else "report.unpublished",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "status": report.status},
            )
        except Exception:
            pass

        return report

    async def set_report_star(
        self,
        db: AsyncSession,
        report_id: str,
        current_user: User,
        organization: Organization,
        starred: bool,
    ) -> dict:
        """Star or unstar a report for the current user.

        Starring is per-user, so each user maintains their own set of starred
        reports independently. A report can be starred by any user who can view
        it (including reports shared with them read-only). Returns the resulting
        starred state.
        """
        from app.models.report_star import ReportStar

        result = await db.execute(
            select(Report).filter(
                Report.id == report_id,
                Report.organization_id == organization.id,
                Report.report_type == 'regular',
            )
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Look up any existing star row, including soft-deleted ones, so we
        # reuse it rather than violating the (report_id, user_id) uniqueness.
        existing_result = await db.execute(
            select(ReportStar).filter(
                ReportStar.report_id == report_id,
                ReportStar.user_id == current_user.id,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if starred:
            if existing is None:
                db.add(ReportStar(report_id=report_id, user_id=current_user.id))
            elif existing.deleted_at is not None:
                existing.deleted_at = None
        else:
            if existing is not None and existing.deleted_at is None:
                existing.deleted_at = datetime.utcnow()

        await db.commit()

        return {"id": str(report_id), "is_starred": starred}

    async def get_public_report(self, db: AsyncSession, report_id: str, user=None) -> ReportSchema:
        # Load only what ReportSchema serializes. Report's mapper-level
        # lazy="selectin" relationships would otherwise hydrate the entire
        # report graph — every step version's data JSON, every artifact
        # version, the chat history — just to render report metadata.
        result = await db.execute(
            select(Report)
            .options(
                lazyload("*"),
                # UserSchema serializes external_user_mappings — load it, the
                # top-level wildcard turned User's own eager defaults off too.
                selectinload(Report.user).selectinload(User.external_user_mappings),
                selectinload(Report.external_platform),
                selectinload(Report.widgets).options(lazyload("*")),
                selectinload(Report.dashboard_layout_versions).options(lazyload("*")),
                selectinload(Report.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.connections).options(lazyload("*")),
                ),
            )
            .filter(Report.id == report_id)
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Check artifact visibility (replaces old status == 'published' check)
        await self._check_visibility(db, report, 'artifact_visibility', user)
        
        schema = ReportSchema.from_orm(report)
        # Enrich fork lineage
        await self._enrich_fork_lineage(db, report, schema)
        # Attach minimal general settings from organization settings
        try:
            from app.models.organization_settings import OrganizationSettings
            settings_result = await db.execute(select(OrganizationSettings).filter(OrganizationSettings.organization_id == report.organization_id))
            settings = settings_result.scalar_one_or_none()
            if settings and isinstance(settings.config, dict):
                general = settings.config.get("general", {}) or {}
                schema.general = ReportSchema.PublicGeneralSettings(
                    ai_analyst_name=general.get("ai_analyst_name", "AI Analyst"),
                    bow_credit=general.get("bow_credit", True),
                    icon_url=general.get("icon_url")
                )
        except Exception:
            pass

        return schema

    async def get_public_layouts(self, db: AsyncSession, report_id: str, user=None):
        # Ensure report exists and has artifact visibility.
        # lazyload("*") — the visibility check needs the report row only, not
        # the selectin cascade (all step data, artifacts, completions, ...).
        result = await db.execute(
            select(Report).options(lazyload("*"))
            .where(Report.id == report_id).where(Report.report_type == 'regular')
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        rows = await db.execute(
            select(DashboardLayoutVersion).options(lazyload("*"))
            .where(DashboardLayoutVersion.report_id == report_id).order_by(
                DashboardLayoutVersion.created_at.asc()
            )
        )
        layouts = rows.scalars().all()

        from app.schemas.dashboard_layout_version_schema import DashboardLayoutVersionSchema
        return [DashboardLayoutVersionSchema.from_orm(l) for l in layouts]

    async def get_public_queries(self, db: AsyncSession, report_id: str, artifact_id: str | None = None, user=None):
        """Get queries for a shared report.

        If artifact_id is provided, only returns queries for visualizations used by that artifact.
        """
        # Verify report exists and check artifact visibility.
        # lazyload("*") everywhere below — these endpoints only serve small
        # payloads; without it the mapper-level lazy="selectin" cascade
        # hydrates every step version's data JSON on each request.
        result = await db.execute(
            select(Report).options(lazyload("*")).where(Report.id == report_id)
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # If artifact_id provided, filter to only queries used by that artifact
        query_ids_filter = None
        if artifact_id:
            from app.models.artifact import Artifact
            artifact_result = await db.execute(
                select(Artifact).options(lazyload("*")).where(
                    Artifact.id == artifact_id,
                    Artifact.report_id == report_id,
                    Artifact.deleted_at.is_(None)
                )
            )
            artifact = artifact_result.scalar_one_or_none()
            if artifact and artifact.content:
                visualization_ids = artifact.content.get("visualization_ids", [])
                if visualization_ids:
                    # Get query_ids from visualizations
                    viz_result = await db.execute(
                        select(Visualization.query_id).where(Visualization.id.in_(visualization_ids))
                    )
                    query_ids_filter = [row[0] for row in viz_result.all() if row[0]]

        # Fetch queries that have a successful step, eagerly load visualizations.
        # lazyload("*") stops Query.steps (every version, full data) / widget /
        # report from cascading; PublicQuerySchema needs none of them.
        query_stmt = (
            select(Query)
            .join(Step, Step.id == Query.default_step_id)
            .options(
                lazyload("*"),
                selectinload(Query.visualizations).options(lazyload("*")),
            )
            .where(Query.report_id == report_id, Step.status == 'success')
        )

        # Apply artifact filter if present
        if query_ids_filter is not None:
            query_stmt = query_stmt.where(Query.id.in_(query_ids_filter))

        queries_result = await db.execute(query_stmt)
        queries = queries_result.scalars().all()

        from app.schemas.query_schema import PublicQuerySchema
        return [PublicQuerySchema.model_validate(q) for q in queries]

    async def get_public_step(self, db: AsyncSession, report_id: str, query_id: str, user=None):
        """Get the default step for a query in a shared report."""
        # Verify report exists and check artifact visibility.
        # lazyload("*") on every select here: PublicStepSchema serves the
        # step's own columns; without it the selectin cascade re-hydrates
        # the whole report graph (all step versions' data) per request.
        result = await db.execute(
            select(Report).options(lazyload("*")).where(Report.id == report_id)
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # Fetch query and verify it belongs to this report
        query_result = await db.execute(
            select(Query).options(lazyload("*"))
            .where(Query.id == query_id, Query.report_id == report_id)
        )
        query = query_result.scalar_one_or_none()
        if not query:
            raise HTTPException(status_code=404, detail="Not found")

        # Get the default step (or latest successful step if no default)
        step = None
        if query.default_step_id:
            step_result = await db.execute(
                select(Step).options(lazyload("*"))
                .where(Step.id == query.default_step_id, Step.status == 'success')
            )
            step = step_result.scalar_one_or_none()

        if not step:
            # Fallback to latest successful step
            step_result = await db.execute(
                select(Step).options(lazyload("*"))
                .where(Step.query_id == query_id, Step.status == 'success')
                .order_by(Step.created_at.desc())
                .limit(1)
            )
            step = step_result.scalar_one_or_none()

        if not step:
            raise HTTPException(status_code=404, detail="Not found")

        from app.schemas.step_schema import PublicStepSchema
        # Convert view to dict if it's not already
        view_dict = step.view if isinstance(step.view, dict) else (step.view.dict() if step.view else {})
        return PublicStepSchema(
            id=step.id,
            title=step.title,
            type=step.type,
            code=step.code,
            data_model=step.data_model or {},
            data=step.data or {},
            view=view_dict,
        )

    async def get_public_artifacts(self, db: AsyncSession, report_id: str, user=None):
        """List artifacts for a shared report."""
        # Verify report exists and check artifact visibility.
        # lazyload("*"): this endpoint returns artifact metadata only; the
        # visibility check needs one report row, and Artifact.report would
        # otherwise selectin-cascade the whole graph back in.
        result = await db.execute(
            select(Report).options(lazyload("*")).where(Report.id == report_id)
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # Fetch artifacts for this report
        from app.models.artifact import Artifact
        artifacts_result = await db.execute(
            select(Artifact).options(lazyload("*"))
            .where(Artifact.report_id == report_id, Artifact.deleted_at.is_(None))
            .order_by(Artifact.created_at.desc())
        )
        artifacts = artifacts_result.scalars().all()

        from app.schemas.artifact_schema import ArtifactListSchema
        return [ArtifactListSchema.model_validate(a) for a in artifacts]

    async def get_public_artifact(self, db: AsyncSession, report_id: str, artifact_id: str, user=None):
        """Get a specific artifact for a shared report."""
        # Verify report exists and check artifact visibility (row only — see
        # get_public_artifacts).
        result = await db.execute(
            select(Report).options(lazyload("*")).where(Report.id == report_id)
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # Fetch the artifact and verify it belongs to this report
        from app.models.artifact import Artifact
        artifact_result = await db.execute(
            select(Artifact).options(lazyload("*")).where(
                Artifact.id == artifact_id,
                Artifact.report_id == report_id,
                Artifact.deleted_at.is_(None)
            )
        )
        artifact = artifact_result.scalar_one_or_none()
        if not artifact:
            raise HTTPException(status_code=404, detail="Not found")

        from app.schemas.artifact_schema import ArtifactSchema
        return ArtifactSchema.model_validate(artifact)

    async def get_reports(
        self,
        db: AsyncSession,
        current_user: User,
        organization: Organization,
        page: int = 1,
        limit: int = 10,
        filter: str = "my",
        search: str | None = None,
        scheduled: bool | None = None,
        status: str | None = None,
        data_source_id: str | None = None,
        mode: str | None = None,
        has_artifacts: str | None = None,
        view: str | None = None,
        artifact_mode: str | None = None,
    ):
        with tracer.start_as_current_span("get_reports") as span:

            span.set_attribute("user.id", str(current_user.name))
            span.set_attribute("org.id", str(organization.name))
            # Calculate offset
            offset = (page - 1) * limit

            # Build filter conditions based on filter parameter
            base_conditions = [
                Report.organization_id == organization.id,
                Report.status != 'archived',
            ]

            base_conditions.append(Report.report_type == 'regular')

            # Optional filter by mode (chat/deep/training)
            if mode and mode in ('chat', 'deep', 'training'):
                base_conditions.append(Report.mode == mode)

            # Shared visibility: reports the user has been explicitly shared with
            from app.models.report_share import ReportShare
            shared_with_user = Report.id.in_(
                select(ReportShare.report_id).where(
                    ReportShare.user_id == current_user.id,
                    ReportShare.deleted_at.is_(None),
                )
            )
            # A report is "visible" if it has any non-none visibility and
            # either it's public/internal or the user is in the share list
            visible_to_user = or_(
                Report.artifact_visibility.in_(['public', 'internal']),
                Report.conversation_visibility.in_(['public', 'internal']),
                shared_with_user,
            )

            if filter == "my":
                # Show only reports owned by current user
                base_conditions.append(Report.user_id == current_user.id)
            elif filter == "shared":
                # Show reports shared with the user but NOT owned by them
                base_conditions.append(visible_to_user)
                base_conditions.append(Report.user_id != current_user.id)
            elif filter == "published":
                # Legacy: show shared/published reports visible to the user
                base_conditions.append(visible_to_user)
            else:
                # Default: show reports user can view (owned OR visible)
                base_conditions.append(
                    or_(Report.user_id == current_user.id, visible_to_user)
                )

            # Optional search on report title and completion content
            if search:
                from app.models.completion import Completion
                base_conditions.append(
                    or_(
                        Report.title.ilike(f"%{search}%"),
                        Report.id.in_(
                            select(Completion.report_id).where(
                                or_(
                                    cast(Completion.prompt, SAString).ilike(f"%{search}%"),
                                    cast(Completion.completion, SAString).ilike(f"%{search}%"),
                                )
                            )
                        )
                    )
                )

            # Optional filter by scheduled status (report-level cron OR active scheduled prompts)
            if scheduled is True:
                from app.models.scheduled_prompt import ScheduledPrompt
                base_conditions.append(
                    or_(
                        Report.cron_schedule.isnot(None),
                        Report.id.in_(
                            select(ScheduledPrompt.report_id).where(
                                ScheduledPrompt.is_active == True,
                                ScheduledPrompt.deleted_at == None,
                            )
                        ),
                    )
                )
            elif scheduled is False:
                from app.models.scheduled_prompt import ScheduledPrompt
                base_conditions.append(Report.cron_schedule.is_(None))
                base_conditions.append(
                    ~Report.id.in_(
                        select(ScheduledPrompt.report_id).where(
                            ScheduledPrompt.is_active == True,
                            ScheduledPrompt.deleted_at == None,
                        )
                    )
                )

            # Optional filter by report status (draft/published)
            if status in ('draft', 'published'):
                base_conditions.append(Report.status == status)

            # Optional filter by data source
            if data_source_id:
                base_conditions.append(
                    Report.id.in_(
                        select(report_data_source_association.c.report_id).where(
                            report_data_source_association.c.data_source_id == data_source_id
                        )
                    )
                )

            # Optional filter by artifact presence
            if has_artifacts == 'yes':
                from app.models.artifact import Artifact
                base_conditions.append(
                    Report.id.in_(
                        select(Artifact.report_id).where(Artifact.report_id.isnot(None))
                    )
                )
            elif has_artifacts == 'no':
                from app.models.artifact import Artifact
                base_conditions.append(
                    ~Report.id.in_(
                        select(Artifact.report_id).where(Artifact.report_id.isnot(None))
                    )
                )

            # Optional filter by artifact mode ('page' / 'slides' / 'doc')
            if artifact_mode in ("page", "slides", "doc"):
                from app.models.artifact import Artifact
                base_conditions.append(
                    Report.id.in_(
                        select(Artifact.report_id).where(
                            Artifact.mode == artifact_mode,
                            Artifact.deleted_at.is_(None),
                        )
                    )
                )

            # Per-user starred reports: surface starred reports first.
            # Computed in SQL (not client-side) because the list is paginated.
            from app.models.report_star import ReportStar
            starred_report_ids_subq = select(ReportStar.report_id).where(
                ReportStar.user_id == current_user.id,
                ReportStar.deleted_at.is_(None),
            )
            is_starred_order = case(
                (Report.id.in_(starred_report_ids_subq), 1),
                else_=0,
            )

            # Base query for filtering
            base_query = select(Report).where(*base_conditions)

            # Count total items
            count_query = select(func.count(Report.id)).where(*base_conditions)

            total_result = await db.execute(count_query)
            total = total_result.scalar()

            # ── Minimal view (sidebar / recent-reports on every page) ──────────
            # The sidebar only renders title, star and the artifact-mode icon. It
            # does NOT use widgets/data_sources/queries/completions. Serving the
            # full ReportSchema here means hydrating the whole Report
            # lazy="selectin" object graph (widgets/queries/steps/completions/…)
            # for every row — a per-page cascade. The minimal path noloads every
            # relationship except `user`, and derives artifact_modes from one
            # batched query, so it never touches that graph.
            if view == "minimal":
                from app.models.artifact import Artifact
                from app.schemas.report_schema import PaginationMeta, ReportListResponse
                m_query = (
                    base_query.options(noload("*"), selectinload(Report.user))
                    .order_by(
                        is_starred_order.desc(),
                        func.coalesce(Report.last_activity_at, Report.created_at).desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
                reports = (await db.execute(m_query)).scalars().all()
                report_ids = [str(r.id) for r in reports]

                starred_ids: set[str] = set()
                modes_by_report: dict[str, set[str]] = {}
                if report_ids:
                    starred_ids = {
                        str(row[0]) for row in (await db.execute(
                            select(ReportStar.report_id).where(
                                ReportStar.report_id.in_(report_ids),
                                ReportStar.user_id == current_user.id,
                                ReportStar.deleted_at.is_(None),
                            )
                        )).all()
                    }
                    for rid, am_mode in (await db.execute(
                        select(Artifact.report_id, Artifact.mode).where(
                            Artifact.report_id.in_(report_ids),
                            Artifact.mode.isnot(None),
                        )
                    )).all():
                        modes_by_report.setdefault(str(rid), set()).add(am_mode)

                report_schemas = []
                for report in reports:
                    rs = ReportSchema.from_orm(report)  # heavy fields stay [] / None (noload)
                    rs.user = UserSchema.from_orm(report.user)
                    rs.is_starred = str(report.id) in starred_ids
                    rs.artifact_modes = list(modes_by_report.get(str(report.id), set()))
                    report_schemas.append(rs)

                total_pages = (total + limit - 1) // limit
                meta = PaginationMeta(
                    total=total, page=page, limit=limit, total_pages=total_pages,
                    has_next=page < total_pages, has_prev=page > 1,
                )
                span.add_event("get_reports done (minimal)")
                return ReportListResponse(reports=report_schemas, meta=meta)

            # Suppress DataSource's lazy="selectin" cascade (reports →
            # widgets/queries/completions/…) that would otherwise fire per
            # loaded Report.data_sources. We don't lazyload at Report level
            # because that propagates into Report.user and breaks
            # UserSchema.external_user_mappings serialization.
            query = base_query.options(
                selectinload(Report.user),
                selectinload(Report.widgets),
                selectinload(Report.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.connections).options(lazyload("*")),
                ),
                selectinload(Report.artifacts),
                # These relationships are NOT fields on ReportSchema, so they are
                # never serialized — but the model declares them lazy="selectin",
                # so without this they'd each fire a batched query on every list
                # load (completions especially: the full conversation history for
                # all rows). noload them; output is byte-identical, the cascade
                # is gone. (Per-relationship noload, not lazyload("*"), so it
                # doesn't propagate into Report.user and break UserSchema.)
                noload(Report.completions),
                noload(Report.visualizations),
                noload(Report.text_widgets),
                noload(Report.files),
                noload(Report.shares),
                noload(Report.stars),
                # Only counted (query_count / scheduled_prompt_count), never
                # serialized — hydrating every Query/ScheduledPrompt row just to
                # call len() is wasteful. noload here; counts come from the
                # batched GROUP BY queries below.
                noload(Report.queries),
                noload(Report.scheduled_prompts),
            ).order_by(
                is_starred_order.desc(),
                # Sort by real conversation activity (new message / finalized agent
                # turn), not creation time. Coalesce so reports that predate the
                # column — or have no activity yet — fall back to created_at.
                func.coalesce(Report.last_activity_at, Report.created_at).desc(),
            ).offset(offset).limit(limit)

            result = await db.execute(query)
            span.add_event("query executed")
            reports = result.scalars().all()
            span.add_event("query result loaded into memory ")

            # Batch-fetch instruction counts for training reports
            report_ids = [str(r.id) for r in reports]

            # Batch-fetch which of the loaded reports the user has starred
            starred_ids: set[str] = set()
            if report_ids:
                starred_result = await db.execute(
                    select(ReportStar.report_id).where(
                        ReportStar.report_id.in_(report_ids),
                        ReportStar.user_id == current_user.id,
                        ReportStar.deleted_at.is_(None),
                    )
                )
                starred_ids = {str(row[0]) for row in starred_result.all()}

            instruction_counts: dict[str, int] = {}
            if report_ids:
                from app.models.instruction import Instruction
                from app.models.agent_execution import AgentExecution
                ic_query = (
                    select(
                        AgentExecution.report_id,
                        func.count(Instruction.id),
                    )
                    .join(AgentExecution, Instruction.agent_execution_id == AgentExecution.id)
                    .where(
                        AgentExecution.report_id.in_(report_ids),
                        Instruction.deleted_at == None,
                    )
                    .group_by(AgentExecution.report_id)
                )
                ic_result = await db.execute(ic_query)
                instruction_counts = {str(row[0]): row[1] for row in ic_result.all()}

            webhook_counts: dict[str, int] = {}
            if report_ids:
                from app.models.webhook import Webhook
                wh_query = (
                    select(Webhook.report_id, func.count(Webhook.id))
                    .where(
                        Webhook.report_id.in_(report_ids),
                        Webhook.deleted_at == None,
                        Webhook.is_active == True,
                    )
                    .group_by(Webhook.report_id)
                )
                wh_result = await db.execute(wh_query)
                webhook_counts = {str(row[0]): row[1] for row in wh_result.all()}

            # Batch query_count (matches len(report.queries) — the relationship
            # has no soft-delete filter, so count every row for the report).
            query_counts: dict[str, int] = {}
            if report_ids:
                from app.models.query import Query
                qc_result = await db.execute(
                    select(Query.report_id, func.count(Query.id))
                    .where(Query.report_id.in_(report_ids))
                    .group_by(Query.report_id)
                )
                query_counts = {str(row[0]): row[1] for row in qc_result.all()}

            # Batch active scheduled-prompt count (is_active AND not deleted).
            active_sp_counts: dict[str, int] = {}
            if report_ids:
                from app.models.scheduled_prompt import ScheduledPrompt
                sp_result = await db.execute(
                    select(ScheduledPrompt.report_id, func.count(ScheduledPrompt.id))
                    .where(
                        ScheduledPrompt.report_id.in_(report_ids),
                        ScheduledPrompt.is_active == True,
                        ScheduledPrompt.deleted_at.is_(None),
                    )
                    .group_by(ScheduledPrompt.report_id)
                )
                active_sp_counts = {str(row[0]): row[1] for row in sp_result.all()}

            # Convert to schemas
            # Lifecycle-filter each report's attached data sources (same rules
            # as get_report); resolve the caller's publish visibility once for
            # the whole page instead of per report.
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            publish_visibility = await ds_service._publish_visibility(db, current_user, organization)
            report_schemas = []
            for report in reports:
                report_schema = ReportSchema.from_orm(report)
                report_schema.user = UserSchema.from_orm(report.user)

                live_data_sources = await ds_service.filter_live_data_sources(
                    db, report.data_sources, current_user, organization,
                    visibility=publish_visibility,
                )
                # Manually build data_sources with type computed from connection
                report_schema.data_sources = [
                    DataSourceReportSchema(
                        id=str(ds.id),
                        name=ds.name,
                        organization_id=str(ds.organization_id),
                        created_at=ds.created_at,
                        updated_at=ds.updated_at,
                        context=ds.context,
                        description=ds.description,
                        summary=ds.summary,
                        is_active=ds.is_active,
                        is_public=ds.is_public,
                        owner_user_id=str(ds.owner_user_id) if ds.owner_user_id else None,
                        use_llm_sync=ds.use_llm_sync,
                        publish_status=getattr(ds, "publish_status", "published") or "published",
                        reliability_status=getattr(ds, "reliability_status", "training") or "training",
                        icon=getattr(ds, "icon", None),
                        # Compute type from first connection
                        type=ds.connections[0].type if ds.connections else None,
                    )
                    for ds in live_data_sources
                ]

                # Summary counts (from batched GROUP BY queries above)
                report_schema.query_count = query_counts.get(str(report.id), 0)
                report_schema.artifact_count = len(report.artifacts or [])

                # Active scheduled prompts (from batch query)
                active_sp_count = active_sp_counts.get(str(report.id), 0)
                report_schema.has_scheduled_prompts = active_sp_count > 0
                report_schema.scheduled_prompt_count = active_sp_count

                # Instruction count (from batch query)
                report_schema.instruction_count = instruction_counts.get(str(report.id), 0)

                # Webhook count (from batch query)
                report_schema.webhook_count = webhook_counts.get(str(report.id), 0)

                # Starred state for the current user
                report_schema.is_starred = str(report.id) in starred_ids

                # Compute unique artifact modes for this report
                report_schema.artifact_modes = list(set(
                    a.mode for a in (report.artifacts or []) if a.mode
                ))

                # Get thumbnail URL from latest artifact (prefer page mode)
                if report.artifacts:
                    sorted_artifacts = sorted(
                        [a for a in report.artifacts if a.thumbnail_path],
                        key=lambda a: (a.mode != 'page', -a.created_at.timestamp() if a.created_at else 0)
                    )
                    if sorted_artifacts:
                        # thumbnail_path is like "thumbnails/{artifact_id}.png", serve via /thumbnails/{filename}
                        thumb_path = sorted_artifacts[0].thumbnail_path
                        filename = thumb_path.split("/")[-1] if "/" in thumb_path else thumb_path
                        report_schema.thumbnail_url = f"/thumbnails/{filename}"

                report_schemas.append(report_schema)
            span.add_event("report schemas ready")

            # Calculate pagination metadata
            total_pages = (total + limit - 1) // limit  # Ceiling division
            has_next = page < total_pages
            has_prev = page > 1

            from app.schemas.report_schema import PaginationMeta, ReportListResponse

            meta = PaginationMeta(
                total=total,
                page=page,
                limit=limit,
                total_pages=total_pages,
                has_next=has_next,
                has_prev=has_prev
            )
            span.add_event("get_reports done")
            return ReportListResponse(reports=report_schemas, meta=meta)

    async def bulk_archive_reports(
        self,
        db: AsyncSession,
        report_ids: list[str],
        current_user: User,
        organization: Organization,
    ):
        """
        Archive multiple reports in a single operation.
        Only affects regular reports in the current organization that the user owns.
        """
        if not report_ids:
            return {"archived": 0}

        # Only allow archiving reports the user owns within the org and that are not already archived
        stmt = (
            select(Report)
            .where(
                Report.id.in_(report_ids),
                Report.organization_id == organization.id,
                Report.user_id == current_user.id,
                Report.report_type == "regular",
                Report.status != "archived",
            )
        )
        result = await db.execute(stmt)
        reports = result.scalars().all()

        count = 0
        archived_ids = []
        for report in reports:
            report.status = "archived"
            archived_ids.append(str(report.id))
            count += 1

        if count:
            await self._delete_scheduled_prompts_for_reports(db, archived_ids)
            await db.commit()

            # Audit log
            try:
                await audit_service.log(
                    db=db,
                    organization_id=str(organization.id),
                    action="report.bulk_archived",
                    user_id=str(current_user.id),
                    resource_type="report",
                    details={"count": count, "report_ids": archived_ids},
                )
            except Exception:
                pass

        return {"archived": count}

    @staticmethod
    async def _enrich_fork_lineage(db: AsyncSession, report: Report, schema: "ReportSchema"):
        """Populate fork lineage fields on a ReportSchema from the Report model."""
        forked_from_id = getattr(report, "forked_from_id", None)
        if forked_from_id:
            schema.forked_from_id = forked_from_id
            from app.models.user import User
            # lazyload("*"): only the parent's title and owner name are read —
            # don't cascade the parent report's whole graph.
            result = await db.execute(
                select(Report)
                .options(lazyload("*"), selectinload(Report.user).options(lazyload("*")))
                .where(Report.id == forked_from_id)
            )
            parent = result.scalar_one_or_none()
            if parent:
                schema.forked_from_title = parent.title
                if parent.user:
                    schema.forked_from_user_name = parent.user.name or parent.user.email

    async def _set_slug_for_report(self, db: AsyncSession, report: Report):
        title_slug = report.title.replace(" ", "-").lower()
        title_slug = "".join(e for e in title_slug if e.isalnum() or e == "-")

        _uuid = uuid.uuid4().hex[:4]

        while (await db.execute(select(Report).filter(Report.slug == (title_slug + "-" + _uuid)))).scalar_one_or_none():
            _uuid = uuid.uuid4().hex[:6]
        else:
            title_slug = title_slug + "-" + _uuid
            report.slug = title_slug

    async def _associate_files_with_report(self, db: AsyncSession, report: Report, file_ids: list[str]):
        # Fetch the files asynchronously
        result = await db.execute(select(File).filter(File.id.in_(file_ids)))
        files = result.scalars().all()
        
        report.files.extend(files)

        await db.commit()

        # Create association table entries
        return report

    async def _associate_data_sources_with_report(self, db: AsyncSession, report: Report, data_source_ids: list[str]):
        # Fetch the data sources asynchronously with memberships preloaded
        result = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.data_source_memberships))
            .filter(DataSource.id.in_(data_source_ids))
        )
        data_sources = result.scalars().all()
        
        report.data_sources.extend(data_sources)
        await db.commit()  

        return report

    async def _report_data_source_names(self, db: AsyncSession, report_id: str) -> dict:
        """{data_source_id: name} currently attached to the report. Post
        set_data_sources this is already access-filtered (the actual persisted
        set), so it's safe to name."""
        from app.models.data_source import DataSource
        rows = (await db.execute(
            select(DataSource.id, DataSource.name)
            .join(report_data_source_association,
                  report_data_source_association.c.data_source_id == DataSource.id)
            .where(report_data_source_association.c.report_id == str(report_id))
        )).all()
        return {str(i): n for (i, n) in rows}

    async def _report_visible_data_source_names(self, db: AsyncSession, report, current_user, organization) -> dict:
        """{id: name} of the report's currently-attached sources that the acting
        user is allowed to see — so a scope-change event never leaks the name of
        a source the user has no access to."""
        from app.models.data_source import DataSource
        rows = (await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.data_source_memberships))
            .join(report_data_source_association,
                  report_data_source_association.c.data_source_id == DataSource.id)
            .where(report_data_source_association.c.report_id == str(report.id))
        )).scalars().all()
        if current_user is not None and organization is not None:
            from app.services.data_source_service import DataSourceService
            rows = await DataSourceService().filter_user_visible_data_sources(
                db, list(rows), current_user, organization
            )
        return {str(ds.id): ds.name for ds in rows}

    async def _report_has_user_turn(self, db: AsyncSession, report_id: str) -> bool:
        """True once the report has ≥1 user completion — i.e. a real
        conversation, not just the initial greeting / setup. Gates
        agent_scope_changed so a brand-new report's hydration PUT is silent."""
        from app.models.completion import Completion
        row = (await db.execute(
            select(Completion.id)
            .where(Completion.report_id == str(report_id), Completion.role == 'user')
            .limit(1)
        )).first()
        return row is not None

    async def set_data_sources_for_report(self, db: AsyncSession, report: Report, data_source_ids: list[str], current_user: User = None, organization: Organization = None) -> Report:
        """Replace a report's data source associations atomically with the provided ids.

        When current_user/organization are provided, the requested ids are
        filtered to the data sources that user is allowed to see — otherwise a
        user could pin a private source they have no access to and query it.
        """
        from sqlalchemy import delete
        from app.models.data_source_file_association import data_source_file_association

        # Delete existing associations directly via SQL to avoid ORM state tracking issues
        await db.execute(
            delete(report_data_source_association).where(
                report_data_source_association.c.report_id == report.id
            )
        )

        # Expire and refresh the relationship so ORM sees it as empty (avoids MissingGreenlet on lazy load)
        db.expire(report, ["data_sources"])
        await db.refresh(report, ["data_sources"])

        # Remove files that were snapshotted onto the report from data sources
        # that are no longer attached. We only drop a file when it (a) belongs to
        # some data source (so user-uploaded files, which belong to none, are
        # spared), (b) does not belong to any data source that remains attached,
        # and (c) was snapshotted, not produced/mentioned during a chat
        # (completion_id IS NULL). This mirrors the add-only snapshot below.
        remaining_ds_files = (
            select(data_source_file_association.c.file_id)
            .where(data_source_file_association.c.data_source_id.in_(data_source_ids))
        )
        files_owned_by_any_ds = select(data_source_file_association.c.file_id)
        await db.execute(
            delete(report_file_association).where(
                report_file_association.c.report_id == report.id,
                report_file_association.c.completion_id.is_(None),
                report_file_association.c.file_id.in_(files_owned_by_any_ds),
                report_file_association.c.file_id.notin_(remaining_ds_files),
            )
        )
        # Expire so the ORM re-reads report.files before the snapshot add below.
        db.expire(report, ["files"])

        if data_source_ids:
            # Load all requested data sources (scoped to the org when known)
            ds_filters = [DataSource.id.in_(data_source_ids)]
            if organization is not None:
                ds_filters.append(DataSource.organization_id == organization.id)
            result = await db.execute(
                select(DataSource)
                .options(
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.files),
                )
                .filter(*ds_filters)
            )
            new_data_sources = result.scalars().all()

            # Access gate: drop sources the user isn't allowed to see.
            if current_user is not None and organization is not None:
                from app.services.data_source_service import DataSourceService
                new_data_sources = await DataSourceService().filter_user_visible_data_sources(
                    db, list(new_data_sources), current_user, organization
                )

            # Add new associations
            report.data_sources.extend(new_data_sources)

            # Snapshot any new files from these data sources into the
            # report (dedup against whatever's already attached).
            await db.refresh(report, ["files"])
            existing_ids = {str(f.id) for f in report.files}
            for ds in new_data_sources:
                for f in ds.files:
                    if str(f.id) not in existing_ids:
                        report.files.append(f)
                        existing_ids.add(str(f.id))

        await db.flush()
        return report
    

    async def _get_report_messages(self, report: Report):
        messages = report.completions

        context = []
        for completion in messages:
            context.append("====== START MESSAGE =======")
            if completion.role == 'user':
                context.append(f"role: {completion.role}\n content: {completion.prompt['content']}")
            else:
                context.append(f"role: {completion.role}\n content: {completion.completion['content']}")
            context.append("====== END MESSAGE =======")

        return "\n".join(context)

    def _parse_cron_expression(self, cron_expression: str) -> dict:
        # Gracefully handle unschedule values
        if not cron_expression:
            return None
        if isinstance(cron_expression, str) and cron_expression.strip().lower() in {"none", "null", "false"}:
            return None

        parts = cron_expression.split()
        if len(parts) == 6:
            second, minute, hour, day, month, day_of_week = parts
            return {
                'second': second,
                'minute': minute,
                'hour': hour,
                'day': day,
                'month': month,
                'day_of_week': cron_dow_to_apscheduler(day_of_week)
            }
        elif len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
            return {
                'minute': minute,
                'hour': hour,
                'day': day,
                'month': month,
                'day_of_week': cron_dow_to_apscheduler(day_of_week)
            }
        else:
            raise ValueError("Invalid cron expression format")
    
    async def scheduled_rerun_report_steps(self, report_id: str, current_user_id: str, organization_id: str):
        # Claim this fire so only one worker/replica reruns the report and emails
        # subscribers (all workers run a scheduler against the shared job store).
        if not await asyncio.to_thread(claim_scheduled_run, f"report_{report_id}"):
            return
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            # Load current_user and organization here
            current_user = await db.get(User, current_user_id)
            organization = await db.get(Organization, organization_id)

            # Now call rerun_report_steps with the fresh db and loaded objects
            await self.rerun_report_steps(db, report_id, current_user, organization, notify_subscribers=True)

    async def set_report_schedule(self, db: AsyncSession, report_id: str, cron_expression: str, current_user: User, organization: Organization, notification_subscribers: list = None) -> Report:
        
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        try:
            # Try to remove existing job if it exists
            scheduler.remove_job(job_id=f"report_{report_id}")
        except JobLookupError:
            # Job doesn't exist yet, that's fine
            pass
        
        # Continue with scheduling the new job (only if a valid cron is provided)
        cron_expression_parsed = self._parse_cron_expression(cron_expression)

        if cron_expression_parsed is not None:
            job = scheduler.add_job(
                func=self.scheduled_rerun_report_steps,
                trigger='cron',
                id=f"report_{report_id}",
                args=[report_id, current_user.id, organization.id],
                replace_existing=True,
                **cron_expression_parsed
            )
            logger.info(f"Scheduled new cron job for report {report_id}: {job}")
            next_run = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            logger.info(f"Next run time: {next_run}")
        
        # Update the cron expression in the report (normalize unschedule values to None)
        report.cron_schedule = None if cron_expression in (None, '', 'None') else cron_expression

        # Persist notification subscribers (clear if unscheduling)
        if cron_expression in (None, '', 'None'):
            report.notification_subscribers = None
        elif notification_subscribers is not None:
            report.notification_subscribers = notification_subscribers

        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(report, "notification_subscribers")

        await db.commit()
        await db.refresh(report)
        # Telemetry: report schedule changed
        try:
            await telemetry.capture(
                "report_scheduled" if cron_expression is not None else "report_unscheduled",
                {
                    "report_id": str(report.id),
                    "status": "scheduled" if cron_expression is not None else "unscheduled",
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.scheduled" if report.cron_schedule else "report.unscheduled",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "cron_schedule": report.cron_schedule},
            )
        except Exception:
            pass

        return report

    async def toggle_conversation_share(
        self, 
        db: AsyncSession, 
        report_id: str, 
        current_user: User, 
        organization: Organization
    ) -> dict:
        """Toggle conversation sharing for a report. Generates token if enabling."""
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        # Toggle the enabled state
        new_enabled = not report.conversation_share_enabled
        
        if new_enabled:
            # Generate a new token if enabling and no token exists
            if not report.conversation_share_token:
                report.conversation_share_token = uuid.uuid4().hex
            report.conversation_share_enabled = True
            report.conversation_visibility = 'public'
        else:
            # Keep the token but disable sharing (allows re-enabling with same URL)
            report.conversation_share_enabled = False
            report.conversation_visibility = 'none'
        
        await db.commit()
        await db.refresh(report)
        
        # Telemetry
        try:
            await telemetry.capture(
                "conversation_share_toggled",
                {
                    "report_id": str(report.id),
                    "enabled": report.conversation_share_enabled,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.share_toggled",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "share_enabled": report.conversation_share_enabled},
            )
        except Exception:
            pass

        return {
            "enabled": report.conversation_share_enabled,
            "token": report.conversation_share_token if report.conversation_share_enabled else None,
        }

    async def get_public_conversation(
        self,
        db: AsyncSession,
        share_token: str,
        limit: int = 10,
        before: str | None = None,
        user=None,
    ) -> dict:
        """Fetch a shared conversation by its token."""
        result = await db.execute(
            select(Report)
            .options(selectinload(Report.user))
            .filter(Report.conversation_share_token == share_token)
        )
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Check conversation visibility
        await self._check_visibility(db, report, 'conversation_visibility', user)
        
        # Fetch completions for this report
        from app.models.completion import Completion
        from app.models.completion_block import CompletionBlock
        from app.models.plan_decision import PlanDecision
        from app.models.tool_execution import ToolExecution
        
        # Build query with pagination - fetch newest first, then reverse for display
        completions_query = select(Completion).where(Completion.report_id == report.id)
        
        # If 'before' cursor provided, fetch older completions
        if before:
            cursor_result = await db.execute(select(Completion).where(Completion.id == before))
            cursor_completion = cursor_result.scalar_one_or_none()
            if cursor_completion:
                completions_query = completions_query.where(
                    Completion.created_at < cursor_completion.created_at
                )
        
        # Order by newest first, limit, then we'll reverse
        completions_stmt = (
            completions_query
            .order_by(Completion.created_at.desc())
            .limit(limit + 1)  # Fetch one extra to check if there are more
        )
        completions_res = await db.execute(completions_stmt)
        fetched = list(completions_res.scalars().all())
        
        # Check if there are more older completions
        has_more = len(fetched) > limit
        if has_more:
            fetched = fetched[:limit]  # Remove the extra one
        
        # Reverse to get chronological order (oldest first)
        all_completions = list(reversed(fetched))
        
        # Get the cursor for the next page (oldest completion in this batch)
        next_before = all_completions[0].id if all_completions and has_more else None
        
        completion_ids = [c.id for c in all_completions]
        system_completion_ids = [c.id for c in all_completions if c.role == 'system']
        
        # Fetch blocks for system completions
        blocks: list = []
        pd_map: dict = {}
        te_map: dict = {}
        if system_completion_ids:
            blocks_join_stmt = (
                select(CompletionBlock, PlanDecision, ToolExecution)
                .where(CompletionBlock.completion_id.in_(system_completion_ids))
                .outerjoin(PlanDecision, CompletionBlock.plan_decision_id == PlanDecision.id)
                .outerjoin(ToolExecution, CompletionBlock.tool_execution_id == ToolExecution.id)
                .order_by(CompletionBlock.completion_id.asc(), CompletionBlock.block_index.asc())
            )
            join_res = await db.execute(blocks_join_stmt)
            for row in join_res.all():
                b = row[0]
                pd = row[1]
                te = row[2]
                blocks.append(b)
                if pd is not None:
                    pd_map[pd.id] = pd
                if te is not None:
                    te_map[te.id] = te
        
        
        # Build per-completion block lists (sanitized)
        completion_id_to_blocks: dict = {cid: [] for cid in completion_ids}
        for b in blocks:
            pd = pd_map.get(b.plan_decision_id) if b.plan_decision_id else None
            te = te_map.get(b.tool_execution_id) if b.tool_execution_id else None
            
            # Build sanitized block (no internal IDs, no user feedback)
            block_data = {
                "id": b.id,
                "block_index": b.block_index,
                "status": b.status,
                "content": b.content,
                "reasoning": b.reasoning,
                "started_at": b.started_at.isoformat() if b.started_at else None,
                "completed_at": b.completed_at.isoformat() if b.completed_at else None,
            }
            
            if pd:
                block_data["plan_decision"] = {
                    "reasoning": pd.reasoning,
                    "assistant": pd.assistant,
                    "final_answer": pd.final_answer,
                    "analysis_complete": pd.analysis_complete,
                }
            
            if te:
                # Sanitized tool execution - include results but strip internal IDs
                block_data["tool_execution"] = {
                    "id": te.id,
                    "tool_name": te.tool_name,
                    "tool_action": te.tool_action,
                    "status": te.status,
                    "result_summary": te.result_summary,
                    "result_json": te.result_json,
                    "duration_ms": te.duration_ms,
                }
            
            completion_id_to_blocks[b.completion_id].append(block_data)
        
        # Assemble sanitized completions
        sanitized_completions = []
        for c in all_completions:
            c_blocks = completion_id_to_blocks.get(c.id, [])
            c_blocks.sort(key=lambda x: x["block_index"])
            
            completion_data = {
                "id": c.id,
                "role": c.role,
                "status": c.status,
                "prompt": c.prompt if c.role == "user" else None,
                "completion_blocks": c_blocks if c.role == "system" else [],
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            sanitized_completions.append(completion_data)
        
        return {
            "report_id": report.id,
            "title": report.title,
            "user_name": report.user.name if report.user else "Unknown",
            "completions": sanitized_completions,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "has_more": has_more,
            "next_before": next_before,
        }

    async def get_report_summary(
        self, db: AsyncSession, report_id: str
    ) -> dict:
        """Return all query tool executions and instruction mutations for a report,
        independent of completion pagination."""
        from app.models.completion import Completion
        from app.models.completion_block import CompletionBlock
        from app.models.tool_execution import ToolExecution
        from app.schemas.tool_execution_schema import ToolExecutionSchema
        from app.schemas.step_schema import StepSchema
        from app.schemas.report_summary_schema import (
            SummaryToolExecutionSchema,
            SummaryInstructionItem,
        )
        from app.serializers.completion_v2 import (
            _extract_data_source_ids,
            _resolve_data_sources,
        )

        # 1) Fetch all successful tool executions that created steps (queries)
        query_stmt = (
            select(ToolExecution, Completion.id.label("completion_id"))
            .join(CompletionBlock, CompletionBlock.tool_execution_id == ToolExecution.id)
            .join(Completion, Completion.id == CompletionBlock.completion_id)
            .where(
                Completion.report_id == report_id,
                Completion.deleted_at == None,
                ToolExecution.status == "success",
                ToolExecution.created_step_id != None,
            )
            .order_by(CompletionBlock.created_at.asc())
        )
        query_res = await db.execute(query_stmt)
        query_rows = query_res.all()

        # 2) Batch-load steps
        step_ids = {row.ToolExecution.created_step_id for row in query_rows if row.ToolExecution.created_step_id}
        step_map: dict[str, Step] = {}
        if step_ids:
            step_res = await db.execute(select(Step).where(Step.id.in_(list(step_ids))))
            for s in step_res.scalars().all():
                step_map[s.id] = s

        # 3) Batch-resolve data sources
        all_ds_ids: list[str] = []
        te_to_ds_ids: dict[str, list[str]] = {}
        for row in query_rows:
            te = row.ToolExecution
            ds_ids = _extract_data_source_ids(te)
            if ds_ids:
                te_to_ds_ids[te.id] = ds_ids
                all_ds_ids.extend(ds_ids)

        ds_schema_map: dict[str, object] = {}
        if all_ds_ids:
            from app.models.data_source import DataSource as DS
            from app.models.connection import Connection
            from app.models.domain_connection import domain_connection
            from app.schemas.completion_v2_schema import ToolExecutionDataSourceSchema

            ds_rows = await db.execute(
                select(DS.id, DS.name, Connection.type)
                .join(domain_connection, domain_connection.c.data_source_id == DS.id)
                .join(Connection, Connection.id == domain_connection.c.connection_id)
                .where(DS.id.in_(list(set(all_ds_ids))))
            )
            for r in ds_rows:
                ds_id = str(r[0])
                if ds_id not in ds_schema_map:
                    ds_schema_map[ds_id] = ToolExecutionDataSourceSchema(id=ds_id, name=r[1], type=r[2])

        # 4) Build query schemas
        queries: list[SummaryToolExecutionSchema] = []
        seen_steps = set()
        for row in query_rows:
            te = row.ToolExecution
            step_id = te.created_step_id
            if step_id and step_id in seen_steps:
                continue
            if step_id:
                seen_steps.add(step_id)

            base = ToolExecutionSchema.from_orm(te)
            te_data = base.model_dump()
            # Strip heavy payloads
            rj = te_data.get("result_json")
            if isinstance(rj, dict):
                rj.pop("widget_data", None)
            from app.ai.llm.pii.display import redact_deep_display
            te_data["result_json"] = redact_deep_display(rj)

            created_step_schema = None
            step_obj = step_map.get(step_id) if step_id else None
            if step_obj:
                step_dict = {
                    **step_obj.__dict__,
                    "data_model": getattr(step_obj, "data_model", None) or {},
                    "data": getattr(step_obj, "data", None) or {},
                }
                created_step_schema = StepSchema.model_validate(step_dict)

            ds_list = None
            ds_ids_for_te = te_to_ds_ids.get(te.id)
            if ds_ids_for_te:
                ds_list = [ds_schema_map[did] for did in ds_ids_for_te if did in ds_schema_map] or None

            queries.append(SummaryToolExecutionSchema(
                **te_data,
                created_step=created_step_schema,
                data_sources=ds_list,
            ))

        # 5) Fetch instruction create/edit tool executions
        instr_stmt = (
            select(ToolExecution, Completion.id.label("completion_id"))
            .join(CompletionBlock, CompletionBlock.tool_execution_id == ToolExecution.id)
            .join(Completion, Completion.id == CompletionBlock.completion_id)
            .where(
                Completion.report_id == report_id,
                Completion.deleted_at == None,
                ToolExecution.status == "success",
                ToolExecution.tool_name.in_(["create_instruction", "edit_instruction"]),
            )
            .order_by(CompletionBlock.created_at.asc())
        )
        instr_res = await db.execute(instr_stmt)
        instr_rows = instr_res.all()

        instructions: list[SummaryInstructionItem] = []
        seen_instr_ids: dict[str, int] = {}  # instruction_id -> index in list
        for row in instr_rows:
            te = row.ToolExecution
            completion_id = row.completion_id
            rj = te.result_json or {}
            if not rj.get("success") or not rj.get("instruction_id"):
                continue
            args = te.arguments_json or {}
            text = args.get("text", "")
            instr_id = rj["instruction_id"]
            is_edit = te.tool_name == "edit_instruction"

            existing_idx = seen_instr_ids.get(instr_id)
            title = text.split("\n")[0].replace("#", "").strip()[:60] if text else "Instruction"

            item = SummaryInstructionItem(
                instruction_id=instr_id,
                title=instructions[existing_idx].title if existing_idx is not None else title,
                category=args.get("category", instructions[existing_idx].category if existing_idx is not None else "general"),
                is_edit=is_edit or (instructions[existing_idx].is_edit if existing_idx is not None else False),
                line_count=len([l for l in text.split("\n") if l.strip()]) if text else (instructions[existing_idx].line_count if existing_idx is not None else 0),
                message_id=str(completion_id),
                build_id=rj.get("build_id"),
            )

            if existing_idx is not None:
                instructions[existing_idx] = item
            else:
                seen_instr_ids[instr_id] = len(instructions)
                instructions.append(item)

        # 6) Find the most recent draft / pending build referenced by these
        # training tool calls, so the pill can offer approve/discard.
        from app.models.instruction_build import InstructionBuild
        from app.models.build_content import BuildContent
        from app.schemas.report_summary_schema import PendingTrainingBuildSchema
        pending_build = None
        staged_instruction_ids: set[str] = set()
        build_ids = [i.build_id for i in instructions if i.build_id]
        if build_ids:
            build_res = await db.execute(
                select(InstructionBuild)
                .where(
                    InstructionBuild.id.in_(list(set(build_ids))),
                    InstructionBuild.status.in_(["draft", "pending_approval"]),
                    InstructionBuild.deleted_at == None,
                )
                .order_by(InstructionBuild.created_at.desc())
            )
            candidate_builds = build_res.scalars().all()
            # Pick the most recent build that still has staged contents. Accepting
            # each create_instruction individually (POST /instructions/{id}/
            # accept-staged) detaches only that instruction from the shared draft
            # while leaving the draft in `draft` status. Once every instruction has
            # been accepted the draft is an empty husk — status alone would still
            # report it as pending, so the publish pill would nag the user to
            # publish changes that are already live. Gate on live BuildContent rows
            # (not build status or the total_instructions counter) so an emptied
            # draft no longer surfaces as a pending training build.
            for build_obj in candidate_builds:
                content_res = await db.execute(
                    select(BuildContent.instruction_id)
                    .where(BuildContent.build_id == build_obj.id)
                )
                content_ids = {str(cid) for (cid,) in content_res.all()}
                if not content_ids:
                    continue
                staged_instruction_ids = content_ids
                pending_build = PendingTrainingBuildSchema(
                    id=str(build_obj.id),
                    status=build_obj.status,
                    total_instructions=len(content_ids),
                )
                break

        # Restrict the instructions list to the current pending build so the
        # session pill only shows truly-pending changes. Without this, edits
        # whose builds were already published earlier in the session leak in, and
        # instructions already accepted out of the shared draft (still carrying
        # its build_id in their tool-call result) would linger. Match on both the
        # build id and the set of instructions still staged in that build.
        if pending_build:
            instructions = [
                i for i in instructions
                if i.build_id == pending_build.id and i.instruction_id in staged_instruction_ids
            ]
        else:
            instructions = []

        return {
            "queries": queries,
            "instructions": instructions,
            "pending_training_build": pending_build.model_dump() if pending_build else None,
        }