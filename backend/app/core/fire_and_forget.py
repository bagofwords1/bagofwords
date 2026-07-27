"""Fire-and-forget asyncio tasks that survive garbage collection.

The event loop keeps only weak references to tasks, so a task created with a
bare ``asyncio.create_task(...)`` and never awaited can be garbage-collected
mid-execution — it just stops at an await point, silently. That is fatal for
the ORM-event delivery hooks (Slack/Teams/Google Chat block senders, websocket
broadcasts), which are exactly such tasks: the longer they await (debounce
sleeps, HTTP calls), the likelier they vanish before finishing.

``spawn`` pins the task in a module-level set until it completes.
"""

import asyncio
from typing import Coroutine

_pending_tasks: set = set()


def spawn(coro: Coroutine) -> "asyncio.Task":
    """Create a background task and hold a strong reference until it's done."""
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task
