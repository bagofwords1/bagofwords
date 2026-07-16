"""Connection type → client class.

The control plane resolves the same `type` string against its own registry to
read `capabilities` and `description` locally (A5), so the strings here must
match `backend/app/schemas/data_source_registry.py` exactly. A type this agent
advertises but Bow cannot resolve registers a connection nothing can use.
"""

from __future__ import annotations

from typing import Any, Type

from .base import DataSourceClient
from .postgresql_client import PostgresqlClient

_REGISTRY: dict[str, Type[DataSourceClient]] = {
    "postgresql": PostgresqlClient,
}


def resolve_client_class(type_name: str) -> Type[DataSourceClient]:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise ValueError(
            f"unsupported data source type {type_name!r}; "
            f"this agent supports: {', '.join(sorted(_REGISTRY))}"
        ) from None


def construct_client(type_name: str, params: dict[str, Any]) -> DataSourceClient:
    """Build a client, passing only what its constructor accepts.

    Config and credentials are merged by the caller and arrive as one dict, so
    narrowing here is what lets a connection carry agent-side keys the client
    itself knows nothing about.
    """
    import inspect

    cls = resolve_client_class(type_name)
    sig = inspect.signature(cls.__init__)
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_kwargs:
        allowed = params
    else:
        allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}
    return cls(**allowed)


def supported_types() -> list[str]:
    return sorted(_REGISTRY)
