"""
Tool utility functions for formatting and display.
"""
import json
from typing import List, Dict, Any
from app.schemas.ai.planner import ToolDescriptor


def format_tool_schemas(tool_catalog: List[ToolDescriptor]) -> str:
    """
    Format tool schemas for prompt inclusion.
    
    Args:
        tool_catalog: List of tool descriptors with schema information
        
    Returns:
        Formatted string with tool schemas for LLM consumption
    """
    schema_lines = []
    for tool in tool_catalog or []:
        if hasattr(tool, 'schema') and tool.schema:
            try:
                # Handle both dict schemas and method-based schemas
                schema_data = tool.schema
                if callable(schema_data):
                    # If it's a method, call it to get the actual schema
                    schema_data = schema_data()
                
                # Serialize the schema safely
                schema_json = json.dumps(schema_data, indent=2, default=str)
                schema_lines.append(f"{tool.name}: {schema_json}")
            except (TypeError, ValueError) as e:
                # If serialization fails, include basic info
                schema_lines.append(f"{tool.name}: <schema serialization failed: {str(e)}>")
    return "\n".join(schema_lines)


def format_tool_catalog_for_prompt(tool_catalog: List[ToolDescriptor]) -> str:
    """
    Format complete tool catalog for prompt inclusion with names, descriptions, and schemas.
    
    Args:
        tool_catalog: List of tool descriptors
        
    Returns:
        Formatted string with tool information for LLM consumption
    """
    lines = []
    for tool in tool_catalog or []:
        lines.append(f"Tool: {tool.name}")
        if tool.description:
            lines.append(f"  Description: {tool.description}")
        if hasattr(tool, 'schema') and tool.schema:
            try:
                # Handle both dict schemas and method-based schemas
                schema_data = tool.schema
                if callable(schema_data):
                    schema_data = schema_data()
                schema_json = json.dumps(schema_data, indent=4, default=str)
                lines.append(f"  Schema: {schema_json}")
            except (TypeError, ValueError) as e:
                lines.append(f"  Schema: <serialization failed: {str(e)}>")
        lines.append("")  # Empty line between tools
    return "\n".join(lines)


def get_tool_by_name(tool_catalog: List[ToolDescriptor], name: str) -> ToolDescriptor | None:
    """
    Get a specific tool from the catalog by name.
    
    Args:
        tool_catalog: List of tool descriptors
        name: Tool name to find
        
    Returns:
        Tool descriptor if found, None otherwise
    """
    for tool in tool_catalog or []:
        if tool.name == name:
            return tool
    return None
