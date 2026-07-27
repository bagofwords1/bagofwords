from typing import ClassVar, List, Optional, Any
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class InstructionLabelItem(BaseModel):
    """Label attached to an instruction (for tracking/display)."""
    id: Optional[str] = None
    name: str
    color: Optional[str] = None


class InstructionItem(BaseModel):
    id: str
    category: Optional[str] = None
    text: str

    # Load tracking fields
    load_mode: Optional[str] = None       # 'always' | 'intelligent'
    load_reason: Optional[str] = None     # 'always' | 'search_match:0.85'
    source_type: Optional[str] = None     # 'user' | 'git' | 'ai' | 'dbt' | 'markdown'
    title: Optional[str] = None           # For display/debugging
    labels: Optional[List[InstructionLabelItem]] = None  # Associated labels

    # Usage stats (from InstructionStats)
    usage_count: Optional[int] = None     # Total times this instruction was used

    # Version/Build lineage tracking (for reproducibility)
    version_id: Optional[str] = None      # InstructionVersion.id
    version_number: Optional[int] = None  # InstructionVersion.version_number
    content_hash: Optional[str] = None    # InstructionVersion.content_hash
    build_number: Optional[int] = None    # InstructionBuild.build_number

    # Data source IDs for which this instruction is the primary
    primary_for: List[str] = []

    # Display names of tables this instruction is scoped to (from
    # InstructionReference rows with object_type='datasource_table').
    table_refs: List[str] = []


class SkillCatalogItem(BaseModel):
    """An instruction or skill advertised to the agent without its full text.

    Rather than force-loading full content, catalog entries carry a compact
    reference (short id + title + description). The agent calls the
    `read_instruction` tool with `short_id` to pull the full text on demand.
    Used both for skills (<available_skills>) and for intelligent instructions
    beyond the load capacity (<available_instructions>).
    """
    id: str
    short_id: str
    title: str
    description: Optional[str] = None
    # Display names of tables the entry is scoped to (helps the agent pick).
    table_refs: List[str] = []
    # Aggregated org-wide usage (InstructionStats), used for ordering.
    usage_count: Optional[int] = None


class InstructionsSection(ContextSection):
    tag_name: ClassVar[str] = "instructions"

    items: List[InstructionItem] = []
    # Skills advertised (not force-loaded) — rendered as <available_skills>.
    skills: List[SkillCatalogItem] = []
    # Intelligent instructions beyond the load capacity — advertised (not
    # force-loaded), rendered as <available_instructions> for agents that can
    # call read_instruction (the planner). Excluded from render() by default so
    # tool-less consumers (coder/answer) don't see references they can't open.
    available_instructions: List[SkillCatalogItem] = []

    @staticmethod
    def _tables_attr(table_refs: List[str], cap: int = 5) -> str:
        shown = [t for t in table_refs[:cap] if t]
        suffix = ", …" if len(table_refs) > cap else ""
        return ", ".join(shown) + suffix

    def render(self, include_catalog: bool = False) -> str:
        if not self.items and not self.skills and not (include_catalog and self.available_instructions):
            return ""
        rendered: List[str] = []

        if self.items:
            parts: List[str] = []
            for inst in self.items:
                attrs = {"id": inst.id, "category": inst.category or ""}
                if inst.title:
                    attrs["title"] = inst.title
                if inst.table_refs:
                    attrs["tables"] = self._tables_attr(inst.table_refs)
                parts.append(
                    xml_tag(
                        "instruction",
                        xml_escape(inst.text.strip()),
                        attrs,
                    )
                )
            rendered.append(xml_tag(self.tag_name, "\n".join(parts)))

        if self.skills:
            skill_parts: List[str] = [
                "Skills available on demand. Each lists a short id, title and a one-line "
                "description — the full instructions are NOT shown here. When a skill is "
                "relevant to the user's request, call read_instruction with its short_id "
                "to load the full text before acting on it."
            ]
            for sk in self.skills:
                attrs = {"short_id": sk.short_id, "title": sk.title}
                body = xml_escape((sk.description or "").strip())
                skill_parts.append(xml_tag("skill", body, attrs))
            rendered.append(xml_tag("available_skills", "\n".join(skill_parts)))

        if include_catalog and self.available_instructions:
            cat_parts: List[str] = [
                "More organization instructions exist beyond the ones loaded above. "
                "Each entry lists a short id and title (and the tables it is scoped "
                "to) — the full text is NOT shown here. If an entry looks relevant "
                "to the user's request, call read_instruction with its short_id to "
                "load the full text BEFORE acting. If you suspect a rule exists but "
                "nothing here matches, call search_instructions."
            ]
            for ref in self.available_instructions:
                attrs = {"short_id": ref.short_id, "title": ref.title}
                if ref.table_refs:
                    attrs["tables"] = self._tables_attr(ref.table_refs)
                body = xml_escape((ref.description or "").strip())
                cat_parts.append(xml_tag("instruction_ref", body, attrs))
            rendered.append(xml_tag("available_instructions", "\n".join(cat_parts)))

        return "\n".join(rendered)


