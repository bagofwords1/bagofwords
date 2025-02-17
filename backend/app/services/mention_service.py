from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List, Optional
from app.models.mention import Mention
from app.schemas.mention_schema import MentionCreate, MentionUpdate
from app.models.user import User
from app.models.organization import Organization
from app.models.completion import Completion

class MentionService:
    
    async def create_completion_mentions(self, db: AsyncSession, completion: Completion) -> Mention:

        # todo - parse mention content to extract all ids and data

        mentions = completion.prompt["mentions"]
        db_mentions = []

        for memory_mention in mentions[0]['items']:
            m = MentionCreate(completion_id=completion.id,
                              report_id=completion.report_id,
                              type="MEMORY",
                              mention_content=memory_mention["title"],
                              object_id=memory_mention["id"])
            
            db_mention = await self.create_mention(db, m, completion)
            db_mentions.append(db_mention)

        for file_mention in mentions[1]['items']:
            m = MentionCreate(completion_id=completion.id,
                              report_id=completion.report_id,
                              type="FILE",
                              mention_content=file_mention["filename"],
                              object_id=file_mention["id"])
            
            db_mention = await self.create_mention(db, m, completion)
            db_mentions.append(db_mention)

        for data_source_mention in mentions[2]['items']:
            m = MentionCreate(completion_id=completion.id,
                              report_id=completion.report_id,
                              type="DATA_SOURCE",
                              mention_content=data_source_mention["name"],
                              object_id=data_source_mention["id"])
            
            db_mention = await self.create_mention(db, m, completion)
            db_mentions.append(db_mention)

        return db_mentions
    
    async def create_mention(self, db: AsyncSession, mention: MentionCreate, completion: Completion) -> Mention:
        db_mention = Mention(**mention.dict())
        db.add(db_mention)
        await db.commit()
        await db.refresh(db_mention)
        return db_mention