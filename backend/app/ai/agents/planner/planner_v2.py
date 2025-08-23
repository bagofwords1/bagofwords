import asyncio
import json
import time
from typing import AsyncIterator, Dict, List, Optional

from app.ai.llm import LLM
from app.ai.context.instruction_context_builder import InstructionContextBuilder
from app.ai.schemas.planner import PlannerDecision, PlannerInput, ToolDescriptor
from app.ai.utils.token_counter import count_tokens


class _PartialJSONParser:
    """Very small tolerant JSON assembler for streaming outputs.

    Attempts to parse buffer as a single JSON object. Returns None until valid.
    On finalize, performs a last best-effort parse.
    """

    def parse(self, buffer: str) -> Optional[Dict]:
        try:
            if not buffer or buffer[0] != "{" or "}" not in buffer:
                return None
            return json.loads(buffer)
        except Exception:
            return None

    def finalize(self, buffer: str) -> Dict:
        try:
            return json.loads(buffer)
        except Exception:
            return {}


class PlannerV2:
    """Single-action planner with streaming decision snapshots.

    - Streams token deltas from the LLM
    - Emits accumulating decision snapshots (thought → action → input → final)
    - Does not call tools; only decides next action or final answer
    """

    def __init__(
        self,
        model,
        instruction_context_builder: InstructionContextBuilder,
        tool_catalog: List[Dict],
    ) -> None:
        self.llm = LLM(model)
        self.instruction_context_builder = instruction_context_builder
        self.tool_catalog = tool_catalog
        self.parser = _PartialJSONParser()

    async def execute(
        self,
        user_message: str,
        schemas_excerpt: str,
        history_summary: str,
        last_observation: Optional[Dict],
        external_platform: Optional[str],
        sigkill_event: asyncio.Event,
    ) -> AsyncIterator[Dict]:
        org_instructions = await self.instruction_context_builder.get_instructions_context()

        # Validate tool catalog via Pydantic for consistency
        try:
            _catalog_models: List[ToolDescriptor] = [ToolDescriptor(**t) for t in self.tool_catalog]
        except Exception:
            _catalog_models = []

        # Build PlannerInput for clarity (not persisted)
        _p_input = PlannerInput(
            user_message=user_message,
            schemas_excerpt=schemas_excerpt,
            history_summary=history_summary,
            last_observation=last_observation,
            external_platform=external_platform,
            tool_catalog=_catalog_models,
        )

        tools_json = json.dumps([m.model_dump() for m in _catalog_models], ensure_ascii=False)

        prompt = f"""
You are a planner. Decide ONE next action or give a final answer.

<context>
  <platform>{external_platform}</platform>
  <organization_instructions>{org_instructions}</organization_instructions>
  <goal>{user_message}</goal>
  <schemas>{schemas_excerpt}</schemas>
  <history>{history_summary}</history>
  <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>
  <tools_json>{tools_json}</tools_json>
</context>

Output rules:
- JSON ONLY, no markdown.
- Emit visible messages:
  - reasoning_message: concise rationale for the user
  - assistant_message: a short explanation before/after tool calls
- If answerable from context, set analysis_complete=true and provide final_answer (and optional reasoning_message).
- Else choose exactly one tool and minimal arguments (no data_model columns/series here).

Expected JSON, strict:
{{
  "analysis_complete": boolean,
  "reasoning_message": string | null,
  "assistant_message": string | null,
  "action": {{                   // exactly one action when not complete
    "type": "tool_call",
    "name": string,             // tool name from the catalog
    "arguments": object         // minimal, validated by the tool schema
  }} | null,
  "final_answer": string | null
}}
"""

        # approximate prompt tokens
        prompt_tokens = count_tokens(prompt, getattr(self.llm, "model_name", None))

        buffer = ""
        t_start = time.monotonic()
        first_token_ms = None
        completion_tokens = 0

        async for chunk in self.llm.inference_stream(prompt):
            if sigkill_event.is_set():
                break
            # SSE heartbeat/empty chunks guard
            if not chunk:
                continue

            buffer += chunk
            # forward token delta for UI
            yield {"type": "planner.tokens", "delta": chunk}
            if first_token_ms is None:
                first_token_ms = (time.monotonic() - t_start) * 1000.0
            completion_tokens += count_tokens(chunk, getattr(self.llm, "model_name", None))

            decision = self.parser.parse(buffer)
            if not decision or not isinstance(decision, dict):
                continue

            snapshot, err = self._to_snapshot(decision, streaming_complete=False)
            if isinstance(snapshot, dict):
                metrics = snapshot.get("metrics") or {}
                if first_token_ms is not None:
                    metrics.setdefault("first_token_ms", round(first_token_ms, 2))
                snapshot["metrics"] = metrics
                if err:
                    snapshot["error"] = err
            yield {"type": "planner.decision.partial", "data": snapshot}

        final = self.parser.finalize(buffer) or {}

        result, err = self._to_snapshot(final, streaming_complete=True)
        if isinstance(result, dict):
            metrics = result.get("metrics") or {}
            if first_token_ms is not None:
                metrics["first_token_ms"] = round(first_token_ms, 2)
            metrics["thinking_ms"] = round((time.monotonic() - t_start) * 1000.0, 2)
            metrics["token_usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            result["metrics"] = metrics
            if err:
                result["error"] = err
        yield {"type": "planner.decision.final", "data": result}

    def _to_snapshot(self, raw: Dict, streaming_complete: bool):
        """Map raw JSON to PlannerDecision dict. Returns (dict, error|None)."""
        data = {
            "analysis_complete": bool(raw.get("analysis_complete", False)),
            "reasoning_message": raw.get("reasoning_message") or raw.get("reasoning") or raw.get("thought"),
            "assistant_message": raw.get("assistant_message") or raw.get("message"),
            "action": raw.get("action") if not raw.get("analysis_complete") else None,
            "final_answer": raw.get("final_answer") if raw.get("analysis_complete") else None,
            "streaming_complete": streaming_complete,
        }
        try:
            validated = PlannerDecision(**data)
            return validated.model_dump(), None
        except Exception as e:
            err = {"type": "validation_error", "message": str(e) if streaming_complete else "partial_validation"}
            return data, err

