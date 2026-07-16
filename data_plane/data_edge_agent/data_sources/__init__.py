"""Data source clients for the edge agent.

These mirror the convention in `backend/app/data_sources/clients` — same method
names, same return shapes — but they are the agent's own. The control plane's
versions carry quota metering, RLS hooks and ORM-backed table metadata, none of
which exists out here; importing them would drag `app.models` into a process
that is deliberately isolated from it.
"""

from .base import DataSourceClient
from .registry import resolve_client_class, construct_client

__all__ = ["DataSourceClient", "resolve_client_class", "construct_client"]
