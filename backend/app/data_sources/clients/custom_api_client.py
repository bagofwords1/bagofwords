import logging
from typing import List, Dict, Any, Optional
from app.data_sources.clients.tool_provider_base import ToolProviderClient

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        base_url: str,
        auth_type: str = "none",
        token: Optional[str] = None,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_key_header: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        endpoints: Optional[List[Dict[str, Any]]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type
        # For per-user OAuth (oauth_app/oauth) the token arrives as access_token;
        # for a static bearer it arrives as token. Accept either.
        self.token = token or access_token
        self.api_key = api_key
        self.api_key_header = api_key_header or "X-API-Key"
        self.custom_headers = headers or {}
        self.endpoints = endpoints or []

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers with auth and custom headers."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth_type in self._BEARER_AUTH_TYPES and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.auth_type == "api_key" and self.api_key:
            headers[self.api_key_header] = self.api_key
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

    def list_tools(self) -> List[Dict[str, Any]]:
        """Convert endpoint definitions to tool format."""
        tools = []
        for ep in self.endpoints:
            # Build JSON Schema from parameter definitions
            properties = {}
            required = []
            for param in ep.get("parameters", []):
                prop = {"type": param.get("type", "string")}
                if param.get("description"):
                    prop["description"] = param["description"]
                properties[param["name"]] = prop
                if param.get("required", False):
                    required.append(param["name"])

            input_schema = {
                "type": "object",
                "properties": properties,
            }
            if required:
                input_schema["required"] = required

            tool = {
                "name": ep["name"],
                "description": ep.get("description", ""),
                "input_schema": input_schema,
                "output_schema": {},
            }
            # Hint the discovery layer to gate write endpoints behind
            # confirmation. Absent → the caller keeps its own default.
            default_policy = self._endpoint_default_policy(ep)
            if default_policy:
                tool["default_policy"] = default_policy
            tools.append(tool)
        return tools

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an API endpoint as a tool call."""
        import httpx

        ep = self._find_endpoint(tool_name)
        if not ep:
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": f"Endpoint '{tool_name}' not found in configuration",
            }

        try:
            method = ep.get("method", "GET").upper()
            path = ep.get("path", "/")
            params = ep.get("parameters", [])

            # Substitute path parameters and separate query/body params
            query_params = {}
            body_params = {}
            for param_def in params:
                name = param_def["name"]
                if name not in arguments:
                    continue
                location = param_def.get("in", "query")
                if location == "path":
                    path = path.replace(f"{{{name}}}", str(arguments[name]))
                elif location == "query":
                    query_params[name] = arguments[name]
                elif location == "body":
                    body_params[name] = arguments[name]

            url = f"{self.base_url}{path}"
            headers = self._build_headers()

            with httpx.Client(timeout=30.0) as client:
                if method in ("GET", "DELETE", "HEAD"):
                    response = client.request(method, url, headers=headers, params=query_params)
                else:
                    response = client.request(
                        method, url, headers=headers,
                        params=query_params, json=body_params or arguments,
                    )

            response.raise_for_status()

            # Parse response
            content_type_header = response.headers.get("content-type", "")
            if "application/json" in content_type_header:
                data = response.json()
                content_type = self._detect_content_type(data)
            else:
                data = response.text
                content_type = "text"

            return {
                "success": True,
                "data": data,
                "content_type": content_type,
                "error": None,
            }

        except httpx.HTTPStatusError as e:
            logger.error(f"API call failed: {tool_name}: {e.response.status_code} {e.response.text[:500]}")
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": f"HTTP {e.response.status_code}: {e.response.text[:500]}",
            }
        except Exception as e:
            logger.error(f"API call failed: {tool_name}: {e}")
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": str(e),
            }

    def _detect_content_type(self, data: Any) -> str:
        """Detect whether data is tabular, text, or generic JSON."""
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            return "tabular"
        if isinstance(data, str):
            return "text"
        return "json"

    def test_connection(self) -> Dict[str, Any]:
        """Test connectivity by sending a HEAD request to the base URL.

        A response is only a *success* when the API actually answers OK. An
        error status (404 wrong base URL, 401/403 bad credentials, 5xx) means we
        reached a host but the connection is not usable, so it must not be
        reported as green/connected. 405 is treated as reachable because many
        APIs reject HEAD on the base path while still being valid.
        """
        import httpx

        try:
            headers = self._build_headers()
            with httpx.Client(timeout=10.0) as client:
                response = client.head(self.base_url, headers=headers)
            status = response.status_code
            if status < 400 or status == 405:
                return {
                    "success": True,
                    "message": f"Connected to API at {self.base_url} (HTTP {status})",
                }
            return {
                "success": False,
                "message": (
                    f"Reached {self.base_url} but it returned HTTP {status} — "
                    f"check the base URL, endpoint path, and credentials."
                ),
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to connect to API: {e}",
            }
