from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import current_session_user, get_user_manager
from app.models.user import User

router = APIRouter(tags=["invites"])


class InviteAccept(BaseModel):
    token: str = Field(..., min_length=1, max_length=64)


@router.post("/invites/accept")
async def accept_invite(
    payload: InviteAccept,
    user: User = Depends(current_session_user),
    user_manager=Depends(get_user_manager),
):
    """Accept an invite link as the signed-in user.

    Registration consumes the link for new users; this is the same step for
    someone who already has an account. It runs above the organization
    membership check because it is what establishes the membership.
    """
    membership = await user_manager.accept_invite(user, payload.token)
    return {"organization_id": str(membership.organization_id) if membership else None}
