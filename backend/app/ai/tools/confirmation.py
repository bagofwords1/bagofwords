"""
Confirmation registry for tool confirmations.

Allows tools to pause execution and wait for user approval via the frontend.
"""

import asyncio
import logging
from typing import Dict

logger = logging.getLogger(__name__)

PENDING_CONFIRMATIONS: Dict[str, asyncio.Future] = {}


async def wait_for_confirmation(confirmation_id: str, timeout: float = 5.0) -> dict:
    """Wait for a user confirmation response, auto-approving on timeout."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    PENDING_CONFIRMATIONS[confirmation_id] = future
    logger.info(f"Confirmation {confirmation_id}: waiting (timeout={timeout}s, pending={len(PENDING_CONFIRMATIONS)})")
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        logger.info(f"Confirmation {confirmation_id}: resolved by user — {result}")
        return result
    except asyncio.TimeoutError:
        logger.info(f"Confirmation {confirmation_id}: timed out, auto-approving")
        return {"approved": True}
    finally:
        PENDING_CONFIRMATIONS.pop(confirmation_id, None)


def resolve_confirmation(confirmation_id: str, response: dict) -> bool:
    """Resolve a pending confirmation. Returns True if found and resolved."""
    future = PENDING_CONFIRMATIONS.get(confirmation_id)
    if future is None or future.done():
        logger.warning(f"Confirmation {confirmation_id}: not found or already done (pending={list(PENDING_CONFIRMATIONS.keys())})")
        return False
    future.set_result(response)
    logger.info(f"Confirmation {confirmation_id}: resolved with {response}")
    return True
