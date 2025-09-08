import asyncio
from typing import AsyncIterator, Dict, Any, Type, Optional
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import AnswerQuestionInput, AnswerQuestionOutput
from app.ai.tools.schemas.events import ToolEvent, ToolStartEvent, ToolProgressEvent, ToolPartialEvent, ToolStdoutEvent, ToolEndEvent
from app.ai.llm import LLM
import json


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
            is_active=False,
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
                history_summary = await context_hub.get_history_summary(context_hub.observation_builder.to_dict() if getattr(context_hub, "observation_builder", None) else None)
            except Exception:
                history_summary = ""

        # Build answer prompt (grounded; no fabrication)
        header = f"""
You are a helpful data analyst. Answer the user's question concisely using ONLY the provided context.
If the context is insufficient, ask for a brief, targeted clarification.

Context:
  <platform>{platform}</platform>
  {instructions_context}
  {schemas_excerpt}
  {resources_context if resources_context else 'No metadata resources available'}
  {history_summary}
  {messages_context if messages_context else 'No detailed conversation history available'}
  <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>
  <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

Question:
{data.question}

Guidance:
- Be kind, brief, and direct. Do not repeat the question.
- Use simple markdown for formatting. No JSON in your output.
- Do not expose schemas/messages explicitly; just answer.
- If you reference a table relationship or schema, keep it human-readable.
"""

        # Stream from LLM and forward partials
        llm = LLM(runtime_ctx.get("model"))
        buffer = ""
        chunk_count = 0
        full_answer = ""

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "llm_call_start"})

        async for chunk in llm.inference_stream(header):
            # Guard against empty SSE heartbeats
            if not chunk:
                continue
            buffer += chunk
            full_answer += chunk
            chunk_count += 1

            # Periodically emit partials for smoother UX
            if chunk_count >= 5:
                yield ToolPartialEvent(type="tool.partial", payload={"delta": buffer})
                buffer = ""
                chunk_count = 0

        # Flush remaining buffer
        if buffer:
            yield ToolPartialEvent(type="tool.partial", payload={"delta": buffer})

        # End event with structured output and observation to signal completion
        observation = {
            "summary": "Answered user question from available context.",
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