from typing import List, Optional, Set, Tuple, Dict
import re
import logging

from sqlalchemy import select, and_, or_, func
from sqlalchemy.orm import selectinload, lazyload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction import Instruction
from app.models.instruction_stats import InstructionStats
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.instruction_version import InstructionVersion
from app.models.instruction_reference import InstructionReference
from app.models.organization import Organization
from app.models.user import User
from app.models.user_data_source_overlay import UserDataSourceTable

from app.ai.context.sections.instructions_section import InstructionsSection, InstructionItem, InstructionLabelItem, SkillCatalogItem

logger = logging.getLogger(__name__)


class InstructionContextBuilder:
    """
    Helper for fetching instructions that should be supplied to the LLM.

    Supports two loading strategies:
    - `load_always_instructions()`: Load instructions with load_mode='always'
    - `search_instructions()`: Search instructions with load_mode='intelligent' by keyword
    - `build()`: Combined always + intelligent instructions (with proper tracking)

    Load behavior:
    1. Load ALL 'always' instructions first
    2. Fill remaining capacity with 'intelligent' instructions (keyword-matched)
    3. Skip 'disabled' instructions
    
    The max_instructions_in_context setting (default 50) controls total capacity.
    'Always' instructions take priority and can exceed the limit.
    """
    
    # Common stopwords to filter out when extracting keywords
    STOPWORDS = {
        "the", "a", "an", "of", "and", "for", "to", "in", "by", "with", "on", 
        "is", "are", "be", "this", "that", "it", "as", "at", "from", "or",
        "what", "how", "when", "where", "why", "which", "who", "can", "will",
        "should", "would", "could", "have", "has", "had", "do", "does", "did",
        "i", "you", "we", "they", "he", "she", "my", "your", "our", "their",
        "me", "us", "them", "all", "some", "any", "no", "not", "but", "if",
        "show", "get", "find", "give", "tell", "list", "display", "want", "need",
    }
    
    # Default max instructions in context
    DEFAULT_MAX_INSTRUCTIONS = 50

    # Max advertised (not force-loaded) intelligent instructions in the
    # <available_instructions> catalog.
    CATALOG_LIMIT = 50

    # Untitled catalog entries show this many chars of the body as their title.
    CATALOG_SNIPPET_LEN = 140

    def __init__(self, db: AsyncSession, organization: Organization, current_user: Optional[User] = None, organization_settings=None, data_source_ids: Optional[List[str]] = None, mode: Optional[str] = None, channel: Optional[str] = None):
        self.db = db
        self.organization = organization
        self.current_user = current_user
        self.organization_settings = organization_settings
        self.data_source_ids = data_source_ids
        # Current request mode ('chat' | 'deep' | 'training' | ...) and delivery
        # channel ('app' | 'slack' | 'teams' | 'email' | 'mcp' | ...). Used to
        # honor per-instruction applicable_modes / applicable_channels scoping.
        # When either is None, that dimension is not filtered (include all).
        self.mode = mode
        self.channel = channel

    def _passes_mode_channel(self, applicable_modes, applicable_channels) -> bool:
        """Return True if an instruction with the given scoping applies to the
        current request mode/channel.

        An empty/None ``applicable_modes`` (or ``applicable_channels``) means the
        instruction applies to every mode (or channel). When the builder has no
        current mode/channel set, that dimension is not filtered.
        """
        if self.mode and applicable_modes:
            modes = applicable_modes if isinstance(applicable_modes, list) else []
            if modes and self.mode not in modes:
                return False
        if self.channel and applicable_channels:
            channels = applicable_channels if isinstance(applicable_channels, list) else []
            if channels and self.channel not in channels:
                return False
        return True
    
    def _get_max_instructions(self) -> int:
        """Get max instructions limit from org settings or default."""
        if self.organization_settings:
            try:
                config = self.organization_settings.get_config("max_instructions_in_context")
                if config and config.value is not None:
                    return int(config.value)
            except Exception:
                pass
        return self.DEFAULT_MAX_INSTRUCTIONS

    async def load_instructions(
        self,
        *,
        status: str = "published",
        category: Optional[str] = None,
    ) -> List[Instruction]:
        """
        Load instructions from the database.

        Parameters
        ----------
        status : InstructionStatus, optional
            Filter by status (defaults to `PUBLISHED`).
        category : InstructionCategory | None, optional
            If provided, restrict the results to this category.

        Returns
        -------
        List[Instruction]
            Matching Instruction ORM objects.
        """
        stmt = (
            select(Instruction)
            .where(Instruction.status == status)
            .where(Instruction.organization_id == self.organization.id)
            .where(Instruction.deleted_at.is_(None))
        )

        if category is not None:
            stmt = stmt.where(Instruction.category == category)

        result = await self.db.execute(stmt)
        return result.scalars().all()
    
    async def load_always_instructions(
        self,
        *,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        categories: Optional[List[str]] = None,
    ) -> List[Instruction]:
        """
        Load instructions with load_mode='always'.

        These instructions are always included in the AI context.

        Parameters
        ----------
        data_source_ids : List[str] | None, optional
            If provided, filter to instructions associated with these data sources
            or instructions without data source restrictions.
        category : str | None, optional
            If provided, restrict to this category (deprecated, use categories).
        categories : List[str] | None, optional
            If provided, restrict to these categories.

        Returns
        -------
        List[Instruction]
            Instructions that should always be loaded.
        """
        stmt = (
            select(Instruction)
            .options(
                selectinload(Instruction.data_sources).options(lazyload("*")),
                selectinload(Instruction.labels),
            )
            .where(
                and_(
                    Instruction.status == "published",
                    Instruction.organization_id == self.organization.id,
                    Instruction.deleted_at.is_(None),
                    or_(
                        Instruction.load_mode == "always",
                        Instruction.load_mode.is_(None),  # Treat NULL as always for backwards compat
                    ),
                )
            )
        )

        # Support both single category (legacy) and multiple categories
        if categories is not None and len(categories) > 0:
            stmt = stmt.where(Instruction.category.in_(categories))
        elif category is not None:
            stmt = stmt.where(Instruction.category == category)

        result = await self.db.execute(stmt)
        instructions = result.scalars().all()

        # Filter by data sources: include global instructions (no data sources assigned)
        # and instructions associated with the specified data sources
        if data_source_ids is not None:
            filtered = []
            for inst in instructions:
                inst_ds_ids = {str(ds.id) for ds in inst.data_sources} if inst.data_sources else set()
                if not inst_ds_ids or inst_ds_ids.intersection(data_source_ids):
                    filtered.append(inst)
            instructions = filtered

        # Filter by current request mode/channel scoping
        instructions = [
            inst for inst in instructions
            if self._passes_mode_channel(
                getattr(inst, "applicable_modes", None),
                getattr(inst, "applicable_channels", None),
            )
        ]

        # Filter by per-user table accessibility (user_data_source_tables overlay)
        instructions = await self._filter_instructions_by_table_accessibility(instructions)

        return instructions

    async def load_instructions_by_ids(
        self,
        instruction_ids: List[str],
        *,
        load_mode_filter: Optional[str] = "intelligent",
    ) -> List[InstructionItem]:
        """
        Load instructions by their IDs, only if they exist in the active build.

        Parameters
        ----------
        instruction_ids : List[str]
            List of instruction IDs to load.
        load_mode_filter : str | None, optional
            If provided, only return instructions with this load_mode.
            Defaults to 'intelligent' to only load contextually triggered instructions.

        Returns
        -------
        List[InstructionItem]
            List of instruction items matching the criteria.
        """
        if not instruction_ids:
            return []

        # Only load instructions that exist in the main build
        build_result = await self.db.execute(
            select(InstructionBuild).where(
                and_(
                    InstructionBuild.organization_id == self.organization.id,
                    InstructionBuild.is_main == True,
                    InstructionBuild.deleted_at == None,
                )
            )
        )
        build = build_result.scalar_one_or_none()
        if not build:
            # No build exists — fall back to direct query
            return await self._load_instructions_by_ids_direct(instruction_ids, load_mode_filter=load_mode_filter)

        # Load via build contents — only instructions present in the build
        contents_result = await self.db.execute(
            select(BuildContent)
            .options(
                selectinload(BuildContent.instruction).selectinload(Instruction.data_sources).options(lazyload("*")),
                selectinload(BuildContent.instruction).selectinload(Instruction.labels),
                selectinload(BuildContent.instruction_version),
            )
            .where(
                and_(
                    BuildContent.build_id == build.id,
                    BuildContent.instruction_id.in_(instruction_ids),
                )
            )
        )
        contents = contents_result.scalars().all()
        if not contents:
            return []

        # Filter and build items
        all_ids = []
        valid_entries = []
        for content in contents:
            instruction = content.instruction
            version = content.instruction_version
            if not instruction or not version:
                continue
            if instruction.status != "published":
                continue
            if version.load_mode == "disabled":
                continue
            if load_mode_filter and version.load_mode != load_mode_filter:
                continue
            # Apply data source filtering
            if self.data_source_ids is not None:
                inst_ds_ids = {str(ds.id) for ds in instruction.data_sources} if instruction.data_sources else set()
                if inst_ds_ids and not inst_ds_ids.intersection(self.data_source_ids):
                    continue
            all_ids.append(str(instruction.id))
            valid_entries.append((content, instruction, version, build))

        if not valid_entries:
            return []

        usage_counts = await self._batch_load_usage_counts(all_ids)
        items: List[InstructionItem] = []
        for content, instruction, version, bld in valid_entries:
            inst_id = str(instruction.id)
            items.append(InstructionItem(
                id=inst_id,
                category=instruction.category,
                text=version.text or "",
                load_mode=version.load_mode or "intelligent",
                load_reason="table_reference",
                source_type=instruction.source_type,
                title=version.title,
                labels=self._extract_labels(instruction),
                usage_count=usage_counts.get(inst_id),
                version_id=str(version.id),
                version_number=version.version_number,
                content_hash=version.content_hash,
                build_number=bld.build_number,
            ))

        # Filter by per-user table accessibility
        items = await self._filter_items_by_table_accessibility(items)

        return items

    async def _load_instructions_by_ids_direct(
        self,
        instruction_ids: List[str],
        *,
        load_mode_filter: Optional[str] = None,
    ) -> List[InstructionItem]:
        """Fallback: load instructions directly when no build exists."""
        stmt = (
            select(Instruction)
            .options(
                selectinload(Instruction.data_sources).options(lazyload("*")),
                selectinload(Instruction.labels),
            )
            .where(
                and_(
                    Instruction.id.in_(instruction_ids),
                    Instruction.status == "published",
                    Instruction.organization_id == self.organization.id,
                    Instruction.deleted_at.is_(None),
                )
            )
        )
        if load_mode_filter:
            stmt = stmt.where(Instruction.load_mode == load_mode_filter)

        result = await self.db.execute(stmt)
        instructions = result.scalars().all()
        if not instructions:
            return []

        all_ids = [str(inst.id) for inst in instructions]
        usage_counts = await self._batch_load_usage_counts(all_ids)

        items: List[InstructionItem] = []
        for inst in instructions:
            if self.data_source_ids is not None:
                inst_ds_ids = {str(ds.id) for ds in inst.data_sources} if inst.data_sources else set()
                if inst_ds_ids and not inst_ds_ids.intersection(self.data_source_ids):
                    continue
            inst_id = str(inst.id)
            items.append(InstructionItem(
                id=inst_id,
                category=inst.category,
                text=inst.text or "",
                load_mode=inst.load_mode or "intelligent",
                load_reason="table_reference",
                source_type=inst.source_type,
                title=inst.title,
                labels=self._extract_labels(inst),
                usage_count=usage_counts.get(inst_id),
            ))

        return items

    async def search_instructions(
        self,
        query: str,
        *,
        limit: int = 10,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        categories: Optional[List[str]] = None,
    ) -> List[Tuple[Instruction, float]]:
        """
        Search instructions with load_mode='intelligent' by keyword relevance.

        If query is empty, returns all intelligent instructions (up to limit)
        with score 0, allowing them to fill remaining capacity.

        Parameters
        ----------
        query : str
            The user query to match against. If empty, returns all intelligent.
        limit : int
            Maximum number of results to return.
        data_source_ids : List[str] | None, optional
            If provided, filter to instructions associated with these data sources.
        category : str | None, optional
            If provided, restrict to this category (deprecated, use categories).
        categories : List[str] | None, optional
            If provided, restrict to these categories.

        Returns
        -------
        List[Tuple[Instruction, float]]
            List of (instruction, score) tuples, sorted by relevance.
        """
        # Extract keywords from query
        keywords = self._extract_keywords(query) if query else set()

        # Load all intelligent instructions
        stmt = (
            select(Instruction)
            .options(
                selectinload(Instruction.data_sources).options(lazyload("*")),
                selectinload(Instruction.labels),
            )
            .where(
                and_(
                    Instruction.status == "published",
                    Instruction.organization_id == self.organization.id,
                    Instruction.deleted_at.is_(None),
                    Instruction.load_mode == "intelligent",
                    # Skills are advertised via the catalog, never auto-loaded.
                    Instruction.kind != "skill",
                )
            )
        )

        # Support both single category (legacy) and multiple categories
        if categories is not None and len(categories) > 0:
            stmt = stmt.where(Instruction.category.in_(categories))
        elif category is not None:
            stmt = stmt.where(Instruction.category == category)

        result = await self.db.execute(stmt)
        all_instructions = result.scalars().all()

        # Filter by data sources
        if data_source_ids is not None:
            all_instructions = [
                inst for inst in all_instructions
                if not inst.data_sources or {str(ds.id) for ds in inst.data_sources}.intersection(data_source_ids)
            ]

        # Filter by current request mode/channel scoping
        all_instructions = [
            inst for inst in all_instructions
            if self._passes_mode_channel(
                getattr(inst, "applicable_modes", None),
                getattr(inst, "applicable_channels", None),
            )
        ]

        # Filter by per-user table accessibility
        all_instructions = await self._filter_instructions_by_table_accessibility(all_instructions)

        # Referenced table names participate in scoring (an instruction scoped
        # to the `invoices` table should match a query mentioning invoices).
        table_refs = await self._batch_load_table_refs([str(i.id) for i in all_instructions])

        # Score every candidate. Zero-score candidates are NOT dropped — they
        # rank last (by usage) and can still fill remaining capacity.
        scored: List[Tuple[Instruction, float]] = []
        for instruction in all_instructions:
            score = 0.0
            if keywords:
                score = self._score_instruction(
                    instruction, keywords,
                    extra_text=" ".join(table_refs.get(str(instruction.id), [])),
                )
            scored.append((instruction, score))

        # Sort by score desc, then aggregated usage desc (org-wide stats)
        usage_counts = await self._batch_load_usage_counts([str(i.id) for i, _ in scored])
        scored.sort(key=lambda x: (x[1], usage_counts.get(str(x[0].id), 0)), reverse=True)
        return scored[:limit]
    
    async def build(
        self,
        query: Optional[str] = None,
        *,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        categories: Optional[List[str]] = None,
        intelligent_limit: int = 10,
        build_id: Optional[str] = None,
    ) -> InstructionsSection:
        """
        Build instructions context with proper load tracking.

        Load behavior:
        1. Load ALL 'always' instructions (they always take priority)
        2. Fill remaining capacity with 'intelligent' instructions matched by keywords
        3. Skip 'disabled' instructions

        Parameters
        ----------
        query : str | None, optional
            The user query for intelligent search. Used to score and rank
            'intelligent' instructions by keyword relevance.
        data_source_ids : List[str] | None, optional
            Filter by data sources.
        category : str | None, optional
            Filter by category (deprecated, use categories).
        categories : List[str] | None, optional
            Filter by multiple categories.
        intelligent_limit : int
            Deprecated - max_instructions_in_context setting is used instead.
        build_id : str | None, optional
            If provided, load instructions from this specific build.
            If None, defaults to the main build (is_main=True) if one exists,
            otherwise falls back to legacy behavior (direct instruction query).

        Returns
        -------
        InstructionsSection
            Instructions section with proper load_mode and load_reason tracking.
        """
        max_instructions = self._get_max_instructions()
        # Use explicitly passed data_source_ids, or fall back to builder default
        effective_ds_ids = data_source_ids if data_source_ids is not None else self.data_source_ids

        # Try to load from build if build_id is provided or main build exists
        build_result = await self._load_from_build(
            build_id=build_id,
            query=query or "",
            max_instructions=max_instructions,
            data_source_ids=effective_ds_ids,
        )

        if build_result is not None:
            # Build-based loading - return items with version tracking
            build_items, catalog = build_result
            section = InstructionsSection(items=build_items, available_instructions=catalog)
        else:
            # Fallback to legacy behavior (direct instruction query)
            section = await self._build_legacy(
                query=query,
                data_source_ids=effective_ds_ids,
                category=category,
                categories=categories,
                max_instructions=max_instructions,
            )

        # Advertise skills (kind='skill') as a compact catalog rather than
        # force-loading their full text. The agent pulls them on demand via
        # the read_instruction tool.
        section.skills = await self._build_skills_catalog(data_source_ids=effective_ds_ids)
        return section
    
    async def _build_skills_catalog(
        self,
        *,
        data_source_ids: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[SkillCatalogItem]:
        """Build the advertised catalog of skills (kind='skill').

        Returns published skills in scope (global + matching data sources), with a
        short id, title and one-line description — NOT their full text. The agent
        reads full text on demand via the read_instruction tool. Per-user table
        accessibility is applied so out-of-scope skills aren't advertised.
        """
        stmt = (
            select(Instruction)
            .options(
                selectinload(Instruction.data_sources).options(lazyload("*")),
            )
            .where(
                and_(
                    Instruction.status == "published",
                    Instruction.organization_id == self.organization.id,
                    Instruction.deleted_at.is_(None),
                    Instruction.kind == "skill",
                )
            )
        )
        result = await self.db.execute(stmt)
        skills = result.scalars().all()

        # Scope by data sources (global skills — no data sources — always included).
        effective_ds_ids = data_source_ids if data_source_ids is not None else self.data_source_ids
        if effective_ds_ids is not None:
            skills = [
                s for s in skills
                if not s.data_sources or {str(ds.id) for ds in s.data_sources}.intersection(effective_ds_ids)
            ]

        # Apply per-user table accessibility (same rule as loaded instructions).
        skills = await self._filter_instructions_by_table_accessibility(skills)

        items: List[SkillCatalogItem] = []
        for s in skills[:limit]:
            sid = str(s.id)
            items.append(SkillCatalogItem(
                id=sid,
                short_id=sid[:8],
                title=s.title or (s.text[:60] if s.text else sid[:8]),
                description=self._skill_description(s),
            ))
        return items

    @staticmethod
    def _skill_description(instruction: Instruction) -> Optional[str]:
        """Derive a one-line description for the skills catalog.

        Precedence: explicit user-authored `description` → structured_data
        description (git/dbt sources) → first non-empty line of the text.
        """
        if instruction.description and instruction.description.strip():
            desc = instruction.description.strip()
        else:
            sd = instruction.structured_data
            if isinstance(sd, dict) and sd.get("description"):
                desc = str(sd["description"]).strip()
            else:
                # First non-empty line of the text, trimmed.
                text = (instruction.text or "").strip()
                desc = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not desc:
            return None
        return desc if len(desc) <= 160 else desc[:157] + "…"

    async def _load_from_build(
        self,
        build_id: Optional[str] = None,
        query: str = "",
        max_instructions: int = 50,
        data_source_ids: Optional[List[str]] = None,
    ) -> Optional[Tuple[List[InstructionItem], List[SkillCatalogItem]]]:
        """
        Load instructions from a specific build or the main build.

        Load behavior:
        1. Load ALL 'always' instructions (they take priority)
        2. Fill remaining capacity with 'intelligent' instructions ranked by
           keyword score, then aggregated usage (zero-score candidates are not
           dropped — they fill remaining slots)
        3. Advertise over-capacity intelligent instructions as catalog entries
        4. Skip 'disabled' instructions

        Returns (loaded_items, catalog_entries), or None if no build is
        available (fallback to legacy).
        """
        # Get the build
        if build_id:
            build_result = await self.db.execute(
                select(InstructionBuild)
                .where(
                    and_(
                        InstructionBuild.id == build_id,
                        InstructionBuild.deleted_at == None,
                    )
                )
            )
            build = build_result.scalar_one_or_none()
        else:
            # Try to get the main build
            build_result = await self.db.execute(
                select(InstructionBuild)
                .where(
                    and_(
                        InstructionBuild.organization_id == self.organization.id,
                        InstructionBuild.is_main == True,
                        InstructionBuild.deleted_at == None,
                    )
                )
            )
            build = build_result.scalar_one_or_none()
        
        if not build:
            return None  # No build available, fallback to legacy
        
        # Load build contents with versions
        contents_result = await self.db.execute(
            select(BuildContent)
            .options(
                selectinload(BuildContent.instruction).selectinload(Instruction.data_sources).options(lazyload("*")),
                selectinload(BuildContent.instruction).selectinload(Instruction.labels),
                selectinload(BuildContent.instruction_version),
            )
            .where(BuildContent.build_id == build.id)
        )
        contents = contents_result.scalars().all()

        if not contents:
            return [], []  # Build exists but is empty
        
        # Separate by load_mode
        always_contents: List[Tuple[BuildContent, Instruction, InstructionVersion]] = []
        intelligent_contents: List[Tuple[BuildContent, Instruction, InstructionVersion]] = []
        
        for content in contents:
            instruction = content.instruction
            version = content.instruction_version
            
            if not instruction or not version:
                continue
            
            # Skip unpublished and disabled
            if instruction.status != "published":
                continue
            if version.load_mode == "disabled":
                continue
            # Skills are advertised via the skills catalog (read on demand), not
            # force-loaded into context — skip them here.
            if getattr(instruction, "kind", "instruction") == "skill":
                continue

            # Skip instructions scoped to other modes/channels than this request.
            # Build-based loading is version-driven, so read the version snapshot.
            if not self._passes_mode_channel(
                getattr(version, "applicable_modes", None),
                getattr(version, "applicable_channels", None),
            ):
                continue

            # Filter by data sources: include global instructions (no data sources)
            # and instructions associated with the report's data sources
            if data_source_ids is not None:
                inst_ds_ids = {str(ds.id) for ds in instruction.data_sources} if instruction.data_sources else set()
                if inst_ds_ids and not inst_ds_ids.intersection(data_source_ids):
                    continue

            # Categorize by load_mode
            if version.load_mode == "intelligent":
                intelligent_contents.append((content, instruction, version))
            else:
                # 'always' or None (treat NULL as always for backwards compat)
                always_contents.append((content, instruction, version))
        
        # Batch load usage counts and table references for all candidates
        all_instruction_ids = [str(c.instruction_id) for c in contents]
        usage_counts = await self._batch_load_usage_counts(all_instruction_ids)
        db_table_refs = await self._batch_load_table_refs(all_instruction_ids)

        def _table_refs_for(inst_id: str, version: InstructionVersion) -> List[str]:
            # Prefer the version's denormalized snapshot; fall back to live rows.
            return self._version_table_refs(version) or db_table_refs.get(inst_id, [])

        # Build items for 'always' instructions (they all get loaded)
        always_items: List[InstructionItem] = []
        for content, instruction, version in always_contents:
            inst_id = str(instruction.id)
            always_items.append(InstructionItem(
                id=inst_id,
                category=instruction.category,
                text=version.text or "",
                load_mode=version.load_mode or "always",
                load_reason="always",
                source_type=instruction.source_type,
                title=version.title,
                labels=self._extract_labels(instruction),
                usage_count=usage_counts.get(inst_id),
                table_refs=_table_refs_for(inst_id, version),
                # Version/Build lineage tracking
                version_id=str(version.id),
                version_number=version.version_number,
                content_hash=version.content_hash,
                build_number=build.build_number,
            ))

        # Rank ALL intelligent candidates by score (labels + table names count),
        # then aggregated usage. Zero-score candidates rank last but are kept.
        keywords = self._extract_keywords(query) if query else set()
        ranked: List[Tuple[InstructionItem, float, InstructionVersion]] = []
        for content, instruction, version in intelligent_contents:
            inst_id = str(instruction.id)
            refs = _table_refs_for(inst_id, version)
            label_names = " ".join(
                l.name for l in (self._extract_labels(instruction) or []) if l.name
            )
            score = self._score_instruction_version(
                version, keywords, extra_text=" ".join(refs) + " " + label_names,
            )
            item = InstructionItem(
                id=inst_id,
                category=instruction.category,
                text=version.text or "",
                load_mode="intelligent",
                load_reason=f"search_match:{score:.2f}" if score > 0 else "fill",
                source_type=instruction.source_type,
                title=version.title,
                labels=self._extract_labels(instruction),
                usage_count=usage_counts.get(inst_id),
                table_refs=refs,
                # Version/Build lineage tracking
                version_id=str(version.id),
                version_number=version.version_number,
                content_hash=version.content_hash,
                build_number=build.build_number,
            )
            ranked.append((item, score, version))
        ranked.sort(key=lambda x: (x[1], x[0].usage_count or 0), reverse=True)

        # Apply per-user table accessibility BEFORE splitting so inaccessible
        # candidates neither consume load slots nor leak into the catalog.
        accessible = await self._filter_items_by_table_accessibility([it for it, _, _ in ranked])
        accessible_ids = {it.id for it in accessible}
        ranked = [entry for entry in ranked if entry[0].id in accessible_ids]

        remaining_slots = max(0, max_instructions - len(always_items))
        intelligent_items = [it for it, _, _ in ranked[:remaining_slots]]

        # Over-capacity intelligent instructions are advertised, not dropped.
        catalog: List[SkillCatalogItem] = [
            self._catalog_entry(
                inst_id=it.id,
                title=it.title,
                description=version.description,
                structured_data=version.structured_data,
                text=it.text,
                table_refs=it.table_refs,
                usage_count=it.usage_count,
            )
            for it, _, version in ranked[remaining_slots:remaining_slots + self.CATALOG_LIMIT]
        ]

        items = always_items + intelligent_items

        # Load referenced instructions (dependencies)
        loaded_ids = {item.id for item in items}
        dep_items = await self._load_referenced_instructions(
            loaded_ids, max_instructions - len(items), build=build
        )
        items.extend(dep_items)

        # Filter by per-user table accessibility
        items = await self._filter_items_by_table_accessibility(items)

        logger.debug(
            f"_load_from_build: loaded {len(always_items)} always + "
            f"{len(intelligent_items)} intelligent + "
            f"{len(dep_items)} dependencies + {len(catalog)} catalog "
            f"(max={max_instructions})"
        )

        return items, catalog
    
    async def _build_legacy(
        self,
        query: Optional[str] = None,
        *,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        categories: Optional[List[str]] = None,
        max_instructions: int = 50,
    ) -> InstructionsSection:
        """
        Legacy build method - loads instructions directly without build system.
        Used as fallback when no build is available.

        Load behavior:
        1. Load ALL 'always' instructions (they take priority)
        2. Fill remaining capacity with 'intelligent' instructions (keyword-matched)
        """
        # Load always instructions
        always_instructions = await self.load_always_instructions(
            data_source_ids=data_source_ids,
            category=category,
            categories=categories,
        )

        # Calculate remaining slots for intelligent instructions
        remaining_slots = max(0, max_instructions - len(always_instructions))

        # Rank intelligent instructions: top of the ranking fills remaining
        # slots, the rest (up to CATALOG_LIMIT) is advertised as catalog entries.
        intelligent_ranked: List[Tuple[Instruction, float]] = await self.search_instructions(
            query or "",
            limit=remaining_slots + self.CATALOG_LIMIT,
            data_source_ids=data_source_ids,
            category=category,
            categories=categories,
        )
        intelligent_results = intelligent_ranked[:remaining_slots]
        catalog_candidates = intelligent_ranked[remaining_slots:]

        # Collect all instruction IDs for batch stats loading
        all_instruction_ids = [str(inst.id) for inst in always_instructions]
        all_instruction_ids.extend([str(inst.id) for inst, _ in intelligent_ranked])

        # Batch-load usage stats and table references
        usage_counts = await self._batch_load_usage_counts(all_instruction_ids)
        table_refs = await self._batch_load_table_refs(all_instruction_ids)

        # Deduplicate and build items with tracking
        seen_ids: Set[str] = set()
        items: List[InstructionItem] = []

        # Add always instructions first (they all get loaded)
        for inst in always_instructions:
            inst_id = str(inst.id)
            if inst_id not in seen_ids:
                seen_ids.add(inst_id)
                items.append(InstructionItem(
                    id=inst_id,
                    category=inst.category,
                    text=inst.text or "",
                    load_mode=inst.load_mode or "always",
                    load_reason="always",
                    source_type=inst.source_type,
                    title=inst.title,
                    labels=self._extract_labels(inst),
                    usage_count=usage_counts.get(inst_id),
                    table_refs=table_refs.get(inst_id, []),
                ))

        # Add intelligent (search-matched) instructions to fill remaining slots
        for inst, score in intelligent_results:
            inst_id = str(inst.id)
            if inst_id not in seen_ids:
                seen_ids.add(inst_id)
                items.append(InstructionItem(
                    id=inst_id,
                    category=inst.category,
                    text=inst.text or "",
                    load_mode="intelligent",
                    load_reason=f"search_match:{score:.2f}" if score > 0 else "fill",
                    source_type=inst.source_type,
                    title=inst.title,
                    labels=self._extract_labels(inst),
                    usage_count=usage_counts.get(inst_id),
                    table_refs=table_refs.get(inst_id, []),
                ))

        # Over-capacity intelligent instructions become catalog entries.
        catalog: List[SkillCatalogItem] = [
            self._catalog_entry(
                inst_id=str(inst.id),
                title=inst.title,
                description=inst.description,
                structured_data=inst.structured_data,
                text=inst.text or "",
                table_refs=table_refs.get(str(inst.id), []),
                usage_count=usage_counts.get(str(inst.id)),
            )
            for inst, _ in catalog_candidates
            if str(inst.id) not in seen_ids
        ]

        # Load referenced instructions (dependencies)
        loaded_ids = {item.id for item in items}
        dep_items = await self._load_referenced_instructions(
            loaded_ids, max_instructions - len(items), build=None
        )
        items.extend(dep_items)

        # Filter by per-user table accessibility
        items = await self._filter_items_by_table_accessibility(items)

        logger.debug(
            f"_build_legacy: loaded {len(always_instructions)} always + "
            f"{len(intelligent_results)} intelligent + "
            f"{len(dep_items)} dependencies + {len(catalog)} catalog "
            f"(max={max_instructions})"
        )

        return InstructionsSection(items=items, available_instructions=catalog)

    # --------------------------------------------------------------------- #
    # Private helpers                                                       #
    # --------------------------------------------------------------------- #
    
    async def _load_referenced_instructions(
        self,
        loaded_ids: Set[str],
        remaining_slots: int,
        build: Optional[InstructionBuild] = None,
        _depth: int = 0,
    ) -> List[InstructionItem]:
        """
        Load instructions that are referenced by already-loaded instructions.

        Uses InstructionReference records (object_type='instruction') to find
        dependencies. Recurses once to resolve transitive references (A->B->C).
        """
        if remaining_slots <= 0 or not loaded_ids:
            return []

        # Step 1: Find instruction references from loaded instructions
        ref_result = await self.db.execute(
            select(InstructionReference.object_id)
            .where(
                and_(
                    InstructionReference.instruction_id.in_(loaded_ids),
                    InstructionReference.object_type == 'instruction',
                )
            )
        )
        referenced_ids = {row[0] for row in ref_result.all()}
        missing_ids = referenced_ids - loaded_ids
        if not missing_ids:
            return []

        # Step 2: Load the missing instructions
        dep_items: List[InstructionItem] = []

        if build:
            # Build mode: load via BuildContent to get versioned text
            contents_result = await self.db.execute(
                select(BuildContent)
                .options(
                    selectinload(BuildContent.instruction).selectinload(Instruction.data_sources).options(lazyload("*")),
                    selectinload(BuildContent.instruction).selectinload(Instruction.labels),
                    selectinload(BuildContent.instruction_version),
                )
                .where(
                    and_(
                        BuildContent.build_id == build.id,
                        BuildContent.instruction_id.in_(missing_ids),
                    )
                )
            )
            contents = contents_result.scalars().all()

            usage_counts = await self._batch_load_usage_counts(list(missing_ids))

            for content in contents:
                instruction = content.instruction
                version = content.instruction_version
                if not instruction or not version:
                    continue
                if instruction.status != "published" or instruction.deleted_at is not None:
                    continue
                if version.load_mode == "disabled":
                    continue

                inst_id = str(instruction.id)
                dep_items.append(InstructionItem(
                    id=inst_id,
                    category=instruction.category,
                    text=version.text or "",
                    load_mode=version.load_mode or "always",
                    load_reason="dependency",
                    source_type=instruction.source_type,
                    title=version.title,
                    labels=self._extract_labels(instruction),
                    usage_count=usage_counts.get(inst_id),
                    version_id=str(version.id),
                    version_number=version.version_number,
                    content_hash=version.content_hash,
                    build_number=build.build_number,
                ))
                if len(dep_items) >= remaining_slots:
                    break
        else:
            # Legacy mode: load instructions directly
            inst_result = await self.db.execute(
                select(Instruction)
                .where(
                    and_(
                        Instruction.id.in_(missing_ids),
                        Instruction.organization_id == self.organization.id,
                        Instruction.status == "published",
                        Instruction.deleted_at.is_(None),
                        Instruction.load_mode != "disabled",
                    )
                )
            )
            instructions = inst_result.scalars().all()

            usage_counts = await self._batch_load_usage_counts(list(missing_ids))

            for inst in instructions:
                inst_id = str(inst.id)
                dep_items.append(InstructionItem(
                    id=inst_id,
                    category=inst.category,
                    text=inst.text or "",
                    load_mode=inst.load_mode or "always",
                    load_reason="dependency",
                    source_type=inst.source_type,
                    title=inst.title,
                    labels=self._extract_labels(inst),
                    usage_count=usage_counts.get(inst_id),
                ))
                if len(dep_items) >= remaining_slots:
                    break

        if not dep_items:
            return []

        logger.debug(f"_load_referenced_instructions: loaded {len(dep_items)} dependency instructions (depth={_depth})")

        # Step 3: One level of transitive resolution
        if _depth < 1:
            new_loaded_ids = loaded_ids | {item.id for item in dep_items}
            new_remaining = remaining_slots - len(dep_items)
            transitive = await self._load_referenced_instructions(
                new_loaded_ids, new_remaining, build=build, _depth=_depth + 1
            )
            dep_items.extend(transitive)

        return dep_items

    async def _batch_load_usage_counts(self, instruction_ids: List[str]) -> Dict[str, int]:
        """Batch-load usage counts for multiple instructions."""
        if not instruction_ids:
            return {}
        
        try:
            org_id_str = str(self.organization.id)
            instruction_ids_set = set(instruction_ids)
            
            # Query ALL org-wide stats for this org, then filter in Python
            # This avoids any issues with IN clause on different ID formats
            stmt = (
                select(InstructionStats)
                .where(
                    and_(
                        InstructionStats.org_id == org_id_str,
                        or_(
                            InstructionStats.report_id.is_(None),
                            InstructionStats.report_id == "",
                        ),
                    )
                )
            )
            result = await self.db.execute(stmt)
            all_stats = result.scalars().all()
            
            # Filter to requested instruction IDs and build dict
            counts: Dict[str, int] = {}
            for stat in all_stats:
                stat_inst_id = str(stat.instruction_id)
                if stat_inst_id in instruction_ids_set and stat.usage_count:
                    counts[stat_inst_id] = stat.usage_count
            
            logger.debug(f"_batch_load_usage_counts: Found {len(counts)} stats for {len(instruction_ids)} instructions")
            return counts
        except Exception as e:
            logger.warning(f"Failed to load usage counts: {e}")
            return {}
    
    def _extract_labels(self, instruction: Instruction) -> Optional[List[InstructionLabelItem]]:
        """Extract labels from instruction for tracking."""
        if not hasattr(instruction, 'labels') or not instruction.labels:
            return None
        return [
            InstructionLabelItem(
                id=str(label.id) if hasattr(label, 'id') else None,
                name=label.name if hasattr(label, 'name') else str(label),
                color=label.color if hasattr(label, 'color') else None,
            )
            for label in instruction.labels
        ]

    async def _batch_load_table_refs(self, instruction_ids: List[str]) -> Dict[str, List[str]]:
        """Batch-load referenced table display names per instruction.

        Reads InstructionReference rows with object_type='datasource_table' and
        returns {instruction_id: ["invoices", "orders.total", ...]} using the
        stored display_text (references without one are skipped — we render
        names, not raw ids).
        """
        if not instruction_ids:
            return {}
        try:
            result = await self.db.execute(
                select(
                    InstructionReference.instruction_id,
                    InstructionReference.display_text,
                    InstructionReference.column_name,
                )
                .where(
                    and_(
                        InstructionReference.instruction_id.in_(instruction_ids),
                        InstructionReference.object_type == "datasource_table",
                    )
                )
            )
            refs: Dict[str, List[str]] = {}
            for inst_id, display_text, column_name in result.all():
                name = (display_text or "").strip()
                if not name:
                    continue
                ref = f"{name}.{column_name}" if column_name else name
                bucket = refs.setdefault(str(inst_id), [])
                if ref not in bucket:
                    bucket.append(ref)
            return refs
        except Exception as e:
            logger.warning(f"Failed to load table refs: {e}")
            return {}

    @staticmethod
    def _version_table_refs(version: InstructionVersion) -> List[str]:
        """Table display names from a version's denormalized references_json."""
        refs: List[str] = []
        rj = getattr(version, "references_json", None) or []
        if isinstance(rj, list):
            for r in rj:
                if not isinstance(r, dict) or r.get("object_type") != "datasource_table":
                    continue
                name = (r.get("display_text") or "").strip()
                if not name:
                    continue
                col = r.get("column_name")
                ref = f"{name}.{col}" if col else name
                if ref not in refs:
                    refs.append(ref)
        return refs

    def _catalog_entry(
        self,
        *,
        inst_id: str,
        title: Optional[str],
        description: Optional[str],
        structured_data,
        text: str,
        table_refs: List[str],
        usage_count: Optional[int],
    ) -> SkillCatalogItem:
        """Build an <available_instructions> catalog entry.

        Titled instructions advertise title + one-line description. Untitled
        ones advertise the first CATALOG_SNIPPET_LEN chars of the body as the
        title (whitespace-collapsed) with no separate description — deriving
        both from the same body would print it twice.
        """
        if title and title.strip():
            desc = None
            if description and description.strip():
                desc = description.strip()
            elif isinstance(structured_data, dict) and structured_data.get("description"):
                desc = str(structured_data["description"]).strip()
            else:
                stripped = (text or "").strip()
                desc = next((ln.strip() for ln in stripped.splitlines() if ln.strip()), None)
            if desc and len(desc) > 160:
                desc = desc[:157] + "…"
            return SkillCatalogItem(
                id=inst_id,
                short_id=inst_id[:8],
                title=title.strip(),
                description=desc,
                table_refs=table_refs,
                usage_count=usage_count,
            )

        snippet = " ".join((text or "").split())
        if len(snippet) > self.CATALOG_SNIPPET_LEN:
            snippet = snippet[: self.CATALOG_SNIPPET_LEN - 1] + "…"
        return SkillCatalogItem(
            id=inst_id,
            short_id=inst_id[:8],
            title=snippet or inst_id[:8],
            description=None,
            table_refs=table_refs,
            usage_count=usage_count,
        )
    
    async def _get_user_inaccessible_table_ids(self) -> Set[str]:
        """Return datasource_table IDs the current user explicitly cannot access.

        Only applies when user_data_source_tables rows exist (i.e. the connection
        uses auth_policy='user_required' and an overlay sync has run).  If there
        are no overlay rows for the user, returns an empty set (= no filtering).
        """
        if not self.current_user:
            return set()

        result = await self.db.execute(
            select(UserDataSourceTable.data_source_table_id)
            .where(
                UserDataSourceTable.user_id == str(self.current_user.id),
                UserDataSourceTable.is_accessible == False,
                UserDataSourceTable.data_source_table_id.isnot(None),
            )
        )
        return {row[0] for row in result.all()}

    async def _filter_instructions_by_table_accessibility(
        self,
        instructions: List[Instruction],
    ) -> List[Instruction]:
        """Remove instructions whose table references are all inaccessible to the user.

        Rules:
        - No table references → keep (global / text-only instruction)
        - All referenced tables inaccessible → exclude
        - At least one referenced table accessible → keep
        - No current_user → keep all (system/admin context)
        """
        inaccessible = await self._get_user_inaccessible_table_ids()
        if not inaccessible:
            return instructions

        # Batch-load table references for all candidate instructions
        instruction_ids = [str(inst.id) for inst in instructions]
        if not instruction_ids:
            return instructions

        ref_result = await self.db.execute(
            select(InstructionReference.instruction_id, InstructionReference.object_id)
            .where(
                InstructionReference.instruction_id.in_(instruction_ids),
                InstructionReference.object_type == "datasource_table",
            )
        )

        # Build map: instruction_id -> set of referenced table IDs
        refs_by_instruction: Dict[str, Set[str]] = {}
        for inst_id, table_id in ref_result.all():
            refs_by_instruction.setdefault(inst_id, set()).add(table_id)

        filtered = []
        for inst in instructions:
            inst_id = str(inst.id)
            table_refs = refs_by_instruction.get(inst_id)
            if not table_refs:
                # No table references — keep
                filtered.append(inst)
            elif table_refs - inaccessible:
                # At least one accessible table — keep
                filtered.append(inst)
            else:
                # All referenced tables inaccessible — exclude
                logger.debug(
                    f"Excluding instruction {inst_id} — all table refs inaccessible for user {self.current_user.id}"
                )
        return filtered

    async def _filter_items_by_table_accessibility(
        self,
        items: List[InstructionItem],
    ) -> List[InstructionItem]:
        """Same as _filter_instructions_by_table_accessibility but for InstructionItem objects.

        Used in build-based loading where we have InstructionItem (not ORM Instruction).
        """
        inaccessible = await self._get_user_inaccessible_table_ids()
        if not inaccessible:
            return items

        item_ids = [item.id for item in items]
        if not item_ids:
            return items

        ref_result = await self.db.execute(
            select(InstructionReference.instruction_id, InstructionReference.object_id)
            .where(
                InstructionReference.instruction_id.in_(item_ids),
                InstructionReference.object_type == "datasource_table",
            )
        )

        refs_by_instruction: Dict[str, Set[str]] = {}
        for inst_id, table_id in ref_result.all():
            refs_by_instruction.setdefault(inst_id, set()).add(table_id)

        filtered = []
        for item in items:
            table_refs = refs_by_instruction.get(item.id)
            if not table_refs:
                filtered.append(item)
            elif table_refs - inaccessible:
                filtered.append(item)
            else:
                logger.debug(
                    f"Excluding instruction {item.id} — all table refs inaccessible for user"
                )
        return filtered

    def _extract_keywords(self, text: str) -> Set[str]:
        """Extract meaningful keywords from text."""
        # Lowercase and split on non-alphanumeric (including underscores for better matching)
        words = re.split(r'[^a-z0-9]+', text.lower())
        # Filter out stopwords and short words
        keywords = {
            w for w in words
            if w and len(w) >= 2 and w not in self.STOPWORDS
        }
        return keywords

    @staticmethod
    def _stem(word: str) -> str:
        """Very light suffix stripper so morphological variants map to the same
        stem (revenues/revenue, churned/churn, cancelling/cancel, matches/match).

        Both query and document keywords go through this, so the only thing
        that matters is consistency — not linguistic correctness.
        """
        if len(word) <= 3:
            return word
        if word.endswith("ies") and len(word) > 4:
            return word[:-3] + "y"
        stemmed = word
        if word.endswith("es") and len(word) - 2 >= 3 and (
            word[-3] in "sxz" or word.endswith(("ches", "shes"))
        ):
            stemmed = word[:-2]          # matches -> match, boxes -> box
        elif word.endswith("s") and not word.endswith("ss") and len(word) - 1 >= 3:
            stemmed = word[:-1]          # revenues -> revenue, sales -> sale
        else:
            for suffix in ("ing", "ed"):
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    stemmed = word[: -len(suffix)]
                    break
        # Collapse a trailing double consonant (cancell -> cancel, plann -> plan)
        if len(stemmed) >= 4 and stemmed[-1] == stemmed[-2] and stemmed[-1] not in "aeiou":
            stemmed = stemmed[:-1]
        return stemmed

    def _score_instruction(
        self,
        instruction: Instruction,
        keywords: Set[str],
        extra_text: str = "",
    ) -> float:
        """
        Score an instruction based on keyword matching.

        Matches in title/labels/table names are weighted above body matches.
        Returns a score between 0 and 1.
        """
        searchable = self._build_searchable_text(instruction)
        priority_parts = []
        if instruction.title:
            priority_parts.append(instruction.title)
        try:
            if instruction.labels:
                priority_parts.extend(l.name for l in instruction.labels if getattr(l, "name", None))
        except Exception:
            pass  # labels not loaded (lazy='raise') — skip
        if extra_text:
            priority_parts.append(extra_text)
        return self._combined_score(searchable, " ".join(priority_parts), keywords)

    def _score_instruction_version(
        self,
        version: InstructionVersion,
        keywords: Set[str],
        extra_text: str = "",
    ) -> float:
        """
        Score an instruction version based on keyword matching.

        Used for build-based loading to match against version.text/title plus
        priority fields (title, labels, referenced table names).
        Returns a score between 0 and 1.
        """
        parts = [version.text or ""]
        if version.structured_data:
            if isinstance(version.structured_data, dict):
                if version.structured_data.get('name'):
                    parts.append(version.structured_data['name'])
                if version.structured_data.get('description'):
                    parts.append(version.structured_data['description'])
        priority_parts = []
        if version.title:
            priority_parts.append(version.title)
        if extra_text:
            priority_parts.append(extra_text)
        return self._combined_score(" ".join(parts), " ".join(priority_parts), keywords)

    def _combined_score(self, body: str, priority: str, keywords: Set[str]) -> float:
        """Coverage score over the body, boosted by matches in priority text
        (title / labels / table names). Capped at 1.0."""
        body_score = self._score_text(f"{body} {priority}", keywords)
        priority_score = self._score_text(priority, keywords) if priority else 0.0
        return min(1.0, body_score + 0.5 * priority_score)

    def _score_text(self, searchable: str, keywords: Set[str]) -> float:
        """
        Score text by query-keyword coverage: what fraction of the query's
        keywords appear in the text (exactly, stem-equal, or as a substring in
        either direction). Returns a score between 0 and 1.

        Unlike Jaccard (intersection / union of both vocabularies), coverage
        does not penalize long instructions — only unmatched *query* words
        lower the score.
        """
        if not keywords:
            return 0.0
        searchable_lower = searchable.lower()
        searchable_keywords = self._extract_keywords(searchable)
        if not searchable_keywords and not searchable_lower.strip():
            return 0.0

        stemmed_searchable = {self._stem(w) for w in searchable_keywords}

        matched = 0.0
        for kw in keywords:
            if kw in searchable_keywords:
                matched += 1.0
                continue
            if self._stem(kw) in stemmed_searchable:
                matched += 0.9
                continue
            # Substring in the raw text (helps joined words: "invoiceline")
            if len(kw) >= 3 and kw in searchable_lower:
                matched += 0.8
                continue
            # Symmetric containment between keywords ("churn" ~ "churned",
            # "cancellation" query vs "cancel" in text)
            if len(kw) >= 4 and any(
                len(sk) >= 4 and (kw in sk or sk in kw) for sk in searchable_keywords
            ):
                matched += 0.7
        return matched / len(keywords)

    def _build_searchable_text(self, instruction: Instruction) -> str:
        """Build searchable text from instruction fields."""
        parts = [instruction.text or ""]

        if instruction.title:
            parts.append(instruction.title)

        if instruction.description:
            parts.append(instruction.description)

        if instruction.formatted_content:
            parts.append(instruction.formatted_content)

        # Include structured data fields if present
        if instruction.structured_data:
            if instruction.structured_data.get('name'):
                parts.append(instruction.structured_data['name'])
            if instruction.structured_data.get('description'):
                parts.append(instruction.structured_data['description'])
            if instruction.structured_data.get('path'):
                parts.append(instruction.structured_data['path'])
            # Include column names
            columns = instruction.structured_data.get('columns', [])
            for col in columns:
                if isinstance(col, dict) and col.get('name'):
                    parts.append(col['name'])

        return " ".join(parts)
    
    @staticmethod
    def _format_instruction(instruction: Instruction) -> str:
        """
        Render a single instruction in a minimal, self-describing format.
        """
        return (
            f"  <instruction id=\"{instruction.id}\" "
            f"category=\"{instruction.category}\""
            f">\n"
            f"{instruction.text.strip()}\n"
            f"  </instruction>"
        )