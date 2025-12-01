import asyncio
from typing import AsyncIterator, Dict, Any, Type, Optional
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import AnswerQuestionInput, AnswerQuestionOutput
from app.ai.tools.schemas.events import ToolEvent, ToolStartEvent, ToolProgressEvent, ToolPartialEvent, ToolStdoutEvent, ToolEndEvent
from app.ai.llm import LLM
from app.dependencies import async_session_maker
import json

from partialjson.json_parser import JSONParser


class AnswerQuestionTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="answer_question",
            description="For any user question that is about schema, metadata, context, or history, use this tool to answer the question.",
            category="action",  # Can be used in both research and action modes
            version="1.0.0",
            input_schema=AnswerQuestionInput.model_json_schema(),
            output_schema=AnswerQuestionOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=90,
            is_active=True,
            idempotent=False,
            tags=["question", "context", "answer", "streaming"],
            examples=[
                {
                    "input": {"question": "What is the schema for the orders table?"}
                }, 
                {
                    "input": {"question": "Are there dbt models about the orders table?"}
                },
                {
                    "input": {"question": "Summarize the KPIs visible in the current dashboard widgets."}
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return AnswerQuestionInput

    @property  
    def output_model(self) -> Type[BaseModel]:
        return AnswerQuestionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        # Validate input via schema (lightweight)
        data = AnswerQuestionInput(**tool_input)

        # Emit start
        yield ToolStartEvent(type="tool.start", payload={"question": data.question})

        # Gather context using ContextHub view provided by orchestrator
        organization_settings = runtime_ctx.get("settings")
        context_view = runtime_ctx.get("context_view")
        context_hub = runtime_ctx.get("context_hub")

        # Schemas
        _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
        schemas_excerpt = _schemas_section_obj.render() if _schemas_section_obj else ""
        # Resources
        _resources_section_obj = getattr(context_view.static, "resources", None) if context_view else None
        resources_context = _resources_section_obj.render() if _resources_section_obj else ""
        # Instructions
        _instructions_section_obj = getattr(context_view.static, "instructions", None) if context_view else None
        instructions_context = _instructions_section_obj.render() if _instructions_section_obj else ""
        # Messages
        _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        # Mentions
        _mentions_section_obj = getattr(context_view.static, "mentions", None) if context_view else None
        mentions_context = _mentions_section_obj.render() if _mentions_section_obj else "<mentions>No mentions for this turn</mentions>"
        # Entities (warm; ContextHub exposes entities under warm)
        _entities_section_obj = getattr(context_view.warm, "entities", None) if context_view else None
        entities_context = _entities_section_obj.render() if _entities_section_obj else ""
        # Platform
        platform = (getattr(context_view, "meta", {}) or {}).get("external_platform") if context_view else None
        # Observations and history
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
                history_summary = context_hub.get_history_summary(context_hub.observation_builder.to_dict() if getattr(context_hub, "observation_builder", None) else None)
            except Exception:
                history_summary = ""

        # Build answer prompt (grounded; no fabrication)
        header = f"""
You are a helpful data analyst. Answer the user's question concisely using ONLY the provided context.
If the context is insufficient, ask for a brief, targeted clarification in the answer.

Context:
  <platform>{platform}</platform>
  {instructions_context}
  {schemas_excerpt}
  {mentions_context}
  {entities_context}
  {resources_context if resources_context else 'No metadata resources available'}
  {history_summary}
  {messages_context if messages_context else 'No detailed conversation history available'}
  <past_observations>{past_observations if past_observations else '[]'}</past_observations>
  <last_observation>{last_observation if last_observation else 'None'}</last_observation>

Question:
{data.question}

Rules:
- Respond with a single JSON object only. No prose, no code fences.
- The object must have exactly one key "answer" whose value is a Markdown text.
- Make sure the markdown text and format is readable, understandable and follows the best practices. Use spacing and paragraphs to make the text readable.
- Keep the answer brief and factual based on the context.

Examples:
answer: "## Customer table\n\nCustomer table has the following columns: id, name, email, phone, address."
answer: "The `order_id` column is a foreign key to the `order` table."
answer: "The best way to get to payments, based on the schema and guided by the instruction is to query the `payment` table."

Output (strict JSON):
{{
    "answer": "## Markdown text..."
}}
"""
        prompt = header

        parser = JSONParser()

        # Stream from LLM and forward partials
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        buffer = ""
        chunk_count = 0
        full_answer = ""
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_call_start"})

        async for chunk in llm.inference_stream(
            prompt,
            usage_scope="answer_question",
            usage_scope_ref_id=None,
        ):
            
            # Guard against empty SSE heartbeats
            if not chunk:
                continue
            buffer += chunk
            try:
                result = parser.parse(buffer)
                if isinstance(result, dict):
                    full_answer = result.get("answer", full_answer)
            except Exception:
                # Incomplete/partial JSON; keep accumulating
                pass
            chunk_count += 1

            # Periodically emit partials for smoother UX
            if chunk_count >= 3 and full_answer:
                yield ToolPartialEvent(type="tool.partial", payload={"answer": full_answer})
                chunk_count = 0

        # Flush remaining buffer with a final parse attempt
        if buffer:
            try:
                final_result = parser.parse(buffer)
                if isinstance(final_result, dict):
                    full_answer = final_result.get("answer", full_answer)
            except Exception:
                pass
            if full_answer:
                yield ToolPartialEvent(type="tool.partial", payload={"answer": full_answer})

        # End event with structured output and observation to signal completion
        observation = {
            "summary": full_answer.strip(),
            "analysis_complete": True,
            "final_answer": full_answer.strip()
        }

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": AnswerQuestionOutput(answer=full_answer.strip(), citations=[]).model_dump(),
                "observation": observation,
            },
        )