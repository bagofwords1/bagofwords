from typing import Dict, Type
from .base_adapter import PlatformAdapter
from .slack_adapter import SlackAdapter
from .teams_adapter import TeamsAdapter
from app.models.external_platform import ExternalPlatform

class PlatformAdapterFactory:
    """Factory for creating platform adapters"""

    _adapters: Dict[str, Type[PlatformAdapter]] = {
        "slack": SlackAdapter,
        "teams": TeamsAdapter,
        # "email": EmailAdapter,  # Future
    }
    
    @classmethod
    def create_adapter(cls, platform: ExternalPlatform) -> PlatformAdapter:
        """Create a platform adapter for the given platform"""
        
        adapter_class = cls._adapters.get(platform.platform_type)
        if not adapter_class:
            raise ValueError(f"Unsupported platform type: {platform.platform_type}")
        
        return adapter_class(platform)
    
    @classmethod
    def register_adapter(cls, platform_type: str, adapter_class: Type[PlatformAdapter]):
        """Register a new platform adapter"""
        cls._adapters[platform_type] = adapter_class
