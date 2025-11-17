from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlalchemy import and_, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.instruction_label import InstructionLabel, instruction_label_association
from app.models.organization import Organization
from app.models.user import User
from app.schemas.instruction_label_schema import (
    InstructionLabelCreate,
    InstructionLabelSchema,
    InstructionLabelUpdate,
)


class InstructionLabelService:
    """CRUD service for managing instruction labels."""

    async def list_labels(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
    ) -> List[InstructionLabelSchema]:
        """Return all labels for the organization."""
        stmt = (
            select(InstructionLabel)
            .where(
                and_(
                    InstructionLabel.organization_id == organization.id,
                    InstructionLabel.deleted_at == None,
                )
            )
            .order_by(func.lower(InstructionLabel.name))
        )
        res = await db.execute(stmt)
        labels = res.scalars().all()
        return [InstructionLabelSchema.from_orm(label) for label in labels]

    async def create_label(
        self,
        db: AsyncSession,
        payload: InstructionLabelCreate,
        organization: Organization,
        current_user: User,
    ) -> InstructionLabelSchema:
        """Create a new instruction label."""
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Label name is required")

        await self._ensure_unique_name(db, name, organization)

        model = InstructionLabel(
            name=name,
            color=payload.color,
            description=payload.description,
            organization_id=organization.id,
            created_by_user_id=current_user.id if current_user else None,
        )
        db.add(model)
        await db.commit()
        await db.refresh(model)
        return InstructionLabelSchema.from_orm(model)

    async def update_label(
        self,
        db: AsyncSession,
        label_id: str,
        payload: InstructionLabelUpdate,
        organization: Organization,
        current_user: User,
    ) -> InstructionLabelSchema:
        """Update label metadata."""
        label = await self._get_label(db, label_id, organization)

        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Label name cannot be empty")
            if name.lower() != label.name.lower():
                await self._ensure_unique_name(db, name, organization)
            label.name = name

        if payload.color is not None:
            label.color = payload.color

        if payload.description is not None:
            label.description = payload.description

        await db.commit()
        await db.refresh(label)
        return InstructionLabelSchema.from_orm(label)

    async def delete_label(
        self,
        db: AsyncSession,
        label_id: str,
        organization: Organization,
        current_user: User,
    ) -> bool:
        """Soft delete a label."""
        # Get label including deleted ones for idempotent delete
        stmt = select(InstructionLabel).where(
            and_(
                InstructionLabel.id == label_id,
                InstructionLabel.organization_id == organization.id,
            )
        )
        res = await db.execute(stmt)
        label = res.scalar_one_or_none()
        if not label:
            raise HTTPException(status_code=404, detail="Instruction label not found")
        
        # If already deleted, return success (idempotent)
        if label.deleted_at:
            return True

        await self._remove_instruction_associations(db, label.id)
        label.deleted_at = datetime.utcnow()
        await db.commit()
        return True

    async def _ensure_unique_name(
        self,
        db: AsyncSession,
        name: str,
        organization: Organization,
    ):
        stmt = select(InstructionLabel).where(
            and_(
                InstructionLabel.organization_id == organization.id,
                InstructionLabel.deleted_at == None,
                func.lower(InstructionLabel.name) == name.lower(),
            )
        )
        res = await db.execute(stmt)
        existing = res.scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail="A label with that name already exists")

    async def _get_label(
        self,
        db: AsyncSession,
        label_id: str,
        organization: Organization,
    ) -> InstructionLabel:
        stmt = select(InstructionLabel).where(
            and_(
                InstructionLabel.id == label_id,
                InstructionLabel.organization_id == organization.id,
                InstructionLabel.deleted_at == None,
            )
        )
        res = await db.execute(stmt)
        label = res.scalar_one_or_none()
        if not label:
            raise HTTPException(status_code=404, detail="Instruction label not found")
        return label

    async def _remove_instruction_associations(
        self,
        db: AsyncSession,
        label_id: str,
    ) -> None:
        await db.execute(
            delete(instruction_label_association).where(
                instruction_label_association.c.label_id == label_id
            )
        )

