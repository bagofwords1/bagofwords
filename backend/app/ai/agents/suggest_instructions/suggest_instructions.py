from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import json
from partialjson.json_parser import JSONParser

from app.ai.llm import LLM
from app.models.llm_model import LLMModel
from sqlalchemy.ext.asyncio import AsyncSession


# Category definitions with descriptions
INSTRUCTION_CATEGORIES = {
    "dashboard": "Instructions for dashboard layout, grid structure, block composition, and high-level style guidelines across the dashboard.",
    "visualization": "Instructions for specific chart types, colors, encodings, axes, labels, legends, and visual presentation of a single visualization.",
    "system": "Instructions for agent workflow customization, clarify flows, failure handling, and how the AI analyst should behave.",
    "general": "General rules, business rules, metric definitions, terminology, thresholds, and other domain knowledge.",
    "code_gen": "Instructions for SQL/code generation, joins, filters, aggregations, naming, casting, and query patterns.",
}

ALLOWED_CATEGORIES = set(INSTRUCTION_CATEGORIES.keys())

# Minimum confidence threshold to emit an instruction
MIN_CONFIDENCE_THRESHOLD = 0.6


class SuggestInstructions:

    def __init__(
        self,
        model: LLMModel,
        usage_session_maker: Optional[Callable[[], AsyncSession]] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker)

    def _build_category_description(self) -> str:
        """Build category descriptions for the prompt."""
        lines = []
        for cat, desc in INSTRUCTION_CATEGORIES.items():
            lines.append(f'  - "{cat}": {desc}')
        return "\n".join(lines)

    def _format_trigger_conditions(self, conditions: List[Dict[str, str]]) -> str:
        """Format trigger conditions for the prompt."""
        if not conditions:
            return "No specific trigger conditions."
        
        lines = []
        for i, cond in enumerate(conditions, 1):
            name = cond.get("name", "unknown")
            hint = cond.get("hint", "")
            lines.append(f"{i}. [{name}] {hint}")
        return "\n".join(lines)

    async def stream_suggestions(
        self, context_view: Any = None, context_hub: Any = None, conditions: List[Dict[str, str]] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream instruction suggestions as they become valid.

        Args:
            context_view: The context view containing static/warm context sections.
            context_hub: The context hub for accessing observation history.
            conditions: List of trigger conditions that fired, each with {"name", "hint"}.

        Yields dicts with keys {"text", "category", "confidence"}.
        """
        # Build context from provided view/hub
        schemas_excerpt = getattr(getattr(context_view, "static", None), "schemas", None)
        schemas_excerpt = schemas_excerpt.render() if schemas_excerpt else ""

        resources_section = getattr(getattr(context_view, "static", None), "resources", None)
        resources_context = resources_section.render() if resources_section else ""

        instructions_section = getattr(getattr(context_view, "static", None), "instructions", None)
        instructions_context = instructions_section.render() if instructions_section else ""

        messages_section = getattr(getattr(context_view, "warm", None), "messages", None)
        messages_context = messages_section.render() if messages_section else ""

        past_observations = []
        last_observation = None
        if context_hub and getattr(context_hub, "observation_builder", None):
            try:
                past_observations = context_hub.observation_builder.tool_observations or []
                last_observation = context_hub.observation_builder.get_latest_observation()
            except Exception:
                past_observations = []
                last_observation = None

        history_summary = ""
        if context_hub and hasattr(context_hub, "get_history_summary"):
            try:
                history_summary = context_hub.get_history_summary(
                    context_hub.observation_builder.to_dict() if getattr(context_hub, "observation_builder", None) else None
                )
            except Exception:
                history_summary = ""

        trigger_section = self._format_trigger_conditions(conditions or [])
        category_desc = self._build_category_description()

        header = f"""
You are a helpful analytics assistant. Your goal is to improve our system AI analyst by turning newly learned facts or failure learnings into durable instructions.

You are triggered by one of two reasons, or it could be both:
1) Clarification flow: User sent a message that triggered the AI Analyst to use the clarify tool, and then provided a concrete definition after a clarify question. Extract that definition and convert it into 1–3 concise, unambiguous instructions.
2) Code recovery flow: A create_data action succeeded after 1+ internal retries/errors. Propose 1–3 instructions that would avoid similar failures next time (validation, column naming, joins, filters, limits, casting, etc.).

Follow the guidance below and only return instructions when you have high confidence. Otherwise, return an empty list.

<trigger_conditions>
{trigger_section}
</trigger_conditions>

IMPORTANT — Use your judgment:
- The trigger conditions above are heuristics that MAY indicate a learning opportunity, but they are NOT always correct.
- Before proposing an instruction, ask yourself: "Is there actually something reusable and valuable to learn from this conversation?"
- If the conversation is just normal exploration, casual questions, or doesn't contain a clear definition/rule/pattern — return an empty list.
- Only propose instructions when you are confident they will genuinely improve future responses.
- Quality over quantity: one excellent instruction is better than three mediocre ones.

Clarification flow requirements:
- Use ONLY the user initial message, the AI Analyst's clarification question, and the user's clarification to define the rule/glossary (e.g., what a specific metric/term/fact means, thresholds, time windows, exclusions).
- Make the instruction complete, specific, measurable, and reusable across future questions when that clarification trigger message is sent.

Code recovery flow requirements:
- Infer the most likely root cause(s) from the conversation and context (e.g., missing join keys, non-existent columns, invalid casts, empty results, limits too high/low).
- Write prescriptive instructions that would help the model generate better data modeling/code on first attempt.

General rules:
- 1–3 instructions max. Each instruction must end with a period.
- Instructions CANNOT be duplicate or conflict with ANY of the existing instructions. Review the existing instructions carefully and ensure your instructions are not a duplicate or conflict.
- Include a confidence score (0.0 to 1.0) for each instruction. Only include instructions with confidence >= 0.5.

Categories (choose the most appropriate one):
{category_desc}

Examples (clarification → instruction):
- User clarified: "Active user = user with ≥1 session in the last 30 days."
  Instruction: {{"text": "Treat an active user as a user with at least one session in the last 30 days for all activity-based metrics.", "category": "general", "confidence": 0.95}}

Examples (code recovery → instruction):
- {{"text": "Always join payments to customers on customer_id and filter out NULL customer_id before aggregation.", "category": "code_gen", "confidence": 0.85}}
- {{"text": "Cast date strings to DATE before grouping by day and use timezone-aware truncation to avoid off-by-one errors.", "category": "code_gen", "confidence": 0.80}}

Examples (visualization instruction):
- {{"text": "Use a stacked bar chart when comparing parts of a whole across categories.", "category": "visualization", "confidence": 0.75}}
- {{"text": "Always show currency values with 2 decimal places and include the currency symbol.", "category": "visualization", "confidence": 0.90}}

Examples (dashboard instruction):
- {{"text": "Place KPI metric cards in the top row of the dashboard before any charts.", "category": "dashboard", "confidence": 0.85}}

Examples (system instruction):
- {{"text": "When the user asks about 'recent' data without specifying a time range, default to the last 30 days.", "category": "system", "confidence": 0.80}}

Longer example:
- Instruction: {{"text": "Authoritative Net Revenue (NR) Calculation — SaaS. Include only invoice_lines with line_type IN ('recurring','usage'), is_trial=false; recognize revenue pro‑rata over service_start → service_end; convert to USD using daily EOD spot; exclude VAT/taxes and processor fees; allocate refunds/credits to original service days; stop recognition at cancellation_effective_at; clamp per‑day NR to ≥ 0 after discounts.", "category": "general", "confidence": 0.95}}

Context:
  {instructions_context}
  {history_summary}
  {messages_context if messages_context else 'No recent messages'}
  <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>
  <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

Return a single JSON object matching this schema exactly:
{{
  "instructions": [
    {{"text": "...", "category": "dashboard|visualization|system|general|code_gen", "confidence": 0.0-1.0}}
  ]
}}
"""

        async for item in self._stream_and_parse(header, "suggest_instructions.stream"):
            yield item

    async def onboarding_suggestions(self, context_view: Any = None) -> AsyncIterator[Dict[str, Any]]:
        """Stream instruction suggestions for onboarding.

        Yields dicts with keys {"text", "category", "confidence"}.
        """
        # Build lightweight onboarding context (schemas, metadata resources, optional messages)
        schemas_excerpt = getattr(getattr(context_view, "static", None), "schemas", None)
        schemas_excerpt = schemas_excerpt.render() if schemas_excerpt else ""

        resources_section = getattr(getattr(context_view, "static", None), "resources", None)
        resources_context = resources_section.render() if resources_section else ""

        messages_section = getattr(getattr(context_view, "warm", None), "messages", None)
        messages_context = messages_section.render() if messages_section else ""

        category_desc = self._build_category_description()

        header = f"""
You are a helpful analytics assistant. Your goal is to improve our system AI analyst by turning newly learned facts or failure learnings into durable instructions.

The user has just connected a data source and is onboarding.

General rules:
- 1–3 instructions max. Each instruction must end with a period.
- Instructions CANNOT be duplicate or conflict with ANY of the existing instructions. Review the existing instructions carefully and ensure your instructions are not a duplicate or conflict.
- Include a confidence score (0.0 to 1.0) for each instruction. Only include instructions with confidence >= 0.6.

Categories (choose the most appropriate one):
{category_desc}

Examples (clarification → instruction):
- User clarified: "Active user = user with ≥1 session in the last 30 days."
  Instruction: {{"text": "Treat an active user as a user with at least one session in the last 30 days for all activity-based metrics.", "category": "general", "confidence": 0.95}}

Examples (code_gen instruction):
- {{"text": "Always join payments to customers on customer_id and filter out NULL customer_id before aggregation.", "category": "code_gen", "confidence": 0.85}}
- {{"text": "Cast date strings to DATE before grouping by day and use timezone-aware truncation to avoid off-by-one errors.", "category": "code_gen", "confidence": 0.80}}

Examples (visualization instruction):
- {{"text": "Use a stacked bar chart when comparing parts of a whole across categories.", "category": "visualization", "confidence": 0.75}}

Longer example:
- Instruction: {{"text": "Authoritative Net Revenue (NR) Calculation — SaaS. Include only invoice_lines with line_type IN ('recurring','usage'), is_trial=false; recognize revenue pro‑rata over service_start → service_end; convert to USD using daily EOD spot; exclude VAT/taxes and processor fees; allocate refunds/credits to original service days; stop recognition at cancellation_effective_at; clamp per‑day NR to ≥ 0 after discounts.", "category": "general", "confidence": 0.95}}

Context:
Schema
  {schemas_excerpt if schemas_excerpt else 'No schema available'}

Metadata Resources
  {resources_context if resources_context else 'No metadata resources available'}

Recent Messages
  {messages_context if messages_context else 'No recent messages'}

Return a single JSON object matching this schema exactly:
{{
  "instructions": [
    {{"text": "...", "category": "dashboard|visualization|system|general|code_gen", "confidence": 0.0-1.0}}
  ]
}}
"""

        async for item in self._stream_and_parse(header, "suggest_instructions.onboarding"):
            yield item

    async def _stream_and_parse(
        self, prompt: str, usage_scope: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """Shared streaming and parsing logic for instruction suggestions.

        Yields dicts with keys {"text", "category", "confidence"}.
        """
        parser = JSONParser()
        buffer = ""
        partial_items: dict[int, dict] = {}
        emitted_indices: set[int] = set()
        yielded_count = 0

        async for chunk in self.llm.inference_stream(
            prompt,
            usage_scope=usage_scope,
            usage_scope_ref_id=None,
        ):
            if not chunk:
                continue
            buffer += chunk
            try:
                parsed = parser.parse(buffer)
            except Exception:
                parsed = None

            if isinstance(parsed, dict):
                arr = parsed.get("instructions")
                if isinstance(arr, list):
                    for idx, item in enumerate(arr):
                        if not isinstance(item, dict):
                            continue
                        
                        current = partial_items.get(idx, {})
                        
                        # Parse text
                        if "text" in item and isinstance(item.get("text"), str):
                            current["text"] = item.get("text").strip()
                        
                        # Parse category
                        if "category" in item and isinstance(item.get("category"), str):
                            current["category"] = item.get("category").strip()
                        
                        # Parse confidence
                        if "confidence" in item:
                            try:
                                conf_val = item.get("confidence")
                                if isinstance(conf_val, (int, float)):
                                    current["confidence"] = float(conf_val)
                                elif isinstance(conf_val, str):
                                    current["confidence"] = float(conf_val)
                            except (ValueError, TypeError):
                                pass
                        
                        partial_items[idx] = current

                        # Validate completeness
                        text = (current.get("text") or "").strip()
                        category = (current.get("category") or "").strip()
                        confidence = current.get("confidence")

                        is_valid = (
                            len(text) >= 12
                            and text.endswith(".")
                            and category in ALLOWED_CATEGORIES
                            and isinstance(confidence, float)
                            and 0.0 <= confidence <= 1.0
                            and confidence >= MIN_CONFIDENCE_THRESHOLD
                        )

                        if is_valid and idx not in emitted_indices and yielded_count < 3:
                            emitted_indices.add(idx)
                            yielded_count += 1
                            yield {
                                "text": text,
                                "category": category,
                                "confidence": confidence,
                            }

    async def enhance_instruction(
        self,
        instruction: str,
        instructions_context: str,
        data_source_context: str,
        context_view: Any = None,
    ) -> str:
        """User is creating an instruction and requested AI to enhance it"""

        header = f"""
        You are a helpful analytics assistant. Your goal is to enhance an instruction to make it more clear and concise.

        Data Source Context (reference only):
        {data_source_context or 'No data source context available'}

        Instructions Context (sample and reference only):
        {instructions_context[:10000] or 'No instructions context available'}

        The user has provided the following DRAFT INSTRUCTION to enhance:
        {instruction}

        Please enhance the instruction to make it more clear and concise. The output should be fed into LLM as a rule to be followed.
        Respect the existing instructions.

        Output format:
        {{
          "enhanced_instruction": "..."
        }}
        """

        parser = JSONParser()
        buffer = ""
        async for chunk in self.llm.inference_stream(
            header,
            usage_scope="suggest_instructions.enhance",
            usage_scope_ref_id=None,
        ):
            if not chunk:
                continue
            buffer += chunk
            try:
                parsed = parser.parse(buffer)
            except Exception:
                parsed = None

        return parsed if isinstance(parsed, dict) else None
