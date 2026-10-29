"""Bounded encrypted file storage; set BOW_ARTIFACT_STORAGE to a shared volume."""

import asyncio
import io
import os
import uuid
from pathlib import Path
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import lazyload
from app.models.file import File
from app.models.artifact_resource import ArtifactFileBinding, ArtifactRecordIndex
from app.services.artifact_resource_service import fail, key, digest

MAX_FILE = 10 * 1024 * 1024


def storage_root():
    value = os.environ.get("BOW_ARTIFACT_STORAGE")
    if not value:
        fail("UNAVAILABLE", "Artifact storage is not configured", 503)
    root = Path(value).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def checked_bytes(data, name):
    if not data or len(data) > MAX_FILE:
        fail("VALIDATION", "File must be between 1 byte and 10 MiB")
    ext = Path(name).suffix.lower()
    if ext == ".txt":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            fail("VALIDATION", "Text files must be UTF-8")
        return data, "text/plain"
    if ext == ".pdf" and data.startswith(b"%PDF-"):
        return data, "application/pdf"
    if ext in (".png", ".jpg", ".jpeg"):
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 20000000 or image.format not in ("PNG", "JPEG"):
                    fail("VALIDATION", "Image dimensions or format are unsupported")
                image.load()
                out = io.BytesIO()
                image.save(out, format=image.format)
                result = out.getvalue()
                if len(result) > MAX_FILE:
                    fail("VALIDATION", "Decoded image is too large")
                return result, "image/png" if image.format == "PNG" else "image/jpeg"
        except (OSError, ValueError):
            fail("VALIDATION", "Invalid image")
    fail("VALIDATION", "Supported files: UTF-8 text, PDF, PNG and JPEG")


class ArtifactFiles:
    def __init__(self, resources):
        self.resources = resources
        self.db = resources.db

    async def upload(self, resource_name, upload):
        root = storage_root()
        chunks, size = [], 0
        while chunk := await upload.read(65536):
            size += len(chunk)
            if size > MAX_FILE:
                fail("VALIDATION", "File exceeds 10 MiB")
            chunks.append(chunk)
        data, media = await asyncio.to_thread(checked_bytes, b"".join(chunks), upload.filename or "")
        from app.models.artifact import Artifact
        from app.models.artifact_resource import ArtifactResource
        from sqlalchemy import update, func
        from datetime import datetime

        await self.db.execute(
            update(Artifact).where(Artifact.id == self.resources.artifact.id).values(updated_at=datetime.utcnow())
        )
        resource, definition = await self.resources.resource(resource_name, lock=True)
        if definition.kind != "files" or not self.resources.actor or not self.resources.member:
            fail("FORBIDDEN", "Authenticated file resource required", 403)
        self.resources.check(definition.permissions.create)
        total = await self.db.scalar(
            select(func.coalesce(func.sum(ArtifactResource.bytes), 0)).where(
                ArtifactResource.artifact_id == self.resources.artifact.id
            )
        )
        if total + len(data) > 268435456:
            fail("QUOTA_EXCEEDED", "Artifact storage quota reached", 429)
        if resource.bytes + len(data) > definition.max_bytes or resource.count >= definition.max_records:
            fail("QUOTA_EXCEEDED", "File quota reached", 429)
        from app.services.artifact_storage_budget import reserve

        await reserve(self.resources, self.resources.actor, len(data), 1)
        identity = str(uuid.uuid4())
        path = root / (identity + ".blob")
        # No partially-written file becomes ready. Remove bytes on failed commit.
        encrypted = await asyncio.to_thread(Fernet(key()).encrypt, data)
        await asyncio.to_thread(path.write_bytes, encrypted)
        file = File(
            id=identity,
            filename=Path(upload.filename or "file").name[:200],
            path=str(path),
            content_type=media,
            source_kind="artifact_resource",
            user_id=self.resources.actor,
            organization_id=self.resources.artifact.organization_id,
        )
        try:
            self.db.add(file)
            await self.db.flush([file])
            self.db.add(
                ArtifactFileBinding(
                    artifact_id=self.resources.artifact.id,
                    resource_id=resource.id,
                    file_id=file.id,
                    owner_id=self.resources.actor,
                    size=len(data),
                )
            )
            resource.bytes += len(data)
            resource.count += 1
            await self.resources.audit("artifact.file.upload", resource.id, file_id=identity)
            await self.db.flush()
            await self.db.commit()
        except BaseException:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise
        return {
            "id": identity,
            "resourceId": resource.id,
            "name": file.filename,
            "mediaType": media,
            "size": len(data),
            "status": "ready",
        }

    async def binding(self, file_id, action="read"):
        binding = (
            await self.db.execute(
                select(ArtifactFileBinding).where(
                    ArtifactFileBinding.file_id == file_id,
                    ArtifactFileBinding.artifact_id == self.resources.artifact.id,
                    ArtifactFileBinding.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            fail("NOT_FOUND", "File not found", 404)
        _, definition = await self.resources.resource(binding.resource_id)
        rule = getattr(definition.permissions, action)
        self.resources.check(rule, binding)
        # Public bytes require a reference in a record readable by this viewer.
        if action == "read" and not any(r.audience != "public" for r in self.resources.allowed_rules(rule, binding)):
            from app.models.artifact_resource import ArtifactRecord, ArtifactResource
            from app.schemas.artifact_resource_schema import ResourceDefinition
            from sqlalchemy import and_, or_, exists

            collections = (
                await self.db.execute(
                    select(ArtifactResource).where(
                        ArtifactResource.artifact_id == self.resources.artifact.id,
                        ArtifactResource.kind == "collection",
                        ArtifactResource.deleted_at.is_(None),
                    )
                )
            ).scalars()
            branches = []
            for collection in collections:
                declared = ResourceDefinition.model_validate(collection.definition)
                for branch in self.resources.allowed_rules(declared.permissions.read):
                    names = [
                        name
                        for name, field in declared.fields.items()
                        if field.type == "file" and (branch.fields is None or name in branch.fields)
                    ]
                    if names:
                        branches.append(
                            and_(
                                self.resources.row_scope(collection.id, branch),
                                exists(
                                    select(ArtifactRecordIndex.id).where(
                                        ArtifactRecordIndex.record_id == ArtifactRecord.id,
                                        ArtifactRecordIndex.resource_id == collection.id,
                                        ArtifactRecordIndex.field.in_(names),
                                        ArtifactRecordIndex.value_hash == digest(file_id),
                                    )
                                ),
                            )
                        )
            # Test existence after applying row and field policies in SQL. A long
            # list of private references cannot hide a later permitted reference.
            if not branches or not await self.db.scalar(select(ArtifactRecord.id).where(or_(*branches)).limit(1)):
                fail("NOT_FOUND", "File not found", 404)
        file = (
            await self.db.execute(
                select(File)
                .options(lazyload("*"))
                .where(
                    File.id == file_id,
                    File.organization_id == self.resources.artifact.organization_id,
                    File.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if file is None:
            fail("NOT_FOUND", "File not found", 404)
        return binding, file

    async def metadata(self, file_id):
        binding, file = await self.binding(file_id)
        return {
            "id": file.id,
            "resourceId": binding.resource_id,
            "name": file.filename,
            "mediaType": file.content_type,
            "size": binding.size,
            "status": "ready",
        }

    async def content(self, file_id):
        _, file = await self.binding(file_id)
        path = storage_root() / (str(file.id) + ".blob")
        if not path.is_file():
            fail("UNAVAILABLE", "File bytes unavailable", 503)
        encrypted = await asyncio.to_thread(path.read_bytes)
        return await asyncio.to_thread(Fernet(key()).decrypt, encrypted), file.content_type

    async def remove(self, file_id):
        from app.services.artifact_resource_service import digest

        # Same lock order as record writes and uploads prevents races with references.
        from app.models.artifact import Artifact
        from sqlalchemy import update
        from datetime import datetime

        await self.db.execute(
            update(Artifact).where(Artifact.id == self.resources.artifact.id).values(updated_at=datetime.utcnow())
        )
        binding, file = await self.binding(file_id, "delete")
        resource, _ = await self.resources.resource(binding.resource_id, lock=True)
        from app.models.artifact_resource import ArtifactResource
        from app.schemas.artifact_resource_schema import ResourceDefinition
        from sqlalchemy import and_, or_

        resources = (
            await self.db.execute(
                select(ArtifactResource).where(
                    ArtifactResource.artifact_id == self.resources.artifact.id, ArtifactResource.deleted_at.is_(None)
                )
            )
        ).scalars()
        fields = []
        for row in resources:
            names = [
                name
                for name, field in ResourceDefinition.model_validate(row.definition).fields.items()
                if field.type == "file"
            ]
            if names:
                fields.append(and_(ArtifactRecordIndex.resource_id == row.id, ArtifactRecordIndex.field.in_(names)))
        if fields and await self.db.scalar(
            select(ArtifactRecordIndex.id)
            .where(ArtifactRecordIndex.value_hash == digest(file_id), or_(*fields))
            .limit(1)
        ):
            fail("CONFLICT", "Remove record references before deleting this file", 409)
        from datetime import datetime
        from app.services.artifact_storage_budget import reserve

        await reserve(self.resources, binding.owner_id, -binding.size, -1)
        binding.deleted_at = file.deleted_at = datetime.utcnow()
        resource.bytes -= binding.size
        resource.count -= 1
        await self.resources.audit("artifact.file.delete", resource.id, file_id=file_id)
        await self.db.commit()
        # A crash here leaves inaccessible encrypted bytes, never an exposed file.
        await asyncio.to_thread((storage_root() / (str(file.id) + ".blob")).unlink, missing_ok=True)
        return {"id": file_id, "deleted": True}
