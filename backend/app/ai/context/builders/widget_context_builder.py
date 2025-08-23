"""
Widget Context Builder - Ports proven logic from agent._build_observation_data()
"""
import json
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.widget import Widget
from app.models.step import Step

from app.settings.logging_config import get_logger



class WidgetContextBuilder:
    """
    Builds widget and observation context for agent execution.
    
    Ports the proven logic from agent._get_report_widgets_and_steps() and
    agent._build_observation_data() with widget associations and step data.
    """
    
    def __init__(self, db: AsyncSession, report):
        self.db = db
        self.report = report
    
    async def build_context(
        self,
        max_widgets: int = 5,
        status_filter: Optional[List[str]] = None,
        include_data_preview: bool = True
    ) -> str:
        """
        Build comprehensive widget context.
        
        Ports proven logic from agent._get_report_widgets_and_steps() and
        agent._build_observation_data().
        
        Args:
            max_widgets: Maximum number of widgets to include
            status_filter: Filter by widget status (if applicable)
            include_data_preview: Whether to include data previews
        
        Returns:
            Formatted widget context string with observation data
        """
        context = []
        
        # Get all widgets and their latest steps
        widgets_and_steps = await self._get_report_widgets_and_steps(self.report.id)
        
        # Apply max_widgets limit
        if len(widgets_and_steps) > max_widgets:
            widgets_and_steps = widgets_and_steps[-max_widgets:]  # Get latest widgets
        
        # Process each widget and step
        for widget, latest_step in widgets_and_steps:
            observation_data = await self._build_observation_data(
                widget, 
                latest_step,
                include_data_preview=include_data_preview
            )
            
            # Format observation data for context
            widget_context = self._format_widget_context(observation_data)
            context.append(widget_context)
        
        return "\n\n".join(context)
    
    async def _get_report_widgets_and_steps(self, report_id: int) -> List[tuple]:
        """
        Get all widgets and their latest steps for a report.
        
        Exact port from agent._get_report_widgets_and_steps() - proven logic.
        """
        result = []
        
        # Get all widgets for this report
        widgets = await self.db.execute(
            select(Widget).where(Widget.report_id == report_id)
        )
        widgets = widgets.scalars().all()
        
        for widget in widgets:
            # Safely check the widget type using getattr
            widget_type = getattr(widget, 'type', None)
            if widget_type == 'text':
                continue  # Skip text widgets created by the designer
            
            # Get the latest step for each data widget
            latest_step = await self.db.execute(
                select(Step)
                .filter(Step.widget_id == widget.id)
                .order_by(Step.created_at.desc())
                .limit(1)
            )
            latest_step = latest_step.scalar_one_or_none()
            
            # Only add widgets that have an associated step
            if latest_step:
                result.append((widget, latest_step))
        
        return result
    
    async def _build_observation_data(
        self, 
        widget, 
        step, 
        include_data_preview: bool = True
    ) -> Dict[str, Any]:
        """
        Build structured observation data from widget and step results.
        
        Exact port from agent._build_observation_data() - proven logic.
        """
        if not widget or not step:
            logger.warning("Cannot build observation data: widget or step is None")
            return {
                "widget_title": "N/A",
                "widget_type": "unknown",
                "data_preview": "No data available",
                "stats": {}
            }
        
        # For now, assume we can show data (organization settings check can be added later)
        allow_llm_see_data = True
        
        observation_data = {
            "widget_id": str(widget.id) if widget else "N/A",
            "widget_title": widget.title if widget else "N/A",
            "widget_type": "unknown",
            "step_id": str(step.id) if step else "N/A",
            "step_title": step.title if step else "N/A",
            "data_model": None,
            "stats": {},
            "column_names": [],
            "row_count": 0,
            "data": [],
            "data_preview": "No data available"
        }
        
        if step:
            # Safely get data model type and structure
            if step.data_model and isinstance(step.data_model, dict):
                observation_data["widget_type"] = step.data_model.get("type", "unknown")
                observation_data["data_model"] = step.data_model
            
            # Always include metadata about the data if available
            if step.data and isinstance(step.data, dict):
                if "info" in step.data:
                    observation_data["stats"] = step.data["info"]
                
                if "columns" in step.data and isinstance(step.data["columns"], list):
                    observation_data["column_names"] = [
                        col.get("field", "?") for col in step.data["columns"]
                    ]
                
                if "rows" in step.data and isinstance(step.data["rows"], list):
                    rows = step.data["rows"]
                    observation_data["row_count"] = len(rows)
                    observation_data["data"] = rows
                    
                    # Only include formatted preview if allowed and requested
                    if (include_data_preview and 
                        allow_llm_see_data and 
                        rows and 
                        observation_data["column_names"]):
                        try:
                            # Create data preview with limited rows
                            columns = observation_data["column_names"]
                            preview_rows = rows[:5]  # First 5 rows
                            
                            # Format preview as table string
                            preview_lines = []
                            header = " | ".join(columns)
                            separator = "-" * len(header)
                            preview_lines.append(header)
                            preview_lines.append(separator)
                            
                            for row_dict in preview_rows:
                                row_values = [
                                    str(row_dict.get(col, "N/A")) for col in columns
                                ]
                                preview_lines.append(" | ".join(row_values))
                            
                            observation_data["data_preview"] = "\n".join(preview_lines)
                        except Exception as e:
                            logger.error(f"Error building data preview: {e}")
                            observation_data["data_preview"] = "Error formatting preview."
        else:
            logger.warning(
                f"Building observation data for widget {widget.id if widget else 'N/A'} "
                "without a step."
            )
        
        return observation_data
    
    def _format_widget_context(self, observation_data: Dict[str, Any]) -> str:
        """Format observation data into readable context string."""
        parts = [
            f"Widget: {observation_data['widget_title']} (ID: {observation_data['widget_id']})",
            f"Type: {observation_data['widget_type']}",
            f"Step: {observation_data['step_title']} (ID: {observation_data['step_id']})",
        ]
        
        # Add data model if available
        if observation_data['data_model']:
            parts.append(f"Data Model: {json.dumps(observation_data['data_model'], indent=2)}")
        
        # Add stats if available
        if observation_data['stats']:
            parts.append(f"Stats: {json.dumps(observation_data['stats'], indent=2)}")
        
        # Add row count and columns
        if observation_data['row_count'] > 0:
            parts.append(f"Rows: {observation_data['row_count']}")
            if observation_data['column_names']:
                parts.append(f"Columns: {', '.join(observation_data['column_names'])}")
        
        # Add data preview if available
        if observation_data['data_preview'] != "No data available":
            parts.extend([
                "Data Preview:",
                observation_data['data_preview']
            ])
        
        return "\n".join(parts)
    
    async def get_widget_count(self) -> int:
        """Get total number of data widgets for this report."""
        widgets_and_steps = await self._get_report_widgets_and_steps(self.report.id)
        return len(widgets_and_steps)
    
    async def render(self, max_widgets: int = 5) -> str:
        """Render a human-readable view of widget context."""
        widgets_and_steps = await self._get_report_widgets_and_steps(self.report.id)
        
        parts = [
            f"Widget Context: {len(widgets_and_steps)} widgets available",
            "=" * 45
        ]
        
        if not widgets_and_steps:
            parts.append("\nNo widgets created yet")
            return "\n".join(parts)
        
        # Show recent widgets (limited by max_widgets)
        recent_widgets = widgets_and_steps[-max_widgets:] if len(widgets_and_steps) > max_widgets else widgets_and_steps
        
        parts.append(f"\nRecent {len(recent_widgets)} widgets:")
        for i, (widget, step) in enumerate(recent_widgets):
            widget_type = getattr(widget, 'type', 'unknown')
            step_title = step.title if step else 'No step'
            row_count = 0
            
            # Get row count if available
            if step and step.data and isinstance(step.data, dict) and 'rows' in step.data:
                row_count = len(step.data['rows'])
            
            parts.append(f"  {i+1}. {widget.title} ({widget_type}) - {row_count} rows")
            parts.append(f"     Step: {step_title}")
        
        return "\n".join(parts)