import os
import pytest
import uuid
from sqlalchemy import create_engine, text
from tests.utils.user_creds import main_user


def _pending_invite_token(email: str):
    """Read the pending invite token for an email straight from the test DB.

    Registration now gates on the invite token (the real sign-up form carries it
    from the invite link). Tests invite-then-register by email, so we transparently
    look up and supply the token here -- keeping every existing invite+register
    test working without per-test changes. Returns None when there's no pending
    invite (first user, open signups, domain invites, etc.).
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        from app.settings.config import settings as _s
        url = getattr(_s, "TEST_DATABASE_URL", None)
    if not url:
        return None
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    try:
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT invite_token FROM memberships "
                        "WHERE email = :e AND user_id IS NULL "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"e": email},
                ).fetchone()
        finally:
            engine.dispose()
        return row[0] if row else None
    except Exception:
        return None


@pytest.fixture
def create_user(test_client):
    def _create_user(name=None, email=main_user["email"], password=main_user["password"], invite_token=None):
        # Generate unique name if not provided to avoid UNIQUE constraint failures
        if name is None:
            name = f"testuser_{uuid.uuid4().hex[:8]}"
        # Auto-supply the invite token when one exists (mirrors clicking the link).
        if invite_token is None:
            invite_token = _pending_invite_token(email)
        body = {"name": name, "email": email, "password": password}
        if invite_token:
            body["invite_token"] = invite_token
        response = test_client.post("/api/auth/register", json=body)
        assert response.status_code == 201, response.json()
        return {"name": name, "email": email, "password": password}
    return _create_user
