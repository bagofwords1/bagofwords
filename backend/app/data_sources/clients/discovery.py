"""Shared completeness contract for catalog discovery (tables, files and tools)."""
from dataclasses import dataclass, field
from typing import Any
from app.errors import AppError, ErrorCode


class IncompleteDiscoveryError(AppError):
    """The last successful catalog must be retained, without any reconciliation."""

    def __init__(self, message=None):
        super().__init__(ErrorCode.CATALOG_DISCOVERY_INCOMPLETE, message,
                         status_code=503)


@dataclass
class DiscoveryResult:
    items: list[Any]
    complete: bool = True
    failures: list[str] = field(default_factory=list)


class ValidatedDiscovery(list):
    """A snapshot validated once by an adapter or its consuming service.

    Keep explicit completeness separate from legacy successful list returns:
    only an explicit result can authorize emptying an existing catalog.
    """

    def __init__(self, items, *, explicit_complete=False):
        super().__init__(items)
        self.explicit_complete = explicit_complete


def reject_unconfirmed_empty(result, *, has_existing):
    if has_existing and not result and not getattr(result, "explicit_complete", False):
        raise IncompleteDiscoveryError(
            "Empty discovery did not confirm completeness; previous catalog retained"
        )


class DiscoveryClient:
    """Clients may recover with a fallback, but must report any missing coverage.

    Optional enrichment failures need not mark discovery incomplete when the
    authoritative item/column listing still succeeds. Reset per invocation;
    never use global flags because different connections crawl concurrently.
    """

    def reset_discovery(self):
        self._discovery_failures = []

    def mark_discovery_incomplete(self, scope: str):
        if not hasattr(self, "_discovery_failures"):
            self._discovery_failures = []
        self._discovery_failures.append(scope)


def require_complete_discovery(client, result):
    """Validate before *any* catalog/overlay writes, including async overrides.

    Bridge existing partial-discovery hooks into the common contract so Qlik,
    SQL routine discovery and Power BI all fail closed. Reading status itself
    must succeed: a broken diagnostics hook cannot establish completeness.
    """
    if isinstance(result, ValidatedDiscovery):
        return result
    explicit_complete = isinstance(result, DiscoveryResult) and result.complete
    failures = list(getattr(client, "_discovery_failures", None) or [])
    if isinstance(result, DiscoveryResult):
        if not result.complete or result.failures:
            failures.extend(result.failures or ["incomplete catalog"])
        items = result.items
    else:
        items = result
    if items is None:
        failures.append("no catalog snapshot returned")
    for hook in ("unindexed_table_prefixes", "unindexed_entry_types"):
        method = getattr(client, hook, None)
        if method is not None and method():
            failures.append("unread catalog entries")
    stats_method = getattr(client, "index_stats", None)
    stats = stats_method() if stats_method is not None else {}
    if (stats or {}).get("unreadable_datasets") or getattr(client, "discovery_diagnostics", None):
        failures.append("unreadable semantic models")
    if failures:
        # Scope labels, not raw provider responses/credentials, belong in the
        # surfaced message. Provider-specific diagnostics remain on the client.
        raise IncompleteDiscoveryError(
            "Catalog discovery incomplete; previous catalog retained (" + "; ".join(failures[:5]) + ")"
        )
    return ValidatedDiscovery(items, explicit_complete=explicit_complete)
