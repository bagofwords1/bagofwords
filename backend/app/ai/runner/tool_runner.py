import asyncio
import random
import time
from typing import Any, Dict, AsyncIterator, Optional

from pydantic import ValidationError as PydValidationError

from app.ai.runner.policies import RetryPolicy, TimeoutPolicy


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
        self.validation_failure_count = 0  # Track validation failures
        self.max_validation_failures = 2   # Max before giving up

    async def run(self, tool, arguments: Dict[str, Any], runtime_ctx: Dict[str, Any], emit) -> Dict[str, Any]:
        # Runtime mode access control - check if tool is allowed in current mode
        tool_allowed_modes = getattr(tool.metadata, 'allowed_modes', None) if hasattr(tool, 'metadata') else None
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

        # Validate input if tool declares schema
        try:
            if getattr(tool, "input_model", None) is not None:
                arguments = tool.input_model(**arguments).model_dump()
        except PydValidationError as ve:
            self.validation_failure_count += 1
            
            # Build detailed error message
            error_details = ve.errors()
            field_errors = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in error_details]
            
            if self.validation_failure_count >= self.max_validation_failures:
                return {
                    "summary": f"Tool '{tool.name}' failed validation {self.max_validation_failures} times and cannot be executed",
                    "error": {
                        "type": "repeated_validation_error", 
                        "message": f"Repeated validation failures: {'; '.join(field_errors)}",
                        "details": error_details,
                        "suggestion": "Check tool schema requirements and fix input format"
                    },
                    "analysis_complete": True,
                    "final_answer": f"Unable to complete task due to repeated tool validation errors: {'; '.join(field_errors)}"
                }
            
            return {
                "summary": f"Invalid input for '{tool.name}' (attempt {self.validation_failure_count}/{self.max_validation_failures})",
                "error": {
                    "type": "validation_error", 
                    "details": error_details,
                    "field_errors": field_errors,
                    "message": f"Validation failed: {'; '.join(field_errors)}"
                },
            }

        attempt = 0
        backoff = self.retry.backoff_ms
        last_error = None
        last_error_type = None
        _run_start = time.monotonic()
        _ts_first_event: float | None = None

        while True:
            attempt += 1
            start_ts = time.monotonic()
            try:
                # envelope start
                await emit({"type": "tool.start", "payload": {"attempt": attempt}})

                # set up timeouts
                idle_timer: Optional[asyncio.Task] = None
                hard_timer: Optional[asyncio.Task] = None

                async def idle_timeout(duration_s: int):
                    await asyncio.sleep(duration_s)
                    raise asyncio.TimeoutError("idle timeout")

                async def hard_timeout():
                    await asyncio.sleep(max(self.timeout.hard_timeout_s, self.timeout.start_timeout_s + self.timeout.idle_timeout_s))
                    raise asyncio.TimeoutError("hard timeout")

                # Start hard timeout watchdog
                hard_timer = asyncio.create_task(hard_timeout())

                last_observation = None
                last_output = None
                _stage_timestamps: list[tuple[str, float]] = []
                try:
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
                finally:
                    if hard_timer and not hard_timer.cancelled():
                        hard_timer.cancel()

                # Reset validation failure count on successful execution
                self.validation_failure_count = 0
                # Output schema validation (generic)
                if getattr(tool, "output_model", None) is not None and last_output is not None:
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

            except asyncio.TimeoutError as te:
                await emit({"type": "tool.error", "payload": {"message": str(te)}})
                err_type = "timeout_error"
                last_error = str(te)
                last_error_type = err_type
            except Exception as e:
                await emit({"type": "tool.error", "payload": {"message": str(e)}})
                err_type = "runtime_error"
                last_error = str(e)
                last_error_type = err_type

            # retry decision
            if attempt >= self.retry.max_attempts or err_type not in self.retry.retry_on:
                # Preserve detailed error information for better debugging
                error_details = {
                    "type": last_error_type or err_type,
                    "message": last_error or "unknown error",
                    "attempts": attempt,
                    "max_attempts": self.retry.max_attempts,
                    "suggestion": "Tool execution failed repeatedly. Check tool implementation or input arguments."
                }
                
                return {
                    "summary": f"Execution failed for '{tool.name}' after {attempt} attempts",
                    "error": error_details,
                    "analysis_complete": True,
                    "final_answer": f"Unable to execute {tool.name} - {last_error or 'repeated failures'}"
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
        except asyncio.TimeoutError:
            next_ev.cancel()
            raise asyncio.TimeoutError("idle timeout")
        except StopAsyncIteration:
            return
        else:
            yield ev

        # After first event, use recurring idle_timeout_s
        while True:
            idle_timer = asyncio.create_task(asyncio.sleep(idle_timeout_s))
            next_ev = asyncio.create_task(it.__anext__())
            done, pending = await asyncio.wait({next_ev, idle_timer}, return_when=asyncio.FIRST_COMPLETED)
            if next_ev in done:
                idle_timer.cancel()
                try:
                    ev = next_ev.result()
                except StopAsyncIteration:
                    break
                yield ev
            else:
                next_ev.cancel()
                raise asyncio.TimeoutError("idle timeout")

