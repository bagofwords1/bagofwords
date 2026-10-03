# Audit Log Stream service
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ee.audit.service import audit_service
from app.ee.audit.streams.destinations import REGISTRY, build_destination
from app.ee.audit.streams.destinations.base import FATAL, SendResult
from app.ee.audit.streams.envelope import ENVELOPE_VERSION, iso_utc
from app.ee.audit.streams.exporter import stream_status
from app.ee.audit.streams.models import AuditLogStream
from app.ee.audit.streams.schemas import (
    DestinationField,
    DestinationSpec,
    StreamCreate,
    StreamResponse,
    StreamStatus,
    StreamTestRequest,
    StreamTestResult,
    StreamUpdate,
)
from app.errors import AppError, ErrorCode

MASK = "••••"


def mask_secret(value: Any) -> str:
    s = str(value or "")
    return MASK + s[-4:] if len(s) > 8 else MASK


def destination_specs() -> List[DestinationSpec]:
    return [
        DestinationSpec(type=t, fields=[DestinationField(**f.to_dict()) for f in cls.fields])
        for t, cls in REGISTRY.items()
    ]


def _coerce(key: str, kind: str, value: Any) -> Any:
    if kind == "bool":
        return value is True or str(value).lower() in ("true", "1", "yes", "on")
    if kind == "number":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise AppError.bad_request(ErrorCode.AUDIT_STREAM_INVALID_FIELD, f"{key} must be a number", field=key)
    if kind == "headers":
        if not isinstance(value, dict):
            raise AppError.bad_request(ErrorCode.AUDIT_STREAM_INVALID_FIELD, f"{key} must be an object", field=key)
        return {str(k): str(v) for k, v in value.items()}
    return value.strip() if isinstance(value, str) else value


def normalize(destination: str, config: Dict[str, Any], secrets: Dict[str, Any]) -> Tuple[dict, dict]:
    """Keep only declared fields, coerce types, enforce required fields."""
    cls = REGISTRY.get(destination)
    if cls is None:
        raise AppError.bad_request(ErrorCode.AUDIT_STREAM_INVALID_DESTINATION, f"Unknown destination: {destination}", destination=destination)
    out_c: dict = {}
    out_s: dict = {}
    for f in cls.fields:
        src = secrets if f.secret else config
        v = src.get(f.key)
        if v in (None, ""):
            if f.required and f.default is None:
                raise AppError.bad_request(ErrorCode.AUDIT_STREAM_MISSING_FIELD, f"Missing required field: {f.key}", field=f.key)
            continue
        v = _coerce(f.key, f.kind, v)
        if f.kind == "url" and not str(v).lower().startswith(("http://", "https://")):
            raise AppError.bad_request(ErrorCode.AUDIT_STREAM_INVALID_FIELD, f"{f.key} must be an http(s) URL", field=f.key)
        (out_s if f.secret else out_c)[f.key] = v
    return out_c, out_s


def _merge_secrets(stored: dict, incoming: Optional[dict]) -> dict:
    merged = dict(stored)
    for k, v in (incoming or {}).items():
        if v is None or (isinstance(v, str) and v.startswith(MASK)):
            continue
        if v == "":
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


def _activate(st: AuditLogStream, now: datetime) -> None:
    st.state = "active"
    st.next_attempt_at = None
    st.consecutive_failures = 0
    st.last_error = None
    if st.cursor_seq is None and st.start_from == "now" and st.start_after is None:
        st.start_after = now


class AuditStreamService:
    async def _get(self, db: AsyncSession, organization_id: str, stream_id: str) -> AuditLogStream:
        st = (await db.execute(
            select(AuditLogStream).where(
                AuditLogStream.id == stream_id,
                AuditLogStream.organization_id == organization_id,
                AuditLogStream.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if st is None:
            raise AppError.not_found(ErrorCode.AUDIT_STREAM_NOT_FOUND, "Audit log stream not found")
        return st

    async def serialize(self, db: AsyncSession, st: AuditLogStream) -> StreamResponse:
        try:
            secrets = {k: mask_secret(v) for k, v in st.get_secrets().items()}
        except Exception:
            secrets = {}
        return StreamResponse(
            id=st.id, name=st.name, destination=st.destination, config=st.config or {},
            secrets=secrets, action_filter=st.action_filter, state=st.state, start_from=st.start_from,
            delivered_count=st.delivered_count or 0,
            last_delivered_at=st.last_delivered_at, last_attempt_at=st.last_attempt_at,
            last_error=st.last_error, consecutive_failures=st.consecutive_failures or 0,
            next_attempt_at=st.next_attempt_at, created_at=st.created_at, updated_at=st.updated_at,
            status=StreamStatus(**(await stream_status(db, st))),
        )

    async def list(self, db: AsyncSession, organization_id: str) -> List[StreamResponse]:
        rows = (await db.execute(
            select(AuditLogStream).where(
                AuditLogStream.organization_id == organization_id,
                AuditLogStream.deleted_at.is_(None),
            ).order_by(AuditLogStream.created_at)
        )).scalars().all()
        return [await self.serialize(db, st) for st in rows]

    async def get(self, db: AsyncSession, organization_id: str, stream_id: str) -> StreamResponse:
        return await self.serialize(db, await self._get(db, organization_id, stream_id))

    async def create(self, db: AsyncSession, organization_id: str, user_id: str, data: StreamCreate, request=None) -> StreamResponse:
        config, secrets = normalize(data.destination, data.config, data.secrets)
        if data.destination == "s3" and config.get("role_arn") and not config.get("external_id"):
            config["external_id"] = uuid.uuid4().hex
        st = AuditLogStream(
            organization_id=organization_id, name=data.name.strip(), destination=data.destination,
            config=config, action_filter=[p for p in (data.action_filter or []) if p] or None,
            start_from=data.start_from, state="inactive", created_by_user_id=user_id,
            delivered_count=0, consecutive_failures=0,
        )
        st.set_secrets(secrets)
        if data.activate:
            _activate(st, datetime.utcnow())
        db.add(st)
        await db.flush()
        await audit_service.log(
            db=db, organization_id=organization_id, action="audit_stream.created", user_id=user_id,
            resource_type="audit_log_stream", resource_id=st.id,
            details={"title": st.name, "destination": st.destination, "state": st.state, "start_from": st.start_from},
            request=request, commit=False,
        )
        await db.commit()
        await db.refresh(st)
        return await self.serialize(db, st)

    async def update(self, db: AsyncSession, organization_id: str, user_id: str, stream_id: str, data: StreamUpdate, request=None) -> StreamResponse:
        st = await self._get(db, organization_id, stream_id)
        changed: Dict[str, Any] = {}
        if data.name is not None and data.name.strip() != st.name:
            st.name = data.name.strip()
            changed["name"] = st.name
        if data.config is not None or data.secrets is not None:
            merged_secrets = _merge_secrets(st.get_secrets(), data.secrets)
            config, secrets = normalize(st.destination, data.config if data.config is not None else (st.config or {}), merged_secrets)
            if st.destination == "s3" and config.get("role_arn") and not config.get("external_id"):
                config["external_id"] = (st.config or {}).get("external_id") or uuid.uuid4().hex
            if config != (st.config or {}):
                changed["config"] = sorted(config.keys())
            st.config = config
            if secrets != st.get_secrets():
                changed["secrets"] = sorted(secrets.keys())  # names only, never values
            st.set_secrets(secrets)
        if data.action_filter is not None:
            st.action_filter = [p for p in data.action_filter if p] or None
            changed["action_filter"] = st.action_filter
        action = "audit_stream.updated"
        if data.state == "active" and st.state != "active":
            _activate(st, datetime.utcnow())
            action = "audit_stream.activated"
        elif data.state == "active":
            st.next_attempt_at = None  # "resume now" on an already active stream
        elif data.state == "inactive" and st.state != "inactive":
            st.state = "inactive"
            st.next_attempt_at = None
            action = "audit_stream.paused"
        await audit_service.log(
            db=db, organization_id=organization_id, action=action, user_id=user_id,
            resource_type="audit_log_stream", resource_id=st.id,
            details={"title": st.name, "destination": st.destination, "state": st.state, "changed": changed},
            request=request, commit=False,
        )
        await db.commit()
        await db.refresh(st)
        return await self.serialize(db, st)

    async def delete(self, db: AsyncSession, organization_id: str, user_id: str, stream_id: str, request=None) -> None:
        st = await self._get(db, organization_id, stream_id)
        details = {"title": st.name, "destination": st.destination, "delivered_count": st.delivered_count}
        await db.delete(st)
        await audit_service.log(
            db=db, organization_id=organization_id, action="audit_stream.deleted", user_id=user_id,
            resource_type="audit_log_stream", resource_id=stream_id, details=details,
            request=request, commit=False,
        )
        await db.commit()

    async def test(self, db: AsyncSession, organization_id: str, user, data: StreamTestRequest) -> StreamTestResult:
        stored: dict = {}
        if data.stream_id:
            st = await self._get(db, organization_id, data.stream_id)
            if st.destination != data.destination:
                raise AppError.bad_request(ErrorCode.AUDIT_STREAM_INVALID_DESTINATION, "Destination cannot be changed", destination=data.destination)
            stored = st.get_secrets()
        config, secrets = normalize(data.destination, data.config, _merge_secrets(stored, data.secrets))
        now = datetime.utcnow()
        event = {
            "id": str(uuid.uuid4()),
            "version": ENVELOPE_VERSION,
            "action": "audit_stream.test",
            "occurred_at": iso_utc(now),
            "organization": {"id": organization_id, "name": None},
            "actor": {"type": "user", "id": str(user.id), "email": getattr(user, "email", None)},
            "targets": [{"type": "audit_log_stream", "id": data.stream_id}],
            "context": {"ip_address": None, "user_agent": None},
            "metadata": {"test": True, "message": "Bag of Words audit log stream test event"},
        }
        dest = build_destination(data.destination, config, secrets)
        try:
            res = await dest.send([event])
        except Exception as e:
            res = SendResult(FATAL, f"{type(e).__name__}: {e}"[:500])
        finally:
            await dest.close()
        return StreamTestResult(ok=res.ok, kind=res.kind, error=res.error, status=res.status)


audit_stream_service = AuditStreamService()
