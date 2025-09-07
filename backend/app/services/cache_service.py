"""
Cache service for improved performance
"""
import json
import asyncio
from typing import Any, Optional, Dict
from datetime import datetime, timedelta
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class InMemoryCache:
    """Simple in-memory cache with TTL support"""
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cleanup_task = None
        self._initialized = False
    
    def _start_cleanup_task(self):
        """Start background task to clean expired entries"""
        if self._cleanup_task is None and not self._initialized:
            try:
                self._cleanup_task = asyncio.create_task(self._cleanup_expired())
                self._initialized = True
            except RuntimeError:
                # No event loop running, will start later
                pass
    
    async def _cleanup_expired(self):
        """Remove expired cache entries"""
        while True:
            try:
                now = datetime.utcnow()
                expired_keys = []
                
                for key, entry in self._cache.items():
                    if entry['expires_at'] < now:
                        expired_keys.append(key)
                
                for key in expired_keys:
                    del self._cache[key]
                
                if expired_keys:
                    logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")
                
                # Run cleanup every 5 minutes
                await asyncio.sleep(300)
                
            except Exception as e:
                logger.error(f"Error in cache cleanup: {e}")
                await asyncio.sleep(60)  # Retry after 1 minute on error
    
    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        try:
            entry = self._cache.get(key)
            if entry is None:
                return None
            
            if entry['expires_at'] < datetime.utcnow():
                del self._cache[key]
                return None
            
            return entry['value']
        except Exception as e:
            logger.error(f"Error getting cache key {key}: {e}")
            return None
    
    async def set(self, key: str, value: Any, ttl_seconds: int = 300) -> bool:
        """Set value in cache with TTL"""
        try:
            expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
            self._cache[key] = {
                'value': value,
                'expires_at': expires_at,
                'created_at': datetime.utcnow()
            }
            return True
        except Exception as e:
            logger.error(f"Error setting cache key {key}: {e}")
            return False
    
    async def delete(self, key: str) -> bool:
        """Delete key from cache"""
        try:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting cache key {key}: {e}")
            return False
    
    async def clear(self) -> bool:
        """Clear all cache entries"""
        try:
            self._cache.clear()
            return True
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        now = datetime.utcnow()
        total_entries = len(self._cache)
        expired_entries = sum(1 for entry in self._cache.values() if entry['expires_at'] < now)
        
        return {
            'total_entries': total_entries,
            'active_entries': total_entries - expired_entries,
            'expired_entries': expired_entries,
            'memory_usage_mb': len(str(self._cache)) / (1024 * 1024)  # Rough estimate
        }

# Global cache instance
cache = InMemoryCache()

class CacheService:
    """Service for caching frequently accessed data"""
    
    @staticmethod
    async def get_user_permissions(user_id: str, organization_id: str) -> Optional[Dict[str, Any]]:
        """Get cached user permissions"""
        key = f"user_permissions:{user_id}:{organization_id}"
        return await cache.get(key)
    
    @staticmethod
    async def set_user_permissions(user_id: str, organization_id: str, permissions: Dict[str, Any], ttl: int = 900) -> bool:
        """Cache user permissions for 15 minutes by default"""
        key = f"user_permissions:{user_id}:{organization_id}"
        return await cache.set(key, permissions, ttl)
    
    @staticmethod
    async def get_data_source_schema(data_source_id: str) -> Optional[Dict[str, Any]]:
        """Get cached data source schema"""
        key = f"data_source_schema:{data_source_id}"
        return await cache.get(key)
    
    @staticmethod
    async def set_data_source_schema(data_source_id: str, schema: Dict[str, Any], ttl: int = 3600) -> bool:
        """Cache data source schema for 1 hour by default"""
        key = f"data_source_schema:{data_source_id}"
        return await cache.set(key, schema, ttl)
    
    @staticmethod
    async def get_organization_settings(organization_id: str) -> Optional[Dict[str, Any]]:
        """Get cached organization settings"""
        key = f"org_settings:{organization_id}"
        return await cache.get(key)
    
    @staticmethod
    async def set_organization_settings(organization_id: str, settings: Dict[str, Any], ttl: int = 1800) -> bool:
        """Cache organization settings for 30 minutes by default"""
        key = f"org_settings:{organization_id}"
        return await cache.set(key, settings, ttl)
    
    @staticmethod
    async def invalidate_user_cache(user_id: str):
        """Invalidate all cache entries for a user"""
        # This is a simple implementation - in production you might want a more sophisticated approach
        keys_to_delete = []
        for key in cache._cache.keys():
            if f":{user_id}:" in key or key.endswith(f":{user_id}"):
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            await cache.delete(key)
    
    @staticmethod
    async def invalidate_organization_cache(organization_id: str):
        """Invalidate all cache entries for an organization"""
        keys_to_delete = []
        for key in cache._cache.keys():
            if f":{organization_id}" in key or key.endswith(f":{organization_id}"):
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            await cache.delete(key)
    
    @staticmethod
    async def get_cache_stats() -> Dict[str, Any]:
        """Get cache statistics"""
        return cache.get_stats()