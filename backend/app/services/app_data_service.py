"""Records of artifact apps: the server side of `useCollection`.

Every operation runs the same ordered checks (design spec section 9):

  1. identity from the session only; writes need a signed-in user
  2. the artifact (parent identity, never a version) and its report exist
  3. Layer 1: ``report_service._check_visibility`` for artifact visibility
  4. the collection is declared by the effective declaration
  5. Layer 2: the collection's rules for this principal (``app_data_rules``)
  6. record validation and limits
  7. optimistic concurrency (conditional UPDATE on ``version``)
  8. audit in the same transaction as the write

Nothing here touches ``app_records`` before check 4 has found a declared
collection, so artifacts without storage never query that table (this keeps
unmigrated databases working).
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import distinct, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

import app.schemas.app_storage as app_storage
from app.ee.audit.service import audit_service
from app.errors import AppError, ErrorCode
from app.models.app_record import AppRecord
from app.models.artifact import Artifact, ArtifactVersion
from app.models.membership import Membership
from app.models.report import Report
from app.models.report_share import ReportShare
from app.models.user import User
from app.schemas.app_storage import (
    AppRecordCreate,
    AppRecordDelete,
    AppRecordDeleted,
    AppRecordList,
    AppRecordOut,
    AppRecordUpdate,
    AppRecordUser,
    CollectionSpec,
    StorageDeclaration,
    parse_storage_declaration,
)
from app.services.app_data_rules import (
    Principal,
    RecordValidationError,
    can_create,
    can_modify,
    can_read,
    project_record_data,
    validate_new_record,
    validate_record_patch,
    visible_author_filter,
)
from app.services.report_service import ReportService
from app.settings.config import settings

logger = logging.getLogger(__name__)

AUDIT_RESOURCE_TYPE = "app_record"


@dataclass
class _Context:
    artifact: Artifact
    report: Report
    spec: CollectionSpec
    principal: Principal


def _unauthenticated() -> AppError:
    return AppError.unauthorized(ErrorCode.APP_DATA_UNAUTHENTICATED, "Sign in to use this app's data")


def _forbidden() -> AppError:
    return AppError.forbidden(ErrorCode.APP_DATA_FORBIDDEN, "Not allowed for this collection")


def _record_not_found() -> AppError:
    return AppError.not_found(ErrorCode.APP_DATA_RECORD_NOT_FOUND, "Record not found")


def _denied(principal: Principal) -> AppError:
    return _unauthenticated() if principal == "anonymous" else _forbidden()


def _validation_error(exc: RecordValidationError) -> AppError:
    # Both params are always present so a localized message never shows a raw
    # placeholder: `field` is "" for record-level problems (not_an_object,
    # record_too_large); `reason` is a stable machine token from app_data_rules.
    params = {"field": exc.field or "", "reason": exc.reason}
    if exc.code == "too_large":
        return AppError(ErrorCode.APP_DATA_TOO_LARGE, "Record is too large", status_code=413, params=params)
    return AppError(ErrorCode.APP_DATA_VALIDATION, f"Invalid record: {exc}", status_code=422, params=params)


def visibility_error(exc: HTTPException) -> Exception:
    """Map a Layer-1 ``_check_visibility`` HTTPException onto app-data errors."""
    if exc.status_code == 401:
        return _unauthenticated()
    if exc.status_code == 403:
        return _forbidden()
    if exc.status_code == 404:
        return AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact not found")
    # Not a visibility decision: never disguise it as a 404.
    return exc


def _readable(record: AppRecord) -> bool:
    """False for rows whose data cannot be read (e.g. undecryptable -> None)."""
    if isinstance(record.data, dict):
        return True
    logger.warning("app_records row %s has unreadable data; skipping it", record.id)
    return False


class AppDataService:
    def __init__(self) -> None:
        self._reports = ReportService()

    # ------------------------------------------------------------------
    # Declaration
    # ------------------------------------------------------------------

    async def effective_declaration(self, db: AsyncSession, artifact_id: str) -> Optional[StorageDeclaration]:
        """Storage declared by the latest completed, live version (or None)."""
        content = (await db.execute(
            select(ArtifactVersion.content)
            .where(
                ArtifactVersion.artifact_id == str(artifact_id),
                ArtifactVersion.status == "completed",
                ArtifactVersion.deleted_at.is_(None),
            )
            .order_by(ArtifactVersion.version.desc())
            .limit(1)
        )).scalar_one_or_none()
        if not isinstance(content, dict):
            return None
        try:
            return parse_storage_declaration(content.get("storage"))
        except ValidationError as exc:
            # Fail closed: a declaration we cannot parse declares nothing. Log
            # field paths and error types only, never declared values.
            problems = ", ".join(
                f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['type']}" for err in exc.errors()
            )
            logger.warning("artifact %s has an invalid storage declaration (%s); treating it as none",
                           artifact_id, problems)
            return None

    # ------------------------------------------------------------------
    # Checks 1-5 (shared by every operation)
    # ------------------------------------------------------------------

    async def _principal(self, db: AsyncSession, report: Report, user: Optional[User]) -> Principal:
        if user is None:
            return "anonymous"
        if str(user.id) == str(report.user_id):
            return "owner"
        member = (await db.execute(
            select(Membership.id).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(report.organization_id),
            ).limit(1)
        )).scalar_one_or_none()
        if member is not None:
            return "member"
        group_ids = ReportService._user_group_ids_subquery(user.id, report.organization_id)
        share = (await db.execute(
            select(ReportShare.id).where(
                ReportShare.report_id == report.id,
                ReportShare.share_type == "artifact",
                ReportShare.deleted_at.is_(None),
                or_(ReportShare.user_id == str(user.id), ReportShare.group_id.in_(group_ids)),
            ).limit(1)
        )).scalar_one_or_none()
        return "member" if share is not None else "outsider"

    async def _open(
        self,
        db: AsyncSession,
        *,
        artifact_id: str,
        collection: str,
        user: Optional[User],
        write: bool,
    ) -> _Context:
        # 1. identity
        if write and user is None:
            raise _unauthenticated()

        # 2. artifact and report exist, same organization
        not_found = AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND, "Artifact not found")
        artifact = (await db.execute(
            select(Artifact).options(lazyload("*")).where(
                Artifact.id == str(artifact_id), Artifact.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if artifact is None:
            raise not_found
        report = (await db.execute(
            select(Report).options(lazyload("*")).where(
                Report.id == str(artifact.report_id), Report.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if report is None or str(report.organization_id) != str(artifact.organization_id):
            raise not_found

        # 3. Layer 1. An AppError (bow source access) passes through unchanged.
        try:
            await self._reports._check_visibility(db, report, "artifact_visibility", user)
        except HTTPException as exc:
            raise visibility_error(exc)

        # 4. collection declared
        declaration = await self.effective_declaration(db, artifact.id)
        spec = declaration.collections.get(collection) if declaration is not None else None
        if spec is None:
            raise AppError.not_found(
                ErrorCode.APP_DATA_COLLECTION_NOT_DECLARED, "Collection is not declared", collection=collection,
            )

        # RD11: an unverified user never writes while verification is enforced.
        # No rule lets an outsider write, so this is the refusal every write
        # would get; the reason tells the viewer what to do about it.
        if write and not user.is_verified and settings.bow_config.features.verify_emails:
            raise AppError.forbidden(ErrorCode.APP_DATA_FORBIDDEN, "Verify your email address to change this app's data",
                                     reason="email_unverified")

        principal = await self._principal(db, report, user)
        return _Context(artifact=artifact, report=report, spec=spec, principal=principal)

    async def _load_record(self, db: AsyncSession, ctx: _Context, collection: str, record_id: str,
                           user: User, *, need_data: bool = True) -> AppRecord:
        record = (await db.execute(
            select(AppRecord).where(
                AppRecord.id == str(record_id),
                AppRecord.artifact_id == str(ctx.artifact.id),
                AppRecord.collection == collection,
                AppRecord.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if record is None or (need_data and not _readable(record)):
            raise _record_not_found()
        # per_user records of someone else do not exist for this caller.
        if ctx.spec.scope == "per_user" and str(record.user_id) != str(user.id):
            raise _record_not_found()
        return record

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    async def _user_names(self, db: AsyncSession, user_ids: List[str]) -> Dict[str, str]:
        if not user_ids:
            return {}
        rows = await db.execute(select(User.id, User.name).where(User.id.in_(set(user_ids))))
        return {str(uid): name for uid, name in rows.all()}

    @staticmethod
    def _to_out(spec: CollectionSpec, record: AppRecord, names: Dict[str, str], user: Optional[User],
                *, data: Dict[str, Any], version: int, updated_at: datetime) -> AppRecordOut:
        author_id = str(record.user_id)
        return AppRecordOut(
            id=str(record.id),
            data=project_record_data(spec, data),
            user=AppRecordUser(id=author_id, name=names[author_id]) if author_id in names else None,
            version=version,
            created_at=record.created_at,
            updated_at=updated_at,
            mine=user is not None and author_id == str(user.id),
        )

    async def _audit(self, db: AsyncSession, ctx: _Context, action: str, user: User, record_id: str,
                     collection: str, version: int, request: Optional[Request]) -> None:
        await audit_service.log(
            db,
            organization_id=str(ctx.report.organization_id),
            action=action,
            user_id=str(user.id),
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=str(record_id),
            details={"artifact_id": str(ctx.artifact.id), "collection": collection, "version": version},
            request=request,
            commit=False,
        )

    async def _versioned_write(self, db: AsyncSession, record_id: str, version: int, values: Dict[str, Any]) -> None:
        """7. Conditional UPDATE; 0 rows -> gone (404) or stale (409)."""
        result = await db.execute(
            update(AppRecord)
            .where(AppRecord.id == str(record_id), AppRecord.version == version, AppRecord.deleted_at.is_(None))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            return
        await db.rollback()
        current = (await db.execute(
            select(AppRecord.version, AppRecord.deleted_at).where(AppRecord.id == str(record_id))
        )).one_or_none()
        if current is None or current.deleted_at is not None:
            raise _record_not_found()
        raise AppError.conflict(
            ErrorCode.APP_DATA_CONFLICT, "Record was changed by someone else", current_version=current.version,
        )

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def list_records(self, db: AsyncSession, *, artifact_id: str, collection: str,
                           user: Optional[User]) -> AppRecordList:
        ctx = await self._open(db, artifact_id=artifact_id, collection=collection, user=user, write=False)
        # 5. can_read decides first; only then is the author filter applied.
        if not can_read(ctx.spec, ctx.principal):
            raise _denied(ctx.principal)
        user_id = str(user.id) if user is not None else None
        if ctx.spec.scope == "per_user" and user_id is None:
            # Never list a private collection without an author filter.
            raise _denied(ctx.principal)
        author = visible_author_filter(ctx.spec, ctx.principal, user_id=user_id)

        stmt = select(AppRecord).where(
            AppRecord.artifact_id == str(ctx.artifact.id),
            AppRecord.collection == collection,
            AppRecord.deleted_at.is_(None),
        )
        if author is not None:
            stmt = stmt.where(AppRecord.user_id == author)
        stmt = stmt.order_by(AppRecord.created_at.asc(), AppRecord.id.asc())
        records = [r for r in (await db.execute(stmt)).scalars().all() if _readable(r)]
        names = await self._user_names(db, [str(r.user_id) for r in records])
        return AppRecordList(items=[
            self._to_out(ctx.spec, r, names, user, data=r.data, version=r.version, updated_at=r.updated_at)
            for r in records
        ])

    async def create_record(self, db: AsyncSession, *, artifact_id: str, collection: str, user: Optional[User],
                            payload: AppRecordCreate, request: Optional[Request] = None) -> AppRecordOut:
        ctx = await self._open(db, artifact_id=artifact_id, collection=collection, user=user, write=True)
        if not can_create(ctx.spec, ctx.principal):
            raise _denied(ctx.principal)
        try:
            data = validate_new_record(ctx.spec, payload.data)
        except RecordValidationError as exc:
            raise _validation_error(exc)
        limit = app_storage.MAX_RECORDS_PER_COLLECTION  # read at call time (tests lower it)
        live = (await db.execute(
            select(func.count(AppRecord.id)).where(
                AppRecord.artifact_id == str(ctx.artifact.id),
                AppRecord.collection == collection,
                AppRecord.deleted_at.is_(None),
            )
        )).scalar_one()
        if live >= limit:
            raise AppError(ErrorCode.APP_DATA_LIMIT_REACHED, "Collection is full", status_code=422,
                           params={"limit": limit})

        record = AppRecord(
            organization_id=str(ctx.report.organization_id),
            report_id=str(ctx.report.id),
            artifact_id=str(ctx.artifact.id),
            collection=collection,
            user_id=str(user.id),
            version=1,
            data=data,
        )
        db.add(record)
        await db.flush()
        await self._audit(db, ctx, "app_data.record_created", user, record.id, collection, 1, request)
        names = await self._user_names(db, [str(user.id)])
        out = self._to_out(ctx.spec, record, names, user, data=data, version=1, updated_at=record.updated_at)
        await db.commit()
        return out

    async def update_record(self, db: AsyncSession, *, artifact_id: str, collection: str, record_id: str,
                            user: Optional[User], payload: AppRecordUpdate,
                            request: Optional[Request] = None) -> AppRecordOut:
        ctx = await self._open(db, artifact_id=artifact_id, collection=collection, user=user, write=True)
        record = await self._load_record(db, ctx, collection, record_id, user)
        if not can_modify(ctx.spec, ctx.principal, user_id=str(user.id), record_user_id=str(record.user_id)):
            raise _denied(ctx.principal)
        try:
            data = validate_record_patch(ctx.spec, record.data, payload.data)
        except RecordValidationError as exc:
            raise _validation_error(exc)

        now = datetime.utcnow()
        new_version = payload.version + 1
        await self._versioned_write(db, record.id, payload.version,
                                    {"data": data, "version": new_version, "updated_at": now})
        await self._audit(db, ctx, "app_data.record_updated", user, record.id, collection, new_version, request)
        names = await self._user_names(db, [str(record.user_id)])
        out = self._to_out(ctx.spec, record, names, user, data=data, version=new_version, updated_at=now)
        await db.commit()
        return out

    async def delete_record(self, db: AsyncSession, *, artifact_id: str, collection: str, record_id: str,
                            user: Optional[User], payload: AppRecordDelete,
                            request: Optional[Request] = None) -> AppRecordDeleted:
        ctx = await self._open(db, artifact_id=artifact_id, collection=collection, user=user, write=True)
        # A delete needs no data, so an unreadable row can still be removed
        # (it counts toward the record limit): by the report owner or its author.
        record = await self._load_record(db, ctx, collection, record_id, user, need_data=False)
        if isinstance(record.data, dict):
            allowed = can_modify(ctx.spec, ctx.principal, user_id=str(user.id), record_user_id=str(record.user_id))
        else:
            allowed = ctx.principal == "owner" or (
                ctx.principal == "member" and str(record.user_id) == str(user.id))
        if not allowed:
            raise _denied(ctx.principal)

        now = datetime.utcnow()
        rid = str(record.id)
        await self._versioned_write(db, rid, payload.version, {"deleted_at": now, "updated_at": now})
        await self._audit(db, ctx, "app_data.record_deleted", user, rid, collection, payload.version, request)
        await db.commit()
        return AppRecordDeleted(id=rid, deleted=True)

    async def collection_stats(self, db: AsyncSession, artifact_id: str) -> Dict[str, Dict[str, int]]:
        """Live record and distinct-author counts per collection."""
        rows = await db.execute(
            select(AppRecord.collection, func.count(AppRecord.id), func.count(distinct(AppRecord.user_id)))
            .where(AppRecord.artifact_id == str(artifact_id), AppRecord.deleted_at.is_(None))
            .group_by(AppRecord.collection)
        )
        return {collection: {"records": records, "users": users} for collection, records, users in rows.all()}


app_data_service = AppDataService()
