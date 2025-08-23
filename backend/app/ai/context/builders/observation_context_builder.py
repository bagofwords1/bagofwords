"""
Observation Context Builder - Manages tool execution results and output context.
"""
import json
from typing import Dict, Any, List, Optional
from datetime import datetime


class ObservationContextBuilder:
    """
    Builds and manages observation context from tool executions.
    
    Tracks tool execution results, widget updates, step data, and other
    useful outputs that should be included in agent context.
    """
    
    def __init__(self):
        # Tool execution observations indexed by execution order
        self.tool_observations: List[Dict[str, Any]] = []
        self.execution_count: int = 0
        
        # Widget and step updates
        self.widget_updates: List[Dict[str, Any]] = []
        self.step_updates: List[Dict[str, Any]] = []
        
        # Other useful outputs (files created, data processed, etc.)
        self.artifacts: Dict[str, Any] = {}
    
    def add_tool_observation(self, tool_name: str, tool_input: Dict[str, Any], observation: Dict[str, Any]):
        """
        Add an observation from a tool execution.
        
        Args:
            tool_name: Name of the tool that was executed
            tool_input: Input parameters passed to the tool
            observation: Tool execution result with summary and artifacts
        """
        self.execution_count += 1
        
        tool_observation = {
            "execution_number": self.execution_count,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "timestamp": datetime.utcnow().isoformat(),
            "observation": observation
        }
        
        self.tool_observations.append(tool_observation)
        
        # Extract useful artifacts if present
        if observation and "artifacts" in observation:
            artifacts = observation["artifacts"]
            if artifacts:
                self.artifacts[f"{tool_name}_{self.execution_count}"] = artifacts
    
    def add_widget_update(self, widget_id: int, widget_data: Dict[str, Any]):
        """
        Track widget creation or updates.
        
        Args:
            widget_id: ID of the widget that was created/updated
            widget_data: Widget data including title, type, etc.
        """
        self.widget_updates.append({
            "widget_id": widget_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": widget_data
        })
    
    def add_step_update(self, step_id: int, step_data: Dict[str, Any]):
        """
        Track step creation or updates.
        
        Args:
            step_id: ID of the step that was created/updated  
            step_data: Step data including status, results, etc.
        """
        self.step_updates.append({
            "step_id": step_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": step_data
        })
    
    def get_execution_count(self) -> int:
        """Get the current number of tool executions."""
        return self.execution_count
    
    def get_tools_used(self) -> List[str]:
        """Get list of tools that have been executed."""
        return [obs["tool_name"] for obs in self.tool_observations]
    
    def has_observations(self) -> bool:
        """Check if any tool executions have been recorded."""
        return len(self.tool_observations) > 0
    
    def get_latest_observation(self) -> Optional[Dict[str, Any]]:
        """Get the most recent tool observation."""
        if self.tool_observations:
            return self.tool_observations[-1]
        return None
    
    def get_observation_summary(self, tool_name: str) -> Optional[str]:
        """Get the summary for the latest execution of a specific tool."""
        for obs in reversed(self.tool_observations):
            if obs["tool_name"] == tool_name and obs["observation"]:
                return obs["observation"].get("summary")
        return None
    
    def build_context(self, format_for_prompt: bool = True, max_observations: int = 5) -> str:
        """
        Build observation context string.
        
        Args:
            format_for_prompt: If True, format for LLM prompt inclusion.
                             If False, format for debugging/inspection.
            max_observations: Maximum number of recent observations to include
                             
        Returns:
            Formatted observation context string
        """
        if not self.has_observations():
            return ""
        
        if format_for_prompt:
            return self._build_prompt_context(max_observations)
        else:
            return self._build_debug_context()
    
    def _build_prompt_context(self, max_observations: int) -> str:
        """Build context formatted for LLM prompt inclusion."""
        lines = ["<recent_tool_executions>"]
        
        # Include recent tool observations
        recent_observations = self.tool_observations[-max_observations:]
        for obs in recent_observations:
            tool_name = obs["tool_name"]
            observation = obs["observation"]
            
            if observation and "summary" in observation:
                lines.append(f"  <tool_execution tool=\"{tool_name}\">")
                lines.append(f"    {observation['summary']}")
                lines.append(f"  </tool_execution>")
        
        # Include widget updates if any
        if self.widget_updates:
            lines.append(f"  <widgets_created_or_updated count=\"{len(self.widget_updates)}\">")
            for widget_update in self.widget_updates[-3:]:  # Last 3 widgets
                widget_data = widget_update["data"]
                lines.append(f"    Widget ID {widget_update['widget_id']}: {widget_data.get('title', 'Untitled')}")
            lines.append(f"  </widgets_created_or_updated>")
        
        lines.append("</recent_tool_executions>")
        return "\n".join(lines)
    
    def _build_debug_context(self) -> str:
        """Build detailed context for debugging/inspection."""
        context = {
            "execution_count": self.execution_count,
            "tool_observations": self.tool_observations,
            "widget_updates": self.widget_updates,
            "step_updates": self.step_updates,
            "artifacts": self.artifacts
        }
        return json.dumps(context, indent=2)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert observation context to dictionary for serialization.
        
        Returns:
            Dictionary representation of observation context
        """
        return {
            "execution_count": self.execution_count,
            "tool_observations": self.tool_observations,
            "widget_updates": self.widget_updates,
            "step_updates": self.step_updates,
            "artifacts": self.artifacts
        }
    
    def clear(self):
        """Clear all observation context."""
        self.tool_observations.clear()
        self.widget_updates.clear()
        self.step_updates.clear()
        self.artifacts.clear()
        self.execution_count = 0