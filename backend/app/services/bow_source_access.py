"""Conservative access lineage for monitoring snapshots and their containing reports."""
from sqlalchemy import select
from sqlalchemy.orm import lazyload

from app.core.console_access import resolve_console_scope
from app.errors import AppError, ErrorCode
from app.models.organization import Organization
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association as assoc


async def can_read(db, access, user, *, check_revision=True):
    if not access:
        return True
    if user is None:
        return False
    org = await db.get(Organization, access.get("organization_id"))
    if org is None:
        return False
    try:
        scope = await resolve_console_scope(db, org, user)
    except Exception:
        return False
    required = access.get("scope_ids")
    if not scope.is_org_wide and (required is None or not set(required).issubset(scope.data_source_ids)):
        return False
    if check_revision and not scope.is_org_wide:
        # Check the reports that contributed data, not unrelated organization activity.
        from app.services.diagnosis.service import _reports_in_scope
        reports = access.get("report_ids", [])
        visible = set((await db.execute(select(Report.id).where(Report.organization_id == org.id,
            Report.id.in_(reports), Report.id.in_(_reports_in_scope(scope.data_source_ids))))).scalars())
        if not set(reports).issubset(visible):
            return False
    return True


async def assert_read(db, access, user, *, check_revision=True):
    if not await can_read(db, access, user, check_revision=check_revision):
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)


def merge_access(old, new):
    if not old:
        return dict(new)
    if old["organization_id"] != new["organization_id"]:
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)
    result = dict(new)
    a, b = old.get("scope_ids"), new.get("scope_ids")
    result["scope_ids"] = sorted(set(a) | set(b)) if a is not None and b is not None else None
    result["report_ids"] = sorted(set(old.get("report_ids", [])) | set(new.get("report_ids", [])))
    return result


async def protect_report(db, report_id, access):
    if not report_id:
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)
    report = (await db.execute(select(Report).options(lazyload("*")).where(Report.id == str(report_id)).with_for_update())).scalar_one()
    if str(report.organization_id) != access["organization_id"]:
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)
    if report.artifact_visibility == "public" or report.conversation_visibility == "public":
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)
    report.bow_source_access = merge_access(report.bow_source_access, access)
    from app.models.query import Query
    from sqlalchemy import update
    await db.execute(update(Query).where(Query.report_id == str(report_id)).values(source_refs=[{"id": "builtin:bow", "version": 1}]))
    await db.commit()


async def report_access(db, report_id):
    return await db.scalar(select(Report.bow_source_access).where(Report.id == str(report_id))) if report_id else None


async def step_access(db, step):
    from app.models.widget import Widget
    return await db.scalar(select(Report.bow_source_access).join(Widget, Widget.report_id == Report.id)
                           .where(Widget.id == str(step.widget_id)))


async def guard_route(db, user, kwargs):
    """Resolve route IDs without loading artifacts; run before ordinary role shortcuts."""
    from app.models.query import Query
    from app.models.completion import Completion
    from app.models.widget import Widget
    from app.models.artifact import Artifact
    from app.models.step import Step
    from app.models.entity import Entity
    report_ids = set()
    if kwargs.get("report_id"):
        report_ids.add(str(kwargs["report_id"]))
    for key, model in (("query_id", Query), ("completion_id", Completion), ("widget_id", Widget), ("artifact_id", Artifact)):
        if kwargs.get(key):
            rid = await db.scalar(select(model.report_id).where(model.id == str(kwargs[key])))
            if rid:
                report_ids.add(str(rid))
    if kwargs.get("step_id"):
        rid = await db.scalar(select(Widget.report_id).join(Step, Step.widget_id == Widget.id).where(Step.id == str(kwargs["step_id"])))
        if rid:
            report_ids.add(str(rid))
    if kwargs.get("entity_id"):
        access = await db.scalar(select(Entity.bow_source_access).where(Entity.id == str(kwargs["entity_id"])))
        await assert_read(db, access, user)
    for rid in report_ids:
        await assert_read(db, await report_access(db, rid), user)


async def visible_reports_clause(db, organization_id, user):
    """Additional SQL predicate, composed before counts/pagination in listings."""
    rows = (await db.execute(select(Report.id, Report.bow_source_access)
        .where(Report.organization_id == str(organization_id), Report.bow_source_access.isnot(None)))).all()
    denied = [rid for rid, access in rows if access and not await can_read(db, access, user)]
    return Report.id.notin_(denied)


async def install_entity_client(db, organization, user, entity, clients):
    access = getattr(entity, "bow_source_access", None)
    if not access:
        return
    await assert_read(db, access, user)
    from app.models.step import Step
    from app.models.widget import Widget
    from app.data_sources.clients.bow_client import install_bow_client
    report = (await db.execute(select(Report).join(Widget, Widget.report_id == Report.id)
        .join(Step, Step.widget_id == Widget.id).where(Step.id == entity.source_step_id))).scalar_one_or_none()
    if report is None:
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)
    await install_bow_client(db, organization, user, report, clients)


async def assert_shareable(db, report_id, public):
    if public and await report_access(db, report_id):
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED)


async def visible_entities_clause(db, organization_id, user):
    from app.models.entity import Entity
    rows = (await db.execute(select(Entity.id, Entity.bow_source_access)
        .where(Entity.organization_id == str(organization_id), Entity.bow_source_access.isnot(None)))).all()
    denied = [eid for eid, access in rows if access and not await can_read(db, access, user)]
    return Entity.id.notin_(denied)
