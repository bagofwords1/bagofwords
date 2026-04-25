import asyncio
import inspect
from abc import ABC, abstractmethod
from typing import Optional

from app.data_sources.clients.progress import ProgressCallback


def _accepts_progress_callback(fn) -> bool:
    """Inspect a `get_schemas`-like method to see if it accepts a
    `progress_callback` kwarg. Used by the async wrapper so we don't catch a
    bare `TypeError` raised from inside the call (which would mask real bugs
    inside the client).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    params = sig.parameters
    if "progress_callback" in params:
        return True
    # Accept anything with **kwargs since it'll silently ignore the kwarg.
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class DataSourceClient(ABC):

    def __init__(self):
        pass

    def connect(self):
        pass

    @property
    @abstractmethod
    def description(self):
        pass

    @abstractmethod
    def test_connection(self):
        pass

    @abstractmethod
    def get_schemas(self):
        pass

    @abstractmethod
    def get_schema(self, table_name):
        pass

    @abstractmethod
    def prompt_schema(self):
        pass

    @abstractmethod
    def execute_query(self, **kwargs):
        pass

    # Async wrappers — offload blocking I/O to a thread so the event loop stays free.

    async def atest_connection(self):
        return await asyncio.to_thread(self.test_connection)

    async def aget_schemas(self, progress_callback: Optional[ProgressCallback] = None):
        """Forwards `progress_callback` to the sync `get_schemas` only if it
        accepts the kwarg, determined by signature introspection.

        We do NOT catch a bare `TypeError` from the call: a real `TypeError`
        from inside `get_schemas` (e.g. a bug in a client) should surface,
        not be silently retried without progress.
        """
        if progress_callback is None or not _accepts_progress_callback(self.get_schemas):
            return await asyncio.to_thread(self.get_schemas)
        return await asyncio.to_thread(self.get_schemas, progress_callback=progress_callback)

    async def aget_schema(self, table_name):
        return await asyncio.to_thread(self.get_schema, table_name)

    async def aprompt_schema(self):
        return await asyncio.to_thread(self.prompt_schema)

    async def aexecute_query(self, *args, **kwargs):
        return await asyncio.to_thread(self.execute_query, *args, **kwargs)
