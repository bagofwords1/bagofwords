from typing import List, Optional, Set, Tuple
import re
import logging

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction import (
    Instruction
)

from app.models.organization import Organization
from app.models.user import User


from app.ai.context.sections.instructions_section import InstructionsSection, InstructionItem

logger = logging.getLogger(__name__)


class InstructionContextBuilder:
    """
    Helper for fetching instructions that should be supplied to the LLM.

    Supports two loading strategies:
    - `load_always_instructions()`: Load instructions with load_mode='always'
    - `search_instructions()`: Search instructions with load_mode='intelligent' by keyword
    - `build_full_context()`: Combined always + intelligent instructions

    Usage example
    -------------
    ```python
    builder = InstructionContextBuilder(db_session, organization)
    
    # All published instructions, regardless of category
    all_instructions = await builder.load_instructions()

    # Load instructions with load_mode='always'
    always_instructions = await builder.load_always_instructions()
    
    # Search intelligently loaded instructions
    relevant_instructions = await builder.search_instructions("revenue metrics")
    
    # Build full context combining both strategies
    full_context = await builder.build_full_context("user query about revenue")
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
    
    async def build_full_context(
        self,
        query: str,
        *,
        data_source_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
        intelligent_limit: int = 10,
    ) -> InstructionsSection:
        """
        Build combined context with 'always' and 'intelligent' instructions.
        
        Parameters
        ----------
        query : str
            The user query for intelligent search.
        data_source_ids : List[str] | None, optional
            Filter by data sources.
        category : str | None, optional
            Filter by category.
        intelligent_limit : int
            Max intelligent instructions to include.
            
        Returns
        -------
        InstructionsSection
            Combined context section.
        """
        # Load always instructions
        always_instructions = await self.load_always_instructions(
            data_source_ids=data_source_ids,
            category=category,
        )
        
        # Search intelligent instructions
        intelligent_results = await self.search_instructions(
            query,
            limit=intelligent_limit,
            data_source_ids=data_source_ids,
            category=category,
        )
        intelligent_instructions = [inst for inst, _ in intelligent_results]
        
        # Deduplicate (in case an instruction appears in both)
        seen_ids: Set[str] = set()
        combined: List[Instruction] = []
        
        for inst in always_instructions:
            if inst.id not in seen_ids:
                seen_ids.add(inst.id)
                combined.append(inst)
        
        for inst in intelligent_instructions:
            if inst.id not in seen_ids:
                seen_ids.add(inst.id)
                combined.append(inst)
        
        # Build section
        items = [
            InstructionItem(
                id=str(i.id),
                category=i.category,
                text=i.text or ""
            )
            for i in combined
        ]
        return InstructionsSection(items=items)

    async def build(
        self,
        *,
        status: str = "published",
        category: Optional[str] = None,
    ) -> InstructionsSection:
        """Build object-based instructions section (legacy method)."""
        instructions = await self.load_instructions(status=status, category=category)
        items = [InstructionItem(id=str(i.id), category=i.category, text=i.text or "") for i in instructions]
        return InstructionsSection(items=items)

    # --------------------------------------------------------------------- #
    # Private helpers                                                       #
    # --------------------------------------------------------------------- #
    
    def _extract_keywords(self, text: str) -> Set[str]:
        """Extract meaningful keywords from text."""
        # Lowercase and split on non-alphanumeric
        words = re.split(r'[^a-z0-9_]+', text.lower())
        # Filter out stopwords and short words
        keywords = {
            w for w in words 
            if w and len(w) >= 2 and w not in self.STOPWORDS
        }
        return keywords
    
    def _score_instruction(self, instruction: Instruction, keywords: Set[str]) -> float:
        """
        Score an instruction based on keyword matching.
        
        Returns a score between 0 and 1.
        """
        # Build searchable text from instruction
        searchable = self._build_searchable_text(instruction)
        searchable_keywords = self._extract_keywords(searchable)
        
        if not searchable_keywords:
            return 0.0
        
        # Calculate Jaccard similarity
        intersection = len(keywords & searchable_keywords)
        union = len(keywords | searchable_keywords)
        
        if union == 0:
            return 0.0
        
        return intersection / union
    
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