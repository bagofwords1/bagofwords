"""Things that happen to custom tables, for whoever needs to react.

A refresh that publishes a new artifact says so here. Nothing listens yet
beyond the log; the hook exists so that work depending on a table being fresh
(another table built from it, a cache to drop) subscribes to the moment it
happens instead of polling for it.
"""

import logging
from collections import defaultdict
from collections.abc import Callable

logger = logging.getLogger(__name__)

TABLE_PUBLISHED = "custom_table.published"

_subscribers: dict = defaultdict(list)


def subscribe(event: str, handler: Callable[[dict], None]) -> None:
    _subscribers[event].append(handler)


def unsubscribe(event: str, handler: Callable[[dict], None]) -> None:
    if handler in _subscribers.get(event, []):
        _subscribers[event].remove(handler)


def emit(event: str, payload: dict) -> None:
    """Tell subscribers. A failing subscriber is logged, never raised: what
    it reacts to has already happened."""
    for handler in list(_subscribers.get(event, [])):
        try:
            handler(payload)
        except Exception:
            logger.exception("custom table event handler failed", extra={"event": event})
