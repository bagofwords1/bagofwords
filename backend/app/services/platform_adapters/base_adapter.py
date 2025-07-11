from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping

class PlatformAdapter(ABC):
    """Base class for platform adapters"""
    
    def __init__(self, platform: ExternalPlatform):
        self.platform = platform
        self.config = platform.platform_config
        self.credentials = platform.decrypt_credentials()
    
    @abstractmethod
    async def process_incoming_message(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process incoming message from platform"""
        pass
    
    @abstractmethod
    async def send_response(self, message_data: Dict[str, Any]) -> bool:
        """Send response back to platform"""
        pass
    
    @abstractmethod
    async def get_user_info(self, external_user_id: str) -> Dict[str, Any]:
        """Get user information from platform"""
        pass
    
    @abstractmethod
    async def verify_webhook_signature(self, request_body: bytes, signature: str, timestamp: str) -> bool:
        """Verify webhook signature"""
        pass
    
    @abstractmethod
    async def send_verification_message(self, channel_id: str, email: str, token: str) -> bool:
        """Send verification message to user"""
        pass
