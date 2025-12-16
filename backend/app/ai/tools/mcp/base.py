"""Base class for MCP tools."""

from abc import ABC, abstractmethod
from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.organization import Organization


class MCPTool(ABC):
    """Base class for MCP tools.
    
    MCP tools are exposed via the /mcp API to external LLMs like Claude and Cursor.
    They receive authenticated user/organization context and can call internal services.
    """
    
    name: str
    description: str
    
    @property
    @abstractmethod
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema for tool input arguments."""
        pass
    
    @abstractmethod
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute the tool with authenticated context.
        
        Args:
            args: Tool arguments from the MCP client
            db: Database session
            user: Authenticated user (from API key)
            organization: User's organization (from API key)
            
        Returns:
            Tool result as a dictionary
        """
        pass
    
    def to_schema(self) -> Dict[str, Any]:
        """Convert tool to MCP schema format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
