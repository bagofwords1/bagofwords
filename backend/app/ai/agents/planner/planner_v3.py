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
import json
import logging
import re
import time
from typing import AsyncIterator, Callable, List, Optional

logger = logging.getLogger(__name__)

# Matches the (possibly unterminated) value of the "title" argument inside a
# partial tool-input JSON stream — used to surface a connection tool's
# human-readable label token-by-token before the full input is parsed.
_STREAMING_TITLE_RE = re.compile(r'"title"\s*:\s*"((?:\\.|[^"\\])*)')


def _extract_streaming_title(buf: str) -> Optional[str]:
    """Best-effort extraction of the `title` string from a partial tool-input
    JSON buffer. Returns the decoded title so far, or None if not present yet."""
    m = _STREAMING_TITLE_RE.search(buf)
    if not m:
        return None
    raw = m.group(1)
    if raw.endswith("\\"):  # dangling escape mid-stream — drop it to decode cleanly
        raw = raw[:-1]
    try:
        return json.loads(f'"{raw}"') or None
    except Exception:
        return raw or None


from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLM
from app.ai.llm.types import (
    LLMStreamEvent,
    Message,
    MessageStopEvent,
    ReasoningCompleteEvent,
    ReasoningDeltaEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolSpec,
    ToolUseCompleteEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
    WebSearchCompleteEvent,
    WebSearchResultEvent,
    WebSearchStartEvent,
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
    PlannerWebSearchEvent,
)
from app.services.usage_policy_service import UsageLimitContext

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
        usage_context: Optional[UsageLimitContext] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker, usage_context=usage_context)
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
        thinking: Optional[dict] = None,
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

        # Per-tool accumulators. Anthropic supports multiple tool_use blocks
        # per response (parallel tool calls); we now collect ALL of them so
        # the agent loop can dispatch each. Indexed by tool_use_id so
        # ToolUseStart and ToolUseComplete events can match up reliably even
        # when the model emits them out of order.
        completed_actions: list[Action] = []
        action_id_index: dict[str, int] = {}  # tool_use_id -> index in completed_actions
        input_buffers: dict[str, str] = {}  # tool_use_id -> accumulated partial input JSON
        stop_reason: Optional[str] = None
        final_prompt_tokens = prompt_tokens_est
        final_completion_tokens = 0
        final_cache_read_tokens = 0
        final_cache_creation_tokens = 0

        try:
            async for evt in self.llm.inference_stream_v2(
                model_id=None,  # LLM facade resolves from self.llm.model
                messages=messages,
                system=v3_input.system_blocks or v3_input.system,
                tools=tools,
                images=v3_input.images,
                thinking=thinking,
                web_search=planner_input.web_search_enabled,
                web_search_domains=planner_input.web_search_domains or None,
                disable_parallel_tools=not planner_input.parallel_tools_enabled,
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
                        data=self._build_decision(state, completed_actions, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, ToolUseStartEvent):
                    state.saw_tool_use = True
                    if state.reasoning_end_time is None:
                        state.reasoning_end_time = time.monotonic()
                    if state.first_token_time is None:
                        state.first_token_time = time.monotonic()
                    # Append a placeholder action; ToolUseComplete will fill its arguments.
                    placeholder = Action(type="tool_call", name=evt.name, arguments={})
                    completed_actions.append(placeholder)
                    if evt.id:
                        action_id_index[evt.id] = len(completed_actions) - 1
                    yield PlannerDecisionEvent(
                        type="planner.decision.partial",
                        data=self._build_decision(state, completed_actions, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, ToolUseInputDeltaEvent):
                    # Stream the human-readable `title` argument as the model
                    # generates it, so the UI paints the tool's live label
                    # token-by-token. The frontend reads action.arguments.title
                    # off decision.partial (see reports/[id]/index.vue). The full,
                    # authoritative arguments still arrive parsed at
                    # ToolUseCompleteEvent, which overwrites this placeholder.
                    if evt.id and evt.partial_json:
                        input_buffers[evt.id] = input_buffers.get(evt.id, "") + evt.partial_json
                        idx = action_id_index.get(evt.id)
                        if idx is not None:
                            title = _extract_streaming_title(input_buffers[evt.id])
                            if title and completed_actions[idx].arguments.get("title") != title:
                                completed_actions[idx].arguments["title"] = title
                                yield PlannerDecisionEvent(
                                    type="planner.decision.partial",
                                    data=self._build_decision(state, completed_actions, stop_reason, is_final=False),
                                )
                    continue

                if isinstance(evt, ReasoningStartEvent):
                    if state.first_token_time is None:
                        state.first_token_time = time.monotonic()
                    if state.reasoning_start_time is None:
                        state.reasoning_start_time = time.monotonic()
                    continue

                if isinstance(evt, ReasoningDeltaEvent):
                    if state.first_token_time is None:
                        state.first_token_time = time.monotonic()
                    state.thinking_buffer += evt.text
                    completion_tokens += estimate_tokens_fast(evt.text)
                    # Emit a decision.partial so agent_v2's plan_streamer paints
                    # the streaming reasoning into the UI's thinking-box. The
                    # streamer reads decision.reasoning_message — see
                    # _build_decision below.
                    yield PlannerDecisionEvent(
                        type="planner.decision.partial",
                        data=self._build_decision(state, completed_actions, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, ReasoningCompleteEvent):
                    # Buffer is already filled by deltas. Mark reasoning end so
                    # metrics (thinking_ms) reflect the full block duration.
                    if state.reasoning_end_time is None:
                        state.reasoning_end_time = time.monotonic()
                    continue

                if isinstance(evt, WebSearchStartEvent):
                    # Provider-executed search begins. We record the tool on
                    # completion (when the query is available), so just keep
                    # timing here — no thinking-box text (it renders as a tool).
                    if state.first_token_time is None:
                        state.first_token_time = time.monotonic()
                    state.web_search_count += 1
                    continue

                if isinstance(evt, WebSearchCompleteEvent):
                    # Surface the finished search to the agent so it creates a
                    # tool-execution record + block (renders like other tools).
                    yield PlannerWebSearchEvent(
                        type="planner.web_search",
                        id=evt.id or f"ws_{state.web_search_count}",
                        query=evt.query,
                        queries=list(evt.queries) if evt.queries else None,
                        status=evt.status,
                    )
                    continue

                if isinstance(evt, WebSearchResultEvent):
                    # Collect citations (deduped by URL, order preserved). The
                    # model also inlines these as markdown links in its answer;
                    # we keep a copy to render a Sources footer for the audit.
                    if evt.url and not any(u == evt.url for _, u in state.web_search_citations):
                        state.web_search_citations.append((evt.title or evt.url, evt.url))
                    continue

                if isinstance(evt, ToolUseCompleteEvent):
                    finished = Action(
                        type="tool_call",
                        name=evt.name,
                        arguments=evt.input or {},
                    )
                    # Replace placeholder by tool_use_id when possible; fall
                    # back to "last placeholder for this name" or append.
                    idx = action_id_index.get(evt.id) if evt.id else None
                    if idx is not None and 0 <= idx < len(completed_actions):
                        completed_actions[idx] = finished
                    else:
                        # Best-effort: replace last placeholder with empty args + matching name
                        replaced = False
                        for i in range(len(completed_actions) - 1, -1, -1):
                            a = completed_actions[i]
                            if a.name == evt.name and not a.arguments:
                                completed_actions[i] = finished
                                replaced = True
                                break
                        if not replaced:
                            completed_actions.append(finished)
                    yield PlannerDecisionEvent(
                        type="planner.decision.partial",
                        data=self._build_decision(state, completed_actions, stop_reason, is_final=False),
                    )
                    continue

                if isinstance(evt, MessageStopEvent):
                    stop_reason = evt.stop_reason
                    continue

                if isinstance(evt, UsageEvent):
                    final_prompt_tokens = evt.input_tokens or final_prompt_tokens
                    final_completion_tokens = evt.output_tokens or final_completion_tokens
                    if evt.cache_read_tokens:
                        final_cache_read_tokens = evt.cache_read_tokens
                    if evt.cache_creation_tokens:
                        final_cache_creation_tokens = evt.cache_creation_tokens
                    continue

        except Exception as exc:
            logger.warning(
                "[planner_v3] stream loop failed: %r (thinking=%s)",
                exc, thinking,
            )
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
            completed_actions,
            stop_reason,
            is_final=True,
            prompt_tokens=final_prompt_tokens or prompt_tokens_est,
            completion_tokens=final_completion_tokens or completion_tokens,
            cache_read_tokens=final_cache_read_tokens,
            cache_creation_tokens=final_cache_creation_tokens,
        )
        yield PlannerDecisionEvent(type="planner.decision.final", data=final_decision)

    # ------------------------------------------------------------------
    # Decision construction
    # ------------------------------------------------------------------

    def _build_decision(
        self,
        state: PlannerStateV3,
        actions: list[Action],
        stop_reason: Optional[str],
        is_final: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> PlannerDecision:
        # First action drives single-action SSE compatibility (UI streaming kickoff).
        first_action: Optional[Action] = actions[0] if actions else None

        # analysis_complete: stop_reason="end_turn" AND no actions emitted
        analysis_complete = (stop_reason == "end_turn") and (not actions)

        # plan_type: derived from the FIRST tool's category. With multi-tool
        # responses, the loop iterates all of them; the plan_type tag is
        # primarily a per-block label so first-action's category is fine.
        plan_type: Optional[str] = None
        if first_action is not None:
            cat = self._tool_category.get(first_action.name)
            if cat in ("research", "action"):
                plan_type = cat
            else:
                plan_type = "action"  # default for unknown tools

        # All assistant text — pre-tool or end_turn — flows into assistant_message.
        # Distinction "is this final" lives on analysis_complete.
        # final_answer is kept on the schema for v2 compatibility but is None
        # on v3. reasoning_message carries provider-native extended-thinking
        # text (Anthropic thinking blocks) when the caller opted in via the
        # ``thinking`` config; otherwise it stays None.
        assistant_message: Optional[str] = state.reasoning_buffer.strip() or None
        reasoning_message: Optional[str] = state.thinking_buffer.strip() or None

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
                    cache_read_tokens=cache_read_tokens or None,
                    cache_creation_tokens=cache_creation_tokens or None,
                ) if is_final else None,
            )

        try:
            return PlannerDecision(
                analysis_complete=analysis_complete,
                plan_type=plan_type,
                reasoning_message=reasoning_message,  # provider-native thinking, when enabled
                assistant_message=assistant_message,
                action=first_action,
                actions=list(actions),
                final_answer=None,             # v3: collapsed into assistant_message
                streaming_complete=is_final,
                metrics=metrics,
                web_search_citations=(
                    [{"title": t, "url": u} for t, u in state.web_search_citations]
                    if is_final else []
                ),
            )
        except Exception as exc:
            logger.warning(
                "[planner_v3] decision build failed (is_final=%s): %r",
                is_final, exc,
            )
            return PlannerDecision(
                analysis_complete=False,
                streaming_complete=is_final,
                error=PlannerError(
                    code="validation_error" if is_final else "partial_validation",
                    message=str(exc),
                ),
                metrics=metrics,
            )
