"""Browser data-source client.

Backs the `browser` connection type. Unlike SQL/file clients it has no catalog
to index and no query language: it is a *capability provider*. Its only jobs are

  1. declare the BROWSER capability, so the agent's tool catalog includes the
     browser tools when a browser connection is attached to the report, and
  2. carry the `url_patterns` allowlist those tools enforce at runtime.

`test_connection` validates the allowlist itself (there is no server to reach at
config time — the "server" is whatever public/internal sites the patterns name).
The pattern grammar and its host-shape rule live in `_browser_common` so the
tool layer and this validation agree on exactly one definition.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.data_sources.clients.base import Capability, DataSourceClient


class BrowserClient(DataSourceClient):
    """A set of allowlisted URLs the agent may browse. No auth, no catalog."""

    capabilities = {Capability.BROWSER}

    def __init__(self, url_patterns: List[str] | None = None,
                 allow_downloads: bool = True, **kwargs: Any) -> None:
        super().__init__()
        self.url_patterns = list(url_patterns or [])
        self.allow_downloads = bool(allow_downloads)

    @property
    def description(self) -> str:
        n = len(self.url_patterns)
        return f"Browser connection scoped to {n} URL pattern(s)."

    def test_connection(self) -> Dict[str, Any]:
        # Validate the allowlist. Reachability is not testable here — the patterns
        # may name internal hosts only reachable at run time — so a well-formed,
        # non-empty allowlist is a pass.
        from app.ai.tools.implementations._browser_common import validate_url_pattern

        if not self.url_patterns:
            return {"success": False, "message": "Add at least one allowed URL pattern."}
        bad: List[str] = []
        for pat in self.url_patterns:
            err = validate_url_pattern(pat)
            if err:
                bad.append(f"{pat} — {err}")
        if bad:
            return {"success": False, "message": "Invalid URL pattern(s): " + "; ".join(bad)}
        return {"success": True, "message": f"{len(self.url_patterns)} URL pattern(s) configured."}

    # --- Tool-provider contract ---------------------------------------------
    # The registry marks browser as is_connection=False, so the create/refresh
    # flows treat it as a tool provider and call (a)list_tools. But the browser
    # tools are BUILTIN and reach the agent via the BROWSER capability, not via
    # discovered ConnectionTool rows — so there is nothing to list here. Return
    # an empty list so tool discovery is a clean no-op rather than crashing.

    def list_tools(self) -> List[dict]:
        return []

    async def alist_tools(self) -> List[dict]:
        return []

    # --- DataSourceClient contract: nothing to index or query ---------------

    def get_schemas(self, *args, **kwargs) -> List[Any]:
        return []

    def get_schema(self, table_name: str) -> Any:
        return None

    def prompt_schema(self) -> str:
        return ""

    def execute_query(self, **kwargs: Any) -> Any:
        raise NotImplementedError("A browser connection is not queryable.")
