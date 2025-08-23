import json
from typing import List, Dict, Any
from app.schemas.ai.planner import PlannerInput, ToolDescriptor


class PromptBuilder:
    """Builds prompts for the planner with intelligent plan type decision logic."""
    
    @staticmethod
    def build_prompt(planner_input: PlannerInput, org_instructions: str) -> str:
        """Build the full prompt from PlannerInput and org instructions."""
        
        # Separate tools by category for better decision making
        research_tools = []
        action_tools = []
        
        for tool in planner_input.tool_catalog or []:
            tool_info = {
                "name": tool.name,
                "description": tool.description,
            }
            
            # Categorize tools based on research_accessible field
            if tool.research_accessible:
                research_tools.append(tool_info)
            else:
                # If not research_accessible, it's an action tool
                action_tools.append(tool_info)
        
        research_tools_json = json.dumps(research_tools, ensure_ascii=False)
        action_tools_json = json.dumps(action_tools, ensure_ascii=False)
        
        # Calculate research step count for context
        research_step_count = PromptBuilder._extract_research_step_count(planner_input.history_summary)
        
        return f"""
You are a planner. Decide ONE next action or give a final answer.

<context>
  <platform>{planner_input.external_platform}</platform>
  <organization_instructions>{org_instructions}</organization_instructions>
  <goal>{planner_input.user_message}</goal>
  <schemas>{planner_input.schemas_excerpt}</schemas>
  <history>{planner_input.history_summary}</history>
  <last_observation>{json.dumps(planner_input.last_observation) if planner_input.last_observation else 'None'}</last_observation>
  <research_steps_taken>{research_step_count}</research_steps_taken>
</context>

PLAN TYPE DECISION FRAMEWORK:
You must choose between "research" and "action" based on your confidence and information needs.

🔍 Use plan_type="research" when:
- You need to read files, check schemas, or gather information before acting
- You lack understanding of the codebase structure or data model
- You need to answer questions from existing context
- Your confidence is LOW (<70%) about how to proceed
- You're missing key information needed to achieve the goal
- Research steps taken < 3 (avoid infinite research loops)

⚡ Use plan_type="action" when:
- You have sufficient information to execute the goal
- You're ready to create, modify, or execute something concrete
- Your confidence is HIGH (>70%) about the approach
- All necessary research is complete
- Research steps taken >= 3 (force action to prevent loops)

AVAILABLE TOOLS:
<research_tools>{research_tools_json}</research_tools>
<action_tools>{action_tools_json}</action_tools>

DECISION RULES:
1. If research_steps_taken >= 3, you MUST use "action" (no more research)
2. If last_observation indicates success/completion, prefer "action"  
3. If schemas_excerpt is empty/insufficient, prefer "research" first
4. Choose plan_type based on information sufficiency, if more information is needed, use research tools first

Output rules:
- JSON ONLY, no markdown.
- reasoning_message: USER-FACING explanation of what you're thinking (avoid internal terms like "confidence", "schemas_excerpt", "research_steps")
- assistant_message: brief user-facing message about your next step
- If answerable from context, set analysis_complete=true and provide final_answer
- Tool choice must match plan_type: research tools for "research", action tools for "action"


Expected JSON, strict:
{{
  "analysis_complete": boolean,
  "plan_type": "research" | "action",
  "reasoning_message": string | null,
  "assistant_message": string | null,
  "action": {{
    "type": "tool_call",
    "name": string,
    "arguments": object
  }} | null,
  "final_answer": string | null
}}
"""

    @staticmethod
    def _extract_research_step_count(history_summary: str) -> int:
        """Extract research step count from history for loop prevention."""
        if not history_summary:
            return 0
        
        # Simple heuristic: count research tool mentions
        research_keywords = ['read_file', 'answer_question', 'research']
        count = 0
        for keyword in research_keywords:
            count += history_summary.lower().count(keyword)
        
        return min(count, 5)  # Cap at 5 for safety