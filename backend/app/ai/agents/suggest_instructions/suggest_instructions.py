from typing import Any, AsyncIterator, Callable, Dict, Optional

import json
from partialjson.json_parser import JSONParser

from app.ai.llm import LLM
from app.models.llm_model import LLMModel
from sqlalchemy.ext.asyncio import AsyncSession


class SuggestInstructions:

    def __init__(
        self,
        model: LLMModel,
        usage_session_maker: Optional[Callable[[], AsyncSession]] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker)

    async def stream_suggestions(self, context_view: Any = None, context_hub: Any = None, hint: str = None) -> AsyncIterator[Dict[str, str]]:
        """Stream instruction suggestions as they become valid.

        Yields dicts with keys {"text", "category"}.
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
                history_summary = await context_hub.get_history_summary(
                    context_hub.observation_builder.to_dict() if getattr(context_hub, "observation_builder", None) else None
                )
            except Exception:
                history_summary = ""

        safe_hint = (hint or "").strip()

        header = f"""
You are a helpful analytics assistant. Your goal is to improve our system AI analyst by turning newly learned facts or failure learnings into durable instructions.

You are triggered by one of two reasons, or it could be both:
1) Clarification flow: User sent a message that triggered the AI Analyst to use the clarify tool, and then provided a concrete definition after a clarify question. Extract that definition and convert it into 1–3 concise, unambiguous instructions.
2) Code recovery flow: A create_widget action succeeded after 1+ internal retries/errors. Propose 1–3 instructions that would avoid similar failures next time (validation, column naming, joins, filters, limits, casting, etc.).

Follow the guidance below and only return instructions when you have very high confidence. Otherwise, return an empty list.

<trigger_reason>{safe_hint}</trigger_reason>

Clarification flow requirements:
- Use ONLY the user initial message, the AI Analyst's clarification question, and the user's clarification to define the rule/glossary (e.g., what a speciic metric/term/fact means, thresholds, time windows, exclusions).
- Make the instruction complete, specific, measurable, and reusable across future questions when that clarification trigger message is sent.

Code recovery flow requirements:
- Infer the most likely root cause(s) from the conversation and context (e.g., missing join keys, non-existent columns, invalid casts, empty results, limits too high/low).
- Write prescriptive instructions that would help the model generate better data modeling/code on first attempt.

General rules:
- 1–3 instructions max. Each instruction must end with a period.
- Instructions CANNOT be duplicate or conflict with ANY of the existing instructions. Review the existing instructions carefully and ensure your instructions are not a duplicate or conflict.
- Category must be one of: "code_gen" | "general".
- If confidence is low, return an empty list.

Examples (clarification → instruction):
- User clarified: “Active user = user with ≥1 session in the last 30 days.”
  Instruction (general): “Treat an active user as a user with at least one session in the last 30 days for all activity-based metrics.”

Examples (code recovery → instruction):
- “Always join payments to customers on customer_id and filter out NULL customer_id before aggregation.”
- “Cast date strings to DATE before grouping by day and use timezone-aware truncation to avoid off-by-one errors.”

Longer example:
- Instruction: Authoritative Net Revenue (NR) Calculation — SaaS.
  Rule: Include only invoice_lines with line_type IN ('recurring','usage'), is_trial=false; recognize revenue pro‑rata over service_start → service_end; convert to USD using daily EOD spot; exclude VAT/taxes and processor fees; allocate refunds/credits to original service days; stop recognition at cancellation_effective_at; clamp per‑day NR to ≥ 0 after discounts.

Context:
  {instructions_context}
  {history_summary}
  {messages_context if messages_context else 'No recent messages'}
  <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>
  <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

Return a single JSON object matching this schema exactly:
{{
  "instructions": [
    {{"text": "...", "category": "general|code_gen"}}
  ]
}}
"""

        parser = JSONParser()
        buffer = ""
        allowed_categories = {"code_gen", "general"}
        partial_items: dict[int, dict] = {}
        emitted_indices: set[int] = set()
        yielded_count = 0
        
        async for chunk in self.llm.inference_stream(
            header,
            usage_scope="suggest_instructions.stream",
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
                        if "text" in item and isinstance(item.get("text"), str):
                            current["text"] = item.get("text").strip()
                        if "category" in item and isinstance(item.get("category"), str):
                            current["category"] = item.get("category").strip()
                        partial_items[idx] = current

                        text = (current.get("text") or "").strip()
                        category = (current.get("category") or "").strip()
                        is_valid = (
                            len(text) >= 12 and text.endswith(".") and category in allowed_categories
                        )
                        if is_valid and idx not in emitted_indices and yielded_count < 3:
                            emitted_indices.add(idx)
                            yielded_count += 1
                            yield {"text": text, "category": category}


    async def onboarding_suggestions(self, context_view: Any = None) -> AsyncIterator[Dict[str, str]]:
        """Stream instruction suggestions as they become valid.

        Yields dicts with keys {"text", "category"}.
        """

        # Build lightweight onboarding context (schemas, metadata resources, optional messages)
        schemas_excerpt = getattr(getattr(context_view, "static", None), "schemas", None)
        schemas_excerpt = schemas_excerpt.render() if schemas_excerpt else ""

        resources_section = getattr(getattr(context_view, "static", None), "resources", None)
        resources_context = resources_section.render() if resources_section else ""

        messages_section = getattr(getattr(context_view, "warm", None), "messages", None)
        messages_context = messages_section.render() if messages_section else ""

        header = f"""
You are a helpful analytics assistant. Your goal is to improve our system AI analyst by turning newly learned facts or failure learnings into durable instructions.

The user has just connected a data source and is onboarding.

        
General rules:
- 1–3 instructions max. Each instruction must end with a period.
- Instructions CANNOT be duplicate or conflict with ANY of the existing instructions. Review the existing instructions carefully and ensure your instructions are not a duplicate or conflict.
- Category must be one of: "code_gen" | "general".
- If confidence is low, return an empty list.

Examples (clarification → instruction):
- User clarified: “Active user = user with ≥1 session in the last 30 days.”
  Instruction (general): “Treat an active user as a user with at least one session in the last 30 days for all activity-based metrics.”

Examples (code recovery → instruction):
- “Always join payments to customers on customer_id and filter out NULL customer_id before aggregation.”
- “Cast date strings to DATE before grouping by day and use timezone-aware truncation to avoid off-by-one errors.”

Longer example:
- Instruction: Authoritative Net Revenue (NR) Calculation — SaaS.
  Rule: Include only invoice_lines with line_type IN ('recurring','usage'), is_trial=false; recognize revenue pro‑rata over service_start → service_end; convert to USD using daily EOD spot; exclude VAT/taxes and processor fees; allocate refunds/credits to original service days; stop recognition at cancellation_effective_at; clamp per‑day NR to ≥ 0 after discounts.

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
            {{"text": "...", "category": "general|code_gen"}}
          ]
        }}
"""

        parser = JSONParser()
        buffer = ""
        allowed_categories = {"code_gen", "general"}
        partial_items: dict[int, dict] = {}
        emitted_indices: set[int] = set()
        yielded_count = 0
        
        async for chunk in self.llm.inference_stream(
            header,
            usage_scope="suggest_instructions.onboarding",
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
                        if "text" in item and isinstance(item.get("text"), str):
                            current["text"] = item.get("text").strip()
                        if "category" in item and isinstance(item.get("category"), str):
                            current["category"] = item.get("category").strip()
                        partial_items[idx] = current

                        text = (current.get("text") or "").strip()
                        category = (current.get("category") or "").strip()
                        is_valid = (
                            len(text) >= 12 and text.endswith(".") and category in allowed_categories
                        )
                        if is_valid and idx not in emitted_indices and yielded_count < 3:
                            emitted_indices.add(idx)
                            yielded_count += 1
                            yield {"text": text, "category": category}
    
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