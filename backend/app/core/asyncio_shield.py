"""Awaiting work to its end through the caller's cancellation."""

import asyncio


async def run_to_the_end(coro) -> tuple:
    """Await `coro` to its end however many times the caller is cancelled
    meanwhile (a disconnect, a timeout, a shutdown). Returns its finished
    task and whether the caller was cancelled meanwhile: the caller passes
    the cancellation on, or records it. The work is awaited, not left
    running on its own."""
    task = asyncio.create_task(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task, cancelled
