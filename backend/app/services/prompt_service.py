from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List, Optional
from app.models.prompt import Prompt
from app.schemas.prompt_schema import PromptCreate, PromptUpdate
from app.models.user import User
from app.models.organization import Organization

class PromptService:
    
    async def create_prompt(self, db: AsyncSession, prompt: PromptCreate, current_user: User, organization: Organization) -> Prompt:
        db_prompt = Prompt(**prompt.dict())
        db_prompt.user_id = current_user.id
        db_prompt.organization_id = organization.id
        db.add(db_prompt)
        await db.commit()
        await db.refresh(db_prompt)
        return db_prompt

    async def get_prompts(self, db: AsyncSession, current_user: User, organization: Organization, skip: int = 0, limit: int = 100) -> List[Prompt]:
        result = await db.execute(select(Prompt).offset(skip).limit(limit))
        return result.scalars().all()

    async def get_prompt(self, db: AsyncSession, prompt_id: int) -> Optional[Prompt]:
        result = await db.execute(select(Prompt).filter(Prompt.id == prompt_id))
        return result.scalar_one_or_none()

    async def update_prompt(self, db: AsyncSession, prompt_id: int, prompt: PromptUpdate) -> Optional[Prompt]:
        result = await db.execute(select(Prompt).filter(Prompt.id == prompt_id))
        db_prompt = result.scalar_one_or_none()
        if db_prompt:
            for key, value in prompt.dict(exclude_unset=True).items():
                setattr(db_prompt, key, value)
            await db.commit()
            await db.refresh(db_prompt)
        return db_prompt

    async def delete_prompt(self, db: AsyncSession, prompt_id: int) -> Optional[Prompt]:
        result = await db.execute(select(Prompt).filter(Prompt.id == prompt_id))
        db_prompt = result.scalar_one_or_none()
        if db_prompt:
            await db.delete(db_prompt)
            await db.commit()
        return db_prompt