from typing import List, Optional, Set, Tuple, Dict
import re
import logging

from sqlalchemy import select, and_, or_, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction import Instruction
from app.models.instruction_stats import InstructionStats
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.instruction_version import InstructionVersion
from app.models.organization import Organization
from app.models.user import User

from app.ai.context.sections.instructions_section import InstructionsSection, InstructionItem, InstructionLabelItem

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

    def __init__(self, db: AsyncSession, organization: Organization, current_user: Optional[User] = None, organization_settings=None):
        self.db = db
        self.organization = organization
        self.current_user = current_user
        self.organization_settings = organization_settings
    
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
            If provided, restrict to this category.
            
        Returns
        -------
        List[Instruction]
            Instructions that should always be loaded.
        """
        stmt = (
            select(Instruction)
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
        
        if category is not None:
            stmt = stmt.where(Instruction.category == category)
        
        # TODO: Add data source filtering when needed
        # For now, return all 'always' instructions
        
        result = await self.db.execute(stmt)
        return result.scalars().all()
    
    async def search_instructions(
        self,
        query: str,
        *,
        limit: int = 10,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
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
            If provided, restrict to this category.
            
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
            .where(
                and_(
                    Instruction.status == "published",
                    Instruction.organization_id == self.organization.id,
                    Instruction.deleted_at.is_(None),
                    Instruction.load_mode == "intelligent",
                )
            )
        )
        
        if category is not None:
            stmt = stmt.where(Instruction.category == category)
        
        result = await self.db.execute(stmt)
        all_instructions = result.scalars().all()
        
        # Score and filter by keyword match (or include all if no keywords)
        scored: List[Tuple[Instruction, float]] = []
        for instruction in all_instructions:
            if keywords:
                score = self._score_instruction(instruction, keywords)
                if score > 0:
                    scored.append((instruction, score))
            else:
                # No query - include all with score 0
                scored.append((instruction, 0.0))
        
        # Sort by score descending and limit
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]
    
    async def build(
        self,
        query: Optional[str] = None,
        *,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
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
            Filter by category.
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
        
        # Try to load from build if build_id is provided or main build exists
        build_items = await self._load_from_build(
            build_id=build_id,
            query=query or "",
            max_instructions=max_instructions,
        )
        
        if build_items is not None:
            # Build-based loading - return items with version tracking
            return InstructionsSection(items=build_items)
        
        # Fallback to legacy behavior (direct instruction query)
        return await self._build_legacy(
            query=query,
            data_source_ids=data_source_ids,
            category=category,
            max_instructions=max_instructions,
        )
    
    async def _load_from_build(
        self,
        build_id: Optional[str] = None,
        query: str = "",
        max_instructions: int = 50,
    ) -> Optional[List[InstructionItem]]:
        """
        Load instructions from a specific build or the main build.
        
        Load behavior:
        1. Load ALL 'always' instructions (they take priority)
        2. Fill remaining capacity with 'intelligent' instructions (keyword-matched)
        3. Skip 'disabled' instructions
        
        Returns None if no build is available (fallback to legacy).
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
                selectinload(BuildContent.instruction),
                selectinload(BuildContent.instruction_version),
            )
            .where(BuildContent.build_id == build.id)
        )
        contents = contents_result.scalars().all()
        
        if not contents:
            return []  # Build exists but is empty
        
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
            
            # Categorize by load_mode
            if version.load_mode == "intelligent":
                intelligent_contents.append((content, instruction, version))
            else:
                # 'always' or None (treat NULL as always for backwards compat)
                always_contents.append((content, instruction, version))
        
        # Batch load usage counts for all candidates
        all_instruction_ids = [str(c.instruction_id) for c in contents]
        usage_counts = await self._batch_load_usage_counts(all_instruction_ids)
        
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
                # Version/Build lineage tracking
                version_id=str(version.id),
                version_number=version.version_number,
                content_hash=version.content_hash,
                build_number=build.build_number,
            ))
        
        # Calculate remaining slots for intelligent instructions
        remaining_slots = max_instructions - len(always_items)
        intelligent_items: List[InstructionItem] = []
        
        if remaining_slots > 0 and intelligent_contents:
            # Extract keywords from query for scoring
            keywords = self._extract_keywords(query) if query else set()
            
            # Score and rank intelligent instructions
            scored: List[Tuple[InstructionItem, float]] = []
            for content, instruction, version in intelligent_contents:
                score = self._score_instruction_version(version, keywords)
                # Include if score > 0 OR if we have no query (load all intelligent)
                if score > 0 or not query:
                    inst_id = str(instruction.id)
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
                        # Version/Build lineage tracking
                        version_id=str(version.id),
                        version_number=version.version_number,
                        content_hash=version.content_hash,
                        build_number=build.build_number,
                    )
                    scored.append((item, score))
            
            # Sort by score descending and take top N
            scored.sort(key=lambda x: x[1], reverse=True)
            intelligent_items = [item for item, _ in scored[:remaining_slots]]
        
        logger.info(
            f"_load_from_build: loaded {len(always_items)} always + "
            f"{len(intelligent_items)} intelligent (max={max_instructions})"
        )
        
        return always_items + intelligent_items
    
    async def _build_legacy(
        self,
        query: Optional[str] = None,
        *,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
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
        )
        
        # Calculate remaining slots for intelligent instructions
        remaining_slots = max(0, max_instructions - len(always_instructions))
        
        # Search intelligent instructions to fill remaining slots
        intelligent_results: List[Tuple[Instruction, float]] = []
        if remaining_slots > 0:
            intelligent_results = await self.search_instructions(
                query or "",
                limit=remaining_slots,
                data_source_ids=data_source_ids,
                category=category,
            )
        
        # Collect all instruction IDs for batch stats loading
        all_instruction_ids = [str(inst.id) for inst in always_instructions]
        all_instruction_ids.extend([str(inst.id) for inst, _ in intelligent_results])
        
        # Batch-load usage stats for all instructions
        usage_counts = await self._batch_load_usage_counts(all_instruction_ids)
        
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
                ))
        
        logger.info(
            f"_build_legacy: loaded {len(always_instructions)} always + "
            f"{len(intelligent_results)} intelligent (max={max_instructions})"
        )
        
        return InstructionsSection(items=items)

    # --------------------------------------------------------------------- #
    # Private helpers                                                       #
    # --------------------------------------------------------------------- #
    
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
            
            logger.info(f"_batch_load_usage_counts: Found {len(counts)} stats for {len(instruction_ids)} instructions")
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
    
    def _score_instruction(self, instruction: Instruction, keywords: Set[str]) -> float:
        """
        Score an instruction based on keyword matching.
        
        Uses both exact matching and substring matching for better recall.
        Returns a score between 0 and 1.
        """
        # Build searchable text from instruction
        searchable = self._build_searchable_text(instruction)
        return self._score_text(searchable, keywords)
    
    def _score_instruction_version(self, version: InstructionVersion, keywords: Set[str]) -> float:
        """
        Score an instruction version based on keyword matching.
        
        Used for build-based loading to match against version.text/title.
        Returns a score between 0 and 1.
        """
        # Build searchable text from version fields
        parts = [version.text or ""]
        if version.title:
            parts.append(version.title)
        if version.structured_data:
            if isinstance(version.structured_data, dict):
                if version.structured_data.get('name'):
                    parts.append(version.structured_data['name'])
                if version.structured_data.get('description'):
                    parts.append(version.structured_data['description'])
        searchable = " ".join(parts)
        return self._score_text(searchable, keywords)
    
    def _score_text(self, searchable: str, keywords: Set[str]) -> float:
        """
        Score text based on keyword matching.
        
        Uses both exact matching and substring matching for better recall.
        Returns a score between 0 and 1.
        """
        searchable_lower = searchable.lower()
        searchable_keywords = self._extract_keywords(searchable)
        
        if not searchable_keywords and not searchable_lower:
            return 0.0
        
        if not keywords:
            return 0.0
        
        # Score 1: Exact keyword match (Jaccard similarity)
        exact_intersection = len(keywords & searchable_keywords)
        exact_union = len(keywords | searchable_keywords) if searchable_keywords else 1
        jaccard_score = exact_intersection / exact_union if exact_union > 0 else 0.0
        
        # Score 2: Substring match (check if query keywords appear in searchable text)
        substring_matches = 0
        for kw in keywords:
            if len(kw) >= 3 and kw in searchable_lower:  # Only match keywords 3+ chars
                substring_matches += 1
        substring_score = substring_matches / len(keywords) if keywords else 0.0
        
        # Combined score: max of exact and substring (substring helps when words are joined)
        return max(jaccard_score, substring_score * 0.8)  # Slight penalty for substring-only
    
    def _build_searchable_text(self, instruction: Instruction) -> str:
        """Build searchable text from instruction fields."""
        parts = [instruction.text or ""]
        
        if instruction.title:
            parts.append(instruction.title)
        
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