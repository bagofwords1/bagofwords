"""
Observation Context Builder - Manages tool execution results and output context.
"""
import json
from typing import Dict, Any, List, Optional
from datetime import datetime


from app.ai.context.sections.observations_section import ObservationsSection, ToolExecutionItem, WidgetUpdateItem, StepUpdateItem, VisualizationUpdateItem
from app.ai.data_preview import SAMPLE_ROWS


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
        self.visualization_updates: List[Dict[str, Any]] = []
        
        # Other useful outputs (files created, data processed, etc.)
        self.artifacts: Dict[str, Any] = {}
    
    def add_tool_observation(self, tool_name: str, tool_input: Dict[str, Any], observation: Dict[str, Any], loop_index: int = None):
        """
        Add an observation from a tool execution.

        Args:
            tool_name: Name of the tool that was executed
            tool_input: Input parameters passed to the tool
            observation: Tool execution result with summary and artifacts
            loop_index: Planner loop iteration this observation belongs to.
                A multi-tool decision produces several observations sharing
                one loop_index; they are recorded in action order.
        """
        self.execution_count += 1

        # Compact previous observations after 1 iteration.
        # Any new tool observation triggers stripping of heavy fields
        # from all prior observations, keeping only lightweight metadata.
        # Observations from the SAME iteration are exempt: a parallel
        # multi-tool batch is recorded one entry at a time, and the planner
        # must see the full detail of the whole just-finished batch — not
        # just whichever action happened to be recorded last.
        for prev_obs in self.tool_observations:
            if loop_index is not None and prev_obs.get("loop_index") == loop_index:
                continue
            prev_observation = prev_obs.get("observation", {})
            if prev_obs["tool_name"] in ("create_artifact", "read_artifact", "edit_artifact"):
                if "code" in prev_observation:
                    code_len = len(prev_observation["code"])
                    del prev_observation["code"]
                    prev_observation["code_compacted"] = f"{code_len} chars"
                if "images" in prev_observation:
                    del prev_observation["images"]
                    prev_observation["images_compacted"] = True
                # Windowed-read payloads (long-artifact support) are just as
                # heavy as full code — keep them for one iteration only, same
                # as `code`. The summary line (match counts, line ranges)
                # survives so the planner remembers WHAT it already saw.
                if "matches" in prev_observation:
                    match_count = len(prev_observation["matches"] or [])
                    del prev_observation["matches"]
                    prev_observation["matches_compacted"] = f"{match_count} grep matches (already shown — re-run read_artifact if needed)"
                if "outline" in prev_observation:
                    outline_len = len(prev_observation["outline"] or "")
                    del prev_observation["outline"]
                    prev_observation["outline_compacted"] = f"{outline_len} chars"
                if "runtime_environment" in prev_observation:
                    del prev_observation["runtime_environment"]
                    prev_observation["runtime_environment_compacted"] = True
            elif prev_obs["tool_name"] == "inspect_data":
                if "details" in prev_observation:
                    details_len = len(prev_observation["details"])
                    del prev_observation["details"]
                    prev_observation["details_compacted"] = f"{details_len} chars"
                if "code" in prev_observation:
                    code_len = len(prev_observation["code"])
                    del prev_observation["code"]
                    prev_observation["code_compacted"] = f"{code_len} chars"
            elif prev_obs["tool_name"] in (
                "read_file", "grep_files", "list_files", "search_files",
                "read_email", "list_emails", "search_email",
            ):
                # File/email content / match excerpts / listing inventories: full
                # for the LATEST call, a length marker once superseded —
                # sequential file/mail operations must not stack their payloads
                # into the planner context. The summary line (file id, counts,
                # truncation) survives, so the model still knows WHAT it
                # read/listed without paying for the body again.
                if "details" in prev_observation:
                    details_len = len(prev_observation["details"])
                    del prev_observation["details"]
                    prev_observation["details_compacted"] = (
                        f"{details_len} chars (already read — do not re-read; "
                        "content was shown when this call ran)"
                    )
            elif prev_obs["tool_name"] == "execute_mcp":
                # An MCP result is inlined up to `mcp_result_inline_chars`
                # (50k by default) so the agent can answer from it without a
                # second round trip. That budget is priced for ONE observation:
                # left alone, five sequential calls stack five full payloads
                # into every subsequent prompt, and the last-5-full window in
                # the prompt builder is too late to catch it — this is the same
                # reason read_file and web_fetch compact their bodies here.
                #
                # The summary, file_id, row_count and record_shape survive, so
                # a later step still knows what the call returned and how to
                # re-open it; only the records themselves go.
                if "preview" in prev_observation:
                    prev_preview = prev_observation["preview"]
                    if isinstance(prev_preview, list):
                        shown, unit = len(prev_preview), "records"
                    elif isinstance(prev_preview, (str, type(None))):
                        shown, unit = len(prev_preview or ""), "chars"
                    else:
                        # A full-payload preview is the response dict verbatim.
                        try:
                            shown = len(json.dumps(prev_preview, default=str))
                        except (TypeError, ValueError):
                            shown = len(str(prev_preview))
                        unit = "chars"
                    del prev_observation["preview"]
                    prev_observation["preview_compacted"] = (
                        f"{shown} {unit} (already returned — re-open the file "
                        "with its file_id if you still need them)"
                    )
            elif prev_obs["tool_name"] in ("create_data", "read_query"):
                # Keep a small labeled sample instead of dropping rows entirely,
                # so older results stay referenceable. The latest observation
                # still carries the full budgeted preview. read_query shares this
                # path so its (now full) preview doesn't persist across the loop.
                previews = []
                dp = prev_observation.get("data_preview")
                if isinstance(dp, dict):
                    previews.append(dp)
                for item in prev_observation.get("results_summary") or []:
                    if isinstance(item, dict) and isinstance(item.get("data_preview"), dict):
                        previews.append(item["data_preview"])
                for dp in previews:
                    if isinstance(dp.get("rows"), list) and len(dp["rows"]) > SAMPLE_ROWS:
                        total = dp.get("row_count") or len(dp["rows"])
                        dp["rows"] = dp["rows"][:SAMPLE_ROWS]
                        dp["sampled"] = True
                        dp["note"] = f"sample of {SAMPLE_ROWS}; {total} rows total"
                # Code is stripped for create_data only. read_query is the
                # "show me what I wrote earlier" tool: dropping its code after
                # one iteration puts the planner back where it started — it
                # re-reads the same query to see the code again, which is the
                # loop putting the code in the observation was meant to end.
                # Query code is a few KB (create_data's generated program is
                # not), and its lifetime is already bounded by the prompt
                # builder's last-N-full window: older observations minify down
                # to _OBS_KEEP_KEYS, which excludes code. So let it expire
                # there instead of here.
                if prev_obs["tool_name"] == "create_data" and "code" in prev_observation:
                    code_len = len(prev_observation["code"])
                    del prev_observation["code"]
                    prev_observation["code_compacted"] = f"{code_len} chars"
            elif prev_obs["tool_name"] == "web_fetch":
                if "content" in prev_observation:
                    content_len = len(prev_observation["content"] or "")
                    del prev_observation["content"]
                    prev_observation["content_compacted"] = f"{content_len} chars"
                if "json_ld" in prev_observation:
                    del prev_observation["json_ld"]
                    prev_observation["json_ld_compacted"] = True
                if "metadata" in prev_observation:
                    del prev_observation["metadata"]
                    prev_observation["metadata_compacted"] = True
                if "headings" in prev_observation:
                    del prev_observation["headings"]
                    prev_observation["headings_compacted"] = True

        tool_observation = {
            "execution_number": self.execution_count,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "timestamp": datetime.utcnow().isoformat(),
            "observation": observation
        }
        if loop_index is not None:
            tool_observation["loop_index"] = loop_index

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

    def add_visualization_update(self, visualization_id: str, viz_data: Dict[str, Any]):
        """
        Track visualization creation or updates.
        """
        self.visualization_updates.append({
            "visualization_id": visualization_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": viz_data
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
        if not self.has_observations():
            return ""
        return self._build_prompt_context(max_observations) if format_for_prompt else self._build_debug_context()

    def build(self) -> ObservationsSection:
        obs_items = [
            ToolExecutionItem(
                execution_number=obs.get("execution_number"),
                tool_name=obs.get("tool_name"),
                tool_input=obs.get("tool_input", {}),
                timestamp=obs.get("timestamp"),
                observation=obs.get("observation", {}),
            )
            for obs in self.tool_observations
        ]
        widget_items = [
            WidgetUpdateItem(
                widget_id=wu.get("widget_id"),
                timestamp=wu.get("timestamp"),
                data=wu.get("data", {}),
            )
            for wu in self.widget_updates
        ]
        step_items = [
            StepUpdateItem(
                step_id=su.get("step_id"),
                timestamp=su.get("timestamp"),
                data=su.get("data", {}),
            )
            for su in self.step_updates
        ]
        viz_items = [
            VisualizationUpdateItem(
                visualization_id=vu.get("visualization_id"),
                timestamp=vu.get("timestamp"),
                data=vu.get("data", {}),
            )
            for vu in self.visualization_updates
        ]
        return ObservationsSection(
            execution_count=self.execution_count,
            tool_observations=obs_items,
            widget_updates=widget_items,
            step_updates=step_items,
            visualization_updates=viz_items,
            artifacts=self.artifacts,
        )
    
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
                
                # Include detailed error information if present
                if "error" in observation:
                    error = observation["error"]
                    if "field_errors" in error:
                        lines.append(f"    Field errors: {'; '.join(error['field_errors'])}")
                    elif "message" in error:
                        lines.append(f"    Error: {error['message']}")
                    
                    # Include suggestions if available
                    if "suggestion" in error:
                        lines.append(f"    Suggestion: {error['suggestion']}")
                
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
            "visualization_updates": self.visualization_updates,
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
            "visualization_updates": self.visualization_updates,
            "artifacts": self.artifacts
        }
    
    def clear(self):
        """Clear all observation context."""
        self.tool_observations.clear()
        self.widget_updates.clear()
        self.step_updates.clear()
        self.visualization_updates.clear()
        self.artifacts.clear()
        self.execution_count = 0
