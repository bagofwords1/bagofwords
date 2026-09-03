"""Localhost-only admin UI for the data edge agent (design C4).

A small aiohttp app, bound to loopback, that lets an operator on the customer's
own network see tunnel/connection health and configure the connections and
credentials this agent serves. It is the only place those credentials are ever
entered, and they never leave the box — the whole reason the UI is agent-local.

Two surfaces:
  * a JSON API under `/api/*`, and
  * a single self-contained HTML page served at `/`.

Every write goes through both the encrypted store (persistence) and the live
tunnel (apply now + re-advertise), so a change takes effect without a restart
and survives one.

Security posture: bound to `admin_host` (127.0.0.1 by default). There is no
auth — loopback-only is the boundary, matching the design's "localhost-only".
Credential *values* are never returned by the API; a connection reports only
which credential keys are set.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from ..config import ConnectionConfig
from ..data_sources import registry
from ..store import ConnectionStore, StoreError
from ..tunnel import EdgeAgentTunnel

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
# Credential entry never has a legitimate reason to be large; cap the body so a
# loopback client can't wedge the agent with a giant payload.
_MAX_BODY_BYTES = 256 * 1024


def _connection_view(conn: ConnectionConfig) -> dict[str, Any]:
    """Public shape of a connection — config in clear, credentials as key names
    only. The values never leave the process through this API."""
    return {
        "name": conn.name,
        "type": conn.type,
        "label": conn.label,
        "config": conn.config or {},
        "credential_keys": sorted((conn.credentials or {}).keys()),
        "query_timeout_seconds": conn.query_timeout_seconds,
    }


class AdminServer:
    """Owns the aiohttp runner and routes for the admin UI."""

    def __init__(self, tunnel: EdgeAgentTunnel, store: ConnectionStore,
                 host: str, port: int) -> None:
        self._tunnel = tunnel
        self._store = store
        self._host = host
        self._port = port
        self._runner: Optional[web.AppRunner] = None

    # -- lifecycle --------------------------------------------------------

    def build_app(self) -> web.Application:
        """Assemble the aiohttp application (also the seam tests drive)."""
        app = web.Application(client_max_size=_MAX_BODY_BYTES)
        app.add_routes([
            web.get("/api/status", self._status),
            web.get("/api/types", self._types),
            web.get("/api/connections", self._list_connections),
            web.post("/api/connections", self._create_connection),
            web.get("/api/connections/{name}", self._get_connection),
            web.put("/api/connections/{name}", self._update_connection),
            web.delete("/api/connections/{name}", self._delete_connection),
            web.post("/api/connections/{name}/test", self._test_saved),
            web.post("/api/test", self._test_unsaved),
            web.get("/api/audit", self._audit),
            web.get("/", self._index),
            web.get("/index.html", self._index),
        ])
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await site.start()
        logger.info(
            "edge_agent.admin.listening",
            extra={"url": f"http://{self._host}:{self._port}"},
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- request parsing helpers -----------------------------------------

    async def _parse_connection(self, request: web.Request,
                                name_override: Optional[str] = None) -> ConnectionConfig:
        """Build a ConnectionConfig from a JSON body.

        On update, a blank/absent `credentials` means "keep what's stored" —
        an operator editing a host shouldn't have to retype the password, and
        the UI never receives the value to send back. `credentials` present but
        empty-valued is treated the same way, key by key.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise web.HTTPBadRequest(reason=f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="body must be a JSON object")

        name = name_override or body.get("name")
        if not name:
            raise web.HTTPBadRequest(reason="connection 'name' is required")
        conn_type = body.get("type")
        if not conn_type:
            raise web.HTTPBadRequest(reason="connection 'type' is required")

        config = body.get("config") or {}
        if not isinstance(config, dict):
            raise web.HTTPBadRequest(reason="'config' must be an object")

        creds = body.get("credentials") or {}
        if not isinstance(creds, dict):
            raise web.HTTPBadRequest(reason="'credentials' must be an object")
        # Merge over stored credentials so blank fields keep their saved value.
        merged_creds: dict[str, Any] = {}
        existing = self._store.get(name)
        if existing is not None:
            merged_creds.update(existing.credentials or {})
        for k, v in creds.items():
            if v == "" or v is None:
                continue  # blank = keep existing
            merged_creds[k] = v

        try:
            return ConnectionConfig(
                name=name,
                type=conn_type,
                label=body.get("label"),
                config=config,
                credentials=merged_creds,
                query_timeout_seconds=body.get("query_timeout_seconds"),
            )
        except Exception as e:  # pydantic validation (e.g. subject-unsafe name)
            # A reason header must be a single line; pydantic errors are multi-line.
            raise web.HTTPBadRequest(reason=" ".join(str(e).split()))

    # -- API handlers -----------------------------------------------------

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(self._tunnel.status_snapshot())

    async def _types(self, request: web.Request) -> web.Response:
        return web.json_response({"types": registry.known_types()})

    async def _list_connections(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"connections": [_connection_view(c) for c in self._tunnel.list_connections()]}
        )

    async def _get_connection(self, request: web.Request) -> web.Response:
        conn = self._tunnel.get_connection(request.match_info["name"])
        if conn is None:
            raise web.HTTPNotFound(reason="no such connection")
        return web.json_response(_connection_view(conn))

    async def _create_connection(self, request: web.Request) -> web.Response:
        conn = await self._parse_connection(request)
        if self._tunnel.get_connection(conn.name) is not None:
            raise web.HTTPConflict(reason=f"connection {conn.name!r} already exists")
        await self._persist_and_apply(conn)
        return web.json_response(_connection_view(conn), status=201)

    async def _update_connection(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if self._tunnel.get_connection(name) is None and self._store.get(name) is None:
            raise web.HTTPNotFound(reason="no such connection")
        conn = await self._parse_connection(request, name_override=name)
        await self._persist_and_apply(conn)
        return web.json_response(_connection_view(conn))

    async def _delete_connection(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        removed_live = await self._tunnel.remove_connection(name)
        removed_stored = self._store.delete(name)
        if not (removed_live or removed_stored):
            raise web.HTTPNotFound(reason="no such connection")
        return web.json_response({"deleted": name})

    async def _test_saved(self, request: web.Request) -> web.Response:
        conn = self._tunnel.get_connection(request.match_info["name"])
        if conn is None:
            raise web.HTTPNotFound(reason="no such connection")
        result = await self._tunnel.test_connection_config(conn)
        return web.json_response(result)

    async def _test_unsaved(self, request: web.Request) -> web.Response:
        conn = await self._parse_connection(request)
        result = await self._tunnel.test_connection_config(conn)
        return web.json_response(result)

    async def _audit(self, request: web.Request) -> web.Response:
        """Recent audit entries, newest first (design C4/C5)."""
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 1000))
        return web.json_response({"entries": self._tunnel.audit.recent(limit)})

    # -- persistence + apply ---------------------------------------------

    async def _persist_and_apply(self, conn: ConnectionConfig) -> None:
        # Store first: a crash after applying-but-before-persisting would lose
        # the edit on the next restart, the more surprising failure. A store
        # error surfaces as a 500 before anything is advertised.
        try:
            self._store.upsert(conn)
        except StoreError as e:
            raise web.HTTPInternalServerError(reason=str(e))
        await self._tunnel.apply_connection(conn)

    # -- static UI --------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        index = _STATIC_DIR / "index.html"
        try:
            return web.Response(text=index.read_text(), content_type="text/html")
        except OSError:
            raise web.HTTPInternalServerError(reason="admin UI asset missing")
