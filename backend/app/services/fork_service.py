"""
Service for forking reports.

Creates a new report from an existing published/shared report, duplicating
queries, visualizations, widgets, and artifacts with proper ID remapping.
Generates an AI summary of the original conversation as the first message.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.report import Report
from app.models.completion import Completion
from app.models.query import Query
from app.models.visualization import Visualization
from app.models.widget import Widget
from app.models.artifact import Artifact
from app.models.data_source import DataSource
from app.models.user import User
from app.services.artifact_service import ArtifactService
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

artifact_service = ArtifactService()

# Step status for a fork step that hydrate_fork has not run yet.
FORK_PENDING_STATUS = "pending"

# A step still 'pending' after this long is not being hydrated any more —
# the worker that owned the detached task restarted or crashed mid-run. Past
# it the fork is treated as settled, so the page stops waiting and
# refresh-on-view is no longer held off. Generous next to the observed
# ~0.4s per query: a slow provider must not be mistaken for a dead task.
FORK_HYDRATION_STALE_SECONDS = 300


class ForkEligibility:
    def __init__(self, can_fork: bool, reason: Optional[str] = None):
        self.can_fork = can_fork
        self.reason = reason

    def to_dict(self):
        return {"can_fork": self.can_fork, "reason": self.reason}


class DuplicatedAssets(NamedTuple):
    widget_id_map: Dict[str, str]
    query_id_map: Dict[str, str]
    viz_id_map: Dict[str, str]
    artifact: Optional[Artifact]
    # {new_step_id: source code} for steps whose code was deliberately NOT
    # written into the fork (delegated sources). The hydration pass runs each
    # under the forker's own credentials and writes the code back only where
    # that succeeded. Empty for system-only forks, which copy code as before.
    pending_code: Dict[str, str] = {}


class ForkService:
    """Handles forking a published/shared report into a user's workspace."""

    async def check_eligibility(
        self,
        db: AsyncSession,
        report: Report,
        user: Optional[User],
    ) -> ForkEligibility:
        """Check if a user can fork a given report."""
        if user is None:
            return ForkEligibility(False, "not_logged_in")

        # Check org-level allow_forks setting
        from app.models.organization_settings import OrganizationSettings
        settings_result = await db.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == report.organization_id
            )
        )
        org_settings = settings_result.scalar_one_or_none()
        if org_settings:
            allow_forks = org_settings.get_config("allow_forks")
            if allow_forks is not None:
                val = allow_forks.value if hasattr(allow_forks, 'value') else allow_forks
                if not val:
                    return ForkEligibility(False, "forks_disabled")

        from app.models.membership import Membership
        membership_result = await db.execute(
            select(Membership.organization_id).where(Membership.user_id == user.id)
        )
        user_org_ids = {str(row[0]) for row in membership_result.all()}
        if str(report.organization_id) not in user_org_ids:
            return ForkEligibility(False, "different_org")

        # NOTE: delegated (user_required) sources are deliberately NOT blocked
        # here any more.
        #
        # The block dates from the first version of forking, where the fork did
        # not copy the creator's step at all — it pointed at it
        # (`default_step_id=old_query.default_step_id  # shared step reference`).
        # Forking a delegated report then handed the forker the creator's own
        # materialized rows, and refusing the fork was the only thing standing
        # in the way.
        #
        # That was fixed in "Gate all step.data readers via one accessor" (July
        # 2026), which gave the fork its own step row with `data={}` for exactly
        # these sources — keyed on the same `auth_policy != 'system_only'` test
        # this block used. From then on the block guarded a leak that could no
        # longer happen, while still making every OAuth/OBO source permanently
        # un-forkable, including for users who had signed in and could read it.
        #
        # What replaces it is behavioural rather than categorical: the fork is
        # created with no rows and no SQL, and `hydrate_fork` restores each
        # query only where the forker's OWN credentials could run it. A forker
        # who cannot run anything ends up with no fork at all. Authorization
        # itself is unchanged and still enforced below by
        # `user_can_access_data_source` — auth_policy only ever described HOW a
        # connection authenticates, never who is entitled to it.

        # Check user has access to all data sources
        from app.core.permission_resolver import user_can_access_data_source
        for ds in report.data_sources:
            if ds.is_public:
                continue
            if not await user_can_access_data_source(
                db, str(user.id), str(ds.organization_id), ds
            ):
                return ForkEligibility(False, "no_data_source_access")

        return ForkEligibility(True)

    async def fork_report(
        self,
        db: AsyncSession,
        report_id: str,
        user: User,
        title: Optional[str] = None,
    ) -> Report:
        """Fork a report, creating a new report with duplicated assets."""
        # 1. Load original report with all relationships
        result = await db.execute(
            select(Report)
            .options(
                selectinload(Report.user),
                selectinload(Report.data_sources).selectinload(DataSource.connections),
                selectinload(Report.widgets).selectinload(Widget.steps),
                selectinload(Report.queries).selectinload(Query.visualizations),
                selectinload(Report.queries).selectinload(Query.default_step),
                selectinload(Report.completions),
            )
            .where(Report.id == report_id)
        )
        original = result.unique().scalar_one_or_none()
        if not original:
            raise HTTPException(status_code=404, detail="Report not found")

        # Must be published, have conversation sharing enabled, or live in a
        # project the forking user can view — project collaborators are
        # read-only on member reports, and fork is their edit path.
        if original.status != "published" and not original.conversation_share_enabled:
            project_fork_ok = False
            if getattr(original, "project_id", None):
                from app.models.project import Project
                from app.services.project_service import project_service
                prow = await db.execute(
                    select(Project).where(
                        Project.id == original.project_id,
                        Project.deleted_at.is_(None),
                    )
                )
                proj = prow.scalar_one_or_none()
                project_fork_ok = proj is not None and await project_service.user_can_view_project(db, user, proj)
            if not project_fork_ok:
                raise HTTPException(status_code=403, detail="Report is not available for forking")

        # Check eligibility
        eligibility = await self.check_eligibility(db, original, user)
        if not eligibility.can_fork:
            raise HTTPException(status_code=403, detail=f"Cannot fork: {eligibility.reason}")

        # 2. Create new report
        fork_title = title or f"Fork of {original.title}"
        new_report = Report(
            title=fork_title,
            slug=f"fork-{uuid.uuid4().hex[:8]}",
            status="draft",
            report_type="regular",
            mode=getattr(original, "mode", "chat"),
            theme_name=original.theme_name,
            theme_overrides=original.theme_overrides,
            user_id=str(user.id),
            organization_id=str(original.organization_id),
            forked_from_id=str(original.id),
        )
        db.add(new_report)
        await db.flush()

        # 3. Link data sources
        from app.models.report_data_source_association import report_data_source_association
        for ds in original.data_sources:
            await db.execute(
                report_data_source_association.insert().values(
                    report_id=str(new_report.id),
                    data_source_id=str(ds.id),
                )
            )

        # 4. Duplicate all assets: widgets, queries, visualizations, artifact
        assets = await self._duplicate_assets(db, original, new_report, user)

        # 5. Generate fork summary completion
        await self._create_fork_summary(
            db, original, new_report, user,
            assets.query_id_map, assets.viz_id_map, assets.artifact,
        )

        await db.commit()
        await db.refresh(new_report)

        # Handed to the caller rather than acted on here: hydration runs
        # detached from this request (and this session), so the route owns
        # spawning it. Empty for system-only forks, which are complete already.
        new_report.pending_code = assets.pending_code
        return new_report

    async def is_hydrating(self, db: AsyncSession, report_id: str) -> bool:
        """Is hydrate_fork still due to fill this fork in?

        True while any of the report's steps is still FORK_PENDING_STATUS —
        a status only fork creation sets, so ordinary reports can never read as
        hydrating. Steps pending for longer than FORK_HYDRATION_STALE_SECONDS
        do not count: their task died with its worker, and without the cutoff
        the page would wait forever and refresh-on-view would stay held off.

        The one answer to "is the fork ready?" — the report page polls it
        before rendering, and refresh-on-view consults it so the queries are
        not run a second time underneath the hydration pass.
        """
        from sqlalchemy import func
        from app.models.step import Step

        cutoff = datetime.utcnow() - timedelta(seconds=FORK_HYDRATION_STALE_SECONDS)
        pending = (await db.execute(
            select(func.count(Step.id))
            .select_from(Step)
            .join(Query, Query.default_step_id == Step.id)
            .where(
                Query.report_id == str(report_id),
                Step.status == FORK_PENDING_STATUS,
                Step.created_at >= cutoff,
            )
        )).scalar() or 0
        return pending > 0

    async def hydrate_fork(
        self,
        fork_id: str,
        user_id: str,
        organization_id: str,
        pending_code: Dict[str, str],
    ) -> dict:
        """Run a delegated-source fork's queries as the forker, then keep what worked.

        A fork of a `user_required` source is created empty: no rows (they are
        the creator's slice) and no SQL (the share withholds it from a reader
        with no access). This restores both — but only per step, and only from
        the forker's OWN successful run, so nothing the source owner could see
        crosses over on the strength of the fork alone.

        Runs detached from the request, on its own session: the caller has
        already returned the fork id and the user is on the page. Failures are
        per step and never raise — the fork keeps the charts that ran and marks
        the rest as inaccessible.

        When NOTHING ran, the forker has no access to the source at all: the
        fork would be a shell of empty charts with no way to fill it, so it is
        retired rather than left behind.

        Known limit: "the run succeeded" proves the forker can execute the
        code, which equals access to the source only because step code reaches
        its source through its client (`ds_clients["<agent>:<connection>"]`),
        and a forker with no usable identity has no client under that key — so
        the lookup raises. Code that wrapped the lookup AND its only query in a
        blanket `except` returning an empty frame would "succeed" without
        access and have its SQL written back. No such step existed when this
        was written (all 144 steps in a live install index the client outside
        any try; the four that catch at all retry the same source or wrap a
        secondary query), but it is behaviour of generated code, not a guarantee
        this function enforces.
        """
        from app.dependencies import async_session_maker
        from app.models.organization import Organization
        from app.models.step import Step
        from app.services.step_service import StepService

        step_service = StepService()

        succeeded: list[str] = []
        failed: list[str] = []
        # The subset of `failed` the provider refused outright — told apart
        # from a query that broke (see access_errors) so each gets the right
        # message on the step.
        refused: set[str] = set()

        async def _load(db):
            """The fork, its forker and org — loaded fresh in whichever session
            is about to use them (see the per-step loop below)."""
            report = (await db.execute(
                select(Report)
                .options(
                    selectinload(Report.data_sources).selectinload(DataSource.connections),
                    selectinload(Report.files),
                )
                .where(Report.id == str(fork_id))
            )).unique().scalar_one_or_none()
            user = (await db.execute(select(User).where(User.id == str(user_id)))).scalar_one_or_none()
            organization = (await db.execute(
                select(Organization).where(Organization.id == str(organization_id))
            )).scalar_one_or_none()
            return report, user, organization

        async with async_session_maker() as db:
            report, user, organization = await _load(db)
            if report is None or user is None:
                logger.warning("Fork hydration: report %s or user %s is gone", fork_id, user_id)
                return {"succeeded": 0, "failed": 0, "deleted": False}

            # Clients are per data source, not per step: constructing them
            # resolves credentials and, for some drivers, pays a connection
            # handshake. They hold provider tokens, not database state, so one
            # set serves every step's session below. A source whose client
            # cannot be built at all fails its steps, which is the correct
            # outcome — that IS "no access".
            db_clients = {}
            try:
                from app.services.data_source_service import DataSourceService
                from app.ai.tools.implementations.agent_focus_common import resolve_run_agents

                ds_service = DataSourceService()
                for data_source in await resolve_run_agents(db, organization, user, report):
                    try:
                        db_clients.update(
                            await ds_service.construct_clients(db, data_source, current_user=user)
                        )
                    except Exception as e:
                        logger.info(
                            "Fork hydration: no client for data source %s as user %s: %s",
                            data_source.id, user_id, e,
                        )
            except Exception:
                logger.warning("Fork hydration: client setup failed for %s", fork_id, exc_info=True)

        # One session PER STEP. A failed step must be rolled back — rerun_step
        # assigned its code onto the step in memory, and letting a later commit
        # flush it would write SQL onto a step whose run failed. But a rollback
        # expires every object in its session, and on an async session touching
        # an expired object raises (MissingGreenlet) instead of reloading. With
        # one shared session, the first refused query therefore took down every
        # query after it: a forker with access to half a dashboard got "could
        # not be run" on that half too, nothing counted as succeeded, and the
        # fork was archived as if they had no access at all. (Commits never hit
        # this — expire_on_commit is off — which is why success-then-failure
        # worked and only failure-then-anything broke.) Separate sessions share
        # no state, so a failure cannot reach the next step.
        for step_id, code in (pending_code or {}).items():
            async with async_session_maker() as sdb:
                report, user, organization = await _load(sdb)
                if report is None or user is None:
                    failed.append(step_id)
                    continue
                org_settings = await organization.get_settings(sdb) if organization else None
                try:
                    await step_service.rerun_step(
                        sdb, step_id, current_user=user, report=report,
                        db_clients=db_clients, organization=organization,
                        organization_settings=org_settings,
                        code_override=code,
                    )
                    succeeded.append(step_id)
                except Exception as e:
                    # Discard the in-memory code assignment — the step keeps
                    # neither code nor rows (see above).
                    await sdb.rollback()
                    failed.append(step_id)
                    from app.services.access_errors import is_access_denied
                    if is_access_denied(e):
                        refused.add(step_id)
                    # Logged with the real cause — the one place it is kept, for
                    # telling an access failure from a broken query afterwards.
                    logger.info(
                        "Fork hydration: step %s failed for user %s: %s: %s",
                        step_id, user_id, type(e).__name__, e,
                    )

        async with async_session_maker() as db:
            report, user, organization = await _load(db)
            # Settle every step this pass owned, in one statement per outcome.
            #
            # A step that could not run keeps empty code AND empty data; its
            # reason says why, so the dashboard explains the empty chart. "No
            # access" is claimed only for a recognised provider refusal: a
            # forker WITH access once got it on a parameterized query that
            # failed only because the fork had dropped its parameters. The
            # provider's own error text is never surfaced; it can quote table
            # and model names, the very detail the withheld code was kept back
            # to protect.
            from sqlalchemy import update

            if succeeded:
                await db.execute(
                    update(Step)
                    .where(Step.id.in_([str(i) for i in succeeded]))
                    .values(status="success", status_reason=None)
                )
            # Two outcomes, two sentences. A provider refusal (HTTP 401/403,
            # PowerBIEntityNotFound) is recognisable, and the forker is owed a
            # plain "no access" for it. Anything else — a query that broke,
            # like the KeyError a dropped parameter used to cause — gets the
            # neutral wording rather than a false claim about permissions.
            from app.services.access_errors import NO_ACCESS_REASON

            broke = [str(i) for i in failed if i not in refused]
            if refused:
                await db.execute(
                    update(Step)
                    .where(Step.id.in_([str(i) for i in refused]))
                    .values(status="error", status_reason=NO_ACCESS_REASON)
                )
            if broke:
                await db.execute(
                    update(Step)
                    .where(Step.id.in_(broke))
                    .values(
                        status="error",
                        status_reason=(
                            "This query could not be run with your credentials, "
                            "so it was not copied into your fork."
                        ),
                    )
                )
            if succeeded:
                # This run IS the fork's first refresh. Stamping it is what
                # keeps the page's refresh-on-view from running every query a
                # second time the moment the dashboard mounts — its staleness
                # gate reads last_run_at, and a fork is created with none.
                await db.execute(
                    update(Report)
                    .where(Report.id == str(fork_id))
                    .values(last_run_at=datetime.utcnow())
                )
            nothing_ran = bool(failed) and not succeeded
            if nothing_ran:
                # Archived in the SAME commit that settles the steps. Split
                # across two commits, a status poll landing between them sees
                # no pending step and calls the fork ready, then loads a report
                # still reading 'draft' — and the page renders the empty
                # dashboard instead of the "nothing ran" explanation, which is
                # the one outcome this branch exists to prevent. archive_report
                # below still runs, for the scheduled prompts and the audit
                # entry; setting the status twice is harmless, and doing it
                # here means a failure inside it cannot leave the fork live.
                await db.execute(
                    update(Report)
                    .where(Report.id == str(fork_id))
                    .values(status="archived")
                )
            await db.commit()

            if nothing_ran:
                # Retired exactly the way a user's own delete retires a report
                # (status='archived', scheduled prompts cleared, audited) —
                # never a hard delete. Reports have no ORM delete cascade, so
                # `db.delete` makes SQLAlchemy null out the children's
                # report_id, which the NOT NULL constraints refuse (the fork's
                # summary completion is the first to trip it). Inside a detached
                # task that raise would just be logged, leaving the empty fork
                # in place — the one outcome this branch exists to prevent.
                from app.services.report_service import ReportService

                await ReportService().archive_report(db, str(fork_id), user, organization)
                logger.info(
                    "Fork hydration: retired fork %s — user %s could run none of its %d queries",
                    fork_id, user_id, len(failed),
                )
                return {"succeeded": 0, "failed": len(failed), "deleted": True}

            if succeeded:
                from app.services.thumbnail_service import ThumbnailService

                try:
                    # Regenerated from the FORKER's rows — the creator's
                    # thumbnail was deliberately not copied.
                    await ThumbnailService().regenerate_for_report(str(fork_id))
                except Exception:
                    logger.warning("Fork hydration: thumbnail failed for %s", fork_id, exc_info=True)

        return {"succeeded": len(succeeded), "failed": len(failed), "deleted": False}

    async def _duplicate_assets(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        user: User,
    ) -> DuplicatedAssets:
        """Duplicate widgets, queries, visualizations, and artifact with ID remapping.

        Artifact duplication depends on the viz_id_map produced by query/viz
        duplication, so it's handled here as the final step.
        """
        widget_id_map: Dict[str, str] = {}
        query_id_map: Dict[str, str] = {}
        viz_id_map: Dict[str, str] = {}

        # Whether the source report's step rows are credential-differentiated
        # to the source owner and so must not be copied into the fork:
        #   - user-scoped connections (auth_policy != system_only) — fork
        #     eligibility already blocks these, so this is normally False; it's
        #     defense-in-depth for the detached-source edge (a user_required
        #     source removed from the report after its steps were materialized).
        #   - RLS relations — the shared snapshot is the OWNER's row slice of a
        #     shared system_only materialization, so copying it hands the
        #     forker rows their own identity would never return. These live on
        #     system_only connections, so the user-scoped check alone misses
        #     them.
        from app.models.step import Step
        from app.services.viewer_data_policy import (
            has_user_scoped_connections,
            has_rls_relations,
            redact_applied_params,
        )
        user_scoped = await has_user_scoped_connections(db, str(original.id))
        strict_source = user_scoped or await has_rls_relations(db, str(original.id))

        # `code` needs a NARROWER rule than `data`.
        #
        # The public report already refuses a withheld reader the SQL itself
        # ("the SQL leaks schema/table/filter details even without rows" —
        # report_service._public_step). Copying it into a fork would hand that
        # reader exactly what the share refused them, so the fork has to apply
        # the same rule.
        #
        # But only for user-scoped sources. Under RLS the connection is
        # system_only: the forker CAN run the query, they just resolve a
        # different row slice — so withholding the code there would strand a
        # fork that works today, without closing anything. Only a delegated
        # source can leave the forker with no access at all, which is the case
        # the withholding exists for.
        #
        # So the code is held out of the fork here and kept in memory; the
        # hydration pass writes it back per step, but only onto the steps whose
        # run under the forker's own credentials actually succeeded.
        pending_code: Dict[str, str] = {}
        # (new_query, source parameter specs), remapped once every query has
        # been copied — see the block after the query loop.
        pending_params: List[tuple] = []

        # -- Widgets --
        for old_widget in original.widgets:
            new_widget = Widget(
                title=old_widget.title,
                slug=f"fork-{uuid.uuid4().hex[:8]}",
                status=old_widget.status,
                x=old_widget.x,
                y=old_widget.y,
                width=old_widget.width,
                height=old_widget.height,
                report_id=str(new_report.id),
            )
            db.add(new_widget)
            await db.flush()
            widget_id_map[str(old_widget.id)] = str(new_widget.id)

        # -- Queries & Visualizations --
        for old_query in original.queries:
            old_widget_id = str(old_query.widget_id)
            new_widget_id = widget_id_map.get(old_widget_id)
            if not new_widget_id:
                # Widget not in map — create one for this query
                new_widget = Widget(
                    title=old_query.title or "",
                    slug=f"fork-{uuid.uuid4().hex[:8]}",
                    status="draft",
                    x=0, y=0, width=5, height=9,
                    report_id=str(new_report.id),
                )
                db.add(new_widget)
                await db.flush()
                new_widget_id = str(new_widget.id)
                widget_id_map[old_widget_id] = new_widget_id

            new_query = Query(
                title=old_query.title,
                description=old_query.description,
                report_id=str(new_report.id),
                widget_id=new_widget_id,
                # default_step_id set below to the COPIED step — never the
                # original's step id. Sharing the reference let a fork's rerun
                # overwrite the source report's data (cross-report write) and
                # exposed the source owner's rows through the fork.
                default_step_id=None,
                organization_id=str(new_report.organization_id),
                user_id=str(user.id),
            )
            db.add(new_query)
            await db.flush()
            query_id_map[str(old_query.id)] = str(new_query.id)
            if old_query.parameters:
                pending_params.append((new_query, old_query.parameters))

            # Copy the query's default step into a NEW row owned by the fork.
            old_step = old_query.default_step
            if old_step is not None:
                # Only a step hydration will actually run may be marked
                # pending. A user-scoped step with no code to re-run is never
                # entered into pending_code, so hydrate_fork would never settle
                # it: 'pending' would stick until the staleness cutoff, holding
                # the page on its spinner and refresh-on-view off the report
                # for five minutes (and if EVERY step were blank, hydration is
                # not even spawned, so nothing would settle it at all). Such a
                # step has nothing to withhold either — there is no code and no
                # rows to copy — so it goes straight to its terminal status.
                will_hydrate = user_scoped and bool((old_step.code or "").strip())
                new_step = Step(
                    title=old_step.title,
                    slug=f"fork-{uuid.uuid4().hex[:8]}",
                    # A delegated-source step arrives empty and is filled in by
                    # hydrate_fork. 'pending' (never used by steps otherwise) is
                    # what marks it as not yet run for this forker: the report
                    # page waits on it instead of rendering an empty dashboard,
                    # and refresh-on-view stays off it so the queries do not run
                    # twice. Hydration moves it to 'success' or 'error'.
                    status=FORK_PENDING_STATUS if will_hydrate else old_step.status,
                    status_reason=None if user_scoped else old_step.status_reason,
                    # prompt rides with `code`: same authorship, and the share
                    # never exposes it at all. (Empty on every row today.)
                    prompt="" if user_scoped else old_step.prompt,
                    code="" if user_scoped else old_step.code,
                    # Strict-mode (user-scoped) data is credential-differentiated
                    # to the source owner — never copy it into the fork; the
                    # forker runs it under their own credentials. System-only
                    # data is shared by definition, so copy it as-is.
                    data={} if strict_source else old_step.data,
                    # applied_params travels WITH `data`, never apart from it:
                    # it records the values that snapshot was materialized
                    # with. Dropped alongside a dropped snapshot; carried
                    # alongside a copied one, because the dashboard reads it to
                    # tell a pre-filtered snapshot from an unfiltered one —
                    # without it, ArtifactFrame derives filter options from
                    # rows the creator had already narrowed and offers a
                    # one-value list. Identity-sourced values are stripped on
                    # the way (the same boundary redact_applied_params draws
                    # for a reader): those name the creator, not the data.
                    applied_params=(
                        None if strict_source
                        else redact_applied_params(
                            old_step.applied_params, old_query.parameters,
                            withheld=False,
                        )
                    ),
                    description=old_step.description,
                    type=old_step.type,
                    data_model=old_step.data_model,
                    view=old_step.view,
                    widget_id=new_widget_id,
                    query_id=str(new_query.id),
                )
                db.add(new_step)
                await db.flush()
                new_query.default_step_id = str(new_step.id)
                await db.flush()
                if will_hydrate:
                    pending_code[str(new_step.id)] = old_step.code

            for old_viz in old_query.visualizations:
                new_viz = Visualization(
                    title=old_viz.title,
                    status=old_viz.status,
                    report_id=str(new_report.id),
                    query_id=str(new_query.id),
                    view=old_viz.view,
                )
                db.add(new_viz)
                await db.flush()
                viz_id_map[str(old_viz.id)] = str(new_viz.id)

        # -- Query parameters (depends on the complete query_id_map) --
        #
        # The fork used to drop Query.parameters entirely. Saved step code
        # reads its parameters by name — `depot = params["depot"]` — and with
        # no specs the resolver hands the code `{}`, so every parameterized
        # query in a fork raised KeyError: the dashboard's filters vanished and,
        # under hydration, the query failed outright even for a forker with
        # full access to the data.
        #
        # What is copied is the DEFINITION only — names, types, labels,
        # defaults, identity bindings — which is the dashboard's own design,
        # not anyone's data. The values the creator last ran with
        # (Step.applied_params) stay behind: they can carry identity-derived
        # values (an email, a department), the same boundary
        # redact_applied_params guards on the read path. The forker's first
        # run resolves from the declared defaults instead.
        #
        # `options_source.query_id` points a dropdown at the query that lists
        # its options — a query in the SOURCE report. Left as-is, the fork's
        # filter would keep reading the creator's report (the cross-report
        # reference class the step-copy fix removed), so it is repointed at
        # the fork's own copy. This runs after the loop because the options
        # query may be copied after the query that uses it. A reference to a
        # query outside this report has no copy to point at and is dropped —
        # the parameter still works, it just offers no preset options.
        import copy

        for new_q, specs in pending_params:
            remapped = copy.deepcopy(specs)
            for spec in (remapped if isinstance(remapped, list) else []):
                src = spec.get("options_source") if isinstance(spec, dict) else None
                if isinstance(src, dict) and src.get("query_id"):
                    target = query_id_map.get(str(src["query_id"]))
                    if target:
                        src["query_id"] = target
                    else:
                        spec["options_source"] = None
            new_q.parameters = remapped
        if pending_params:
            await db.flush()

        # -- Artifact (depends on viz_id_map) --
        new_artifact = await self._duplicate_artifact(
            db, original, new_report, user, viz_id_map, strict_source,
        )

        return DuplicatedAssets(
            widget_id_map, query_id_map, viz_id_map, new_artifact, pending_code,
        )

    async def _duplicate_artifact(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        user: User,
        viz_id_map: Dict[str, str],
        strict_source: bool = False,
    ) -> Optional[Artifact]:
        """Duplicate the latest artifact with remapped visualization_ids.

        `strict_source` marks a source whose materialized output belongs to the
        creator's identity. Two fields on the artifact are derived from that
        output rather than from the dashboard's definition, so they are dropped
        rather than copied — see the call sites below.
        """
        latest = await artifact_service.get_latest_by_report(db, str(original.id))
        if not latest:
            return None

        # Remap every visualization id the artifact carries — not only the
        # `visualization_ids` list, but the ids baked into its source too.
        #
        # Dashboards reach their data by id: `vizById("<viz uuid>")` in page
        # code (182 calls across a live install, and the only id-keyed helper
        # the artifact runtime has), and the equivalent references in doc-mode
        # `markdown`. vizById only searches the data the host loaded for THIS
        # artifact, which for a fork is the fork's own visualizations. Remapping
        # just the list left the code asking for the source report's ids, so
        # every fork of a vizById dashboard rendered empty — silently, since
        # vizById returns null rather than raising. (It never fetched the
        # source's data: the failure was empty, not a leak.)
        #
        # Rewriting the serialized content covers code, markdown and the list
        # in one pass. Only ids present in viz_id_map are touched, so file ids
        # and anything else pass through unchanged; UUIDs cannot collide with
        # other text.
        old_content = latest.content or {}
        if viz_id_map:
            import json

            raw = json.dumps(old_content)
            for old_vid, new_vid in viz_id_map.items():
                raw = raw.replace(old_vid, new_vid)
            new_content = json.loads(raw)
        else:
            new_content = dict(old_content)

        new_artifact = Artifact(
            report_id=str(new_report.id),
            user_id=str(user.id),
            organization_id=str(new_report.organization_id),
            title=latest.title,
            mode=latest.mode,
            content=new_content,
            # Authored against the CREATOR's result set, and it states facts
            # about it in prose — real examples carry "the data source has only
            # 9 records" and "two distinct activity types". That is the
            # creator's row count and cardinality, which the forker's own slice
            # need not match. Named as an open residual by the commit that
            # introduced the withholding policy ("artifact prose/content that
            # bakes in creator-derived values at generation time"); dropped
            # here rather than carried into the fork.
            generation_prompt=None if strict_source else latest.generation_prompt,
            version=1,
            status="completed",
        )
        db.add(new_artifact)
        await db.flush()

        # The thumbnail is a rendered screenshot of the dashboard — the
        # creator's actual numbers, baked into a PNG. copy_thumbnail is a raw
        # shutil.copy2 with no policy check of its own, so a strict-source fork
        # must not call it: the hydration pass regenerates the thumbnail from
        # the forker's own run instead (rerun_report_steps already does this
        # whenever a step produced fresh data).
        if latest.thumbnail_path and not strict_source:
            try:
                from app.services.thumbnail_service import ThumbnailService
                thumbnail_service = ThumbnailService()
                new_thumbnail_path = thumbnail_service.copy_thumbnail(
                    str(latest.id), str(new_artifact.id)
                )
                if new_thumbnail_path:
                    new_artifact.thumbnail_path = new_thumbnail_path
            except Exception as e:
                logger.warning("Failed to copy thumbnail during fork: %s", e)

        return new_artifact

    async def _create_fork_summary(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        user: User,
        query_id_map: Dict[str, str],
        viz_id_map: Dict[str, str],
        new_artifact: Optional[Artifact],
    ):
        """Create a summary completion with asset references for the forked report."""
        # Build asset refs list using NEW IDs
        asset_refs: List[Dict[str, Any]] = []

        for old_query in original.queries:
            new_qid = query_id_map.get(str(old_query.id))
            if new_qid:
                asset_refs.append({
                    "type": "query",
                    "id": new_qid,
                    "title": old_query.title or "",
                    "description": old_query.description or "",
                })
            for old_viz in old_query.visualizations:
                new_vid = viz_id_map.get(str(old_viz.id))
                if new_vid:
                    asset_refs.append({
                        "type": "visualization",
                        "id": new_vid,
                        "title": old_viz.title or "",
                    })

        if new_artifact:
            asset_refs.append({
                "type": "artifact",
                "id": str(new_artifact.id),
                "title": new_artifact.title or "",
                "mode": new_artifact.mode,
            })

        # Build summary text from conversation context
        summary_parts = []
        summary_parts.append(f'This report was forked from "{original.title}".')

        if original.queries:
            summary_parts.append(f"\n{len(original.queries)} queries were inherited:")
            for old_query in original.queries:
                new_qid = query_id_map.get(str(old_query.id), "")
                step_info = ""
                if old_query.default_step:
                    step = old_query.default_step
                    step_info = f" ({step.type})"
                    if step.description:
                        step_info += f" - {step.description[:100]}"
                viz_ids = [viz_id_map[str(v.id)] for v in old_query.visualizations if str(v.id) in viz_id_map]
                viz_info = f" | viz: {', '.join(viz_ids)}" if viz_ids else ""
                summary_parts.append(
                    f"- {old_query.title or 'Untitled'}{step_info} [query: {new_qid}{viz_info}]"
                )

        if new_artifact:
            summary_parts.append(
                f"\nAn artifact ({new_artifact.mode} mode) was also inherited: "
                f'"{new_artifact.title or "Untitled"}" [artifact: {str(new_artifact.id)}].'
            )

        summary_text = "\n".join(summary_parts)

        completion = Completion(
            prompt={"content": ""},
            completion={"content": summary_text},
            status="success",
            model="system",
            turn_index=0,
            role="system",
            message_type="ai_completion",
            report_id=str(new_report.id),
            user_id=str(user.id),
            is_fork_summary="true",
            source_report_id=str(original.id),
            fork_asset_refs=asset_refs,
        )
        db.add(completion)


fork_service = ForkService()
