import json
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    SuggestInstructionsInput,
    SuggestInstructionsOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolPartialEvent,
    ToolEndEvent,
)
from app.ai.llm import LLM
from partialjson.json_parser import JSONParser


class SuggestInstructionsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="suggest_instructions",
            description="Create/suggest instructions that can help the AI make better decisions. Use when you are identifying a new fact/rule/instruction that should be added to the instructions as it can help the AI make better decisions.",
            category="action",
            version="1.0.0",
            input_schema=SuggestInstructionsInput.model_json_schema(),
            output_schema=SuggestInstructionsOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=60,
            is_active=True,
            idempotent=False,
            observation_policy="never",
            tags=["instructions", "suggest", "streaming"],
            examples=[
                {"input": {"hint": "definition of active users" }}
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SuggestInstructionsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SuggestInstructionsOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = SuggestInstructionsInput(**tool_input)

        # Emit start
        yield ToolStartEvent(type="tool.start", payload={"hint": data.hint})

        # Gather runtime context similarly to answer_question (no observation recording)
        context_view = runtime_ctx.get("context_view")
        context_hub = runtime_ctx.get("context_hub")

        # Schemas
        _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
        schemas_excerpt = _schemas_section_obj.render() if _schemas_section_obj else ""
        # Resources
        _resources_section_obj = getattr(context_view.static, "resources", None) if context_view else None
        resources_context = _resources_section_obj.render() if _resources_section_obj else ""
        # Instructions (existing)
        _instructions_section_obj = getattr(context_view.static, "instructions", None) if context_view else None
        instructions_context = _instructions_section_obj.render() if _instructions_section_obj else ""
        # Messages
        _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        # Observations and history (read-only)
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

        # Build prompt for instruction suggestions (JSON, stream-parse)
        header = f"""
You are a helpful analytics assistant. Draft 1-3 high-quality instructions to guide future responses.
Each instruction should be concise and unambiguous. Choose a category for each: "code", "general", "data_modeling", "dashboard".
The instruction should be an output of this conversation with the user, and should be created to guide future responses and AI decisions.
You have to be very confident, very specific and very detailed in the instruction.
If you have confidence in 0% or you are not sure, you should not create an instruction.

Context:
  {instructions_context}
  {schemas_excerpt}
  {resources_context if resources_context else 'No metadata resources'}
  {history_summary}
  {messages_context if messages_context else 'No recent messages'}
  <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>
  <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

Hint: {data.hint or 'None'}

Return a single JSON object matching this schema exactly:
{{
  "instructions": [
    {{"text": "...", "category": "code|general|data_modeling|dashboard"}}
  ]
}}
"""

        llm = LLM(runtime_ctx.get("model"))
        parser = JSONParser()
        buffer = ""
        allowed_categories = {"code", "general", "data_modeling", "dashboard"}
        partial_items: dict[int, dict] = {}
        emitted_indices: set[int] = set()
        collected: list[dict] = []

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_call_start"})

        async for chunk in llm.inference_stream(header):
            if not chunk:
                continue
            buffer += chunk
            # Try partial JSON parse
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
                        # Merge fields into partial
                        current = partial_items.get(idx, {})
                        if "text" in item and isinstance(item.get("text"), str):
                            current["text"] = item.get("text").strip()
                        if "category" in item and isinstance(item.get("category"), str):
                            current["category"] = item.get("category").strip()
                        partial_items[idx] = current

                        # Check completeness and validity
                        text = (current.get("text") or "").strip()
                        category = (current.get("category") or "").strip()
                        is_valid = (
                            len(text) >= 12 and text.endswith(".") and category in allowed_categories
                        )
                        if is_valid and idx not in emitted_indices:
                            emitted_indices.add(idx)
                            collected.append({"text": text, "category": category})
                            yield ToolProgressEvent(
                                type="tool.progress",
                                payload={
                                    "stage": "instruction_added",
                                    "instruction": {"text": text, "category": category},
                                },
                            )

        # Finalize payload
        payload_output = SuggestInstructionsOutput(instructions=collected[:3]).model_dump()

        observation = {
            "summary": "Suggested instruction drafts (non-persistent)",
            "analysis_complete": False,
            "final_answer": None,
        }
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": payload_output,
                "observation": observation,
            },
        )


