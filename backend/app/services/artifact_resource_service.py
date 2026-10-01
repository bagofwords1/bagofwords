"""Tenant-scoped resource definitions and records. All surfaces share this service.

Writes serialize on the resource row before inspecting quotas/revisions. Equality
indexes use keyed hashes; encrypted fields are never decrypted for list scans.
"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update, delete, exists, and_, or_
from sqlalchemy.orm import lazyload
from app.errors import AppError
from app.settings.config import settings
from app.models.artifact import Artifact
from app.models.report import Report
from app.models.artifact_resource import ArtifactResource, ArtifactRecord, ArtifactRecordIndex, ArtifactMutation
from app.schemas.artifact_resource_schema import (
    ResourceDefinition,
    ResourceChange,
    RecordRequest,
    validate_record,
)


def fail(code="VALIDATION", message="Invalid artifact resource request", status=400):
    raise AppError("ARTIFACT_RESOURCE_" + code, message, status_code=status)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def key():
    from app.settings.bow_config import encryption_key_is_ephemeral

    if encryption_key_is_ephemeral() and not settings.TESTING:
        fail("UNAVAILABLE", "A stable encryption key is required for artifact resources", 503)
    raw = settings.bow_config.encryption_key
    return raw.encode() if isinstance(raw, str) else raw


def seal(value):
    return Fernet(key()).encrypt(canonical(value).encode()).decode()


def unseal(value):
    return json.loads(Fernet(key()).decrypt(value.encode()))


def digest(value):
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {k: normalize(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        return item

    return hmac.new(key(), canonical(normalize(value)).encode(), hashlib.sha256).hexdigest()


class ArtifactResources:
    def __init__(self, db, artifact, report, user, groups=(), member=False):
        self.db, self.artifact, self.report, self.user = db, artifact, report, user
        self.actor = str(user.id) if user else None
        self.groups = set(groups)
        self.member = member
        self.owner = bool(user and str(report.user_id) == self.actor)

    @classmethod
    async def open(cls, db, artifact_id, user=None, organization_id=None, manage=False):
        from app.services.report_service import ReportService
        from app.core.permission_resolver import principal_belongs_to_org

        # IDs on this API are always parent IDs, never silently version IDs.
        artifact = (
            await db.execute(
                select(Artifact).options(lazyload("*")).where(Artifact.id == artifact_id, Artifact.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if artifact is None or (organization_id and str(artifact.organization_id) != str(organization_id)):
            fail("NOT_FOUND", "Artifact not found", 404)
        report = (
            await db.execute(
                select(Report)
                .options(lazyload("*"))
                .where(
                    Report.id == artifact.report_id,
                    Report.organization_id == artifact.organization_id,
                    Report.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if report is None or report.status == "archived":
            fail("NOT_FOUND", "Artifact not found", 404)
        await ReportService()._check_visibility(db, report, "artifact_visibility", user)
        groups = []
        member = False
        if user:
            if settings.bow_config.features.verify_emails and not user.is_verified:
                fail("FORBIDDEN", "Verified identity required", 403)
            if not await principal_belongs_to_org(db, user, artifact.organization_id):
                # Existing public viewing is allowed; membership cannot imply write authority.
                if manage:
                    fail("FORBIDDEN", "Organization membership required", 403)
            else:
                member = True
                groups = list(
                    (
                        await db.execute(ReportService._user_group_ids_subquery(user.id, artifact.organization_id))
                    ).scalars()
                )
        result = cls(db, artifact, report, user, groups, member)
        if manage:
            if not result.owner:
                fail("FORBIDDEN", "Only the report owner can configure resources", 403)
            from app.core.permissions_decorator import require_org_permission

            await require_org_permission(db, result.actor, str(artifact.organization_id), "update_reports")
        return result

    def audience(self, rule):
        if getattr(rule, "any_of", None):
            return any(self.audience(child) for child in rule.any_of)
        return (
            rule.audience == "public"
            or rule.audience == "owner"
            and self.owner
            or rule.audience == "authenticated"
            and self.member
            or rule.audience == "groups"
            and bool(self.groups.intersection(rule.group_ids))
        )

    def allowed_rules(self, rule, record=None, data=None):
        return [
            r
            for r in (getattr(rule, "any_of", None) or [rule])
            if self.audience(r)
            and (not r.own or self.actor and (record is None or record.owner_id == self.actor))
            and (data is None or all(data.get(k) == v for k, v in r.equals.items()))
        ]

    def check(self, rule, record=None, data=None):
        if not self.allowed_rules(rule, record, data):
            fail("FORBIDDEN", "Resource access denied", 403)

    async def resource(self, name, lock=False):
        clause = and_(
            ArtifactResource.artifact_id == self.artifact.id,
            ArtifactResource.deleted_at.is_(None),
            (ArtifactResource.name == name) | (ArtifactResource.id == name),
        )
        if lock:
            await self.db.execute(
                update(ArtifactResource).where(clause).values(lock_version=ArtifactResource.lock_version + 1)
            )
        row = (
            await self.db.execute(select(ArtifactResource).where(clause).execution_options(populate_existing=True))
        ).scalar_one_or_none()
        if row is None:
            fail("NOT_FOUND", "Resource not found", 404)
        return row, ResourceDefinition.model_validate(row.definition)

    async def definitions(self):
        rows = (
            await self.db.execute(
                select(ArtifactResource)
                .where(ArtifactResource.artifact_id == self.artifact.id, ArtifactResource.deleted_at.is_(None))
                .order_by(ArtifactResource.name)
                .limit(100)
            )
        ).scalars()
        result = []
        for row in rows:
            definition = ResourceDefinition.model_validate(row.definition)
            if self.owner:
                result.append({"id": row.id, "revision": row.revision, **row.definition})
            elif self.audience(definition.permissions.read):
                read_rules = self.allowed_rules(definition.permissions.read)
                allowed = (
                    None
                    if any(r.fields is None for r in read_rules)
                    else set().union(*(set(r.fields) for r in read_rules))
                )
                result.append(
                    {
                        "id": row.id,
                        "name": row.name,
                        "kind": row.kind,
                        "revision": row.revision,
                        "fields": {
                            k: v.model_dump(exclude={"write"})
                            for k, v in definition.fields.items()
                            if allowed is None or k in allowed
                        },
                    }
                )
        return result

    async def audit(self, action, resource_id, **details):
        from app.ee.audit.service import audit_service

        await audit_service.log(
            self.db,
            organization_id=str(self.artifact.organization_id),
            user_id=self.actor,
            action=action,
            resource_type="artifact_resource",
            resource_id=str(resource_id),
            details={"artifact_id": str(self.artifact.id), **details},
            commit=False,
        )

    async def replay(self, request_key, fingerprint):
        if not request_key or not self.actor:
            fail("VALIDATION", "Authenticated mutations require an idempotency key")
        # Retry acknowledgements have a documented seven-day horizon.
        await self.db.execute(
            delete(ArtifactMutation).where(
                ArtifactMutation.artifact_id == self.artifact.id,
                ArtifactMutation.created_at < datetime.utcnow() - timedelta(days=7),
            )
        )
        row = (
            await self.db.execute(
                select(ArtifactMutation).where(
                    ArtifactMutation.artifact_id == self.artifact.id,
                    ArtifactMutation.actor_id == self.actor,
                    ArtifactMutation.key == request_key,
                )
            )
        ).scalar_one_or_none()
        if row:
            if row.fingerprint != fingerprint:
                fail("CONFLICT", "Idempotency key was used for different input", 409)
            return unseal(row.response)

    def remember(self, request_key, fingerprint, response):
        self.db.add(
            ArtifactMutation(
                artifact_id=self.artifact.id,
                actor_id=self.actor,
                key=request_key,
                fingerprint=fingerprint,
                response=seal(response),
            )
        )

    async def configure(self, change: ResourceChange):
        import os

        if os.environ.get("BOW_ARTIFACT_RESOURCES_READ_ONLY") == "true":
            fail("UNAVAILABLE", "Artifact resource writes are temporarily disabled", 503)
        if not self.owner:
            fail("FORBIDDEN", "Only the owner can configure resources", 403)
        # Serializes create/name uniqueness and idempotency on the stable parent.
        await self.db.execute(
            update(Artifact).where(Artifact.id == self.artifact.id).values(updated_at=datetime.utcnow())
        )
        fingerprint = digest(change.model_dump())
        old_response = await self.replay(change.idempotency_key, fingerprint)
        if old_response is not None:
            return old_response
        if change.action == "create":
            if change.definition is None:
                fail()
            from sqlalchemy import func

            count = await self.db.scalar(
                select(func.count())
                .select_from(ArtifactResource)
                .where(ArtifactResource.artifact_id == self.artifact.id)
            )
            if count >= 50:
                fail("QUOTA_EXCEEDED", "Artifact resource limit reached", 429)
            row = ArtifactResource(
                id=str(uuid.uuid4()),
                artifact_id=self.artifact.id,
                organization_id=self.artifact.organization_id,
                name=change.definition.name,
                kind=change.definition.kind,
                definition=change.definition.model_dump(),
                revision=1,
                count=0,
                bytes=0,
            )
            self.db.add(row)
        else:
            row, previous = await self.resource(change.resource, lock=True)
            if change.expected_revision != row.revision:
                fail("CONFLICT", "Resource changed; read its current definition", 409)
            if change.action == "delete":
                if row.count:
                    fail("CONFLICT", "Nonempty resources cannot be deleted", 409)
                dependents = (
                    await self.db.execute(
                        select(ArtifactResource).where(
                            ArtifactResource.artifact_id == self.artifact.id,
                            ArtifactResource.deleted_at.is_(None),
                            ArtifactResource.kind == "ai",
                        )
                    )
                ).scalars()
                if any(r.definition.get("file_resource") in (row.id, row.name) for r in dependents):
                    fail("CONFLICT", "An AI operation still references this resource", 409)
                row.deleted_at = datetime.utcnow()
            else:
                definition = change.definition
                if definition is None or definition.name != row.name or definition.kind != row.kind:
                    fail("VALIDATION", "Resource identity and kind cannot change")
                # No implicit data migrations: populated schemas only gain optional,
                # non-indexed fields. Policy changes remain independently revisioned.
                if row.count:
                    for name, field in previous.fields.items():
                        new = definition.fields.get(name)
                        if new is None or new.model_dump(exclude={"write"}) != field.model_dump(exclude={"write"}):
                            fail("CONFLICT", "This schema change requires a data migration", 409)
                    for name, field in definition.fields.items():
                        if name not in previous.fields and (
                            field.required or field.indexed or field.default is not None
                        ):
                            fail("CONFLICT", "Existing records require an explicit migration", 409)
                row.definition = definition.model_dump()
            row.revision += 1
        definition = change.definition
        if definition:
            if definition.kind == "ai":
                from app.services.llm_service import LLMService
                from app.models.organization import Organization

                org = await self.db.get(Organization, self.artifact.organization_id)
                model_id = definition.model_id or (previous.model_id if change.action == "update" else None)
                model = (
                    await LLMService().get_model_by_id(self.db, org, self.user, model_id)
                    if model_id
                    else await LLMService().get_default_model_for_report(self.db, org, self.user, self.report)
                )
                if (
                    not model
                    or not model.is_enabled
                    or model.deleted_at
                    or not model.provider.is_enabled
                    or model.provider.deleted_at
                ):
                    fail("FORBIDDEN", "Approved model is unavailable", 403)
                definition = definition.model_copy(update={"model_id": str(model.id)})
                row.definition = definition.model_dump()
                if definition.file_resource:
                    _, files = await self.resource(definition.file_resource)
                    if files.kind != "files":
                        fail("VALIDATION", "AI file binding must reference a file resource")
            from app.models.group import Group

            ids = set()
            for rule in [
                definition.permissions.read,
                definition.permissions.create,
                definition.permissions.update,
                definition.permissions.delete,
            ] + [f.write for f in definition.fields.values() if f.write]:
                for leaf in rule.any_of or [rule]:
                    ids.update(leaf.group_ids)
            if ids:
                found = set(
                    (
                        await self.db.execute(
                            select(Group.id).where(
                                Group.id.in_(ids),
                                Group.organization_id == self.artifact.organization_id,
                                Group.deleted_at.is_(None),
                            )
                        )
                    ).scalars()
                )
                if found != ids:
                    fail("VALIDATION", "Unknown group in this organization")
        await self.db.flush()
        response = {"id": row.id, "revision": row.revision, "deleted": row.deleted_at is not None}
        await self.audit("artifact.resource." + change.action, row.id, revision=row.revision)
        self.remember(change.idempotency_key, fingerprint, response)
        return response

    def serialize(self, row, rule):
        data = unseal(row.payload)
        permitted = self.allowed_rules(rule, row, data)
        if not permitted:
            fail("NOT_FOUND", "Record not found", 404)
        if all(r.fields is not None for r in permitted):
            fields = set().union(*(set(r.fields) for r in permitted))
            data = {k: v for k, v in data.items() if k in fields}
        return {
            "id": row.id,
            "data": data,
            "revision": row.revision,
            "createdAt": row.created_at.isoformat(),
            "updatedAt": row.updated_at.isoformat(),
        }

    def row_scope(self, resource_id, rule, filters=None):
        """Shared SQL authorization for row reads and public file references."""
        filters = filters or {}
        branches = []
        for branch in self.allowed_rules(rule):
            if branch.fields is not None and not set(filters) <= set(branch.fields):
                continue
            if any(k in filters and filters[k] != v for k, v in branch.equals.items()):
                continue
            conditions = [ArtifactRecord.resource_id == resource_id]
            if branch.own:
                conditions.append(ArtifactRecord.owner_id == self.actor)
            for field, value in (filters | branch.equals).items():
                conditions.append(
                    exists(
                        select(ArtifactRecordIndex.id).where(
                            ArtifactRecordIndex.record_id == ArtifactRecord.id,
                            ArtifactRecordIndex.resource_id == resource_id,
                            ArtifactRecordIndex.field == field,
                            ArtifactRecordIndex.value_hash == digest(value),
                        )
                    )
                )
            branches.append(and_(*conditions))
        from sqlalchemy import false

        return or_(*branches) if branches else false()

    async def records(self, name, req: RecordRequest):
        write = req.action in ("create", "update", "delete")
        if write:
            import os

            if os.environ.get("BOW_ARTIFACT_RESOURCES_READ_ONLY") == "true":
                fail("UNAVAILABLE", "Artifact resource writes are temporarily disabled", 503)
            await self.db.execute(
                update(Artifact).where(Artifact.id == self.artifact.id).values(updated_at=datetime.utcnow())
            )
        resource, definition = await self.resource(name, lock=write)
        if resource.kind != "collection":
            fail("VALIDATION", "Not a collection")
        rule = getattr(definition.permissions, "read" if not write else req.action)
        self.check(rule)
        if write and (not self.actor or not self.member):
            fail("UNAUTHENTICATED", "Sign in to write records", 401)
        clause = [ArtifactRecord.resource_id == resource.id]
        filters = dict(req.filter)
        for field, value in filters.items():
            declared = definition.fields.get(field)
            if declared is None or not declared.indexed:
                fail("VALIDATION", "Filtering requires a permitted indexed field")
            from app.schemas.artifact_resource_schema import validate_value

            try:
                validate_value(declared, value)
            except ValueError:
                fail("VALIDATION", "Filter value does not match the field type")
        clause.append(self.row_scope(resource.id, rule, filters))
        if req.action == "list":
            scope = digest([resource.id, self.actor, resource.revision, filters, req.order_by])
            descending = req.order_by.startswith("-")
            by_created = req.order_by.lstrip("-") == "created_at"
            column = ArtifactRecord.created_at if by_created else ArtifactRecord.id
            if req.cursor:
                try:
                    cursor = unseal(req.cursor)
                    if cursor["scope"] != scope:
                        raise ValueError()
                    value = datetime.fromisoformat(cursor["created"]) if by_created else cursor["id"]
                    comparison = column < value if descending else column > value
                    tie = ArtifactRecord.id < cursor["id"] if descending else ArtifactRecord.id > cursor["id"]
                    clause.append(or_(comparison, and_(column == value, tie)))
                except (ValueError, KeyError, InvalidToken):
                    fail("VALIDATION", "Invalid or expired cursor")
            rows = list(
                (
                    await self.db.execute(
                        select(ArtifactRecord)
                        .where(*clause)
                        .order_by(
                            column.desc() if descending else column.asc(),
                            ArtifactRecord.id.desc() if descending else ArtifactRecord.id.asc(),
                        )
                        .limit(req.limit + 1)
                    )
                ).scalars()
            )
            items, size = [], 0
            for row in rows[: req.limit]:
                item = self.serialize(row, rule)
                length = len(canonical(item).encode())
                if items and size + length > 1048576:
                    break
                items.append(item)
                size += length
            more = len(rows) > len(items)
            return {
                "items": items,
                "nextCursor": seal({"scope": scope, "id": items[-1]["id"], "created": items[-1]["createdAt"]})
                if more and items
                else None,
            }
        fingerprint = digest([resource.id, req.model_dump()])
        if write:
            prior = await self.replay(req.idempotency_key, fingerprint)
            if prior is not None:
                return prior
        row = None
        if req.action != "create":
            row = (
                await self.db.execute(select(ArtifactRecord).where(*clause, ArtifactRecord.id == req.id))
            ).scalar_one_or_none()
            if row is None:
                fail("NOT_FOUND", "Record not found", 404)
        if not write:
            return self.serialize(row, rule)
        if row and req.expected_revision != row.revision:
            fail("CONFLICT", "Record changed; refresh before editing", 409)
        if req.action == "delete":
            from app.services.artifact_storage_budget import reserve

            await reserve(self, row.owner_id, -row.size, -1)
            resource.count -= 1
            resource.bytes -= row.size
            await self.db.execute(delete(ArtifactRecordIndex).where(ArtifactRecordIndex.record_id == row.id))
            await self.db.delete(row)
            result = {"id": req.id, "deleted": True}
        else:
            before = unseal(row.payload) if row else {}
            try:
                data = validate_record(definition, before | req.data)
            except ValueError as exc:
                fail("VALIDATION", str(exc))
            permitted = [
                r
                for r in self.allowed_rules(rule, row, data)
                if (not row or all(before.get(k) == v for k, v in r.equals.items()))
                and (r.fields is None or set(req.data) <= set(r.fields))
            ]
            if not permitted:
                fail("FORBIDDEN", "Field mutation or record transition is not permitted", 403)
            for name, field in definition.fields.items():
                # The configured initial default is allowed; changing it requires
                # the field's stronger policy, including on record creation.
                baseline = before.get(name) if row else field.default
                if field.write and data.get(name) != baseline:
                    self.check(field.write, row, data)
                if field.type == "file" and data.get(name):
                    # File references are validated by the scoped file service.
                    from app.services.artifact_file_service import ArtifactFiles

                    await ArtifactFiles(self).metadata(data[name])
            size = len(canonical(data).encode())
            new_count = resource.count + (0 if row else 1)
            new_bytes = resource.bytes + size - (row.size if row else 0)
            from sqlalchemy import func

            total_bytes = await self.db.scalar(
                select(func.coalesce(func.sum(ArtifactResource.bytes), 0)).where(
                    ArtifactResource.artifact_id == self.artifact.id
                )
            )
            if total_bytes + size - (row.size if row else 0) > 268435456:
                fail("QUOTA_EXCEEDED", "Artifact storage quota reached", 429)
            if new_count > definition.max_records or new_bytes > definition.max_bytes:
                fail("QUOTA_EXCEEDED", "Collection quota reached", 429)
            from app.services.artifact_storage_budget import reserve

            await reserve(self, row.owner_id if row else self.actor, size - (row.size if row else 0), 0 if row else 1)
            if row:
                await self.db.execute(delete(ArtifactRecordIndex).where(ArtifactRecordIndex.record_id == row.id))
                row.revision += 1
            else:
                row = ArtifactRecord(id=str(uuid.uuid4()), resource_id=resource.id, owner_id=self.actor, revision=1)
                self.db.add(row)
            row.payload, row.size = seal(data), size
            resource.count, resource.bytes = new_count, new_bytes
            for name, field in definition.fields.items():
                if (field.indexed or field.type == "file") and name in data:
                    hashed = digest(data[name])
                    self.db.add(
                        ArtifactRecordIndex(
                            resource_id=resource.id,
                            record_id=row.id,
                            field=name,
                            value_hash=hashed,
                            unique_hash=hashed if field.unique and data[name] is not None else None,
                        )
                    )
            await self.db.flush()
            result = {"id": row.id, "revision": row.revision}
        await self.audit("artifact.record." + req.action, resource.id, record_id=result["id"])
        self.remember(req.idempotency_key, fingerprint, result)
        await self.db.flush()
        return result
