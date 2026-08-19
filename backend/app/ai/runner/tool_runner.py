import asyncio
import hashlib
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any

from pydantic import ValidationError as PydValidationError

from app.ai.runner.policies import RetryPolicy, TimeoutPolicy

# Error-message fragments that mark a failure as non-recoverable: replaying the
# identical call cannot succeed (the prompt is still too big, the account is
# still out of credit), so retrying only doubles cost and latency.
NON_RECOVERABLE_ERROR_PATTERNS = (
    # Context window exceeded
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "prompt is too long",
    "too many tokens",
    "input is too long",
    "request too large",
    # Provider credit / quota exhaustion
    "credit balance",
    "insufficient credit",
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "payment required",
)


def is_non_recoverable_error(message: str | None) -> bool:
    m = (message or "").lower()
    return any(p in m for p in NON_RECOVERABLE_ERROR_PATTERNS)


class ToolRunner:
    """Executes a tool with retries, timeouts, and structured observations.

    Usage:
      runner = ToolRunner(retry_policy, timeout_policy)
      observation = await runner.run(tool, arguments, runtime_ctx, emit)

    emit: async callback(event: dict) to forward streaming events
    Returns: observation dict
    """

    def __init__(self, retry: RetryPolicy | None = None, timeout: TimeoutPolicy | None = None) -> None:
        self.retry = retry or RetryPolicy()
        self.timeout = timeout or TimeoutPolicy()
        # A validation streak is the same tool + normalized arguments in
        # adjacent planner rounds. Historical failures elsewhere in a long run
        # are telemetry, not extra strikes against the current approach.
        self.validation_failure_streaks: dict[str, tuple[str, int, int]] = {}
        self._validation_call_index = 0
        self.max_validation_failures = 3

    def _validation_round(self, runtime_ctx: dict[str, Any]) -> tuple[str, int]:
        raw_round = runtime_ctx.get("planner_round_index")
        if raw_round is not None:
            try:
                return str(runtime_ctx.get("planner_phase") or "main"), int(raw_round)
            except (TypeError, ValueError):
                pass
        self._validation_call_index += 1
        return "tool_call", self._validation_call_index

    @staticmethod
    def _validation_signature(tool_name: str, arguments: dict[str, Any]) -> str:
        normalized = json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return f"{tool_name}:{digest}"

    def _record_validation_failure(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        round_token: tuple[str, int],
    ) -> int:
        phase, round_index = round_token
        for signature, (prior_phase, prior_round, _) in list(
            self.validation_failure_streaks.items()
        ):
            if prior_phase != phase or prior_round < round_index - 1:
                self.validation_failure_streaks.pop(signature, None)

        signature = self._validation_signature(tool_name, arguments)
        previous = self.validation_failure_streaks.get(signature)
        if previous is None or previous[0] != phase:
            failures = 1
        elif previous[1] == round_index:
            # A parallel duplicate in one planner batch is one failed round.
            failures = previous[2]
        elif previous[1] == round_index - 1:
            failures = previous[2] + 1
        else:
            failures = 1
        self.validation_failure_streaks[signature] = (phase, round_index, failures)
        return failures

    async def run(
        self,
        tool,
        arguments: dict[str, Any],
        runtime_ctx: dict[str, Any],
        emit,
    ) -> dict[str, Any]:
        validation_round = self._validation_round(runtime_ctx)
        # Runtime mode access control - check if tool is allowed in current mode.
        # Prefer `spec` (class-cached metadata) over `metadata` (rebuilds tool
        # schemas on every call) — this runs for every tool execution.
        _meta = getattr(tool, 'spec', None) or getattr(tool, 'metadata', None)
        tool_allowed_modes = getattr(_meta, 'allowed_modes', None) if _meta is not None else None
        current_mode = runtime_ctx.get('mode') or 'chat'  # Default to 'chat' if mode is None
        if tool_allowed_modes is not None and current_mode not in tool_allowed_modes:
            return {
                "summary": f"Tool '{tool.name}' is not available in '{current_mode}' mode",
                "error": {
                    "type": "mode_not_allowed",
                    "message": f"Tool '{tool.name}' is only available in modes: {tool_allowed_modes}",
                    "allowed_modes": tool_allowed_modes,
                    "current_mode": current_mode,
                },
            }

        # Arguments that failed JSON parsing arrive as the client layer's
        # _unparsable marker. Running those through schema validation reports
        # "field required" for every field — pointing the model at a problem
        # it doesn't have. Short-circuit with the real one: broken JSON.
        from app.ai.llm.toolcall_args import ERROR_KEY, RAW_KEY, UNPARSABLE_KEY
        if isinstance(arguments, dict) and arguments.get(UNPARSABLE_KEY):
            failures = self._record_validation_failure(
                tool.name, arguments, validation_round
            )
            json_error = arguments.get(ERROR_KEY) or "invalid JSON"
            raw_tail = str(arguments.get(RAW_KEY) or "")[-200:]
            error_message = (
                f"The tool-call arguments were not valid JSON and could not be "
                f"recovered ({json_error}). Re-emit the call as ONE valid JSON "
                f'object. Escape any double quote inside a string value as \\" '
                f'— including in-word quotes in Hebrew/Arabic text (ארה"ב → '
                f'ארה\\"ב) — and do not wrap the object in prose or code fences.'
            )
            observation = {
                "summary": f"Malformed tool-call arguments for '{tool.name}' (attempt {failures}/{self.max_validation_failures})",
                "success": False,
                "error": {
                    "type": "malformed_tool_arguments",
                    "message": error_message,
                    "json_error": json_error,
                    "raw_tail": raw_tail,
                },
            }
            if failures >= self.max_validation_failures:
                observation["retry_exhausted"] = True
                observation["suggested_action"] = "change_strategy"
            return {
                "observation": observation,
                "output": {"success": False, "error_message": error_message},
            }

        # Validate input if tool declares schema
        try:
            if getattr(tool, "input_model", None) is not None:
                arguments = tool.input_model(**arguments).model_dump()
        except PydValidationError as ve:
            failures = self._record_validation_failure(
                tool.name, arguments, validation_round
            )

            # Build detailed error message
            error_details = ve.errors()
            field_errors = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in error_details]
            error_message = f"Validation failed: {'; '.join(field_errors)}"

            # The "output" half lands in the tool_execution's result_json —
            # without success=False there, the UI renders the failed call as
            # a completed tool ("CSV written ✓" on a call that never ran).
            failed_output = {"success": False, "error_message": error_message}

            if failures >= self.max_validation_failures:
                return {
                    "observation": {
                        "summary": f"Tool '{tool.name}' failed validation {self.max_validation_failures} times and cannot be executed",
                        "success": False,
                        "error": {
                            "type": "repeated_validation_error",
                            "message": f"Repeated validation failures: {'; '.join(field_errors)}",
                            "details": error_details,
                            "suggestion": "Check tool schema requirements and fix input format"
                        },
                        "retry_exhausted": True,
                        "suggested_action": "change_strategy",
                    },
                    "output": failed_output,
                }

            return {
                "observation": {
                    "summary": f"Invalid input for '{tool.name}' (attempt {failures}/{self.max_validation_failures})",
                    "success": False,
                    "error": {
                        "type": "validation_error",
                        "details": error_details,
                        "field_errors": field_errors,
                        "message": error_message
                    },
                },
                "output": failed_output,
            }

        attempt = 0
        backoff = self.retry.backoff_ms
        last_error = None
        last_error_type = None
        _run_start = time.monotonic()
        _ts_first_event: float | None = None
        _hard_timeout_s = max(
            self.timeout.hard_timeout_s,
            self.timeout.start_timeout_s + self.timeout.idle_timeout_s,
        )
        _hard_deadline = _run_start + _hard_timeout_s

        while True:
            attempt += 1
            hard_timeout_reached = False
            try:
                # envelope start
                await emit({"type": "tool.start", "payload": {"attempt": attempt}})

                last_observation = None
                last_output = None
                _stage_timestamps: list[tuple[str, float]] = []
                remaining_hard_timeout = _hard_deadline - time.monotonic()
                if remaining_hard_timeout <= 0:
                    raise TimeoutError("hard timeout")
                try:
                    async with asyncio.timeout(remaining_hard_timeout):
                        async for tevt in self._stream_with_idle(
                            tool.run_stream(arguments, runtime_ctx),
                            first_event_timeout_s=self.timeout.start_timeout_s,
                            idle_timeout_s=self.timeout.idle_timeout_s,
                        ):
                            # Handle both Pydantic events and dict events
                            if hasattr(tevt, 'type'):
                                # Pydantic model
                                et = tevt.type
                                payload = tevt.payload if hasattr(tevt, 'payload') else {}
                                # Convert to dict for emission
                                emit_event = tevt.model_dump() if hasattr(tevt, 'model_dump') else tevt
                            else:
                                # Dict event
                                et = tevt.get("type")
                                payload = tevt.get("payload") or {}
                                emit_event = tevt

                            await emit(emit_event)
                            if _ts_first_event is None and et != "tool.start":
                                _ts_first_event = time.monotonic()
                            if et == "tool.progress" and payload.get("timing", True):
                                _stage_timestamps.append((payload.get("stage", "unknown"), time.monotonic()))

                            if et == "tool.error":
                                last_observation = {
                                    "summary": f"Execution failed for '{tool.name}'",
                                    "error": {"type": "runtime_error", "message": payload.get("message") or "unknown"},
                                }
                                break
                            if et == "tool.end":
                                last_observation = payload.get("observation")
                                last_output = payload.get("output")
                except TimeoutError as timeout_error:
                    # asyncio.timeout() has an empty message. Idle timeouts
                    # already carry their own label and remain distinguishable.
                    if not str(timeout_error):
                        raise TimeoutError("hard timeout") from timeout_error
                    raise

                # Output schema validation (generic). Failure envelopes
                # (success=False) deliberately omit success-only fields —
                # validating them against the success model would replace the
                # tool's real error observation (e.g. DATA_GAP remediation,
                # unmatched-diff details) with a generic validation error, so
                # they pass through untouched.
                _is_failure_envelope = isinstance(last_output, dict) and last_output.get("success") is False
                if getattr(tool, "output_model", None) is not None and last_output is not None and not _is_failure_envelope:
                    try:
                        # Validate using the tool-declared output model
                        tool.output_model(**last_output)
                    except PydValidationError as ve:
                        error_details = ve.errors()
                        field_errors = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in error_details]
                        return {
                            "summary": f"Tool '{tool.name}' produced invalid output",
                            "error": {
                                "type": "output_validation_error",
                                "message": "Output validation failed",
                                "details": error_details,
                                "field_errors": field_errors,
                                "suggestion": "Fix output to match declared output schema",
                            },
                        }

                if last_observation is None:
                    last_observation = {"summary": f"Tool '{tool.name}' produced no result", "error": {"type": "empty_result"}}

                # Assemble sub_timings from phase markers and any timing data in the output
                _total_ms = round((time.monotonic() - _run_start) * 1000.0, 1)
                _setup_ms = round((_ts_first_event - _run_start) * 1000.0, 1) if _ts_first_event else None
                sub_timings: dict = {
                    "total_ms": _total_ms,
                    "setup_ms": _setup_ms,
                    "retry_count": attempt - 1,
                }
                if _stage_timestamps:
                    _run_end = time.monotonic()
                    stages = []
                    for i, (stage, ts) in enumerate(_stage_timestamps):
                        end = _stage_timestamps[i + 1][1] if i + 1 < len(_stage_timestamps) else _run_end
                        stages.append({"stage": stage, "ms": round((end - ts) * 1000.0, 1)})
                    sub_timings["stages"] = stages

                if last_output and isinstance(last_output, dict):
                    if last_output.get("query_timings"):
                        sub_timings["queries"] = last_output["query_timings"]
                    if last_output.get("codegen_ms") is not None:
                        sub_timings["codegen_ms"] = last_output["codegen_ms"]
                    if last_output.get("execution_ms") is not None:
                        sub_timings["execution_ms"] = last_output["execution_ms"]

                # Return both observation, output, and sub_timings
                return {"observation": last_observation, "output": last_output, "sub_timings": sub_timings}

            except TimeoutError as te:
                timeout_message = str(te) or "hard timeout"
                hard_timeout_reached = timeout_message == "hard timeout"
                await emit({"type": "tool.error", "payload": {"message": timeout_message}})
                err_type = "timeout_error"
                last_error = timeout_message
                last_error_type = err_type
            except Exception as e:
                await emit({"type": "tool.error", "payload": {"message": str(e)}})
                err_type = "runtime_error"
                last_error = str(e)
                last_error_type = err_type

            # retry decision — non-recoverable errors (context window, provider
            # credit) are never retried: the identical call cannot succeed.
            _non_recoverable = is_non_recoverable_error(last_error)
            if hard_timeout_reached or attempt >= self.retry.max_attempts or err_type not in self.retry.retry_on or _non_recoverable:
                # Preserve detailed error information for better debugging
                error_details = {
                    "type": last_error_type or err_type,
                    "message": last_error or "unknown error",
                    "attempts": attempt,
                    "max_attempts": self.retry.max_attempts,
                    "suggestion": "Tool execution failed repeatedly. Check tool implementation or input arguments."
                }
                if _non_recoverable:
                    error_details["non_recoverable"] = True
                    error_details["suggestion"] = (
                        "This error cannot succeed on retry (context window or provider "
                        "account limit). Reduce the input size or resolve the account issue "
                        "instead of calling the tool again with the same arguments."
                    )

                return {
                    "summary": f"Execution failed for '{tool.name}' after {attempt} attempts",
                    "success": False,
                    "error": error_details,
                    "retry_exhausted": True,
                    "suggested_action": "change_strategy",
                }

            # backoff with jitter
            sleep_ms = backoff + random.randint(0, self.retry.jitter_ms)
            await asyncio.sleep(sleep_ms / 1000.0)
            backoff = int(backoff * self.retry.backoff_multiplier)

    async def _stream_with_idle(
        self,
        aiter: AsyncIterator[dict],
        first_event_timeout_s: int,
        idle_timeout_s: int,
    ):
        # Await first event with first_event_timeout_s
        it = aiter.__aiter__()
        next_ev = asyncio.create_task(it.__anext__())
        try:
            ev = await asyncio.wait_for(next_ev, timeout=first_event_timeout_s)
        except TimeoutError:
            next_ev.cancel()
            raise TimeoutError("idle timeout")
        except StopAsyncIteration:
            return
        else:
            yield ev
        finally:
            if not next_ev.done():
                next_ev.cancel()
                await asyncio.gather(next_ev, return_exceptions=True)

        # After first event, use recurring idle_timeout_s
        while True:
            idle_timer = asyncio.create_task(asyncio.sleep(idle_timeout_s))
            next_ev = asyncio.create_task(it.__anext__())
            try:
                done, _ = await asyncio.wait(
                    {next_ev, idle_timer}, return_when=asyncio.FIRST_COMPLETED
                )
                if next_ev in done:
                    try:
                        ev = next_ev.result()
                    except StopAsyncIteration:
                        break
                    yield ev
                else:
                    raise TimeoutError("idle timeout")
            finally:
                for task in (next_ev, idle_timer):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(next_ev, idle_timer, return_exceptions=True)
