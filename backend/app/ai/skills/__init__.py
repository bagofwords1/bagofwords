"""Pre-built skill catalog.

A *pre-built skill* is a curated playbook that ships with the product. An org
admin installs one from the catalog; installing copies the body into a normal
``Instruction`` row with ``kind='skill'``, so from that point on it behaves
exactly like a hand-authored skill: advertised in ``<available_skills>`` and
pulled on demand by the planner via ``read_instruction``.

See ``catalog.py`` for the loader and ``library/`` for the skill bodies.
"""

from app.ai.skills.catalog import (
    PrebuiltSkill,
    get_prebuilt_skill,
    list_prebuilt_skills,
)

__all__ = ["PrebuiltSkill", "get_prebuilt_skill", "list_prebuilt_skills"]
