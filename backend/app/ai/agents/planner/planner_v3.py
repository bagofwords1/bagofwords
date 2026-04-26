"""Planner v3 — native tool_use streaming.

Drop-in replacement for :class:`PlannerV2` that consumes structured stream events
from :meth:`LLMClient.inference_stream_v2` (tool_use blocks, text deltas, stop
reason, usage) instead of partial-JSON envelope parsing.

Public surface is intentionally identical to v2:
  - Constructor: ``PlannerV3(model, tool_catalog, usage_session_maker=...)``
  - ``async execute(planner_input, sigkill_event) -> AsyncIterator[PlannerEvent]``
  - Yields ``PlannerTokenEvent`` and ``PlannerDecisionEvent`` (partial + final)

The downstream agent loop (`agent_v2.py`) reads ``PlannerDecision`` field
accesses (``analysis_complete``, ``action.name``, ``final_answer``, etc.) and
remains unchanged.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Callable, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLM
from app.ai.llm.types import (
    LLMStreamEvent,
    Message,
    MessageStopEvent,
    TextDeltaEvent,
    ToolSpec,
    ToolUseCompleteEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)
from app.ai.utils.token_counter import estimate_tokens_fast
from app.schemas.ai.planner import (
    Action,
    PlannerDecision,
    PlannerError,
    PlannerInput,
    PlannerMetrics,
    TokenUsage,
    ToolDescriptor,
)
from app.schemas.ai.planner_events import (
    PlannerDecisionEvent,
    PlannerEvent,
    PlannerTokenEvent,
)

from .planner_state_v3 import PlannerStateV3
from .prompt_builder_v3 import PromptBuilderV3


class PlannerV3:
    """Native tool_use planner. Mirrors PlannerV2's I/O contract.

    Compared to v2:
      - No partial-JSON parsing; consumes structured events from the LLM client.
      - Output token count drops dramatically because the model emits only
        tool_use args (or a final text answer), not a JSON envelope.
      - ``analysis_complete`` is derived from the message stop_reason.
      - ``plan_type`` is derived from the chosen tool's category in the
        provided tool catalog (no model output needed).
      - ``assistant_message`` is always None on v3 — pre-tool narration goes
        into ``reasoning_message``; user-facing text goes into ``final_answer``.
    """

    def __init__(
        self,
        model,
        tool_catalog: List[ToolDescriptor],
        usage_session_maker: Optional[Callable[[], "AsyncSession"]] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker)
        self.tool_catalog = tool_catalog
        self.prompt_builder = PromptBuilderV3()
        # Build a name -> category lookup for plan_type derivation
        self._tool_category: dict[str, Optional[str]] = {
            t.name: (t.category or ("research" if t.research_accessible else "action"))
            for t in (tool_catalog or [])
        }

    async def execute(
        self,
        planner_input: PlannerInput,
        sigkill_event: asyncio.Event,
    ) -> AsyncIterator[PlannerEvent]:
        v3_input = self.prompt_builder.build(planner_input)

        state = PlannerStateV3(
            input=v3_input,
            start_time=time.monotonic(),
        )

        # Estimate prompt tokens up front (cheap, used for telemetry only)
        prompt_text = (
            v3_input.system
            + "\n"
            + (v3_input.messages[0]["content"] if v3_input.messages else "")
        )
        prompt_tokens_est = estimate_tokens_fast(prompt_text)
        completion_tokens = 0

        # Reify Pydantic Message dicts back to dataclass Message for client
        messages = [Message(role=m["role"], content=m["content"]) for m in v3_input.messages]
        tools = [
            ToolSpec(
                name=t["name"],
                description=t["description"],
                input_schema=t["input_schema"],
            )
            for t in v3_input.tools
        ]

        # Per-tool accumulators (Anthropic supports multiple tool_use blocks per
        # response; v3 only consumes the first since the planner contract is
        # one action per turn).
        completed_action: Optional[Action] = None
        stop_reason: Optional[str] = None
        final_prompt_tokens = prompt_tokens_est
        final_completion_tokens = 0

        try:
            async for evt in self.llm.inference_stream_v2(
                model_id=None,  # LLM facade resolves from self.llm.model
                messages=messages,
                system=v3_input.system,
                tools=tools,
                images=v3_input.images,
                usage_scope="planner",
                usage_scope_ref_id=None,
                prompt_tokens_estimate=prompt_tokens_est,
            ):
                if sigkill_event.is_set():
                    break

                if isinstance(evt, TextDeltaEvent):
                    if state.first_token_time is None:
                        state.first_token_time = time.monotonic()
                    if state.saw_tool_use:
                        # Anthropic occasionally emits text after a tool_use block;
                        # ignore (we already have an action chosen).
                        continue
                    if state.reasoning_start_time is None:
                        state.reasoning_start_time = time.monotonic()
                    state.reasoning_buffer += evt.text
                    state.final_buffer += evt.text  # collapses if no tool follows
                    completion_tokens += estimate_tokens_fast(evt.text)
                    yield PlannerTokenEvent(type="planner.tokens", delta=evt.text)
                    yield PlannerDecisionEvent(
                        type="planner.decision.partial",
                        data=self._build_decision(state, completed_action, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, ToolUseStartEvent):
                    state.saw_tool_use = True
                    if state.reasoning_end_time is None:
                        state.reasoning_end_time = time.monotonic()
                    if state.first_token_time is None:
                        state.first_token_time = time.monotonic()
                    # Best-effort partial: we don't have args yet but we know the tool name
                    completed_action = Action(type="tool_call", name=evt.name, arguments={})
                    yield PlannerDecisionEvent(
                        type="planner.decision.partial",
                        data=self._build_decision(state, completed_action, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, ToolUseInputDeltaEvent):
                    # Not parsed yet — defer to ToolUseCompleteEvent
                    continue

                if isinstance(evt, ToolUseCompleteEvent):
                    completed_action = Action(
                        type="tool_call",
                        name=evt.name,
                        arguments=evt.input or {},
                    )
                    yield PlannerDecisionEvent(
                        type="planner.decision.partial",
                        data=self._build_decision(state, completed_action, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, MessageStopEvent):
                    stop_reason = evt.stop_reason
                    continue

                if isinstance(evt, UsageEvent):
                    final_prompt_tokens = evt.input_tokens or final_prompt_tokens
                    final_completion_tokens = evt.output_tokens or final_completion_tokens
                    continue

        except Exception as exc:
            err = PlannerError(code="stream_error", message=str(exc))
            decision = PlannerDecision(
                analysis_complete=False,
                streaming_complete=True,
                error=err,
            )
            yield PlannerDecisionEvent(type="planner.decision.final", data=decision)
            return

        # If reasoning never ended (no tool call), close it now
        if state.reasoning_start_time and state.reasoning_end_time is None:
            state.reasoning_end_time = time.monotonic()

        # Emit final decision with metrics
        final_decision = self._build_decision(
            state,
            completed_action,
            stop_reason,
            is_final=True,
            prompt_tokens=final_prompt_tokens or prompt_tokens_est,
            completion_tokens=final_completion_tokens or completion_tokens,
        )
        yield PlannerDecisionEvent(type="planner.decision.final", data=final_decision)

    # ------------------------------------------------------------------
    # Decision construction
    # ------------------------------------------------------------------

    def _build_decision(
        self,
        state: PlannerStateV3,
        action: Optional[Action],
        stop_reason: Optional[str],
        is_final: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> PlannerDecision:
        # analysis_complete: stop_reason="end_turn" AND no action
        analysis_complete = (stop_reason == "end_turn") and (action is None)

        # plan_type: derived from the chosen tool's category, or None when finishing
        plan_type: Optional[str] = None
        if action is not None:
            cat = self._tool_category.get(action.name)
            if cat in ("research", "action"):
                plan_type = cat
            else:
                plan_type = "action"  # default for unknown tools

        # reasoning_message: pre-tool text accumulated before tool_use_start
        reasoning_message: Optional[str] = state.reasoning_buffer.strip() or None
        if action is None and analysis_complete:
            # When the model didn't call a tool, the buffered text IS the final answer,
            # not "reasoning". Suppress reasoning to avoid duplicating into both fields.
            reasoning_message = None

        # final_answer: text accumulated when no tool call follows
        final_answer: Optional[str] = None
        if action is None and analysis_complete:
            final_answer = state.final_buffer.strip() or None

        metrics: Optional[PlannerMetrics] = None
        if is_final and state.start_time is not None:
            now = time.monotonic()
            total_ms = (now - state.start_time) * 1000.0
            first_token_ms = None
            if state.first_token_time:
                first_token_ms = (state.first_token_time - state.start_time) * 1000.0
            thinking_ms = None
            if state.reasoning_start_time and state.reasoning_end_time:
                thinking_ms = (state.reasoning_end_time - state.reasoning_start_time) * 1000.0
            metrics = PlannerMetrics(
                first_token_ms=round(first_token_ms, 2) if first_token_ms else None,
                thinking_ms=round(thinking_ms, 2) if thinking_ms is not None else None,
                total_duration_ms=round(total_ms, 2),
                token_usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ) if is_final else None,
            )

        try:
            return PlannerDecision(
                analysis_complete=analysis_complete,
                plan_type=plan_type,
                reasoning_message=reasoning_message,
                assistant_message=None,  # v3: always None (deprecated field)
                action=action,
                final_answer=final_answer,
                streaming_complete=is_final,
                metrics=metrics,
            )
        except Exception as exc:
            return PlannerDecision(
                analysis_complete=False,
                streaming_complete=is_final,
                error=PlannerError(
                    code="validation_error" if is_final else "partial_validation",
                    message=str(exc),
                ),
                metrics=metrics,
            )
