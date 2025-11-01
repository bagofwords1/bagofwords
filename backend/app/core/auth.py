import uuid
from typing import Optional, Any, List, Callable

from sqlalchemy.ext.asyncio import AsyncSession

import httpx

from fastapi import Depends, Request, HTTPException
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users import exceptions
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy import select, and_
from httpx_oauth.oauth2 import BaseOAuth2

from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

from app.schemas.user_schema import UserCreate
from app.models.organization import Organization
from app.models.membership import Membership

from app.models.user import User
from app.dependencies import get_user_db
from app.models.oauth_account import OAuthAccount
from fastapi.responses import RedirectResponse

from app.settings.config import settings
from app.services.organization_service import OrganizationService
from app.schemas.organization_schema import OrganizationCreate
from app.core.telemetry import telemetry

SECRET = settings.bow_config.encryption_key


DEFAULT_ORG_NAME = "Main Org"
DEFAULT_ORG_DESCRIPTION = ""

class UserManager(BaseUserManager[User, str]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    def parse_id(self, value: Any) -> str:
        return str(value)

    async def on_after_login(
        self, 
        user: User, 
        request: Optional[Request] = None,
        json_body: Optional[dict] = None
    ) -> None:
        # Handle redirect for any OAuth/OIDC callback
        if request and request.url.path.endswith("/callback"):
            import json
            try:
                response_data = json.loads(json_body.body.decode()) if json_body else {}
            except Exception:
                response_data = {}
            token = response_data.get('access_token')
            if token:
                redirect_url = f"{settings.bow_config.base_url}/users/sign-in?access_token={token}&email={user.email}"
                raise HTTPException(status_code=303, headers={"Location": redirect_url})

    async def _attach_open_memberships(self, user: User, session: AsyncSession):
        stmt = select(Membership).where(
            and_(
                Membership.email == user.email,
                Membership.user_id.is_(None)
            )
        )
        open_memberships = (await session.execute(stmt)).scalars().all()
        
        if open_memberships:
            user.is_verified = True

        # Update each open membership with the new user
        for membership in open_memberships:
            membership.user_id = user.id
            membership.email = None  # Clear the email since we now have a user
            # Telemetry: invited user accepted invite and signed up
            try:
                await telemetry.capture(
                    "organization_member_joined_via_invite",
                    {
                        "organization_id": str(membership.organization_id),
                        "membership_id": str(membership.id),
                        "user_id": str(user.id),
                    },
                    user_id=str(user.id),
                    org_id=str(membership.organization_id),
                )
            except Exception:
                pass

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        print(f"User {user.id} has registered.")
        
        # Get open memberships and attach user
        async with self.user_db.session as session:
            await self._attach_open_memberships(user, session)
            
            if not settings.bow_config.features.verify_emails:
                user.is_verified = True
            
            await session.commit()
            # Auto-create organization for the first uninvited user
            await self._ensure_org_for_first_uninvited_user(session, user)

    async def oauth_callback(
        self: "UserManager[User, str]",
        oauth_name: str,
        access_token: str,
        account_id: str,
        account_email: str,
        expires_at: Optional[int] = None,
        refresh_token: Optional[str] = None,
        request: Optional[Request] = None,
        *args,
        **kwargs
    ) -> User:
        try:
            # First try to get user by OAuth account
            user = await self.get_by_oauth_account(oauth_name, account_id)
            return user
        except exceptions.UserNotExists:
            # If OAuth account doesn't exist, check if user exists by email
            try:
                user = await self.get_by_email(account_email)
                # User exists, let's link the OAuth account
                async with self.user_db.session as session:
                    oauth_account = OAuthAccount(
                        oauth_name=oauth_name,
                        access_token=access_token,
                        account_id=account_id,
                        account_email=account_email,
                        expires_at=expires_at,
                        refresh_token=refresh_token,
                        user_id=user.id
                    )
                    session.add(oauth_account)
                    await session.commit()
                return user
            except exceptions.UserNotExists:
                # User doesn't exist at all, create new user with OAuth
                # Enforce invite policy similar to regular registration
                async with self.user_db.session as session:
                    # If uninvited signups are disabled and not first user, require invite
                    user_count = (await session.execute(select(User))).scalars().all().__len__()
                    if user_count > 0 and not settings.bow_config.features.allow_uninvited_signups:
                        stmt = select(Membership).where(
                            and_(
                                Membership.email == account_email,
                                Membership.user_id.is_(None)
                            )
                        )
                        open_membership = (await session.execute(stmt)).scalar_one_or_none()
                        if not open_membership:
                            from fastapi import HTTPException
                            raise HTTPException(
                                status_code=403,
                                detail={
                                    "code": "invitation_required",
                                    "message": "New user registration is disabled. You must be invited to create an account.",
                                },
                            )
                # Fetch user info if needed (e.g., from Google)
                fetched_name = None
                if oauth_name == "google":
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            "https://www.googleapis.com/oauth2/v1/userinfo",
                            headers={"Authorization": f"Bearer {access_token}"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        fetched_name = user_info.get("name")
                if not fetched_name:
                    fetched_name = account_email.split("@")[0]

                # Create the new user
                async with self.user_db.session as session:
                    user = await self.user_db.create(
                        {
                            "email": account_email,
                            "name": fetched_name,
                            "hashed_password": self.password_helper.hash(self.password_helper.generate()),
                            "is_active": True,
                            "is_verified": True,
                            "is_superuser": False,
                        }
                    )
                    
                    # Attach any open memberships
                    await self._attach_open_memberships(user, session)
                    
                    oauth_account = OAuthAccount(
                        oauth_name=oauth_name,
                        access_token=access_token,
                        account_id=account_id,
                        account_email=account_email,
                        expires_at=expires_at,
                        refresh_token=refresh_token,
                        user_id=user.id
                    )
                    session.add(oauth_account)
                    await session.commit()
                    await session.refresh(user)
                    # Auto-create organization for the first uninvited user
                    await self._ensure_org_for_first_uninvited_user(session, user)
                return user

    async def _ensure_org_for_first_uninvited_user(self, session: AsyncSession, user: User) -> None:
        """Create an organization automatically if this is the first user without an invite.

        Conditions:
        - User has no memberships (not invited/attached)
        - Total users == 1
        - Total organizations == 0
        """
        # If user already has a membership, skip
        user_membership = (
            await session.execute(
                select(Membership).where(Membership.user_id == user.id)
            )
        ).scalars().first()
        if user_membership:
            return

        total_users = (await session.execute(select(User))).scalars().all().__len__()
        total_orgs = (await session.execute(select(Organization))).scalars().all().__len__()

        if total_users != 1 or total_orgs != 0:
            return

        org_name = DEFAULT_ORG_NAME
        description = DEFAULT_ORG_DESCRIPTION

        organization_service = OrganizationService()
        await organization_service.create_organization(
            session,
            OrganizationCreate(name=org_name, description=description),
            user,
        )

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        await self._send_reset_password_email(user, token, request)

    async def _send_reset_password_email(self, user: User, token: str, request: Optional[Request] = None):
        import asyncio
        
        base_url = settings.bow_config.base_url
            
        reset_url = f"{base_url}/users/reset-password?token={token}"
        
        message = MessageSchema(
            subject="Reset your password",
            recipients=[user.email],
            body=f"Hello {user.name},<br /><br />You have requested to reset your password for Bag of words. Click the link below to reset your password:<br /><br /> <a href='{reset_url}'>{reset_url}</a><br /><br />If you didn't request this, please ignore this email.<br /><br />Best regards,<br />Bag of words team",
            subtype="html"
        )
        fm = settings.email_client
        
        async def send_email():
            try:
                await fm.send_message(message)
            except Exception as e:
                print(f"Error sending reset password email: {e}")
        
        # Create task without awaiting it
        asyncio.create_task(send_email())

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        await self._send_verification_email(user, token, request)

    async def _send_verification_email(self, user: User, token: str, request: Optional[Request] = None):
        import asyncio
        
        base_url = settings.bow_config.base_url
            
        verification_url = f"{base_url}/users/verify?token={token}"
        
        message = MessageSchema(
            subject="Verify your email",
            recipients=[user.email],
            body=f"Welcome to Bag of words! You are almost ready to start using our platform. Click to verify your email: <br /> {verification_url}",
            subtype="html"
        )
        fm = settings.email_client
        
        async def send_email():
            try:
                await fm.send_message(message)
            except Exception as e:
                print(f"Error sending verification email: {e}")
        
        # Create task without awaiting it
        asyncio.create_task(send_email())

    async def create(
        self,
        user_create: UserCreate,
        safe: bool = False,
        request: Optional[Request] = None,
    ) -> User:
        # Your pre-registration logic here
        # For example:
        await self._validate_user_creation(user_create)
        
        # Call parent create method
        user = await super().create(user_create, safe, request)
        
        return user

    async def _validate_user_creation(self, user_create: UserCreate) -> None:
        async with self.user_db.session as session:
            # Get total user count
            user_count = (await session.execute(select(User))).scalars().all().__len__()
            
            # If not first user and uninvited signups disabled, check for open membership
            if user_count > 0 and not settings.bow_config.features.allow_uninvited_signups:
                # Check if user has an open membership invitation
                stmt = select(Membership).where(
                    and_(
                        Membership.email == user_create.email,
                        Membership.user_id.is_(None)
                    )
                )
                open_membership = (await session.execute(stmt)).scalar_one_or_none()
                
                if not open_membership:
                    raise HTTPException(
                        status_code=400,
                        detail="New user registration is disabled. You must be invited to create an account."
                    )

        
        return
            
            # Add any other pre-registration checks
            # If any check fails, raise an HTTPException

async def get_user_manager(user_db: SQLAlchemyUserDatabase = Depends(get_user_db)):
    yield UserManager(user_db)


bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=3600)


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)


def create_fastapi_users(
    get_user_manager: Callable,
    auth_backend: AuthenticationBackend,
    oauth_providers: List[BaseOAuth2] = None
) -> FastAPIUsers:
    if oauth_providers is None:
        oauth_providers = []
    return FastAPIUsers(get_user_manager, [auth_backend])
# verified user only!
fapi = create_fastapi_users(get_user_manager, auth_backend)
current_user = fapi.current_user(active=True)