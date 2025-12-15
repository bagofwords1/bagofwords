import secrets
import hashlib
from typing import List, Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.api_key import ApiKey
from app.models.user import User
from app.schemas.api_key_schema import ApiKeyCreate, ApiKeyResponse, ApiKeyCreated


class ApiKeyService:
    
    def _generate_api_key(self) -> tuple[str, str, str]:
        """Generate a new API key.
        
        Returns:
            tuple: (full_key, key_hash, key_prefix)
        """
        # Generate 32 random bytes and encode as base64-like string
        random_bytes = secrets.token_urlsafe(32)
        full_key = f"bow_{random_bytes}"
        
        # Hash the key for storage
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()
        
        # Store prefix for identification (e.g., "bow_abc123...")
        key_prefix = full_key[:12]
        
        return full_key, key_hash, key_prefix

    def _hash_api_key(self, key: str) -> str:
        """Hash an API key for comparison."""
        return hashlib.sha256(key.encode()).hexdigest()

    async def create_api_key(
        self,
        db: AsyncSession,
        data: ApiKeyCreate,
        user: User,
    ) -> ApiKeyCreated:
        """Create a new API key for a user.
        
        The full key is only returned once upon creation. Store it securely.
        """
        full_key, key_hash, key_prefix = self._generate_api_key()
        
        api_key = ApiKey(
            user_id=user.id,
            name=data.name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            expires_at=data.expires_at,
        )
        
        db.add(api_key)
        await db.commit()
        await db.refresh(api_key)
        
        return ApiKeyCreated(
            id=api_key.id,
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            created_at=api_key.created_at,
            expires_at=api_key.expires_at,
            last_used_at=api_key.last_used_at,
            key=full_key,
        )

    async def list_api_keys(
        self,
        db: AsyncSession,
        user: User,
    ) -> List[ApiKeyResponse]:
        """List all API keys for a user."""
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id)
            .where(ApiKey.deleted_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
        api_keys = result.scalars().all()
        return [ApiKeyResponse.model_validate(key) for key in api_keys]

    async def delete_api_key(
        self,
        db: AsyncSession,
        key_id: str,
        user: User,
    ) -> bool:
        """Revoke an API key (soft delete).
        
        Returns True if deleted, raises HTTPException if not found.
        """
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.id == key_id)
            .where(ApiKey.user_id == user.id)
            .where(ApiKey.deleted_at.is_(None))
        )
        api_key = result.scalar_one_or_none()
        
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        
        # Soft delete
        api_key.deleted_at = datetime.utcnow()
        await db.commit()
        
        return True

    async def get_user_by_api_key(
        self,
        db: AsyncSession,
        api_key: str,
    ) -> Optional[User]:
        """Validate an API key and return the associated user.
        
        Returns None if the key is invalid, expired, or deleted.
        """
        if not api_key or not api_key.startswith("bow_"):
            return None
        
        # Hash the provided key
        key_hash = self._hash_api_key(api_key)
        
        # Look up the API key
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.key_hash == key_hash)
            .where(ApiKey.deleted_at.is_(None))
        )
        api_key_obj = result.scalar_one_or_none()
        
        if not api_key_obj:
            return None
        
        # Check expiration
        if api_key_obj.expires_at and api_key_obj.expires_at < datetime.utcnow():
            return None
        
        # Update last_used_at
        api_key_obj.last_used_at = datetime.utcnow()
        await db.commit()
        
        # Get the user
        user_result = await db.execute(
            select(User).where(User.id == api_key_obj.user_id)
        )
        return user_result.scalar_one_or_none()


