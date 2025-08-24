from typing import Dict, Callable, List, Optional, Set
from dataclasses import dataclass

from app.ai.tools.implementations import (
    AnswerQuestionTool,
    ClarifyTool,
    CreateDataModelTool,
    CreateAndExecuteCodeTool,
    ModifyDataModelTool,
    ReadFileTool,
)
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.base import Tool


@dataclass
class ToolCatalogFilter:
    """Enhanced filter for tool catalog queries."""
    plan_type: Optional[str] = None  # "research", "action", or None for all
    organization: Optional[str] = None
    required_permissions: Optional[Set[str]] = None
    tags: Optional[Set[str]] = None
    max_research_steps: int = 3  # Prevent infinite research loops


class ToolRegistry:
    """Enhanced metadata-driven tool registry with research flow support."""

    def __init__(self) -> None:
        self._factories: Dict[str, Callable[[], Tool]] = {}
        self._metadata_cache: Dict[str, ToolMetadata] = {}
        
        # Pre-register common tools
        self.register(AnswerQuestionTool)
        self.register(ClarifyTool)
        self.register(CreateDataModelTool)
        self.register(CreateAndExecuteCodeTool)
        self.register(ModifyDataModelTool)
        self.register(ReadFileTool)

    def register(self, tool_class: type[Tool]) -> None:
        """Register a tool class with metadata extraction."""
        temp_instance = tool_class()
        metadata = temp_instance.metadata
        
        self._factories[metadata.name] = tool_class
        self._metadata_cache[metadata.name] = metadata

    def get(self, name: str) -> Optional[Tool]:
        """Get tool instance by name."""
        factory = self._factories.get(name)
        return factory() if factory else None

    def get_metadata(self, name: str) -> Optional[ToolMetadata]:
        """Get tool metadata by name."""
        return self._metadata_cache.get(name)

    def list_tools(self, filter_obj: Optional[ToolCatalogFilter] = None) -> List[ToolMetadata]:
        """Get filtered list of tool metadata."""
        if filter_obj is None:
            return list(self._metadata_cache.values())
        
        filtered_tools = []
        for metadata in self._metadata_cache.values():
            if self._matches_filter(metadata, filter_obj):
                filtered_tools.append(metadata)
        
        return filtered_tools

    def get_catalog_for_plan_type(self, plan_type: str, organization: Optional[str] = None) -> List[Dict]:
        """Get tool catalog filtered by plan type with enhanced metadata."""
        filter_obj = ToolCatalogFilter(plan_type=plan_type, organization=organization)
        metadata_list = self.list_tools(filter_obj)
        
        catalog = []
        for metadata in metadata_list:
            catalog.append({
                "name": metadata.name,
                "description": metadata.description,
                "schema": metadata.input_schema,
                "category": metadata.category,
                "version": metadata.version,
                "research_accessible": metadata.category in ["research", "both"],
                "max_retries": metadata.max_retries,
                "timeout_seconds": metadata.timeout_seconds,
                "tags": metadata.tags,
            })
        
        return catalog

    def get_catalog(self, organization: Optional[str] = None, plan_type: str = "action") -> List[Dict]:
        """Legacy method for backward compatibility."""
        return self.get_catalog_for_plan_type(plan_type, organization)

    def get_research_tools_summary(self) -> Dict[str, str]:
        """Get summary of available research tools for prompt context."""
        research_filter = ToolCatalogFilter(plan_type="research")
        research_tools = self.list_tools(research_filter)
        
        return {
            tool.name: tool.description 
            for tool in research_tools
        }

    def get_action_tools_summary(self) -> Dict[str, str]:
        """Get summary of available action tools for prompt context."""
        action_filter = ToolCatalogFilter(plan_type="action")
        action_tools = self.list_tools(action_filter)
        
        return {
            tool.name: tool.description 
            for tool in action_tools
        }

    def _matches_filter(self, metadata: ToolMetadata, filter_obj: ToolCatalogFilter) -> bool:
        """Enhanced filter matching with research loop prevention."""
        
        # Plan type filtering
        if filter_obj.plan_type:
            if filter_obj.plan_type == "research" and metadata.category not in ["research", "both"]:
                return False
            elif filter_obj.plan_type == "action" and metadata.category not in ["action", "both"]:
                return False
        
        # Organization filtering  
        if filter_obj.organization and metadata.enabled_for_orgs is not None:
            if filter_obj.organization not in metadata.enabled_for_orgs:
                return False
        
        # Permission filtering
        if filter_obj.required_permissions:
            if not filter_obj.required_permissions.issubset(set(metadata.required_permissions)):
                return False
        
        # Tag filtering
        if filter_obj.tags:
            if not filter_obj.tags.intersection(set(metadata.tags)):
                return False
        
        return True