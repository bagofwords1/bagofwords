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

    # Folder path this instruction is filed under, within the relevant scope
    # (e.g. "Finance/Definitions"). Cosmetic organization surfaced to the model
    # as a light topical hint — see InstructionDirectory. None when unfiled.
    path: Optional[str] = None


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
    # Non-table references as "kind:name" (e.g. "instruction:Revenue rule",
    # "resource:orders_model"), so the agent can judge relevance without
    # opening the entry.
    other_refs: List[str] = []
    # Which field the user's message strongly matched ('title' | 'labels' |
    # 'tables'), set when a matching instruction was too long to inline.
    matched: Optional[str] = None
    # Approximate size of the full text, so the agent knows what a read costs.
    tokens: Optional[int] = None
    # Aggregated org-wide usage (InstructionStats), used for ordering.
    usage_count: Optional[int] = None


class InstructionsSection(ContextSection):
    tag_name: ClassVar[str] = "instructions"

    items: List[InstructionItem] = []
    # Skills advertised (not force-loaded) — rendered as <available_skills>.
    skills: List[SkillCatalogItem] = []
    # Smart (intelligent) instructions — never inlined, always advertised.
    # Rendered as <available_instructions> for agents that can call
    # read_instruction (the planner). Excluded from render() by default so
    # tool-less consumers (coder/answer) don't see references they can't open.
    available_instructions: List[SkillCatalogItem] = []
    # Smart instructions the agent READ (read_instruction) in this report.
    # Tool-less consumers (coder, chart step) can't open catalog entries, so
    # they get these in full via render(). The planner (include_catalog=True)
    # doesn't: it already has the text from its own read.
    read_items: List[InstructionItem] = []

    @staticmethod
    def _tables_attr(table_refs: List[str], cap: int = 5) -> str:
        shown = [t for t in table_refs[:cap] if t]
        suffix = ", …" if len(table_refs) > cap else ""
        return ", ".join(shown) + suffix

    def render(self, include_catalog: bool = False) -> str:
        # Tool-less consumers also get what the agent read; the planner reads
        # catalog entries itself and already has their text.
        loaded = list(self.items)
        if not include_catalog and self.read_items:
            seen = {i.id for i in loaded}
            loaded += [i for i in self.read_items if i.id not in seen]
        if not loaded and not self.skills and not (include_catalog and self.available_instructions):
            return ""
        rendered: List[str] = []

        if loaded:
            parts: List[str] = []
            for inst in loaded:
                attrs = {"id": inst.id, "category": inst.category or ""}
                if inst.title:
                    attrs["title"] = inst.title
                if inst.path:
                    attrs["path"] = inst.path
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
                "Organization instructions available on demand. Each entry lists "
                "a short id, title and description (and the tables and other "
                "objects it references) — the full text is NOT shown here. Decide "
                "which entries apply to the user's request and call read_instruction "
                "with their short_id to load the full text BEFORE acting. Entries "
                "marked matched=... share words with the request (listed first) — "
                "a hint, not a guarantee. If you suspect a rule exists but nothing "
                "here matches, call search_instructions."
            ]
            for ref in self.available_instructions:
                attrs = {"short_id": ref.short_id, "title": ref.title}
                if ref.table_refs:
                    attrs["tables"] = self._tables_attr(ref.table_refs)
                if ref.other_refs:
                    attrs["refs"] = self._tables_attr(ref.other_refs)
                if ref.matched:
                    attrs["matched"] = ref.matched
                if ref.tokens:
                    attrs["tokens"] = str(ref.tokens)
                body = xml_escape((ref.description or "").strip())
                cat_parts.append(xml_tag("instruction_ref", body, attrs))
            rendered.append(xml_tag("available_instructions", "\n".join(cat_parts)))

        return "\n".join(rendered)


