"""Loader for the pre-built skill catalog.

Skills are authored as markdown files in ``library/`` with a YAML frontmatter
header, so a reviewer reads them as prose in the diff rather than as escaped
Python strings::

    ---
    key: rca-metric-movement
    title: Root cause analysis for a metric movement
    description: Use when the user asks why a metric moved...
    category: general
    version: "1.0"
    ---
    <body>

The catalog is read-only, code-defined content: it is never stored in the
database. Installing an entry copies its body into an ``Instruction`` row
stamped with ``catalog_key`` + ``catalog_version`` (see
``SkillCatalogService``), which is what lets a later version bump be detected
without matching on titles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

LIBRARY_DIR = Path(__file__).parent / "library"

#: Categories an entry may declare — mirrors ``InstructionCategory``. Kept as a
#: plain set rather than importing the enum so this module stays free of schema
#: imports (it is loaded by both the service and the tests).
VALID_CATEGORIES = {"code_gen", "data_modeling", "general", "dashboard", "visualization"}

#: Agent run-modes an entry may scope itself to. A skill that only makes sense
#: in one mode (its tools exist nowhere else) must say so, or it burns a
#: catalog slot in every other mode.
VALID_MODES = {"chat", "deep", "training", "knowledge", "excel"}

#: Frontmatter keys every entry must declare.
REQUIRED_FIELDS = ("key", "title", "description", "category", "version")

#: The catalog description doubles as the ONE line the planner sees in
#: ``<available_skills>``; the builder truncates at 160 chars
#: (``InstructionContextBuilder._skill_description``). Authoring a longer one is
#: a bug in the skill, not something to silently truncate at install time.
MAX_DESCRIPTION_LEN = 160


@dataclass(frozen=True)
class PrebuiltSkill:
    """One catalog entry. Immutable — the DB row is the mutable copy."""

    key: str
    title: str
    description: str
    category: str
    version: str
    body: str
    #: Free-form tags, surfaced in the UI for grouping/filtering.
    tags: tuple = ()
    #: Agent run-modes this skill applies to (``Instruction.applicable_modes``).
    #: Empty = every mode. A training-only skill must not be advertised in chat.
    modes: tuple = ()
    #: Delivery channels this skill applies to. Empty = every channel.
    channels: tuple = ()

    def to_dict(self) -> Dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "body": self.body,
            "tags": list(self.tags),
            "modes": list(self.modes),
            "channels": list(self.channels),
        }


def _parse_skill_file(path: Path) -> Optional[PrebuiltSkill]:
    """Parse one ``<key>.md`` file. Returns None (and logs) on a malformed file.

    A bad file must not take down the whole catalog — the remaining skills stay
    installable, and the tests assert every shipped file parses.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("skill catalog: cannot read %s: %s", path.name, e)
        return None

    if not raw.startswith("---"):
        logger.error("skill catalog: %s has no frontmatter header", path.name)
        return None

    # Split on the closing '---' of the frontmatter block only.
    parts = raw.split("---", 2)
    if len(parts) < 3:
        logger.error("skill catalog: %s frontmatter is not terminated", path.name)
        return None

    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        logger.error("skill catalog: %s frontmatter is not valid YAML: %s", path.name, e)
        return None

    if not isinstance(meta, dict):
        logger.error("skill catalog: %s frontmatter is not a mapping", path.name)
        return None

    missing = [f for f in REQUIRED_FIELDS if not str(meta.get(f, "") or "").strip()]
    if missing:
        logger.error("skill catalog: %s is missing %s", path.name, ", ".join(missing))
        return None

    key = str(meta["key"]).strip()
    if key != path.stem:
        logger.error(
            "skill catalog: %s declares key %r — the key must match the filename",
            path.name, key,
        )
        return None

    category = str(meta["category"]).strip()
    if category not in VALID_CATEGORIES:
        logger.error(
            "skill catalog: %s declares unknown category %r (valid: %s)",
            path.name, category, ", ".join(sorted(VALID_CATEGORIES)),
        )
        return None

    description = str(meta["description"]).strip()
    if len(description) > MAX_DESCRIPTION_LEN:
        logger.error(
            "skill catalog: %s description is %d chars (max %d) — it would be "
            "truncated in <available_skills>",
            path.name, len(description), MAX_DESCRIPTION_LEN,
        )
        return None

    body = parts[2].strip()
    if not body:
        logger.error("skill catalog: %s has an empty body", path.name)
        return None

    def _str_tuple(field: str) -> tuple:
        value = meta.get(field) or []
        if not isinstance(value, list):
            return ()
        return tuple(str(v).strip() for v in value if str(v).strip())

    modes = _str_tuple("modes")
    unknown_modes = [m for m in modes if m not in VALID_MODES]
    if unknown_modes:
        logger.error(
            "skill catalog: %s declares unknown mode(s) %s (valid: %s)",
            path.name, ", ".join(unknown_modes), ", ".join(sorted(VALID_MODES)),
        )
        return None

    return PrebuiltSkill(
        key=key,
        title=str(meta["title"]).strip(),
        description=description,
        category=category,
        version=str(meta["version"]).strip(),
        body=body,
        tags=_str_tuple("tags"),
        modes=modes,
        channels=_str_tuple("channels"),
    )


@lru_cache(maxsize=1)
def _load_catalog() -> Dict[str, PrebuiltSkill]:
    """Parse every ``library/*.md`` file once per process."""
    catalog: Dict[str, PrebuiltSkill] = {}
    if not LIBRARY_DIR.is_dir():
        logger.warning("skill catalog: library dir %s does not exist", LIBRARY_DIR)
        return catalog
    for path in sorted(LIBRARY_DIR.glob("*.md")):
        skill = _parse_skill_file(path)
        if skill is not None:
            catalog[skill.key] = skill
    return catalog


def list_prebuilt_skills() -> List[PrebuiltSkill]:
    """Every valid catalog entry, ordered by title."""
    return sorted(_load_catalog().values(), key=lambda s: s.title.lower())


def get_prebuilt_skill(key: str) -> Optional[PrebuiltSkill]:
    """One catalog entry by key, or None when the key is unknown."""
    return _load_catalog().get((key or "").strip())
