import asyncio
import json
import logging
import re
from typing import List, Dict, Any, Optional
from app.data_sources.clients.tool_provider_base import ToolProviderClient
from app.data_sources.clients.progress import IndexingCancelled
from app.utils.tabular_payload import detect_content_type

logger = logging.getLogger(__name__)

# Above this, a tool result is NOT parsed into Python objects. json.loads on a
# large response costs several times its size in objects, and anything built
# from it costs that again — with no cap anywhere in this path, one oversized
# response could take the worker down. Comparable agents budget the same way,
# with far smaller caps; this is far more generous because the payload's
# destination is a file, not the context window.
MAX_PARSE_CHARS = 5_000_000


def _over_parse_cap(text: Any) -> bool:
    return isinstance(text, str) and len(text) > MAX_PARSE_CHARS


def looks_like_json(text: Any) -> bool:
    """Would this string have parsed as JSON, if we had tried?

    Skipping the parse must not also lose the FORMAT. An 8 MB JSON response
    handed downstream as anonymous text gets saved as `.txt`, and the consumer
    is told to read it as prose — so it never finds the records that are
    plainly there. Cheap prefix check, no parsing.
    """
    if not isinstance(text, str):
        return False
    head = text.lstrip()[:1]
    return head in ("{", "[")


# Unicode TAG characters (U+E0000–U+E007F) render as nothing but are read by
# the model — a known channel for smuggling instructions through untrusted tool
# output. Strip them from everything a server sends us, keeping only valid
# emoji tag sequences (subdivision flags: U+1F3F4, tags, then U+E007F).
_TAG_SUB_RE = re.compile("(\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F)|[\U000E0000-\U000E007F]")
_TAG_ANY_RE = re.compile("[\U000E0000-\U000E007F]")


def strip_unicode_tags(text: str) -> str:
    if not isinstance(text, str) or not _TAG_ANY_RE.search(text):
        return text
    return _TAG_SUB_RE.sub(lambda m: m.group(1) or "", text)


def scrub_tags(value: Any) -> Any:
    """strip_unicode_tags over every string in a JSON-like value (keys too)."""
    if isinstance(value, str):
        return strip_unicode_tags(value)
    if isinstance(value, list):
        return [scrub_tags(v) for v in value]
    if isinstance(value, dict):
        return {scrub_tags(k): scrub_tags(v) for k, v in value.items()}
    return value


# Tool annotation hints (MCP spec). Advisory, set by the server.
_HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# Cap tools/list pagination so a misbehaving server can't make us walk forever.
_MAX_TOOL_PAGES = 50


def tool_hints(tool: Any) -> Dict[str, bool]:
    """The boolean annotation hints a discovered tool declares (others omitted)."""
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return {}
    out: Dict[str, bool] = {}
    for key in _HINT_KEYS:
        if isinstance(ann, dict):
            val = ann.get(key)
        else:
            # mcp 2.x: snake_case attributes (read_only_hint); 1.x: camelCase.
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            val = getattr(ann, snake, None)
            if val is None:
                val = getattr(ann, key, None)
        if isinstance(val, bool):
            out[key] = val
    return out


def default_policy_for_hints(hints: Dict[str, bool]) -> Optional[str]:
    """Starting policy for a newly-discovered tool.

    Only a tool the server marks read-only runs without asking; a destructive
    tool, or one with no hints at all, starts on "ask". Missing hints are
    treated as write-capable.
    Returns None for "allow" (the caller's default).
    """
    if hints.get("readOnlyHint") is True and hints.get("destructiveHint") is not True:
        return None
    return "ask"


def is_read_only_tool(metadata: Any) -> bool:
    """True when a stored ConnectionTool's metadata records readOnlyHint=true."""
    if not isinstance(metadata, dict):
        return False
    hints = metadata.get("annotations")
    return isinstance(hints, dict) and hints.get("readOnlyHint") is True


# A pooled session the server no longer knows answers 404 → the SDK reports
# "Session terminated". The server rejected the request before running the
# tool, so one retry on a fresh session is safe for any tool.
_SESSION_GONE_MARKERS = ("session terminated", "session not found")

# Transport failures where the call MAY have reached the server. Retried only
# for read-only tools: a write could run twice.
_TRANSPORT_ERROR_NAMES = {
    "ClosedResourceError", "BrokenResourceError", "EndOfStream",
    "RemoteProtocolError", "ReadError", "WriteError", "ConnectError",
    "ReadTimeout", "ConnectTimeout", "PoolTimeout", "ConnectionResetError",
    "BrokenPipeError",
}


def _leaf_exceptions(e: BaseException) -> List[BaseException]:
    if isinstance(e, BaseExceptionGroup):
        out: List[BaseException] = []
        for sub in e.exceptions:
            out.extend(_leaf_exceptions(sub))
        return out
    return [e]


def retry_kind(e: BaseException) -> Optional[str]:
    """'session_gone' (safe for any tool), 'transport' (read-only only), or None."""
    leaves = _leaf_exceptions(e)
    for leaf in leaves:
        if any(m in str(leaf).lower() for m in _SESSION_GONE_MARKERS):
            return "session_gone"
    for leaf in leaves:
        if type(leaf).__name__ in _TRANSPORT_ERROR_NAMES:
            return "transport"
    return None


# ── SDK-shape helpers ───────────────────────────────────────────────────────
# The mcp 2.x types are snake_case (``mime_type``, ``structured_content``,
# ``meta``) with the camelCase wire names as aliases; the 1.x ones were
# camelCase attributes. Read either, so result handling does not depend on the
# installed SDK major.


def _first_attr(obj: Any, *names: str) -> Any:
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return None


def _mime_of(obj: Any) -> Optional[str]:
    return _first_attr(obj, "mime_type", "mimeType")


def _meta_of(obj: Any) -> Optional[Dict[str, Any]]:
    meta = _first_attr(obj, "meta", "_meta")
    if meta is None:
        return None
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump(by_alias=True, exclude_none=True)
    return meta if isinstance(meta, dict) and meta else None


def _input_schema_of(tool: Any) -> Dict[str, Any]:
    schema = _first_attr(tool, "input_schema", "inputSchema")
    return schema if isinstance(schema, dict) else {}


def _output_schema_of(tool: Any) -> Dict[str, Any]:
    schema = _first_attr(tool, "output_schema", "outputSchema")
    return schema if isinstance(schema, dict) else {}


# An MCP App receives the tool result verbatim. Keep that copy bounded: it is
# stored with the tool execution and sent to the browser, and a multi-megabyte
# table belongs in the materialized file, not in an iframe message.
MAX_RAW_RESULT_CHARS = 512_000


def raw_call_tool_result(result: Any) -> Optional[Dict[str, Any]]:
    """The CallToolResult as wire JSON (camelCase), or None when too large.

    Binary blocks (image/audio data, embedded blobs) are left out: they are
    saved as session files, and base64 in an iframe message buys nothing.
    """
    try:
        if hasattr(result, "model_dump"):
            raw = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        elif isinstance(result, dict):
            raw = dict(result)
        else:
            return None
    except Exception:
        return None
    content = []
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("image", "audio"):
            continue
        res = block.get("resource")
        if isinstance(res, dict) and "blob" in res:
            continue
        content.append(block)
    out = {"content": content}
    for key in ("structuredContent", "_meta", "isError"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    out = scrub_tags(out)
    try:
        if len(json.dumps(out, default=str)) > MAX_RAW_RESULT_CHARS:
            return None
    except (TypeError, ValueError):
        return None
    return out


def _client_options() -> Dict[str, Any]:
    """Options every BOW MCP client is built with."""
    from mcp.client.extension import advertise
    from mcp.types import Implementation
    from app.services.mcp_apps import UI_EXTENSION_ID, MCP_APP_MIME_TYPE

    return {
        "client_info": Implementation(name="bagofwords", version=_bow_version()),
        # MCP Apps: we render text/html;profile=mcp-app resources in chat.
        "extensions": [advertise(UI_EXTENSION_ID, {"mimeTypes": [MCP_APP_MIME_TYPE]})],
        # Tool and resource lists are re-read on purpose (refresh, discovery);
        # a response cache inside a pooled client would serve stale catalogs.
        "cache": None,
    }


def _bow_version() -> str:
    try:
        from app.settings.config import settings
        return str(getattr(settings, "PROJECT_VERSION", None) or "0")
    except Exception:
        return "0"


class McpClient(ToolProviderClient):
    """
    Client for connecting to MCP (Model Context Protocol) servers.
    Supports SSE and Streamable HTTP transports (stdio planned for later).
    Uses the `mcp` Python SDK for protocol handling.
    """

    def __init__(
        self,
        server_url: str,
        transport: str = "sse",
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_key_header: Optional[str] = None,
        # OAuth user_required mode: per-user access_token from
        # UserConnectionCredentials is fed in as a bearer token.
        access_token: Optional[str] = None,
        # HTTP Basic (e.g. Atlassian email + API token).
        username: Optional[str] = None,
        password: Optional[str] = None,
        **_ignored,  # OAuth-app fields (authorize_url, token_url, client_id,
                     # client_secret, scopes, audience) live on the connection
                     # creds but are only used by the OAuth service, not the
                     # client. Swallow them.
    ):
        self.server_url = server_url
        self.transport = transport
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.headers = headers or {}
        self.token = token or access_token
        self.api_key = api_key
        self.api_key_header = api_key_header or "X-API-Key"
        self.username = username
        self.password = password

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers, merging any auth headers."""
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        elif self.username and self.password:
            import base64
            pair = f"{self.username}:{self.password}".encode("utf-8")
            h["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        if self.api_key:
            h[self.api_key_header] = self.api_key
        h.update(self.headers)
        return h

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        Connect to the MCP server and retrieve the list of available tools.
        Uses the mcp SDK's ClientSession for protocol handling.
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self._alist_tools())

    async def _alist_tools(self) -> List[Dict[str, Any]]:
        """Async implementation of list_tools using the MCP SDK.

        Follows tools/list nextCursor pagination (capped). Each tool carries
        its annotation hints in ``metadata`` and, from them, the policy a
        newly-discovered tool starts on (``default_policy``).
        """
        from app.services.mcp_apps import parse_tool_ui_meta

        try:
            tools = []
            async with self._connect() as session:
                cursor = None
                for _ in range(_MAX_TOOL_PAGES):
                    result = await session.list_tools(cursor=cursor)
                    for tool in result.tools:
                        hints = tool_hints(tool)
                        metadata: Dict[str, Any] = {"annotations": hints}
                        # MCP Apps: the ui:// resource this tool renders into
                        # and who may call it (model / app). Kept so the agent
                        # never sees app-only tools and the chat can show the UI.
                        ui = parse_tool_ui_meta(_meta_of(tool))
                        if ui:
                            metadata["ui"] = ui
                        tools.append({
                            "name": tool.name,
                            "description": strip_unicode_tags(tool.description or ""),
                            "input_schema": _input_schema_of(tool),
                            "output_schema": _output_schema_of(tool),
                            "metadata": metadata,
                            "default_policy": default_policy_for_hints(hints),
                        })
                    cursor = _first_attr(result, "next_cursor", "nextCursor")
                    if not cursor:
                        break
                else:
                    logger.warning("MCP tools/list: stopped after %d pages", _MAX_TOOL_PAGES)
            return tools
        except (asyncio.CancelledError, IndexingCancelled):
            raise
        except BaseException as e:
            raise RuntimeError(self._unwrap_exception(e)) from None

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool on the MCP server."""
        import asyncio
        # A one-off loop: a pooled session would outlive it, so open a fresh one.
        return asyncio.get_event_loop().run_until_complete(
            self._acall_tool(tool_name, arguments, pooled=False)
        )

    # ------------------------------------------------------------------
    # Resources
    #
    # MCP servers can expose *resources* (data the server reads on demand)
    # alongside tools. The protocol surfaces three calls: resources/list,
    # resources/templates/list, and resources/read. The agent bridges these via
    # the list_mcp_resources / read_mcp_resource tools — the LLM has no native
    # notion of resources, so we hand it list + read so it can pull resource
    # context just-in-time.
    # ------------------------------------------------------------------

    # Cap pagination so a misbehaving server can't make us walk forever.
    _MAX_RESOURCES = 500

    async def alist_resources(self) -> List[Dict[str, Any]]:
        """List concrete resources exposed by the MCP server.

        Returns a list of {uri, name, description, mime_type}. Follows
        nextCursor pagination up to a safety cap.
        """
        try:
            resources: List[Dict[str, Any]] = []
            async with self._connect() as session:
                cursor = None
                while True:
                    result = await session.list_resources(cursor=cursor)
                    for r in (result.resources or []):
                        entry = {
                            "uri": str(getattr(r, "uri", "")),
                            "name": getattr(r, "name", None),
                            "description": getattr(r, "description", None),
                            "mime_type": _mime_of(r),
                        }
                        meta = _meta_of(r)
                        if meta:
                            entry["meta"] = scrub_tags(meta)
                        resources.append(entry)
                        if len(resources) >= self._MAX_RESOURCES:
                            return resources
                    cursor = _first_attr(result, "next_cursor", "nextCursor")
                    if not cursor:
                        break
            return resources
        except BaseException as e:
            raise RuntimeError(self._unwrap_exception(e)) from None

    async def alist_resource_templates(self) -> List[Dict[str, Any]]:
        """List parameterized URI templates exposed by the MCP server.

        Returns a list of {uri_template, name, description, mime_type,
        is_template}. Templates are optional; servers that don't implement
        resources/templates/list raise, which the caller treats as "none".
        """
        try:
            templates: List[Dict[str, Any]] = []
            async with self._connect() as session:
                cursor = None
                while True:
                    result = await session.list_resource_templates(cursor=cursor)
                    for t in (_first_attr(result, "resource_templates", "resourceTemplates") or []):
                        templates.append({
                            "uri_template": _first_attr(t, "uri_template", "uriTemplate"),
                            "name": getattr(t, "name", None),
                            "description": getattr(t, "description", None),
                            "mime_type": _mime_of(t),
                            "is_template": True,
                        })
                        if len(templates) >= self._MAX_RESOURCES:
                            return templates
                    cursor = _first_attr(result, "next_cursor", "nextCursor")
                    if not cursor:
                        break
            return templates
        except BaseException as e:
            raise RuntimeError(self._unwrap_exception(e)) from None

    async def aread_resource(self, uri: str) -> Dict[str, Any]:
        """Read a resource by URI.

        Returns {success, contents, error} where contents is a list of blocks:
        text blocks {type: "text", text, mime_type, uri} and binary blocks
        {type: "binary", byte_size, mime_type, uri}. A failure here is usually a
        bad/unknown URI, so it's reported as success=False rather than raised —
        mirroring acall_tool's contract.
        """
        try:
            async with self._connect() as session:
                result = await session.read_resource(uri)
                contents: List[Dict[str, Any]] = []
                for c in (getattr(result, "contents", None) or []):
                    mime_type = _mime_of(c)
                    item_meta = _meta_of(c)
                    c_uri = str(getattr(c, "uri", uri))
                    # The wire format distinguishes blob from text by the
                    # presence of the blob field, not by text being empty.
                    blob = getattr(c, "blob", None)
                    if blob is not None:
                        # blob is base64; report decoded size, not encoded length.
                        import base64
                        try:
                            byte_size = len(base64.b64decode(blob, validate=False))
                        except Exception:
                            byte_size = len(blob)
                        contents.append({
                            "type": "binary",
                            "byte_size": byte_size,
                            "mime_type": mime_type or "application/octet-stream",
                            "uri": c_uri,
                            # Keep the base64 payload so callers can materialize the
                            # file (e.g. read_mcp_resource → session File for
                            # inspect_data/create_data). Not inlined into the
                            # LLM-visible content — only used for materialization.
                            "blob_b64": blob,
                            **({"meta": scrub_tags(item_meta)} if item_meta else {}),
                        })
                    else:
                        contents.append({
                            "type": "text",
                            "text": strip_unicode_tags(getattr(c, "text", "") or ""),
                            "mime_type": mime_type,
                            "uri": c_uri,
                            # MCP Apps: a ui:// resource carries its CSP and
                            # iframe permissions here (_meta.ui).
                            **({"meta": scrub_tags(item_meta)} if item_meta else {}),
                        })
                return {"success": True, "contents": contents, "error": None}
        except BaseException as e:
            msg = self._unwrap_exception(e)
            logger.error(f"MCP read_resource failed: {uri}: {msg}")
            return {"success": False, "contents": [], "error": msg}

    async def _acall_tool(
        self, tool_name: str, arguments: Dict[str, Any], pooled: bool = True, read_only: bool = False,
    ) -> Dict[str, Any]:
        """Async implementation of call_tool using the MCP SDK.

        A pooled session can go stale (server restart, session timeout). The
        call is retried once on a fresh session when that is safe: always when
        the server rejected the session outright, and on other transport
        failures only for read-only tools, since a write might already have run.
        """
        try:
            return await self._call_once(tool_name, arguments, pooled)
        except BaseException as e:
            kind = retry_kind(e)
            if kind == "session_gone" or (kind == "transport" and read_only):
                logger.info("MCP tool call %s: %s on a stale session; retrying once", tool_name, kind)
                try:
                    return await self._call_once(tool_name, arguments, pooled)
                except BaseException as e2:
                    e = e2
            msg = self._unwrap_exception(e)
            if kind == "transport" and not read_only:
                msg += (
                    " (the connection dropped mid-call, so this write-capable tool was NOT"
                    " retried automatically — it may or may not have run; check before re-running it)"
                )
            logger.error(f"MCP tool call failed: {tool_name}: {msg}")
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": msg,
            }

    async def _call_once(self, tool_name: str, arguments: Dict[str, Any], pooled: bool) -> Dict[str, Any]:
        async with (self._call_session() if pooled else self._connect()) as session:
            result = await session.call_tool(tool_name, arguments)

        data = self._extract_result_data(result)
        data = self._merge_structured(result, data)
        data = scrub_tags(data)
        binaries = self._extract_binaries(result)
        if data is None and binaries:
            # Files only: give the data artifact a pointer rather than a
            # bare null, so the agent knows to look at the saved files.
            data = f"Tool returned {len(binaries)} file(s); they are attached to this session."
        content_type = self._detect_content_type(data)

        is_error = bool(_first_attr(result, "is_error", "isError"))
        # On a tool-level error (isError=True) the MCP spec puts the
        # explanation in the content blocks, not in a transport
        # exception. Surface it in `error` so callers don't see a
        # useless "None" — otherwise the agent retries blindly.
        error_msg = self._extract_error_message(data) if is_error else None

        # A payload we declined to parse is still JSON if it looks like
        # JSON. Say so, or the caller saves 8 MB of records as a .txt
        # and tells the next tool to read it as prose.
        parse_skipped = _over_parse_cap(data) and looks_like_json(data)

        return {
            "success": not is_error,
            "data": data,
            "content_type": content_type,
            # File blobs a tool returned (EmbeddedResource/blob), surfaced
            # so execute_mcp can materialize them into session files.
            "binaries": binaries,
            # The result as the server sent it (content blocks, structured
            # content, _meta): an MCP App is handed exactly this. Bounded —
            # see raw_call_tool_result.
            "raw_result": raw_call_tool_result(result),
            "error": error_msg,
            "parse_skipped": parse_skipped,
            "size_chars": len(data) if isinstance(data, str) else None,
        }

    @staticmethod
    def _merge_structured(result, data: Any) -> Any:
        """Fold a result's ``structuredContent`` into its data.

        The spec says a tool returning structuredContent SHOULD also send it
        serialized in a text block — when one does (compared as parsed JSON),
        the content is kept as-is so the model doesn't see it twice. When the
        structured data is the only copy (no content, or only a status line /
        prose summary next to it), it is kept: dropping it loses the result.
        """
        structured = _first_attr(result, "structured_content", "structuredContent")
        if structured is None:
            return data
        if data is None:
            return structured
        candidates = data if isinstance(data, list) else [data]
        # A tool returning a plain value gets its output schema's wrapper:
        # structuredContent {"result": <value>} next to the value as text
        # (the SDKs do this for str/int returns). Same data, not a second copy.
        same_as = [structured]
        if isinstance(structured, dict) and set(structured) == {"result"}:
            same_as.append(structured["result"])
            if isinstance(structured["result"], str):
                try:
                    same_as.append(json.loads(structured["result"]))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        for part in candidates:
            if any(part == s for s in same_as):
                return data
            if isinstance(part, str):
                try:
                    if any(json.loads(part) == s for s in same_as):
                        return data
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        return {"structured_content": structured, "content": data}

    @staticmethod
    def _parse_text(text: Any) -> Any:
        """JSON-parse a text payload when it is JSON; otherwise return it as-is."""
        if not isinstance(text, str):
            return text
        if _over_parse_cap(text):
            # Parsing multiplies this several times over in Python objects, and
            # a DataFrame built from it again. Hand back the raw string: the
            # caller still writes every byte to a file, so nothing is lost.
            logger.warning(
                "MCP result of %d chars exceeds the %d-char parse cap; "
                "returning it unparsed.",
                len(text), MAX_PARSE_CHARS,
            )
            return text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    @staticmethod
    def _block_part(block) -> Any:
        """What one content block contributes to the tool's result data.

        Returns the parsed payload, a one-line note for content the agent must
        reach another way, or None for a file blob (those travel separately via
        ``_extract_binaries`` and are saved as session files). Never drops a
        block silently: an unknown shape comes back as a visible note, because a
        result that quietly lost its content reads exactly like a tool that
        returned nothing.
        """
        block_type = getattr(block, "type", None)

        # Embedded resource: a file the tool returned inline. GitHub's
        # get_file_contents sends the file body this way, next to a short
        # "successfully downloaded" text block.
        resource = getattr(block, "resource", None)
        if resource is not None:
            text = getattr(resource, "text", None)
            if text is not None:
                return McpClient._parse_text(text)
            if getattr(resource, "blob", None) is not None:
                return None
            uri = getattr(resource, "uri", None)
            return f"[MCP content dropped: embedded resource with no text or blob (uri={uri})]"

        # Image / audio: base64 media. Saved as a session file, not inlined —
        # base64 in the result data costs tokens and no model can read it.
        if block_type in ("image", "audio") or (
            getattr(block, "data", None) is not None and _mime_of(block)
            and not hasattr(block, "text")
        ):
            return None

        if hasattr(block, "text"):
            return McpClient._parse_text(block.text)

        # Top-level blob (non-standard but seen in the wild) → saved as a file.
        if getattr(block, "blob", None) is not None:
            return None

        # Resource link: the tool points at a resource instead of embedding it.
        uri = getattr(block, "uri", None)
        if block_type == "resource_link" or uri:
            details = [f"uri={uri}"]
            for attr, label in (("name", "name"), ("mime_type", "mimeType")):
                val = _mime_of(block) if attr == "mime_type" else getattr(block, attr, None)
                if val:
                    details.append(f"{label}={val}")
            return (
                f"[MCP resource link: {', '.join(details)} — fetch it with "
                f"read_mcp_resource(uri='{uri}')]"
            )

        return f"[MCP content dropped: unsupported block type={block_type or type(block).__name__}]"

    def _extract_result_data(self, result) -> Any:
        """Extract usable data from an MCP CallToolResult.

        One payload comes back as itself (parsed JSON or a string); several
        text parts are joined into one text; mixed parts come back as a list.
        File blobs (embedded resources, images, audio) are not part of the data
        — ``_extract_binaries`` hands them to execute_mcp to save as files.
        """
        if not hasattr(result, "content") or not result.content:
            return None

        parts = [p for p in (self._block_part(c) for c in result.content) if p is not None]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        if all(isinstance(p, str) for p in parts):
            return "\n\n".join(parts)
        return parts

    @staticmethod
    def _extract_binaries(result) -> List[Dict[str, Any]]:
        """Pull file blobs out of a tool result's content blocks.

        Covers an EmbeddedResource whose `.resource` is a BlobResourceContents
        (base64 `blob` + `mimeType` + `uri`), image/audio content (base64
        `data` + `mimeType`), and a content block with a top-level `blob`.
        Returns a list of {blob_b64, mime_type, uri} for execute_mcp to
        materialize.
        """
        out: List[Dict[str, Any]] = []
        for c in (getattr(result, "content", None) or []):
            res = getattr(c, "resource", None)
            src = res if res is not None else c
            blob = getattr(src, "blob", None)
            if not blob and res is None and getattr(c, "type", None) in ("image", "audio"):
                blob = getattr(c, "data", None)
            if not blob:
                continue
            mime = _mime_of(src)
            uri = getattr(src, "uri", None)
            out.append({
                "blob_b64": blob,
                "mime_type": mime or "application/octet-stream",
                "uri": str(uri) if uri else None,
            })
        return out

    @staticmethod
    def _extract_error_message(data: Any) -> str:
        """Pull a human-readable error string out of an isError tool result.

        MCP servers signal tool-level failures with isError=True and the reason
        carried in the content blocks (already parsed into `data` by
        _extract_result_data). The shape varies — a dict like
        {"error": "..."} / {"message": "..."}, a list of such dicts, or a
        plain string. Fall back to a stringified form so the caller always gets
        something more useful than None.
        """
        def _from_dict(d: dict) -> str | None:
            for key in ("error", "message", "detail", "error_message"):
                val = d.get(key)
                if isinstance(val, str) and val.strip():
                    return val
                if isinstance(val, dict):
                    nested = _from_dict(val)
                    if nested:
                        return nested
            return None

        if isinstance(data, dict):
            return _from_dict(data) or json.dumps(data, default=str)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    msg = _from_dict(item)
                    if msg:
                        return msg
                elif isinstance(item, str) and item.strip():
                    return item
            return json.dumps(data, default=str) if data else "Tool reported an error"
        if isinstance(data, str) and data.strip():
            return data
        return "Tool reported an error"

    @staticmethod
    def _unwrap_exception(e: BaseException) -> str:
        """Extract the root cause message from ExceptionGroup or nested exceptions."""
        if isinstance(e, BaseExceptionGroup):
            for exc in e.exceptions:
                return McpClient._unwrap_exception(exc)
        return str(e)

    def _detect_content_type(self, data: Any) -> str:
        """Detect whether data is tabular, text, or generic JSON.

        Recognizes envelope-wrapped tables ({"data": [...], "pages": {...}}),
        not just bare top-level arrays — see app.utils.tabular_payload.
        """
        return detect_content_type(data)

    def _connect(self):
        """
        Open an MCP client for the configured transport.
        Returns an async context manager that yields a connected ``mcp.Client``.

        Streamable HTTP negotiates the protocol era itself (``mode="auto"``): a
        2026-07-28+ server is spoken to statelessly (version and capabilities on
        every request), an older one gets the ``initialize`` handshake. HTTP+SSE
        only ever existed before 2026-07-28, so it goes straight to the
        handshake. Either way the client advertises the MCP Apps extension
        (``io.modelcontextprotocol/ui``) so servers offer their UI-bound tools.
        """
        from contextlib import asynccontextmanager
        from mcp import Client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamable_http_client
        # Pinned admin-tier client: metadata/link-local refused, private only
        # with block_private_outbound, DNS rebinding closed (app.core.egress).
        from app.core.egress import Policy, mcp_client_factory, mcp_http_client

        @asynccontextmanager
        async def _session():
            if self.transport == "sse":
                transport = sse_client(
                    url=self.server_url,
                    headers=self._build_headers(),
                    httpx_client_factory=mcp_client_factory(Policy.ADMIN),
                )
                async with Client(transport, mode="legacy", **_client_options()) as client:
                    yield client

            elif self.transport == "streamable_http":
                async with mcp_http_client(Policy.ADMIN, headers=self._build_headers()) as http:
                    transport = streamable_http_client(self.server_url, http_client=http)
                    async with Client(transport, mode="auto", **_client_options()) as client:
                        yield client

            else:
                raise ValueError(
                    f"Unsupported MCP transport: {self.transport}. "
                    "Supported: 'sse', 'streamable_http'"
                )

        return _session()

    def _call_session(self):
        """A session for a tool call: pooled and reused when possible.

        See ``mcp_session_pool``: reuse skips the initialize handshake and the
        SDK's tools/list after each call on a fresh session. Pooling is off
        when ``BOW_MCP_POOL_SIZE=0``; then every call opens its own session.
        """
        from app.data_sources.clients.mcp_session_pool import POOL, pool_size, session_key

        if pool_size() <= 0:
            return self._connect()
        key = session_key(self.transport, self.server_url, self._build_headers())
        return POOL.session(key, self._connect)

    def test_connection(self) -> Dict[str, Any]:
        """Test connectivity by attempting to initialize and list tools."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self._atest_connection())

    async def _atest_connection(self) -> Dict[str, Any]:
        try:
            async with self._connect() as session:
                result = await session.list_tools()
                tool_count = len(result.tools) if result.tools else 0
                return {
                    "success": True,
                    "message": f"Connected to MCP server. {tool_count} tool(s) available.",
                }
        except BaseException as e:
            return {
                "success": False,
                "message": f"Failed to connect to MCP server: {self._unwrap_exception(e)}",
            }

    # Override async wrappers to use native async implementations
    async def alist_tools(self) -> List[Dict[str, Any]]:
        self.reset_discovery()
        result = await self._alist_tools()
        from app.data_sources.clients.discovery import require_complete_discovery
        return require_complete_discovery(self, result)

    async def acall_tool(self, tool_name: str, arguments: Dict[str, Any], read_only: bool = False) -> Dict[str, Any]:
        return await self._acall_tool(tool_name, arguments, read_only=read_only)

    async def atest_connection(self) -> Dict[str, Any]:
        return await self._atest_connection()
