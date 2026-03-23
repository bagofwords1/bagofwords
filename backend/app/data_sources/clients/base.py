import asyncio
from abc import ABC, abstractmethod


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

    async def aget_schemas(self):
        return await asyncio.to_thread(self.get_schemas)

    async def aget_schema(self, table_name):
        return await asyncio.to_thread(self.get_schema, table_name)

    async def aprompt_schema(self):
        return await asyncio.to_thread(self.prompt_schema)

    async def aexecute_query(self, *args, **kwargs):
        return await asyncio.to_thread(self.execute_query, *args, **kwargs)
