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


class SkillCatalogItem(BaseModel):
    """A skill advertised to the agent without its full text.

    Skills use 'intelligent' (smart) retrieval: rather than force-loading their
    full content, they're listed here as a compact catalog (short id + title +
    description). The agent calls the `read_instruction` tool with `short_id` to
    pull the full text on demand.
    """
    id: str
    short_id: str
    title: str
    description: Optional[str] = None


class InstructionsSection(ContextSection):
    tag_name: ClassVar[str] = "instructions"

    items: List[InstructionItem] = []
    # Skills advertised (not force-loaded) — rendered as <available_skills>.
    skills: List[SkillCatalogItem] = []

    def render(self) -> str:
        if not self.items and not self.skills:
            return ""
        rendered: List[str] = []

        if self.items:
            parts: List[str] = []
            for inst in self.items:
                attrs = {"id": inst.id, "category": inst.category or ""}
                if inst.title:
                    attrs["title"] = inst.title
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
                "relevant to the user's request, call read_skill with its short_id "
                "to load the full text before acting on it."
            ]
            for sk in self.skills:
                attrs = {"short_id": sk.short_id, "title": sk.title}
                body = xml_escape((sk.description or "").strip())
                skill_parts.append(xml_tag("skill", body, attrs))
            rendered.append(xml_tag("available_skills", "\n".join(skill_parts)))

        return "\n".join(rendered)


