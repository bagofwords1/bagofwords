"""Base class for edge agent data source clients."""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd

from .models import Table


class DataSourceClient:
    """The surface the tunnel dispatches onto.

    Sync methods are the implementation; the `a*` wrappers exist because the
    dispatcher prefers them, and because a blocking driver call must not run on
    the agent's event loop — that loop also carries the control subject, so
    blocking it makes cancellation structurally impossible.
    """

    # Rendered into the control plane's codegen prompts.
    relative_date_hint: str = ""

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError("test_connection not supported by this client")

    def get_schemas(self) -> list[Table]:
        raise NotImplementedError("get_schemas not supported by this client")

    def prompt_schema(self) -> str:
        raise NotImplementedError("prompt_schema not supported by this client")

    def execute_query(self, sql: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError("execute_query not supported by this client")

    @property
    def catalog_identity_available(self) -> bool:
        """Can this client crawl a catalog right now?

        False means it has no identity to crawl with, so an empty get_schemas()
        says nothing about the source and must never be mistaken for "the
        catalog is empty". Advertised to the control plane, which otherwise
        defaults it to True and would treat such a crawl as authoritative.
        """
        return True

    # -- async wrappers ------------------------------------------------------

    async def atest_connection(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.test_connection)

    async def aget_schemas(self) -> list[Table]:
        return await asyncio.to_thread(self.get_schemas)

    async def aprompt_schema(self) -> str:
        return await asyncio.to_thread(self.prompt_schema)

    async def aexecute_query(self, sql: str, **kwargs) -> pd.DataFrame:
        return await asyncio.to_thread(self.execute_query, sql, **kwargs)
