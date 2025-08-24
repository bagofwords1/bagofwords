from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.context_snapshot import ContextSnapshot


class ContextSnapshotService:
    async def save_snapshot(
        self,
        db: AsyncSession,
        *,
        agent_execution_id: str,
        kind: str,
        context_view_json: Dict[str, Any],
        prompt_text: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        hash: Optional[str] = None,
    ) -> ContextSnapshot:
        snap = ContextSnapshot(
            agent_execution_id=agent_execution_id,
            kind=kind,
            context_view_json=context_view_json,
            prompt_text=prompt_text,
            prompt_tokens=str(prompt_tokens) if prompt_tokens is not None else None,
            hash=hash,
        )
        db.add(snap)
        await db.commit()
        await db.refresh(snap)
        return snap


