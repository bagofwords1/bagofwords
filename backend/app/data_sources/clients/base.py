import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from app.data_sources.clients.progress import ProgressCallback


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
        """Default: forwards `progress_callback` to the sync `get_schemas`.

        Subclasses whose `get_schemas()` accepts a `progress_callback` kwarg will
        report progress; others receive the callback but ignore it. Either way
        behavior is unchanged when no callback is supplied.
        """
        if progress_callback is None:
            return await asyncio.to_thread(self.get_schemas)
        # Try the modern signature first; fall back to the legacy 0-arg form.
        try:
            return await asyncio.to_thread(self.get_schemas, progress_callback=progress_callback)
        except TypeError:
            return await asyncio.to_thread(self.get_schemas)

    async def aget_schema(self, table_name):
        return await asyncio.to_thread(self.get_schema, table_name)

    async def aprompt_schema(self):
        return await asyncio.to_thread(self.prompt_schema)

    async def aexecute_query(self, *args, **kwargs):
        return await asyncio.to_thread(self.execute_query, *args, **kwargs)
