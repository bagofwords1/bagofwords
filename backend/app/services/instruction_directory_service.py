from typing import List, Optional

from sqlalchemy import and_, or_, func, delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_directory import (
    InstructionDirectory,
    InstructionDirectoryPlacement,
)
from app.models.organization import Organization
from app.models.user import User
from app.schemas.instruction_directory_schema import (
    InstructionDirectoryCreate,
    InstructionDirectorySchema,
    InstructionDirectoryTreeSchema,
    InstructionDirectoryUpdate,
    InstructionPlacementSchema,
)

DIRECTORY_NOT_FOUND = ErrorCode.INSTRUCTION_DIRECTORY_NOT_FOUND


class InstructionDirectoryService:
    """Cosmetic, per-agent instruction folders.

    A directory belongs to one agent (``data_source_id``) or to the Global
    group (``data_source_id is None``). Placement of an instruction into a
    directory lives on the edge, one per (instruction, scope). Nothing here
    touches AI context — it only drives how the /agents tree renders.
    """

    # ── scope helpers ──────────────────────────────────────────────────────
    def _scope_clause(self, organization: Organization, data_source_id: Optional[str]):
        cond = [
            InstructionDirectory.organization_id == organization.id,
            InstructionDirectory.deleted_at == None,  # noqa: E711
        ]
        if data_source_id:
            cond.append(InstructionDirectory.data_source_id == data_source_id)
        else:
            cond.append(InstructionDirectory.data_source_id == None)  # noqa: E711
        return and_(*cond)

    async def get_tree(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        data_source_id: Optional[str],
    ) -> InstructionDirectoryTreeSchema:
        dirs_res = await db.execute(
            select(InstructionDirectory)
            .where(self._scope_clause(organization, data_source_id))
            .order_by(InstructionDirectory.position.asc(), func.lower(InstructionDirectory.name))
        )
        directories = dirs_res.scalars().all()
        dir_ids = [d.id for d in directories]

        placements: List[InstructionPlacementSchema] = []
        if dir_ids:
            pl_res = await db.execute(
                select(InstructionDirectoryPlacement).where(
                    and_(
                        InstructionDirectoryPlacement.directory_id.in_(dir_ids),
                        InstructionDirectoryPlacement.deleted_at == None,  # noqa: E711
                    )
                )
            )
            placements = [
                InstructionPlacementSchema(
                    instruction_id=str(p.instruction_id),
                    directory_id=str(p.directory_id),
                )
                for p in pl_res.scalars().all()
            ]

        return InstructionDirectoryTreeSchema(
            directories=[InstructionDirectorySchema.from_orm(d) for d in directories],
            placements=placements,
        )

    async def _get_directory(
        self, db: AsyncSession, organization: Organization, directory_id: str
    ) -> InstructionDirectory:
        res = await db.execute(
            select(InstructionDirectory).where(
                and_(
                    InstructionDirectory.id == directory_id,
                    InstructionDirectory.organization_id == organization.id,
                    InstructionDirectory.deleted_at == None,  # noqa: E711
                )
            )
        )
        directory = res.scalar_one_or_none()
        if not directory:
            raise AppError.not_found(DIRECTORY_NOT_FOUND, "Instruction directory not found")
        return directory

    async def create(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        payload: InstructionDirectoryCreate,
    ) -> InstructionDirectorySchema:
        name = (payload.name or "").strip()
        if not name:
            raise AppError(ErrorCode.VALIDATION, "Directory name is required")

        scope = payload.data_source_id or None

        parent_id = payload.parent_id or None
        if parent_id:
            parent = await self._get_directory(db, organization, parent_id)
            # A directory can only nest under a parent in the same scope.
            if (parent.data_source_id or None) != scope:
                raise AppError(ErrorCode.VALIDATION, "Parent directory is in a different scope")

        await self._ensure_unique_name(db, organization, scope, parent_id, name)

        # Append at the end of its sibling row.
        max_pos_res = await db.execute(
            select(func.max(InstructionDirectory.position)).where(
                and_(
                    self._scope_clause(organization, scope),
                    (InstructionDirectory.parent_id == parent_id)
                    if parent_id
                    else (InstructionDirectory.parent_id == None),  # noqa: E711
                )
            )
        )
        next_pos = (max_pos_res.scalar() or 0) + 1

        directory = InstructionDirectory(
            organization_id=organization.id,
            data_source_id=scope,
            parent_id=parent_id,
            name=name,
            position=next_pos,
            created_by_user_id=current_user.id if current_user else None,
        )
        db.add(directory)
        await db.commit()
        await db.refresh(directory)
        return InstructionDirectorySchema.from_orm(directory)

    async def update(
        self,
        db: AsyncSession,
        organization: Organization,
        directory_id: str,
        payload: InstructionDirectoryUpdate,
        parent_provided: bool,
    ) -> InstructionDirectorySchema:
        directory = await self._get_directory(db, organization, directory_id)

        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise AppError(ErrorCode.VALIDATION, "Directory name cannot be empty")
            directory.name = name

        if parent_provided:
            new_parent_id = payload.parent_id or None
            if new_parent_id == directory.id:
                raise AppError(ErrorCode.VALIDATION, "A directory cannot be its own parent")
            if new_parent_id:
                parent = await self._get_directory(db, organization, new_parent_id)
                if (parent.data_source_id or None) != (directory.data_source_id or None):
                    raise AppError(ErrorCode.VALIDATION, "Cannot move a directory to a different scope")
                # Reject moving a directory under one of its own descendants.
                if await self._is_descendant(db, organization, ancestor_id=directory.id, node_id=new_parent_id):
                    raise AppError(ErrorCode.VALIDATION, "Cannot move a directory into its own subtree")
            directory.parent_id = new_parent_id

        if payload.position is not None:
            directory.position = payload.position

        await db.commit()
        await db.refresh(directory)
        return InstructionDirectorySchema.from_orm(directory)

    async def delete(
        self,
        db: AsyncSession,
        organization: Organization,
        directory_id: str,
    ) -> bool:
        """Delete a directory. Children re-parent to this directory's parent
        (never cascade-delete folders); placements in this directory are
        removed so their instructions fall back to the scope root. Instructions
        themselves are never touched."""
        directory = await self._get_directory(db, organization, directory_id)

        # Re-parent immediate children up one level.
        await db.execute(
            InstructionDirectory.__table__.update()
            .where(
                and_(
                    InstructionDirectory.parent_id == directory.id,
                    InstructionDirectory.organization_id == organization.id,
                )
            )
            .values(parent_id=directory.parent_id)
        )
        # Drop placements that pointed at this directory (instructions -> root).
        await db.execute(
            sa_delete(InstructionDirectoryPlacement).where(
                InstructionDirectoryPlacement.directory_id == directory.id
            )
        )
        await db.delete(directory)
        await db.commit()
        return True

    async def set_placement(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        instruction_id: str,
        directory_id: Optional[str],
        data_source_id: Optional[str],
    ) -> InstructionDirectoryTreeSchema:
        """Place ``instruction`` into ``directory`` (or, when directory_id is
        None, remove it from any directory in the given scope). Enforces one
        placement per (instruction, scope). Returns the refreshed tree for the
        affected scope so the caller can reconcile without a second round-trip."""
        instruction = await self._get_instruction(db, organization, instruction_id)

        if directory_id:
            directory = await self._get_directory(db, organization, directory_id)
            scope = directory.data_source_id or None
        else:
            scope = data_source_id or None
            directory = None

        # An instruction can only be filed in a scope it actually belongs to:
        # the agent must be one of its data sources, or it must be global.
        ds_ids = await self._instruction_data_source_ids(db, instruction_id)
        if scope is None:
            if ds_ids:
                raise AppError(ErrorCode.VALIDATION, "This instruction is not a global instruction")
        else:
            if scope not in ds_ids:
                raise AppError(ErrorCode.VALIDATION, "This instruction is not attached to that agent")

        # Remove any existing placement for this instruction WITHIN this scope.
        existing_dir_ids = (
            await db.execute(
                select(InstructionDirectory.id).where(self._scope_clause(organization, scope))
            )
        ).scalars().all()
        if existing_dir_ids:
            await db.execute(
                sa_delete(InstructionDirectoryPlacement).where(
                    and_(
                        InstructionDirectoryPlacement.instruction_id == instruction_id,
                        InstructionDirectoryPlacement.directory_id.in_(existing_dir_ids),
                    )
                )
            )

        if directory is not None:
            db.add(
                InstructionDirectoryPlacement(
                    instruction_id=instruction_id,
                    directory_id=directory.id,
                )
            )

        await db.commit()
        return await self.get_tree(db, organization, current_user, scope)

    # ── internals ──────────────────────────────────────────────────────────
    async def _get_instruction(
        self, db: AsyncSession, organization: Organization, instruction_id: str
    ) -> Instruction:
        res = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None,  # noqa: E711
                )
            )
        )
        instruction = res.scalar_one_or_none()
        if not instruction:
            raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
        return instruction

    async def _instruction_data_source_ids(
        self, db: AsyncSession, instruction_id: str
    ) -> set:
        res = await db.execute(
            select(instruction_data_source_association.c.data_source_id).where(
                instruction_data_source_association.c.instruction_id == instruction_id
            )
        )
        return {str(r[0]) for r in res.all()}

    async def _ensure_unique_name(
        self,
        db: AsyncSession,
        organization: Organization,
        scope: Optional[str],
        parent_id: Optional[str],
        name: str,
    ) -> None:
        res = await db.execute(
            select(InstructionDirectory).where(
                and_(
                    self._scope_clause(organization, scope),
                    (InstructionDirectory.parent_id == parent_id)
                    if parent_id
                    else (InstructionDirectory.parent_id == None),  # noqa: E711
                    func.lower(InstructionDirectory.name) == name.lower(),
                )
            )
        )
        if res.scalar_one_or_none():
            raise AppError(ErrorCode.VALIDATION, "A folder with that name already exists here")

    async def _is_descendant(
        self,
        db: AsyncSession,
        organization: Organization,
        ancestor_id: str,
        node_id: str,
    ) -> bool:
        """True if node_id is ancestor_id itself or somewhere below it."""
        current = node_id
        seen = set()
        while current:
            if current == ancestor_id:
                return True
            if current in seen:
                break
            seen.add(current)
            res = await db.execute(
                select(InstructionDirectory.parent_id).where(
                    and_(
                        InstructionDirectory.id == current,
                        InstructionDirectory.organization_id == organization.id,
                    )
                )
            )
            row = res.scalar_one_or_none()
            current = str(row) if row else None
        return False
