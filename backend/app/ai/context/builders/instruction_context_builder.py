from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instruction import (
    Instruction
)

from app.models.organization import Organization
from app.models.user import User


class InstructionContextBuilder:
    """
    Helper for fetching instructions that should be supplied to the LLM.

    Usage example
    -------------
    ```python
    builder = InstructionContextBuilder(db_session)
    # All published instructions, regardless of category
    all_instructions = await builder.load_instructions()

    # Only published code-generation instructions
    code_gen_instructions = await builder.load_instructions(
        category="code_gen"
    )

    # Draft data-modelling instructions
    draft_dm_instructions = await builder.load_instructions(
        status="draft",
        category="data_modeling",
    )
    ```
    """

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
        stmt = select(Instruction).where(Instruction.status == status).where(Instruction.organization_id == self.organization.id)

        if category is not None:
            stmt = stmt.where(Instruction.category == category)

        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def build_context(
        self,
        *,
        status: str = "published",
        category: Optional[str] = None,
    ) -> str:
        """
        Convenience wrapper that returns the instructions as a single string
        block ready to be appended to an LLM prompt.

        The XML-like tags are purely to delimit the section and make parsing
        simpler on the model side.

        Example output
        --------------
        <instructions>
          <instruction id="123" category="code_gen">
            Create SQL that sums revenue by month …
          </instruction>
          ...
        </instructions>
        """
        instructions = await self.load_instructions(status=status, category=category)
        if not instructions:
            return ""

        lines: list[str] = ["<instructions>"]
        for inst in instructions:
            lines.append(self._format_instruction(inst))
        lines.append("</instructions>")
        return "\n".join(lines)

    # --------------------------------------------------------------------- #
    # Private helpers                                                       #
    # --------------------------------------------------------------------- #
    @staticmethod
    def _format_instruction(instruction: Instruction) -> str:
        """
        Render a single instruction in a minimal, self-describing format.
        """
        return (
            f"  <instruction id=\"{instruction.id}\" "
            f"category=\"{instruction.category}\" "
            f"thumbs_up=\"{instruction.thumbs_up}\">\n"
            f"    {instruction.text.strip()}\n"
            f"  </instruction>"
        )

    async def get_instructions_context(self):
        text = await self.build_context()
        return text