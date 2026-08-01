"""Pre-built skills catalog.

Skills ship as files in ``app/skills_catalog/<dir>/SKILL.md`` — YAML frontmatter
(key, name, description, category, version, icon) followed by the markdown body
that becomes the instruction text.

Installing a catalog entry creates a normal org instruction with ``kind='skill'``
so everything downstream — the ``<available_skills>`` advertisement, the
``read_instruction`` tool, versioning, review and editing — works unchanged. The
``catalog_key`` column records where the row came from so the catalog can show
installed state without matching on title.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction import Instruction
from app.models.organization import Organization
from app.models.user import User
from app.schemas.instruction_schema import InstructionCreate

logger = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).resolve().parent.parent / "skills_catalog"

# Frontmatter keys a catalog entry must define for the UI to render it.
_REQUIRED_KEYS = ("key", "name", "description")


class SkillsCatalogService:
    """Reads bundled skill files and installs them into an organization."""

    def _parse_skill_file(self, path: Path) -> Optional[Dict[str, Any]]:
        """Parse one SKILL.md into a catalog entry, or None if malformed.

        A bad file is skipped with a log line rather than raised: one broken
        entry must not take down the whole catalog listing.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("skills_catalog: cannot read %s: %s", path, e)
            return None

        if not raw.startswith("---"):
            logger.warning("skills_catalog: %s has no frontmatter", path)
            return None

        # Split on the closing delimiter of the frontmatter block only.
        parts = raw.split("---", 2)
        if len(parts) < 3:
            logger.warning("skills_catalog: %s frontmatter is unterminated", path)
            return None

        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            logger.warning("skills_catalog: %s has invalid frontmatter: %s", path, e)
            return None

        if not isinstance(meta, dict) or any(not meta.get(k) for k in _REQUIRED_KEYS):
            logger.warning("skills_catalog: %s missing one of %s", path, _REQUIRED_KEYS)
            return None

        return {
            "key": str(meta["key"]),
            "name": str(meta["name"]),
            "description": str(meta["description"]),
            "category": str(meta.get("category") or "general"),
            "version": str(meta.get("version") or "1.0.0"),
            "icon": str(meta.get("icon") or "i-heroicons-sparkles"),
            "text": parts[2].strip(),
        }

    def list_catalog(self) -> List[Dict[str, Any]]:
        """All well-formed catalog entries, sorted by display name."""
        if not CATALOG_DIR.is_dir():
            return []
        entries: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for skill_file in sorted(CATALOG_DIR.glob("*/SKILL.md")):
            entry = self._parse_skill_file(skill_file)
            if entry is None:
                continue
            if entry["key"] in seen_keys:
                logger.warning("skills_catalog: duplicate key %s in %s", entry["key"], skill_file)
                continue
            seen_keys.add(entry["key"])
            entries.append(entry)
        return sorted(entries, key=lambda e: e["name"].lower())

    def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        return next((e for e in self.list_catalog() if e["key"] == key), None)

    async def _installed_rows(self, db: AsyncSession, organization: Organization) -> Dict[str, Instruction]:
        """Map catalog_key -> the installed instruction row for this org."""
        result = await db.execute(
            select(Instruction).where(
                Instruction.organization_id == organization.id,
                Instruction.catalog_key.isnot(None),
                Instruction.status != "archived",
                # Instructions are soft-deleted. Without this a skill the user
                # deleted would still read as installed, and reinstalling it
                # would be refused forever.
                Instruction.deleted_at.is_(None),
            )
        )
        return {row.catalog_key: row for row in result.scalars().all()}

    async def list_with_state(
        self, db: AsyncSession, organization: Organization
    ) -> List[Dict[str, Any]]:
        """Catalog entries decorated with per-org installed state.

        The full skill body is deliberately omitted from the list payload; the
        list view only needs name/description, and the installed row is what the
        detail view opens.
        """
        installed = await self._installed_rows(db, organization)
        items: List[Dict[str, Any]] = []
        for entry in self.list_catalog():
            row = installed.get(entry["key"])
            items.append(
                {
                    "key": entry["key"],
                    "name": entry["name"],
                    "description": entry["description"],
                    "category": entry["category"],
                    "version": entry["version"],
                    "icon": entry["icon"],
                    "installed": row is not None,
                    "instruction_id": str(row.id) if row else None,
                    "installed_version": row.catalog_version if row else None,
                }
            )
        return items

    async def install(
        self,
        db: AsyncSession,
        key: str,
        current_user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Install a catalog entry as a global skill instruction.

        Idempotent: installing an already-installed skill returns the existing
        row rather than creating a duplicate.
        """
        entry = self.get_entry(key)
        if entry is None:
            raise ValueError(f"Unknown skill: {key}")

        installed = await self._installed_rows(db, organization)
        if key in installed:
            row = installed[key]
            return {"instruction_id": str(row.id), "already_installed": True}

        # Imported here: instruction_service imports a wide slice of the app and
        # a module-level import creates a cycle through the tool registry.
        from app.services.instruction_service import InstructionService

        payload = InstructionCreate(
            text=entry["text"],
            title=entry["name"],
            description=entry["description"],
            category=entry["category"],
            kind="skill",
            # Skills are advertised in <available_skills> and pulled on demand
            # via read_instruction — never force-loaded into every prompt.
            load_mode="intelligent",
            status="published",
            source_type="user",
            catalog_key=entry["key"],
            catalog_version=entry["version"],
            data_source_ids=[],  # global: applies to every agent
        )

        created = await InstructionService().create_instruction(
            db, payload, current_user, organization, force_global=True
        )
        return {"instruction_id": str(created.id), "already_installed": False}


skills_catalog_service = SkillsCatalogService()
