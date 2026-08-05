import base64
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.clients.tool_provider_base import ToolProviderClient
from app.utils.tabular_payload import detect_content_type

logger = logging.getLogger(__name__)

# Above this, a response body is NOT parsed into Python objects — mirrors
# mcp_client.MAX_PARSE_CHARS. json.loads on a huge response costs several
# times its size in objects; the payload's destination is a file anyway
# (execute_mcp materializes every byte), so a giant body goes downstream as a
# raw string flagged `parse_skipped` instead of being parsed here.
MAX_PARSE_CHARS = 5_000_000

# Response media types treated as text rather than binary. Anything not JSON
# and not matching these is handed back as a binary blob for materialization.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = (
    "application/xml", "application/xhtml+xml", "application/javascript",
    "application/x-ndjson", "application/csv", "application/x-yaml",
    "application/yaml", "application/sql", "application/graphql",
)

# JSON Schema types a parameter may declare. `object`/`array` let an endpoint
# accept structured payloads (nested filters, lists of records) — the shape
# can be refined further with the parameter's optional `schema` field.
_ALLOWED_PARAM_TYPES = (
    "string", "number", "integer", "boolean", "object", "array",
)


def looks_like_json(text: Any) -> bool:
    """Would this string have parsed as JSON, if we had tried? (Cheap prefix
    check — used when the parse cap made us skip the real parse.)"""
    if not isinstance(text, str):
        return False
    head = text.lstrip()[:1]
    return head in ("{", "[")


class CustomApiClient(ToolProviderClient):
    """
    Client for connecting to custom REST API endpoints.
    Endpoints are defined declaratively in the connection config
    and exposed as callable tools.

    Class name follows the dynamic import convention in Connection.get_client()
    which title-cases each word: "custom_api" → "CustomApi" → "CustomApiClient".
    """

    # Auth types that authenticate every request with a Bearer token. `oauth_app`
    # / `oauth` resolve a per-user OAuth access_token (refreshed upstream in
    # resolve_credentials); `bearer` uses a static admin token.
    _BEARER_AUTH_TYPES = ("bearer", "oauth_app", "oauth")
    # HTTP methods that mutate state — their tools default to "ask" (confirm
    # before running) so an agent can't silently post/delete on a user's behalf.
    _WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
    # Methods whose requests carry no body.
    _BODYLESS_METHODS = ("GET", "DELETE", "HEAD")

    # Request timeout in seconds; an endpoint can override with `timeout`.
    _DEFAULT_TIMEOUT_S = 30.0
    _MAX_TIMEOUT_S = 120.0

    def __init__(
        self,
        base_url: str,
        auth_type: str = "none",
        token: Optional[str] = None,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_key_header: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        endpoints: Optional[List[Dict[str, Any]]] = None,
        csrf_token_flow: bool = False,
        csrf_fetch_path: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type
        # For per-user OAuth (oauth_app/oauth) the token arrives as access_token;
        # for a static bearer it arrives as token. Accept either.
        self.token = token or access_token
        self.api_key = api_key
        self.api_key_header = api_key_header or "X-API-Key"
        self.username = username
        self.password = password
        self.custom_headers = headers or {}
        self.endpoints = endpoints or []
        # SAP Gateway-style CSRF handshake: write requests must carry a token
        # obtained via a GET with `X-CSRF-Token: Fetch`. The token is bound to
        # the session cookies of the fetch response, so fetch and write must
        # share one HTTP session (see call_tool).
        self.csrf_token_flow = bool(csrf_token_flow)
        self.csrf_fetch_path = csrf_fetch_path or "/"

    @property
    def server_url(self) -> str:
        """Parity with McpClient — execute_mcp logs/loopback-checks this."""
        return self.base_url

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers with auth and custom headers."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth_type in self._BEARER_AUTH_TYPES and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.auth_type == "api_key" and self.api_key:
            headers[self.api_key_header] = self.api_key
        elif self.auth_type == "basic" and self.username is not None:
            userpass = f"{self.username}:{self.password or ''}"
            encoded = base64.b64encode(userpass.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        headers.update(self.custom_headers)
        return headers

    def _endpoint_default_policy(self, ep: Dict[str, Any]) -> Optional[str]:
        """The tool policy a newly-discovered endpoint should default to.

        A write endpoint (POST/PUT/PATCH/DELETE) — or one flagged ``confirm:
        true`` — defaults to "ask" so it requires confirmation before running.
        Read endpoints return None (caller keeps its own default, "allow").
        """
        if ep.get("confirm") is True:
            return "ask"
        if ep.get("confirm") is False:
            return None
        method = (ep.get("method") or "GET").upper()
        return "ask" if method in self._WRITE_METHODS else None

    def _find_endpoint(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Find an endpoint definition by tool name."""
        for ep in self.endpoints:
            if ep.get("name") == tool_name:
                return ep
        return None

    @staticmethod
    def _is_pinned(param: Dict[str, Any]) -> bool:
        """A pinned parameter carries an admin-fixed ``value`` (e.g. SAP's
        ``sap-client=400``). It is injected server-side on every call, never
        appears in the tool's input schema, and wins over any model-supplied
        argument of the same name — otherwise a pinned identity/tenant field
        would be spoofable from the prompt."""
        return "value" in param

    @staticmethod
    def _param_json_schema(param: Dict[str, Any]) -> Dict[str, Any]:
        """One parameter definition → its JSON Schema property.

        Beyond the basic ``type``, honors the fields that make complex inputs
        describable — ``enum``, ``default``, ``example``, array ``items``,
        object ``properties``/``required`` — and a full ``schema`` override for
        anything richer. Without these, every endpoint parameter collapses to a
        bare {"type": "string"} and the agent has to guess nested shapes.
        """
        # Full JSON Schema escape hatch: the param's `schema` wins wholesale.
        override = param.get("schema")
        if isinstance(override, dict) and override:
            prop = dict(override)
            if param.get("description") and "description" not in prop:
                prop["description"] = param["description"]
            return prop

        declared = param.get("type", "string")
        prop: Dict[str, Any] = {
            "type": declared if declared in _ALLOWED_PARAM_TYPES else "string"
        }
        if param.get("description"):
            prop["description"] = param["description"]
        if isinstance(param.get("enum"), list) and param["enum"]:
            prop["enum"] = param["enum"]
        if "default" in param:
            prop["default"] = param["default"]
        if "example" in param:
            prop["examples"] = [param["example"]]
        if prop["type"] == "array" and isinstance(param.get("items"), dict):
            prop["items"] = param["items"]
        if prop["type"] == "object":
            if isinstance(param.get("properties"), dict):
                prop["properties"] = param["properties"]
            if isinstance(param.get("required_properties"), list):
                prop["required"] = param["required_properties"]
        return prop

    def list_tools(self) -> List[Dict[str, Any]]:
        """Convert endpoint definitions to tool format."""
        tools = []
        for ep in self.endpoints:
            # Build JSON Schema from parameter definitions
            properties = {}
            required = []
            for param in ep.get("parameters", []):
                if not param.get("name"):
                    continue
                # Pinned params are admin-supplied constants — the model never
                # sees them, so they stay out of the input schema.
                if self._is_pinned(param):
                    continue
                properties[param["name"]] = self._param_json_schema(param)
                if param.get("required", False):
                    required.append(param["name"])

            input_schema = {
                "type": "object",
                "properties": properties,
            }
            if required:
                input_schema["required"] = required

            method = (ep.get("method") or "GET").upper()
            tool = {
                "name": ep["name"],
                "description": ep.get("description", ""),
                "input_schema": input_schema,
                "output_schema": {},
                # HTTP identity of the tool — persisted to
                # ConnectionTool.metadata_json so the UI can show what the
                # tool actually calls (method chip + path).
                "metadata": {"method": method, "path": ep.get("path", "/")},
            }
            # Hint the discovery layer to gate write endpoints behind
            # confirmation. Absent → the caller keeps its own default.
            default_policy = self._endpoint_default_policy(ep)
            if default_policy:
                tool["default_policy"] = default_policy
            # An admin's explicit Ask/Allow choice (confirm: true/false in the
            # endpoint definition) writes through on every refresh; the
            # method-inferred default above only seeds newly-created tools.
            if isinstance(ep.get("confirm"), bool):
                tool["explicit_policy"] = "ask" if ep["confirm"] else "allow"
            tools.append(tool)
        return tools

    def _prepare_request(
        self, ep: Dict[str, Any], arguments: Dict[str, Any]
    ) -> Tuple[str, str, Dict[str, Any], Optional[Dict[str, Any]], Dict[str, str]]:
        """Resolve one call into (method, url, query, body, extra_headers).

        Each declared parameter is routed by its ``in`` location (path, query,
        body, header). Arguments for undeclared names are NOT silently dropped:
        for body-capable methods they fall through into the JSON body (many
        real APIs accept more fields than an admin bothered to declare), and
        for body-less methods they become query params.
        """
        method = (ep.get("method") or "GET").upper()
        path = ep.get("path", "/")
        params = ep.get("parameters", [])

        query_params: Dict[str, Any] = {}
        body_params: Dict[str, Any] = {}
        header_params: Dict[str, str] = {}
        consumed = set()
        for param_def in params:
            name = param_def.get("name")
            if not name:
                continue
            location = param_def.get("in", "query")
            if self._is_pinned(param_def):
                # Pinned value always applies and always wins: consume the name
                # so a model-supplied argument of the same name can't override
                # or leak through the leftovers pass below.
                consumed.add(name)
                value = param_def["value"]
            elif name in arguments:
                consumed.add(name)
                value = arguments[name]
            else:
                continue
            if location == "path":
                path = path.replace(f"{{{name}}}", str(value))
            elif location == "query":
                query_params[name] = value
            elif location == "header":
                header_params[name] = str(value)
            else:  # body
                body_params[name] = value

        # Route arguments the endpoint didn't declare — dropping them silently
        # (the old behavior for GET) turns a slightly-off tool call into a
        # response that ignores the filter the agent asked for.
        leftovers = {k: v for k, v in arguments.items() if k not in consumed}
        has_body = method not in self._BODYLESS_METHODS
        if leftovers:
            if has_body:
                body_params.update(leftovers)
            else:
                query_params.update(leftovers)

        url = f"{self.base_url}{path}"
        body = body_params if has_body else None
        return method, url, query_params, body, header_params

    def _timeout_for(self, ep: Dict[str, Any]) -> float:
        try:
            t = float(ep.get("timeout") or self._DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            return self._DEFAULT_TIMEOUT_S
        return max(1.0, min(self._MAX_TIMEOUT_S, t))

    def _fetch_csrf_token(
        self, client, headers: Dict[str, str],
        query_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """GET the fetch path with ``X-CSRF-Token: Fetch`` and return the token.

        The token arrives in the response header and is bound to the session
        cookies the client just stored — which is why the caller passes in the
        same ``httpx.Client`` that will send the write. Any response status may
        carry a token (SAP answers 200 on ``$metadata`` but 404 on ``/`` while
        still issuing one), so the status is deliberately not checked.

        ``query_params`` are the write call's own query params — forwarded so
        tenant selectors like SAP's ``sap-client`` (usually a pinned param)
        reach the fetch request too; without them SAP rejects the fetch and no
        token ever arrives.
        """
        fetch_headers = dict(headers)
        fetch_headers["X-CSRF-Token"] = "Fetch"
        fetch_headers.pop("Content-Type", None)
        try:
            resp = client.get(
                f"{self.base_url}{self.csrf_fetch_path}",
                headers=fetch_headers,
                params=query_params or None,
            )
            return resp.headers.get("x-csrf-token") or None
        except Exception as e:
            logger.warning(f"CSRF token fetch failed: {e}")
            return None

    @staticmethod
    def _csrf_rejected(response) -> bool:
        """Did the server refuse this request over a missing/stale CSRF token?
        SAP Gateway answers 403 with ``x-csrf-token: Required``; be lenient and
        also accept a 403 whose body mentions CSRF."""
        if response.status_code != 403:
            return False
        if (response.headers.get("x-csrf-token") or "").strip().lower() == "required":
            return True
        return "csrf" in (response.text or "")[:2000].lower()

    @staticmethod
    def _is_texty(mime: str) -> bool:
        mime = (mime or "").split(";")[0].strip().lower()
        if not mime:
            return True
        if mime.startswith(_TEXT_MIME_PREFIXES):
            return True
        if mime in _TEXT_MIME_EXACT:
            return True
        # application/json itself, plus any structured +json / +xml suffix type.
        return "json" in mime or mime.endswith("+xml")

    @staticmethod
    def _extract_error_message(response) -> str:
        """A useful error string from a non-2xx response.

        APIs put the reason in a JSON body ({"error": ...}, {"message": ...},
        {"detail": ...}); surfacing that beats dumping 500 chars of HTML at the
        agent. Falls back to truncated body text.
        """
        text = response.text or ""
        try:
            payload = response.json()
        except Exception:
            payload = None

        def _from(d: Any, depth: int = 0) -> Optional[str]:
            if depth > 3:
                return None
            if isinstance(d, str) and d.strip():
                return d
            if isinstance(d, dict):
                for key in ("error", "message", "detail", "error_message",
                            "error_description", "errors", "title"):
                    if key in d:
                        found = _from(d[key], depth + 1)
                        if found:
                            return found
            if isinstance(d, list):
                for item in d:
                    found = _from(item, depth + 1)
                    if found:
                        return found
            return None

        msg = _from(payload) if payload is not None else None
        if not msg:
            msg = text[:500]
        return f"HTTP {response.status_code}: {msg}"

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an API endpoint as a tool call."""
        import httpx

        ep = self._find_endpoint(tool_name)
        if not ep:
            known = ", ".join(e.get("name", "?") for e in self.endpoints) or "none"
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": (
                    f"Endpoint '{tool_name}' not found in configuration. "
                    f"Configured endpoints: {known}"
                ),
            }

        try:
            method, url, query_params, body, header_params = self._prepare_request(
                ep, arguments or {}
            )
            headers = self._build_headers()
            headers.update(header_params)

            def _send(client, hdrs):
                if body is None:
                    return client.request(method, url, headers=hdrs, params=query_params)
                return client.request(
                    method, url, headers=hdrs,
                    params=query_params, json=body,
                )

            needs_csrf = self.csrf_token_flow and method in self._WRITE_METHODS
            with httpx.Client(timeout=self._timeout_for(ep), follow_redirects=True) as client:
                if needs_csrf:
                    token = self._fetch_csrf_token(client, headers, query_params)
                    if token:
                        headers["X-CSRF-Token"] = token
                response = _send(client, headers)
                # One retry with a fresh token: server-side sessions expire,
                # and a stale token comes back as 403 x-csrf-token: Required.
                if needs_csrf and self._csrf_rejected(response):
                    token = self._fetch_csrf_token(client, headers, query_params)
                    if token:
                        headers["X-CSRF-Token"] = token
                        response = _send(client, headers)

            if response.status_code >= 400:
                error = self._extract_error_message(response)
                logger.error(f"API call failed: {tool_name}: {error}")
                return {
                    "success": False,
                    "data": None,
                    "content_type": "text",
                    "error": error,
                }

            return self._build_result(tool_name, response)

        except httpx.TimeoutException:
            timeout_s = self._timeout_for(ep)
            logger.error(f"API call timed out: {tool_name} after {timeout_s}s")
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": f"Request timed out after {timeout_s:.0f}s",
            }
        except Exception as e:
            logger.error(f"API call failed: {tool_name}: {e}")
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": str(e),
            }

    def _build_result(self, tool_name: str, response) -> Dict[str, Any]:
        """Shape a 2xx response into the ToolProviderClient result contract.

        Mirrors McpClient: JSON is parsed (below the parse cap), oversized
        JSON goes downstream raw with ``parse_skipped`` set, non-text bodies
        are surfaced as ``binaries`` for execute_mcp to materialize into a
        session file.
        """
        mime = response.headers.get("content-type", "")

        # Binary payload (PDF, image, zip, …) → hand back the bytes for
        # materialization instead of mangling them through .text.
        if not self._is_texty(mime):
            blob = response.content or b""
            uri = str(response.request.url) if response.request is not None else tool_name
            return {
                "success": True,
                "data": (
                    f"Binary response ({mime or 'application/octet-stream'}, "
                    f"{len(blob):,} bytes) — saved as a file."
                ),
                "content_type": "text",
                "binaries": [{
                    "blob_b64": base64.b64encode(blob).decode("ascii"),
                    "mime_type": (mime or "application/octet-stream").split(";")[0].strip(),
                    "uri": uri,
                }],
                "error": None,
            }

        text = response.text or ""
        is_json_mime = "json" in (mime or "").lower()

        if len(text) > MAX_PARSE_CHARS:
            # Too big to parse in-process; every byte still reaches the file.
            parse_skipped = is_json_mime or looks_like_json(text)
            logger.warning(
                "API result of %d chars exceeds the %d-char parse cap; "
                "returning it unparsed.", len(text), MAX_PARSE_CHARS,
            )
            return {
                "success": True,
                "data": text,
                "content_type": "text",
                "error": None,
                "parse_skipped": parse_skipped,
                "size_chars": len(text),
            }

        if is_json_mime:
            try:
                data = response.json()
                return {
                    "success": True,
                    "data": data,
                    "content_type": self._detect_content_type(data),
                    "error": None,
                }
            except json.JSONDecodeError:
                pass  # declared JSON but isn't — fall through to text

        # Some APIs return JSON with a text/plain content-type; don't lose the
        # structure over a mislabeled header.
        if looks_like_json(text):
            try:
                data = json.loads(text)
                return {
                    "success": True,
                    "data": data,
                    "content_type": self._detect_content_type(data),
                    "error": None,
                }
            except json.JSONDecodeError:
                pass

        return {
            "success": True,
            "data": text,
            "content_type": "text",
            "error": None,
        }

    def _detect_content_type(self, data: Any) -> str:
        """Detect whether data is tabular, text, or generic JSON.

        Recognizes envelope-wrapped tables ({"data": [...], "pages": {...}}),
        not just bare top-level arrays — see app.utils.tabular_payload.
        """
        return detect_content_type(data)

    # Auth types that sign in per user (X, …). For these the base URL is an API
    # root that legitimately answers 4xx (404 no resource at "/", 401/403 auth
    # required) and there's no user token at setup time — so reaching the host
    # at all IS the only thing we can, and need to, verify.
    _PER_USER_OAUTH_TYPES = ("oauth_app", "oauth")

    def test_connection(self) -> Dict[str, Any]:
        """Test connectivity by sending a HEAD request to the base URL.

        For a data API (none/bearer/api_key/basic) a response is only a
        *success* when the API answers OK — an error status (404 wrong base
        URL, 401/403 bad credentials, 5xx) means we reached a host but the
        connection is not usable. 405 is treated as reachable because many
        APIs reject HEAD on the base path while still being valid.

        For a per-user OAuth API (oauth_app/oauth) the base root commonly answers
        4xx even when the API is perfectly usable, and the per-user token that
        would make it 2xx doesn't exist until the user signs in. Getting any
        (non-5xx) HTTP response there proves the host is reachable — which is the
        real signal. Treating a root 404 as failure here would wrongly roll back
        a valid token at the OAuth callback and block sign-in entirely.
        """
        import httpx

        is_per_user_oauth = self.auth_type in self._PER_USER_OAUTH_TYPES

        try:
            headers = self._build_headers()
            with httpx.Client(timeout=10.0) as client:
                response = client.head(self.base_url, headers=headers)
            status = response.status_code
            ok = status < 400 or status == 405
            if is_per_user_oauth and status < 500:
                # Any answer from the host (incl. 401/403/404) = reachable.
                ok = True
            # Any HTTP response below 5xx proves the HOST is reachable, even
            # when the probe itself failed (an API with no root route answers
            # 404 at "/" while its endpoints work fine — probing the root is a
            # heuristic, not truth). Callers that must not hard-block on the
            # heuristic (saving a connection) check `reachable`, while the
            # explicit Test button surfaces `success`/`message` as a warning.
            reachable = status < 500
            if ok:
                if is_per_user_oauth:
                    return {
                        "success": True,
                        "reachable": True,
                        "message": (
                            f"Server reachable at {self.base_url} — sign-in required. "
                            f"Tools run with each user's own token after they sign in."
                        ),
                    }
                return {
                    "success": True,
                    "reachable": True,
                    "message": f"Connected to API at {self.base_url} (HTTP {status})",
                }
            if status == 404:
                message = (
                    f"Reached {self.base_url} but its root answered HTTP 404. "
                    f"That can be normal for APIs without a root route — use "
                    f"each tool's Test to verify endpoints, or check the base URL."
                )
            elif status in (401, 403):
                message = (
                    f"Reached {self.base_url} but it answered HTTP {status} — "
                    f"check the credentials."
                )
            else:
                message = (
                    f"Reached {self.base_url} but it returned HTTP {status} — "
                    f"check the base URL, endpoint path, and credentials."
                )
            return {
                "success": False,
                "reachable": reachable,
                "message": message,
            }
        except Exception as e:
            return {
                "success": False,
                "reachable": False,
                "message": f"Failed to connect to API: {e}",
            }
