"""Install pre-built skills from the code-defined catalog into an organization.

The catalog itself is read-only content shipped with the product
(``app.ai.skills.library``). Installing an entry **copies** its body into a
normal ``Instruction`` row (``kind='skill'``) owned by the org, stamped with
``catalog_key`` + ``catalog_version``. From then on it is an ordinary skill:
advertised in ``<available_skills>``, pulled on demand via ``read_instruction``,
editable and deletable through the existing instruction surfaces.

Copy-on-install (rather than referencing a global row) is what lets an admin
tune a shipped skill for their org. The version stamp is what lets a later
catalog bump be detected without matching on titles, and ``is_customized``
tells the admin whether an update would overwrite their edits.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.skills.catalog import PrebuiltSkill, get_prebuilt_skill, list_prebuilt_skills
from app.models.instruction import Instruction
from app.models.organization import Organization
from app.models.user import User
from app.schemas.instruction_schema import InstructionCreate, InstructionUpdate
from app.services.instruction_service import InstructionService

logger = logging.getLogger(__name__)


class SkillCatalogService:
    def __init__(self, instruction_service: Optional[InstructionService] = None):
        self.instruction_service = instruction_service or InstructionService()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def _installed_by_key(
        self, db: AsyncSession, organization: Organization
    ) -> Dict[str, List[Instruction]]:
        """catalog_key -> every live row the org has for it, oldest first.

        Normally one row per key: ``install`` checks before creating. That check
        is not atomic and there is no unique constraint behind it, so two
        concurrent installs can both win. Returning the full list (rather than
        collapsing to one) is what lets ``uninstall`` clear the duplicate too —
        otherwise one Disable click leaves a second copy of the same playbook
        advertised in <available_skills>.
        """
        result = await db.execute(
            select(Instruction)
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.catalog_key.isnot(None),
                    Instruction.deleted_at.is_(None),
                )
            )
            .order_by(Instruction.created_at.asc())
        )
        rows: Dict[str, List[Instruction]] = {}
        for row in result.scalars().all():
            rows.setdefault(row.catalog_key, []).append(row)
        for key, key_rows in rows.items():
            if len(key_rows) > 1:
                logger.warning(
                    "org %s has %d rows for pre-built skill %s — the playbook is "
                    "advertised more than once; disabling it will clear them all",
                    organization.id, len(key_rows), key,
                )
        return rows

    @staticmethod
    def _is_customized(skill: PrebuiltSkill, row: Instruction) -> bool:
        """True when the org's copy diverges from the shipped entry.

        Covers every field ``update_to_latest`` overwrites, not just the body:
        an admin who scopes an installed skill to one channel without touching
        the text would otherwise be reported as un-customized, the UI would skip
        its confirmation, and the update would silently reset that scoping.
        (``data_source_ids`` is deliberately absent — the update leaves agent
        scoping alone, so it is not a divergence an update would destroy.)
        """
        def _list(value) -> list:
            return list(value) if isinstance(value, list) else []

        return (
            (row.text or "").strip() != skill.body.strip()
            or (row.title or "") != skill.title
            or (row.description or "") != skill.description
            or (row.category or "") != skill.category
            or _list(row.applicable_modes) != list(skill.modes)
            or _list(row.applicable_channels) != list(skill.channels)
        )

    @classmethod
    def _entry_state(
        cls, skill: PrebuiltSkill, rows: Optional[List[Instruction]],
    ) -> Dict:
        """One catalog entry plus this org's installation state."""
        entry = skill.to_dict()
        if not rows:
            entry.update(
                installed=False,
                instruction_id=None,
                installed_version=None,
                update_available=False,
                is_customized=False,
                status=None,
                duplicate_count=0,
            )
            return entry

        row = rows[0]
        installed_version = row.catalog_version
        entry.update(
            installed=True,
            instruction_id=str(row.id),
            installed_version=installed_version,
            update_available=bool(installed_version and installed_version != skill.version),
            is_customized=cls._is_customized(skill, row),
            status=row.status,
            # >1 means a concurrent install slipped past the idempotency check;
            # surfaced rather than hidden so it is visible instead of showing up
            # as the same playbook advertised twice.
            duplicate_count=len(rows) - 1,
        )
        return entry

    async def list_catalog(
        self, db: AsyncSession, organization: Organization
    ) -> List[Dict]:
        """Every catalog entry, annotated with this org's installation state."""
        installed = await self._installed_by_key(db, organization)
        return [
            self._entry_state(skill, installed.get(skill.key))
            for skill in list_prebuilt_skills()
        ]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    def _require_entry(key: str) -> PrebuiltSkill:
        skill = get_prebuilt_skill(key)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"Unknown pre-built skill '{key}'")
        return skill

    async def install(
        self,
        db: AsyncSession,
        key: str,
        current_user: User,
        organization: Organization,
    ) -> Dict:
        """Enable a catalog skill for the org. Idempotent.

        Re-installing an already-installed key returns the existing row rather
        than creating a duplicate — two rows for one skill would advertise the
        same playbook twice in ``<available_skills>``.
        """
        skill = self._require_entry(key)

        installed = await self._installed_by_key(db, organization)
        if key in installed:
            return self._entry_state(skill, installed[key])

        payload = InstructionCreate(
            text=skill.body,
            title=skill.title,
            description=skill.description,
            category=skill.category,
            kind="skill",
            status="published",
            # Skills are always retrieved intelligently; the service enforces
            # this too, but be explicit about the intent at the call site.
            load_mode="intelligent",
            applicable_modes=list(skill.modes) or None,
            applicable_channels=list(skill.channels) or None,
            catalog_key=skill.key,
            catalog_version=skill.version,
            data_source_ids=[],
        )
        created = await self.instruction_service.create_instruction(
            db, payload, current_user, organization, force_global=True,
        )
        logger.info(
            "installed pre-built skill %s v%s for org %s", skill.key, skill.version, organization.id,
        )
        # Re-read so the returned state reflects what actually landed.
        installed = await self._installed_by_key(db, organization)
        rows = installed.get(key)
        if not rows:  # pragma: no cover - create_instruction raises on failure
            raise HTTPException(status_code=500, detail="Skill install did not persist")
        return self._entry_state(skill, rows)

    async def uninstall(
        self,
        db: AsyncSession,
        key: str,
        current_user: User,
        organization: Organization,
    ) -> Dict:
        """Disable a catalog skill: soft-delete the org's installed row."""
        skill = self._require_entry(key)
        installed = await self._installed_by_key(db, organization)
        rows = installed.get(key)
        if not rows:
            # Already absent — report the (uninstalled) state rather than 404,
            # so a double-click from the UI is not an error.
            return self._entry_state(skill, None)

        # Every row, not just the first: a duplicate left behind would keep the
        # playbook advertised after the admin disabled it.
        for row in rows:
            await self.instruction_service.delete_instruction(
                db, str(row.id), organization, current_user,
            )
        logger.info(
            "uninstalled pre-built skill %s (%d row(s)) for org %s",
            key, len(rows), organization.id,
        )
        return self._entry_state(skill, None)

    async def update_to_latest(
        self,
        db: AsyncSession,
        key: str,
        current_user: User,
        organization: Organization,
    ) -> Dict:
        """Overwrite the installed row with the current catalog version.

        Destructive by design when the admin has edited the skill — the route
        requires an explicit confirmation for that case, and the listing's
        ``is_customized`` flag is what the UI warns from.
        """
        skill = self._require_entry(key)
        installed = await self._installed_by_key(db, organization)
        rows = installed.get(key)
        if not rows:
            raise HTTPException(status_code=404, detail=f"Skill '{key}' is not installed")
        row = rows[0]

        await self.instruction_service.update_instruction(
            db,
            str(row.id),
            InstructionUpdate(
                text=skill.body,
                title=skill.title,
                description=skill.description,
                category=skill.category,
                kind="skill",
                load_mode="intelligent",
                applicable_modes=list(skill.modes),
                applicable_channels=list(skill.channels),
            ),
            organization,
            current_user,
        )
        # The version stamp is not part of InstructionUpdate (it is provenance,
        # not user-editable content), so set it directly.
        row.catalog_version = skill.version
        await db.commit()

        installed = await self._installed_by_key(db, organization)
        logger.info(
            "updated pre-built skill %s to v%s for org %s", key, skill.version, organization.id,
        )
        return self._entry_state(skill, installed.get(key))
