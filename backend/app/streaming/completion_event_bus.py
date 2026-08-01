"""In-process completion event bus (same-worker fast path).

Successor to the old ``websocket_manager``: the WebSocket layer it carried is
gone (it was unauthenticated, and its per-worker connection dict silently
dropped events for clients connected to a different uvicorn worker — clients
now get live updates from the DB-backed activity stream instead), but the
in-process handler fan-out stays. ORM event hooks publish completion/step/
widget updates here, and a same-worker agent run subscribes to catch steering
messages instantly; the agent's per-iteration DB poll remains the
cross-worker fallback, exactly as before.
"""
from typing import Callable, List


class CompletionEventBus:
    def __init__(self):
        self.message_handlers: List[Callable] = []

    async def broadcast_to_report(self, report_id: str, message: str):
        """Fan a serialized event out to this worker's registered handlers.

        Signature kept from the WebSocket era so the ORM hooks and agent
        call sites didn't have to change; ``report_id`` is inside ``message``
        and handlers filter for themselves.
        """
        for handler in list(self.message_handlers):
            try:
                await handler(message)
            except Exception as e:
                print(f"Error notifying message handler: {e}")

    def add_handler(self, handler: Callable):
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    def remove_handler(self, handler: Callable):
        if handler in self.message_handlers:
            self.message_handlers.remove(handler)


# Import-compatible instance name: existing call sites keep reading
# ``websocket_manager.broadcast_to_report(...)`` semantics through this alias.
websocket_manager = CompletionEventBus()
