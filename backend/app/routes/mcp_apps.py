"""MCP Apps host API: what report chat needs to show a tool's interactive UI.

A tool call that rendered into an MCP App carries ``result_json.mcp_app``
(see execute_mcp). From there the chat uses:

* ``GET  /api/mcp_apps/{tool_execution_id}/resource`` — the ``ui://`` document
  and its declared CSP / permissions, read from the tool's own server;
* ``GET  /api/mcp_apps/sandbox`` — the sandbox proxy page the app runs inside,
  served with a Content-Security-Policy built from the declared domains (the
  spec requires the host, not the MCP server, to serve the frame);
* ``POST /api/mcp_apps/{tool_execution_id}/rpc`` — the app's own MCP requests
  (``tools/call``, ``resources/read``), proxied to the SAME server only;
* ``POST /api/mcp_apps/{tool_execution_id}/model_context`` — context the app
  hands the model (``ui/update-model-context``), stored with the tool call so
  the next turn of the conversation sees it.

Viewing needs view access to the report. Anything that acts — tool calls,
resource reads, model context — is the report owner's only: the call runs with
their credentials, under their tool policies.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.services import mcp_apps as apps

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp_apps"])

# App-initiated calls per (user, tool execution) per minute. An app is UI: a
# person clicking. Anything faster is a loop.
_RPC_PER_MINUTE = 60
_RPC_WINDOW: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_MAX_MODEL_CONTEXT_CHARS = 8000
_RPC_METHODS = ("tools/call", "resources/read")


def _rate_limited(user_id: str, te_id: str) -> bool:
    now = time.monotonic()
    q = _RPC_WINDOW[(user_id, te_id)]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= _RPC_PER_MINUTE:
        return True
    q.append(now)
    return False


async def _load(db: AsyncSession, tool_execution_id: str, user: User, organization: Organization):
    """The tool execution, its report and its MCP App payload — or 404.

    404 (not 403) for anything the caller may not see, so execution ids
    cannot be probed across reports or organizations.
    """
    from app.core.derived_access import can_view_report
    from app.models.agent_execution import AgentExecution
    from app.models.report import Report
    from app.models.tool_execution import ToolExecution

    te = await db.get(ToolExecution, tool_execution_id)
    if te is None:
        raise HTTPException(status_code=404, detail="Not found")
    ae = await db.get(AgentExecution, te.agent_execution_id)
    if ae is None or str(ae.organization_id or "") != str(organization.id) or not ae.report_id:
        raise HTTPException(status_code=404, detail="Not found")
    report = (await db.execute(
        select(Report).options(selectinload(Report.data_sources)).where(Report.id == ae.report_id)
    )).scalar_one_or_none()
    if report is None or str(report.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Not found")
    if not await can_view_report(db, user, organization, report):
        raise HTTPException(status_code=404, detail="Not found")

    result = te.result_json if isinstance(te.result_json, dict) else {}
    payload = result.get("mcp_app")
    if not isinstance(payload, dict) or not payload.get("resource_uri") or not payload.get("connection_id"):
        raise HTTPException(status_code=404, detail="This tool call has no MCP App")
    return te, report, payload


def _enabled_or_404(organization: Organization) -> None:
    from app.ai.tools.implementations.execute_mcp import mcp_apps_enabled
    if not mcp_apps_enabled(getattr(organization, "settings", None)):
        raise HTTPException(status_code=404, detail="MCP Apps are turned off for this organization")


def _require_owner(report, user: User) -> None:
    if str(getattr(report, "user_id", "")) != str(user.id):
        raise HTTPException(
            status_code=403,
            detail="Only the person who ran this conversation can use its apps' actions",
        )


async def _connection_client(db: AsyncSession, payload: Dict[str, Any], organization: Organization, user: User):
    from app.models.connection import Connection
    from app.services.connection_service import ConnectionService

    connection = await db.get(Connection, payload["connection_id"])
    if connection is None or str(connection.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="The app's connection no longer exists")
    if connection.type != "mcp":
        raise HTTPException(status_code=400, detail="Only MCP connections serve apps")
    client = await ConnectionService().construct_client(db, connection, user)
    return connection, client


def _sandbox_url(request: Request) -> str:
    """Where the chat loads the sandbox proxy page from."""
    from app.settings.config import settings
    origin = getattr(getattr(settings.bow_config, "mcp_apps", None), "sandbox_origin", None)
    path = "/api/mcp_apps/sandbox"
    return f"{origin}{path}" if origin else path


@router.get("/mcp_apps/{tool_execution_id}/resource")
async def get_mcp_app_resource(
    tool_execution_id: str,
    request: Request,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """The tool's ``ui://`` document, its sandbox policy, and what it renders."""
    _enabled_or_404(organization)
    te, report, payload = await _load(db, tool_execution_id, user, organization)
    connection, client = await _connection_client(db, payload, organization, user)
    try:
        resource = await apps.read_app_resource(client, str(connection.id), payload["resource_uri"])
    except apps.McpAppResourceError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.warning("mcp_apps: reading %s failed: %s", payload["resource_uri"], e)
        raise HTTPException(status_code=502, detail="The MCP server did not return its app")

    # Audit trail for what an app was allowed to reach (spec: hosts SHOULD log).
    logger.info(
        "mcp_apps: serving %s for tool execution %s (csp=%s permissions=%s)",
        payload["resource_uri"], tool_execution_id,
        json.dumps(resource.get("csp")), resource.get("permissions"),
    )
    return {
        "resource_uri": resource["uri"],
        "html": resource["html"],
        "csp": resource["csp"],
        "permissions": resource["permissions"],
        "allow": apps.permissions_allow_attr(resource["permissions"]),
        "prefers_border": resource.get("prefersBorder"),
        "sandbox_url": _sandbox_url(request),
        "connection_name": connection.name,
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input") or {},
        "tool_result": payload.get("tool_result") or {"content": []},
        # Whether this viewer may act through the app (call tools, message
        # the model). Others see it read-only.
        "can_interact": str(getattr(report, "user_id", "")) == str(user.id),
    }


@router.post("/mcp_apps/{tool_execution_id}/rpc")
async def mcp_app_rpc(
    tool_execution_id: str,
    body: dict,
    request: Request,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Proxy an app's MCP request to its own server.

    Body: ``{"method": "tools/call" | "resources/read", "params": {...},
    "confirmed": bool}``. Returns ``{"result": ...}`` or ``{"error":
    {"code", "message"}}`` (JSON-RPC shaped, so the chat relays it as is).
    """
    _enabled_or_404(organization)
    te, report, payload = await _load(db, tool_execution_id, user, organization)
    _require_owner(report, user)

    method = body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    if method not in _RPC_METHODS:
        return {"error": {"code": -32601, "message": f"Method not supported: {method}"}}
    if _rate_limited(str(user.id), tool_execution_id):
        raise HTTPException(status_code=429, detail="Too many requests from this app")

    connection, client = await _connection_client(db, payload, organization, user)

    if method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return {"error": {"code": -32602, "message": "uri is required"}}
        res = await client.aread_resource(uri)
        await _audit(db, organization, user, request, "mcp_app.resource_read",
                     te, {"uri": uri, "success": bool(res.get("success"))})
        if not res.get("success"):
            return {"error": {"code": -32603, "message": res.get("error") or "Read failed"}}
        return {"result": {"contents": [_wire_resource_item(c) for c in res.get("contents") or []]}}

    # tools/call
    name = params.get("name")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    if not isinstance(name, str) or not name:
        return {"error": {"code": -32602, "message": "name is required"}}
    tool = await _connection_tool(db, connection, name)
    if tool is None or not tool.is_enabled:
        return {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
    if not apps.app_callable(tool.metadata_json):
        # Spec MUST: an app may only call tools whose visibility includes "app".
        await _audit(db, organization, user, request, "mcp_app.tool_call_blocked",
                     te, {"tool": name, "reason": "visibility"})
        return {"error": {"code": -32602, "message": f"Tool '{name}' is not callable from an app"}}

    effective = await _effective_policy(db, tool, report, user)
    if effective == "deny":
        await _audit(db, organization, user, request, "mcp_app.tool_call_blocked",
                     te, {"tool": name, "reason": "policy_deny"})
        return {"error": {"code": -32001, "message": f"Tool '{name}' is blocked by policy"}}
    from app.data_sources.clients.mcp_client import is_read_only_tool
    read_only = is_read_only_tool(tool.metadata_json)
    # An app call is a person's click, but the click is on someone else's UI:
    # anything that can write, or that policy says to ask about, needs that
    # person to confirm in BOW's own dialog first.
    needs_confirmation = effective in ("ask", "auto") or not read_only
    if needs_confirmation and body.get("confirmed") is not True:
        return {"error": {
            "code": -32002,
            "message": "confirmation_required",
            "data": {
                "tool": name,
                "arguments": arguments,
                "description": tool.description,
                "read_only": read_only,
                "connection_name": connection.name,
            },
        }}

    result = await client.acall_tool(name, arguments, read_only=read_only)
    await _audit(db, organization, user, request, "mcp_app.tool_called", te, {
        "tool": name, "connection_id": str(connection.id),
        "confirmed": bool(body.get("confirmed")), "success": bool(result.get("success")),
    })
    raw = result.get("raw_result")
    if raw is None:
        if not result.get("success") and result.get("error"):
            raw = {"content": [{"type": "text", "text": str(result.get("error"))}], "isError": True}
        else:
            data = result.get("data")
            text = data if isinstance(data, str) else json.dumps(data, default=str)
            raw = {"content": [{"type": "text", "text": (text or "")[:200_000]}],
                   "isError": not result.get("success")}
    return {"result": raw}


@router.post("/mcp_apps/{tool_execution_id}/model_context")
async def set_mcp_app_model_context(
    tool_execution_id: str,
    body: dict,
    request: Request,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Store what the app wants the model to know (``ui/update-model-context``).

    Kept on the tool call's own history entry, so the next turn of the
    conversation reads it as part of that call's result. Each update replaces
    the previous one, as the spec says.
    """
    _enabled_or_404(organization)
    te, report, payload = await _load(db, tool_execution_id, user, organization)
    _require_owner(report, user)

    content = body.get("content") if isinstance(body.get("content"), list) else None
    structured = body.get("structuredContent")
    texts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    app_context: Dict[str, Any] = {}
    if texts:
        app_context["text"] = "\n".join(texts)
    if structured is not None:
        app_context["structured"] = structured
    from app.data_sources.clients.mcp_client import scrub_tags
    app_context = scrub_tags(app_context)
    rendered = json.dumps(app_context, default=str)
    if len(rendered) > _MAX_MODEL_CONTEXT_CHARS:
        return {"error": {"code": -32602, "message": "Model context is too large"}}

    from app.ai.persisted_summary import TOOL_CONTEXT_KIND_FIELD, TOOL_CONTEXT_OBSERVATION_KIND
    summary = te.context_summary_json if isinstance(te.context_summary_json, dict) else {}
    summary = json.loads(json.dumps(summary, default=str))  # detach from the ORM value
    note = {
        "source": "The app shown with this tool call (MCP App) reported this state; "
                  "it reflects what the user did in the app after the call returned.",
        **app_context,
    }
    if summary.get(TOOL_CONTEXT_KIND_FIELD) == TOOL_CONTEXT_OBSERVATION_KIND and isinstance(summary.get("observation"), dict):
        if app_context:
            summary["observation"]["app_context"] = note
        else:
            summary["observation"].pop("app_context", None)
    else:
        if app_context:
            summary["app_context"] = note
        else:
            summary.pop("app_context", None)
    te.context_summary_json = summary
    await db.commit()
    return {"result": {}}


# ── sandbox proxy page ──────────────────────────────────────────────────────


@router.get("/mcp_apps/sandbox", response_class=HTMLResponse)
async def mcp_app_sandbox(csp: Optional[str] = Query(default=None)):
    """The page an MCP App runs inside (the spec's "sandbox proxy").

    No authentication and no data: it only relays messages between the chat
    and the app document it is handed. Its Content-Security-Policy, built from
    the domains the app declared, also binds that document (an ``srcdoc``
    frame inherits its parent's policy). Only BOW itself may frame it.
    """
    declared: Dict[str, Any] = {}
    if csp:
        try:
            parsed = json.loads(csp[:20_000])
            if isinstance(parsed, dict):
                declared = parsed
        except (TypeError, ValueError):
            declared = {}
    policy = apps.build_app_csp(declared)
    headers = {
        "Content-Security-Policy": f"{policy}; frame-ancestors {_frame_ancestors()}",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    return HTMLResponse(content=_SANDBOX_HTML.replace("__BOW_ORIGINS__", json.dumps(_bow_origins())), headers=headers)


def _bow_origins() -> list:
    from app.settings.config import settings
    out = []
    base = getattr(settings.bow_config, "base_url", None) or ""
    parts = urlsplit(base)
    if parts.scheme and parts.netloc and parts.hostname not in ("0.0.0.0",):
        out.append(f"{parts.scheme}://{parts.netloc}")
    return out


def _frame_ancestors() -> str:
    return " ".join(["'self'", *_bow_origins()])


# The sandbox proxy. Refuses to run unless it is framed AND isolated from BOW
# (an opaque origin, or an origin that is not BOW's): framed directly by a
# BOW page without the sandbox attribute it would share BOW's origin, and
# whatever HTML it was handed would run with the user's session.
_SANDBOX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP App</title>
<style>html,body{margin:0;padding:0;height:100%;background:transparent;overflow:hidden}
iframe{border:0;width:100%;height:100%;display:block;background:transparent}</style>
</head><body><script>
(function () {
  var BOW_ORIGINS = __BOW_ORIGINS__;
  var isolated = self.origin === 'null' || BOW_ORIGINS.indexOf(self.origin) === -1;
  if (window.parent === window || !isolated) {
    document.body.textContent = 'This page only runs inside a sandboxed MCP App frame.';
    return;
  }
  var inner = null;
  function toParent(msg) { window.parent.postMessage(msg, '*'); }
  window.addEventListener('message', function (ev) {
    var msg = ev.data;
    if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') return;
    if (ev.source === window.parent) {
      if (msg.method === 'ui/notifications/sandbox-resource-ready') {
        var p = msg.params || {};
        if (inner) inner.remove();
        inner = document.createElement('iframe');
        inner.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
        if (typeof p.allow === 'string' && p.allow) inner.setAttribute('allow', p.allow);
        inner.setAttribute('title', 'MCP App');
        inner.srcdoc = typeof p.html === 'string' ? p.html : '';
        document.body.appendChild(inner);
        return;
      }
      if (typeof msg.method === 'string' && msg.method.indexOf('ui/notifications/sandbox-') === 0) return;
      if (inner && inner.contentWindow) inner.contentWindow.postMessage(msg, '*');
      return;
    }
    if (inner && ev.source === inner.contentWindow) {
      if (typeof msg.method === 'string' && msg.method.indexOf('ui/notifications/sandbox-') === 0) return;
      toParent(msg);
    }
  });
  toParent({ jsonrpc: '2.0', method: 'ui/notifications/sandbox-proxy-ready', params: {} });
})();
</script></body></html>
"""


# ── helpers ─────────────────────────────────────────────────────────────────


def _wire_resource_item(c: Dict[str, Any]) -> Dict[str, Any]:
    item: Dict[str, Any] = {"uri": c.get("uri")}
    if c.get("mime_type"):
        item["mimeType"] = c["mime_type"]
    if c.get("type") == "binary":
        item["blob"] = c.get("blob_b64") or ""
    else:
        item["text"] = c.get("text") or ""
    if c.get("meta"):
        item["_meta"] = c["meta"]
    return item


async def _connection_tool(db: AsyncSession, connection, name: str):
    from app.models.connection_tool import ConnectionTool
    return (await db.execute(
        select(ConnectionTool).where(
            ConnectionTool.connection_id == str(connection.id),
            ConnectionTool.name == name,
        )
    )).scalar_one_or_none()


async def _effective_policy(db: AsyncSession, tool, report, user: User) -> str:
    """The tool's policy for this user, strictest across the report's agents."""
    from app.models.domain_connection import domain_connection
    from app.services.tool_policy_service import ToolPolicyService

    ds_ids = [str(ds.id) for ds in (report.data_sources or [])]
    carrying = []
    if ds_ids:
        carrying = [str(r[0]) for r in (await db.execute(
            select(domain_connection.c.data_source_id).where(
                domain_connection.c.data_source_id.in_(ds_ids),
                domain_connection.c.connection_id == str(tool.connection_id),
            )
        )).all()]
    resolution = await ToolPolicyService().resolve_for_run(
        db, tool=tool, data_source_ids=carrying or None, user=user,
    )
    if not resolution.is_enabled:
        return "deny"
    return resolution.effective


async def _audit(db, organization, user, request, action, te, details) -> None:
    try:
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db, str(organization.id), action, user_id=str(user.id),
            resource_type="tool_execution", resource_id=str(te.id),
            details=details, request=request,
        )
    except Exception:
        logger.warning("mcp_apps: audit log failed for %s", action, exc_info=True)
