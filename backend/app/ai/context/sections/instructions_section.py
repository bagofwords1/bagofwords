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


class InstructionsSection(ContextSection):
    tag_name: ClassVar[str] = "instructions"

    items: List[InstructionItem] = []

    def render(self) -> str:
        if not self.items:
            return ""
        parts: List[str] = []
        for inst in self.items:
            parts.append(
                xml_tag(
                    "instruction",
                    xml_escape(inst.text.strip()),
                    {"id": inst.id, "category": inst.category or ""},
                )
            )
        return xml_tag(self.tag_name, "\n".join(parts))


