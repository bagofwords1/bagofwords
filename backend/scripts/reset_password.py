"""Admin break-glass: set a local password for an existing user.

For deployments with no SMTP and no SSO, where a user forgot their password and
the emailed forgot-password flow isn't available. Reuses the app's PasswordHelper
(argon2) so the stored hash matches what the login form verifies against.

Run inside the backend container (uses the same BOW_DATABASE_URL as the app):

    # generate a strong random password and print it once
    python scripts/reset_password.py user@example.com

    # or set a specific one
    python scripts/reset_password.py user@example.com 'the-new-password'

Then hand the printed password to the user over a trusted channel. Note: this
does NOT invalidate the user's existing JWT sessions (stateless, 7-day) — not a
concern here since a user who forgot their password has no active session.
"""
import sys
import asyncio

from sqlalchemy import select, func
from fastapi_users.password import PasswordHelper

from app.dependencies import async_session_maker
from app.models.user import User


async def main(email: str, password: str | None) -> int:
    ph = PasswordHelper()
    if not password:
        password = ph.generate()

    async with async_session_maker() as session:
        user = (await session.execute(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )).scalar_one_or_none()

        if user is None:
            print(f"No user found with email {email!r}")
            return 1
        if getattr(user, "is_service_account", False):
            print(f"{email!r} is a service account, not a human login")
            return 1

        user.hashed_password = ph.hash(password)
        user.is_active = True          # in case they were deactivated
        await session.commit()

    print("Password set for:", email)
    print("New password:     ", password)
    print("Share this over a trusted channel; it is not stored anywhere.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/reset_password.py <email> [new-password]")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
