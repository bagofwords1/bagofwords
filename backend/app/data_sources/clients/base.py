import asyncio
import inspect
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional

from app.data_sources.clients.progress import CancelCheck, ProgressCallback


class Capability(str, Enum):
    """Capabilities a DataSourceClient may declare.

    Used by the tool layer to map a client to the agent-callable tools it can
    back. SQL-style sources declare QUERY; file/document sources declare
    LIST_FILES + READ_FILE. A single client may declare both — e.g. SharePoint
    declaring LIST_FILES + READ_FILE for general files and QUERY for promoted
    spreadsheet ranges.

    WRITE_FILE is the only mutating file capability: a client that declares it
    can create/overwrite files back in the source (e.g. a writable network
    directory). Read-only file sources must NOT declare it. Because write is a
    trust-sensitive operation, clients gate it per-instance — the class may
    advertise WRITE_FILE (so the write tool shows in the catalog) while a
    read-only instance drops it from `self.capabilities` so runtime resolution
    rejects the call.
    """

    QUERY = "query"
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    SEARCH_FILES = "search_files"
    # Line-level content grep (regex over raw bytes → matching lines + counts).
    # Only sources that can scan file contents directly declare it (network_dir,
    # s3); provider-indexed sources (SharePoint/Drive) keep SEARCH_FILES only.
    GREP_FILES = "grep_files"
    WRITE_FILE = "write_file"


def _accepts_kwarg(fn, name: str) -> bool:
    """Inspect a `get_schemas`-like method to see if it accepts the given
    kwarg (`progress_callback`, `prior_catalog`, …). Used by the async wrapper
    so we don't catch a bare `TypeError` raised from inside the call (which
    would mask real bugs inside the client).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    params = sig.parameters
    if name in params:
        return True
    # Accept anything with **kwargs since it'll silently ignore the kwarg.
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class DataSourceClient(ABC):

    # Capabilities this client supports. Subclasses override.
    # Defaults to {QUERY} since the historic base contract is execute_query +
    # get_schemas. File-shaped clients set their own set.
    capabilities: set = {Capability.QUERY}

    # When True, listing files live from the source is cheap enough to do on
    # every list_files call (local FS walk, bounded S3 LIST) — so list_files
    # reads the live per-connection client instead of the shared, per-data-
    # source catalog cache. This is both fresher and correctly scoped to the
    # single connection (the cache unions all of a data source's connections).
    # Remote-API sources (Graph/Drive) leave this False and use the cache.
    cheap_live_listing: bool = False

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

    def query(self, *args, **kwargs):
        """Alias for execute_query.

        Model-generated code frequently reaches for the shorter, more natural
        `.query(...)` instead of `.execute_query(...)`. Aliasing here avoids a
        whole class of `AttributeError: '<Client>' object has no attribute
        'query'` failures across every connector.
        """
        return self.execute_query(*args, **kwargs)

    # Async wrappers — offload blocking I/O to a thread so the event loop stays free.

    async def atest_connection(self):
        return await asyncio.to_thread(self.test_connection)

    async def aget_schemas(
        self,
        progress_callback: Optional[ProgressCallback] = None,
        prior_catalog: "dict | None" = None,
    ):
        """Forwards `progress_callback` / `prior_catalog` to the sync
        `get_schemas` only if it accepts each kwarg, determined by signature
        introspection. `prior_catalog` is the previous run's
        `{table_name: metadata_json}` — file-source clients use it to skip
        re-extracting unchanged files (incremental indexing).

        We do NOT catch a bare `TypeError` from the call: a real `TypeError`
        from inside `get_schemas` (e.g. a bug in a client) should surface,
        not be silently retried without progress.
        """
        kwargs = {}
        if progress_callback is not None and _accepts_kwarg(self.get_schemas, "progress_callback"):
            kwargs["progress_callback"] = progress_callback
        if prior_catalog and _accepts_kwarg(self.get_schemas, "prior_catalog"):
            kwargs["prior_catalog"] = prior_catalog
        if not kwargs:
            return await asyncio.to_thread(self.get_schemas)
        return await asyncio.to_thread(self.get_schemas, **kwargs)

    async def aget_schema(self, table_name):
        return await asyncio.to_thread(self.get_schema, table_name)

    async def aprompt_schema(self):
        return await asyncio.to_thread(self.prompt_schema)

    async def aexecute_query(self, *args, **kwargs):
        return await asyncio.to_thread(self.execute_query, *args, **kwargs)

    async def aquery(self, *args, **kwargs):
        """Async alias for aexecute_query (mirrors the sync `query` alias)."""
        return await self.aexecute_query(*args, **kwargs)

    async def awarm_all(
        self,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> list:
        """Pre-warm any local caches needed before queries. No-op for most clients.

        Clients whose warm step is the expensive part of indexing (e.g. QVD →
        Parquet conversion) override this to report `progress_callback` as they
        work and to honor `cancel_check` between chunks so the run can be
        stopped. Base is a no-op that ignores both.
        """
        return []

    def index_stats(self) -> dict:
        """Extra stats to fold into the indexing row after warming — e.g.
        `source_bytes`, `file_count`, `row_count` for file-based sources so the
        UI can show how big the indexed data was. Default empty."""
        return {}

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

    def grep_files(self, pattern: str, **kwargs) -> dict:
        """Line-level content grep: match `pattern` (regex or literal) against
        each line of the connection's text files and return the matching lines
        with context, plus sweep accounting (files scanned/skipped, why the
        sweep stopped, a cursor to resume). Only clients that declare
        Capability.GREP_FILES implement this — see
        `_grep_common.run_grep_sweep` for the shared engine and return shape.
        """
        raise NotImplementedError("grep_files not supported by this client")

    def file_version(self, file_id: str) -> Optional[str]:
        """A cheap, stable version token for a file (mtime+size, ETag, …) used to
        key a content cache and detect staleness — without downloading the file.

        Returning None means "no cheap version available"; callers then skip
        caching and read live. Implementations that DO support it MUST enforce
        the connection's access scope (raise on an out-of-scope id) so a cache
        hit is still access-checked. Default: unsupported.
        """
        return None

    def write_file(
        self,
        filename: str,
        content: Any,
        folder_id: Optional[str] = None,
        overwrite: bool = False,
        **kwargs,
    ) -> dict:
        """Create (or overwrite) a file in the source. Mutating — only clients
        that declare Capability.WRITE_FILE implement this.

        Args:
            filename: Target file name (may include a relative folder path,
                e.g. "contracts/acme.pdf"). Resolved under the connection's
                configured root; path traversal outside the root is rejected.
            content: The bytes or text to write. `str` is written as UTF-8.
            folder_id: Optional destination folder (client-specific id/path).
                None means the connection's configured root.
            overwrite: If False (default) and the target exists, raise instead
                of clobbering it.

        Returns a dict describing the written file:
            {id, name, path, size, modified_at, web_url}
        """
        raise NotImplementedError("write_file not supported by this client")

    async def alist_files(self, folder_id: Optional[str] = None, recursive: bool = False) -> list:
        return await asyncio.to_thread(self.list_files, folder_id, recursive)

    async def aread_file(self, file_id: str, **kwargs) -> Any:
        return await asyncio.to_thread(self.read_file, file_id, **kwargs)

    async def asearch_files(self, query: str, **kwargs) -> list:
        return await asyncio.to_thread(self.search_files, query, **kwargs)

    async def agrep_files(self, pattern: str, **kwargs) -> dict:
        return await asyncio.to_thread(self.grep_files, pattern, **kwargs)

    async def afile_version(self, file_id: str) -> Optional[str]:
        return await asyncio.to_thread(self.file_version, file_id)

    async def awrite_file(
        self,
        filename: str,
        content: Any,
        folder_id: Optional[str] = None,
        overwrite: bool = False,
        **kwargs,
    ) -> dict:
        return await asyncio.to_thread(
            self.write_file, filename, content, folder_id, overwrite, **kwargs
        )
