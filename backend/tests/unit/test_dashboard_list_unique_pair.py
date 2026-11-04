"""dashboard_list treats only its own unique pairs as "already there" (a
repeated star, a concurrent first visit); any other integrity failure must
propagate. The failures are produced on the test database, so the messages
are each dialect's real ones."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.dependencies import async_session_maker
from app.models.artifact_list_seen import ArtifactListSeen
from app.models.artifact_star import ArtifactStar
from app.services.dashboard_list import _is_unique_pair

pytestmark = pytest.mark.db  # opens real sessions -- see tests/unit/conftest.py

STAR = ("uq_artifact_stars_artifact_user", "artifact_stars.artifact_id, artifact_stars.user_id")
SEEN = ("uq_artifact_list_seen_user_org", "artifact_list_seen.user_id, artifact_list_seen.organization_id")


async def _integrity_error(row) -> IntegrityError:
    async with async_session_maker() as db:
        db.add(row)
        with pytest.raises(IntegrityError) as caught:
            await db.flush()
        await db.rollback()
    return caught.value


@pytest.mark.asyncio
async def test_a_not_null_failure_on_a_star_is_not_a_unique_pair():
    exc = await _integrity_error(ArtifactStar(artifact_id=None, user_id=None))
    assert _is_unique_pair(exc, *STAR) is False
    assert _is_unique_pair(exc, *SEEN) is False


@pytest.mark.asyncio
async def test_a_not_null_failure_on_a_watermark_is_not_a_unique_pair():
    exc = await _integrity_error(ArtifactListSeen(user_id=None, organization_id=None, shared_seen_at=None))
    assert _is_unique_pair(exc, *SEEN) is False
    assert _is_unique_pair(exc, *STAR) is False
