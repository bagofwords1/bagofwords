import asyncio
import inspect
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional

from app.data_sources.clients.progress import ProgressCallback


class Capability(str, Enum):
    """Capabilities a DataSourceClient may declare.

    Used by the tool layer to map a client to the agent-callable tools it can
    back. SQL-style sources declare QUERY; file/document sources declare
    LIST_FILES + READ_FILE. A single client may declare both — e.g. SharePoint
    declaring LIST_FILES + READ_FILE for general files and QUERY for promoted
    spreadsheet ranges.
    """

    QUERY = "query"
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    SEARCH_FILES = "search_files"


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

    # Capabilities this client supports. Subclasses override.
    # Defaults to {QUERY} since the historic base contract is execute_query +
    # get_schemas. File-shaped clients set their own set.
    capabilities: set = {Capability.QUERY}

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

    # Opt-in out-of-core query path (v2). Additive — existing `execute_query`
    # behavior is unchanged. Every client gets this via the generic default
    # below; SQLAlchemy clients override it to stream and bound ingest peak.
    def execute_query_lazy(self, sql: str):
        """Return a LazyFrame (out-of-core handle) instead of a DataFrame.

        Generic default: run `execute_query` and spill the result to disk. This
        gives uniform out-of-core downstream compute and disk-backed reuse, but
        ingest still materializes the full result once. Clients that can stream
        (SQLAlchemy-based) override this for a bounded ingest peak. See
        app.data_sources.clients.lazy_frame.
        """
        import pandas as pd
        from app.data_sources.clients.lazy_frame import lazy_from_dataframe

        result = self.execute_query(sql)
        if not isinstance(result, pd.DataFrame):
            result = pd.DataFrame(result)
        return lazy_from_dataframe(result)

    async def aexecute_query_lazy(self, sql: str):
        return await asyncio.to_thread(self.execute_query_lazy, sql)

    async def awarm_all(self) -> list:
        """Pre-warm any local caches needed before queries. No-op for most clients."""
        return []

    # File-shaped capabilities. Default implementations raise NotImplementedError;
    # clients that declare LIST_FILES / READ_FILE / SEARCH_FILES override.

    def list_files(self, folder_id: Optional[str] = None, recursive: bool = False) -> list:
        """List files in a folder. Returns a list of dicts:
        {id, name, path, mime_type, size, modified_at, is_folder, web_url}.
        `folder_id` of None means the connection's configured root.
        """
        raise NotImplementedError("list_files not supported by this client")

    def read_file(self, file_id: str, **kwargs) -> Any:
        """Read a file's content. Return type depends on file:
        - tabular (csv/xlsx/sheets) → pandas.DataFrame
        - text (txt/md/html) → str
        - binary (pdf/docx/other) → bytes
        Optional kwargs (sheet, range, max_bytes) are client-specific.
        """
        raise NotImplementedError("read_file not supported by this client")

    def search_files(self, query: str, **kwargs) -> list:
        """Free-text search over the connection's accessible files."""
        raise NotImplementedError("search_files not supported by this client")

    async def alist_files(self, folder_id: Optional[str] = None, recursive: bool = False) -> list:
        return await asyncio.to_thread(self.list_files, folder_id, recursive)

    async def aread_file(self, file_id: str, **kwargs) -> Any:
        return await asyncio.to_thread(self.read_file, file_id, **kwargs)

    async def asearch_files(self, query: str, **kwargs) -> list:
        return await asyncio.to_thread(self.search_files, query, **kwargs)
