"""
Confirmation registry for tool confirmations.

Allows tools to pause execution and wait for user approval via the frontend.
"""

import asyncio
from typing import Dict


PENDING_CONFIRMATIONS: Dict[str, asyncio.Future] = {}


async def wait_for_confirmation(confirmation_id: str, timeout: float = 5.0) -> dict:
    """Wait for a user confirmation response, auto-approving on timeout."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    PENDING_CONFIRMATIONS[confirmation_id] = future
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        return {"approved": True}
    finally:
        PENDING_CONFIRMATIONS.pop(confirmation_id, None)


def resolve_confirmation(confirmation_id: str, response: dict) -> bool:
    """Resolve a pending confirmation. Returns True if found and resolved."""
    future = PENDING_CONFIRMATIONS.get(confirmation_id)
    if future is None or future.done():
        return False
    future.set_result(response)
    return True
