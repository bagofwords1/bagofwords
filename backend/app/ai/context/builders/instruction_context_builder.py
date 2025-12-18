from typing import List, Optional, Set, Tuple, Dict
import re
import logging

from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction import Instruction
from app.models.instruction_stats import InstructionStats
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

    Usage example
    -------------
    ```python
    builder = InstructionContextBuilder(db_session, organization)
    
    # Load instructions with load_mode='always'
    always_instructions = await builder.load_always_instructions()
    
    # Search intelligently loaded instructions
    relevant_instructions = await builder.search_instructions("revenue metrics")
    
    # Build context with proper load tracking
    # Without query: only loads 'always' instructions
    context = await builder.build()
    
    # With query: loads 'always' + searches 'intelligent' instructions
    context = await builder.build(query="user query about revenue")
    ```
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

    def __init__(self, db: AsyncSession, organization: Organization, current_user: Optional[User] = None):
        self.db = db
        self.organization = organization
        self.current_user = current_user

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
        
        Parameters
        ----------
        query : str
            The user query to match against.
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
        keywords = self._extract_keywords(query)
        
        if not keywords:
            return []
        
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
        
        # Score and filter by keyword match
        scored: List[Tuple[Instruction, float]] = []
        for instruction in all_instructions:
            score = self._score_instruction(instruction, keywords)
            if score > 0:
                scored.append((instruction, score))
        
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
    ) -> InstructionsSection:
        """
        Build instructions context with proper load tracking.
        
        Parameters
        ----------
        query : str | None, optional
            The user query for intelligent search. If None, only 'always'
            instructions are loaded. If provided, also searches for 
            'intelligent' instructions that match.
        data_source_ids : List[str] | None, optional
            Filter by data sources.
        category : str | None, optional
            Filter by category.
        intelligent_limit : int
            Max intelligent instructions to include (only used when query provided).
            
        Returns
        -------
        InstructionsSection
            Instructions section with proper load_mode and load_reason tracking.
        """
        # Load always instructions
        always_instructions = await self.load_always_instructions(
            data_source_ids=data_source_ids,
            category=category,
        )
        
        # Search intelligent instructions only if query is provided
        intelligent_results: List[Tuple[Instruction, float]] = []
        if query:
            intelligent_results = await self.search_instructions(
                query,
                limit=intelligent_limit,
                data_source_ids=data_source_ids,
                category=category,
            )
        
        # Collect all instruction IDs for batch stats loading
        all_instruction_ids = [str(inst.id) for inst in always_instructions]
        all_instruction_ids.extend([str(inst.id) for inst, _ in intelligent_results])
        
        # Batch-load usage stats for all instructions
        usage_counts = await self._batch_load_usage_counts(all_instruction_ids)
        logger.info(f"InstructionContextBuilder.build: usage_counts={usage_counts}")
        
        # Deduplicate and build items with tracking
        seen_ids: Set[str] = set()
        items: List[InstructionItem] = []
        
        # Add always instructions first
        for inst in always_instructions:
            inst_id = str(inst.id)
            if inst_id not in seen_ids:
                seen_ids.add(inst_id)
                usage = usage_counts.get(inst_id)
                logger.info(f"InstructionContextBuilder.build: inst={inst_id}, usage={usage}")
                items.append(InstructionItem(
                    id=inst_id,
                    category=inst.category,
                    text=inst.text or "",
                    load_mode=inst.load_mode or "always",
                    load_reason="always",
                    source_type=inst.source_type,
                    title=inst.title,
                    labels=self._extract_labels(inst),
                    usage_count=usage,
                ))
        
        # Add intelligent (search-matched) instructions
        for inst, score in intelligent_results:
            inst_id = str(inst.id)
            if inst_id not in seen_ids:
                seen_ids.add(inst_id)
                items.append(InstructionItem(
                    id=inst_id,
                    category=inst.category,
                    text=inst.text or "",
                    load_mode="intelligent",
                    load_reason=f"search_match:{score:.2f}",
                    source_type=inst.source_type,
                    title=inst.title,
                    labels=self._extract_labels(inst),
                    usage_count=usage_counts.get(inst_id),
                ))
        
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
        searchable_lower = searchable.lower()
        searchable_keywords = self._extract_keywords(searchable)
        
        if not searchable_keywords and not searchable_lower:
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