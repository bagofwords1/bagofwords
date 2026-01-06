import asyncio
import json
import time
from typing import AsyncIterator, Optional, Callable

from app.ai.llm import LLM
from app.schemas.ai.planner import (
    PlannerDecision,
    PlannerInput,
    ToolDescriptor,
    PlannerMetrics,
    TokenUsage,
    PlannerError,
)
from app.schemas.ai.planner_events import PlannerEvent, PlannerTokenEvent, PlannerDecisionEvent
from app.ai.utils.token_counter import count_tokens
from .planner_state import PlannerState
from .prompt_builder import PromptBuilder
from partialjson.json_parser import JSONParser
from sqlalchemy.ext.asyncio import AsyncSession


class PlannerV2:
    """Single-action planner with streaming decision snapshots.

    - Streams token deltas from the LLM
    - Emits accumulating decision snapshots using typed PlannerEvent models
    - Does not call tools; only decides next action or final answer
    - Fully Pydantic-driven with structured input/output
    """

    def __init__(
        self,
        model,
        tool_catalog: list[ToolDescriptor],
        usage_session_maker: Optional[Callable[[], "AsyncSession"]] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker)
        self.tool_catalog = tool_catalog
        self.parser = JSONParser()
        self.prompt_builder = PromptBuilder()

    async def execute(
        self,
        planner_input: PlannerInput,
        sigkill_event: asyncio.Event,
    ) -> AsyncIterator[PlannerEvent]:
        # Initialize state with Pydantic input
        state = PlannerState(
            input=planner_input,
            start_time=time.monotonic()
        )
        # Build prompt using dedicated builder
        prompt = self.prompt_builder.build_prompt(planner_input)
        # Calculate prompt tokens
        prompt_tokens = count_tokens(prompt, getattr(self.llm, "model_name", None))
        completion_tokens = 0
        # Stream LLM tokens and build decision snapshots
        async for chunk in self.llm.inference_stream(
            prompt,
            usage_scope="planner",
            usage_scope_ref_id=None,
        ):
            if sigkill_event.is_set():
                break
            # SSE heartbeat/empty chunks guard [[memory:5773628]]
            if not chunk:
                continue

            state.buffer += chunk
            
            # Emit typed token event
            yield PlannerTokenEvent(type="planner.tokens", delta=chunk)
            
            # Track first token timing
            if state.first_token_time is None:
                state.first_token_time = time.monotonic()
            completion_tokens += count_tokens(chunk, getattr(self.llm, "model_name", None))

            # Try parsing partial decision (be resilient to JSON decode errors)
            try:
                raw_decision = self.parser.parse(state.buffer)
            except Exception:
                raw_decision = None
            if raw_decision:
                # Track reasoning/assistant field timing transitions
                current_reasoning = raw_decision.get("reasoning_message") or raw_decision.get("reasoning") or raw_decision.get("thought") or ""
                current_assistant = raw_decision.get("assistant_message") or raw_decision.get("message") or ""
                
                # Detect when reasoning_message first gets content
                if current_reasoning and not state._prev_reasoning and state.reasoning_start_time is None:
                    state.reasoning_start_time = time.monotonic()
                
                # Detect when assistant_message first gets content (marks reasoning end)
                if current_assistant and not state._prev_assistant and state.assistant_start_time is None:
                    state.assistant_start_time = time.monotonic()
                
                # Update previous states for next iteration
                state._prev_reasoning = current_reasoning
                state._prev_assistant = current_assistant
                
                decision = self._create_decision(raw_decision, state, False)
                yield PlannerDecisionEvent(
                    type="planner.decision.partial", 
                    data=decision
                )

        # Finalize decision with complete metrics
        try:
            final_raw = self.parser.parse(state.buffer) or {}
        except Exception:
            final_raw = {}
        final_decision = self._create_decision(
            final_raw, 
            state, 
            True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens
        )
        yield PlannerDecisionEvent(
            type="planner.decision.final", 
            data=final_decision
        )

    def _create_decision(
        self, 
        raw: dict, 
        state: PlannerState, 
        is_final: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0
    ) -> PlannerDecision:
        """Create a typed PlannerDecision from raw LLM output."""
        
        # Calculate metrics if final
        metrics = None
        if is_final and state.start_time:
            total_duration_ms = (time.monotonic() - state.start_time) * 1000.0
            first_token_ms = None
            if state.first_token_time and state.start_time:
                first_token_ms = (state.first_token_time - state.start_time) * 1000.0
            
            # Calculate actual reasoning duration:
            # From when reasoning_message first has content to when assistant_message starts
            # (or end of stream if no assistant_message yet)
            thinking_ms = None
            if state.reasoning_start_time is not None:
                reasoning_end = state.assistant_start_time or time.monotonic()
                thinking_ms = (reasoning_end - state.reasoning_start_time) * 1000.0
                
            metrics = PlannerMetrics(
                first_token_ms=round(first_token_ms, 2) if first_token_ms else None,
                thinking_ms=round(thinking_ms, 2) if thinking_ms is not None else None,
                total_duration_ms=round(total_duration_ms, 2),
                token_usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ) if is_final else None
            )
        
        # Build decision data
        # Note: Don't gate final_answer on analysis_complete during streaming - the partial JSON
        # parser may return final_answer before analysis_complete is fully parsed, and we want
        # to stream the final_answer content as it comes in.
        decision_data = {
            "analysis_complete": bool(raw.get("analysis_complete", False)),
            "plan_type": raw.get("plan_type"),  # Extract plan_type from LLM response
            "reasoning_message": raw.get("reasoning_message") or raw.get("reasoning") or raw.get("thought"),
            "assistant_message": raw.get("assistant_message") or raw.get("message"),
            "action": raw.get("action") if not raw.get("analysis_complete") else None,
            "final_answer": raw.get("final_answer"),
            "streaming_complete": is_final,
            "metrics": metrics,
        }
        
        # Validate with Pydantic, handling errors gracefully
        try:
            decision = PlannerDecision(**decision_data)
            return decision
        except Exception as e:
            # Return decision with error info
            error = PlannerError(
                code="validation_error" if is_final else "partial_validation",
                message=str(e),
                details={"raw_data": raw}
            )
            # Use defaults for invalid data
            decision_data.update({
                "analysis_complete": False,
                "error": error
            })
            # Try again with error attached - this should succeed with defaults
            try:
                return PlannerDecision(**decision_data)
            except Exception:
                # Last resort: minimal valid decision
                return PlannerDecision(
                    analysis_complete=False,
                    streaming_complete=is_final,
                    error=error,
                    metrics=metrics
                )
