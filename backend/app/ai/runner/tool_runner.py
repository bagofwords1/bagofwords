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

    async def run(self, tool, arguments: Dict[str, Any], runtime_ctx: Dict[str, Any], emit) -> Dict[str, Any]:
        # Validate input if tool declares schema
        try:
            if getattr(tool, "input_model", None) is not None:
                arguments = tool.input_model(**arguments).model_dump()
        except PydValidationError as ve:
            return {
                "summary": f"Invalid input for '{tool.name}'",
                "error": {"type": "validation_error", "details": ve.errors()},
            }

        attempt = 0
        backoff = self.retry.backoff_ms
        while True:
            attempt += 1
            start_ts = time.monotonic()
            try:
                # envelope start
                await emit({"type": "tool.start", "payload": {"attempt": attempt}})

                # set up timeouts
                idle_timer: Optional[asyncio.Task] = None
                hard_timer: Optional[asyncio.Task] = None

                async def idle_timeout():
                    await asyncio.sleep(self.timeout.idle_timeout_s)
                    raise asyncio.TimeoutError("idle timeout")

                async def hard_timeout():
                    await asyncio.sleep(self.timeout.hard_timeout_s)
                    raise asyncio.TimeoutError("hard timeout")

                idle_timer = asyncio.create_task(idle_timeout())
                hard_timer = asyncio.create_task(hard_timeout())

                last_observation = None
                async for tevt in self._stream_with_idle(tool.run_stream(arguments, runtime_ctx), idle_timer):
                    et = tevt.get("type")
                    # reset idle timer on any event
                    if not idle_timer.cancelled():
                        idle_timer.cancel()
                    idle_timer = asyncio.create_task(idle_timeout())
                    await emit(tevt)
                    if et == "tool.error":
                        payload = tevt.get("payload") or {}
                        last_observation = {
                            "summary": f"Execution failed for '{tool.name}'",
                            "error": {"type": "runtime_error", "message": payload.get("message") or "unknown"},
                        }
                        break
                    if et == "tool.end":
                        payload = tevt.get("payload") or {}
                        last_observation = payload.get("observation")
                if idle_timer and not idle_timer.cancelled():
                    idle_timer.cancel()
                if hard_timer and not hard_timer.cancelled():
                    hard_timer.cancel()

                if last_observation is None:
                    last_observation = {"summary": f"Tool '{tool.name}' produced no result", "error": {"type": "empty_result"}}
                return last_observation

            except asyncio.TimeoutError as te:
                await emit({"type": "tool.error", "payload": {"message": str(te)}})
                err_type = "timeout_error"
            except Exception as e:
                await emit({"type": "tool.error", "payload": {"message": str(e)}})
                err_type = "runtime_error"

            # retry decision
            if attempt >= self.retry.max_attempts or err_type not in self.retry.retry_on:
                return {
                    "summary": f"Execution failed for '{tool.name}'",
                    "error": {"type": err_type, "message": "exhausted retries"},
                }

            # backoff with jitter
            sleep_ms = backoff + random.randint(0, self.retry.jitter_ms)
            await asyncio.sleep(sleep_ms / 1000.0)
            backoff = int(backoff * self.retry.backoff_multiplier)

    async def _stream_with_idle(self, aiter: AsyncIterator[dict], idle_timer: asyncio.Task):
        # Multiplex iterator with an idle timeout task
        it = aiter.__aiter__()
        while True:
            next_ev = asyncio.create_task(it.__anext__())
            done, pending = await asyncio.wait({next_ev, idle_timer}, return_when=asyncio.FIRST_COMPLETED)
            if next_ev in done:
                try:
                    ev = next_ev.result()
                except StopAsyncIteration:
                    break
                yield ev
            else:
                # idle timer fired
                next_ev.cancel()
                raise asyncio.TimeoutError("idle timeout")

