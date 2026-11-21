"""Write actions for built-in connectors (Outlook, Teams, ...).

A built-in connector that can act — not just read — declares a list of
:class:`ConnectorAction`. Each action becomes a ``ConnectionTool`` row on the
connection (``source="native"``), so it goes through exactly the same path as
an MCP or custom-API tool: the connection's Tools tab, per-agent rules, the
run's tool list, policy (allow / ask / auto / deny), the approval card and the
audit trail. Nothing here decides whether a call may run; the policy layer
does that before :meth:`ConnectorActionsMixin.acall_tool` is ever reached.

What each action declares:

- ``effect``: ``modify`` (changes the user's own state, can be undone),
  ``external`` (others see it, cannot be taken back) or ``destructive``.
  The effect sets the tool's default and which policies an admin may pick.
- ``required_scopes``: the permissions the token must carry (any one of each
  inner tuple). A token that lacks them gets a clear refusal, and the Tools
  tab shows what is missing.
- ``preview(args)``: one human-readable line for the approval card.

Writes are never retried: a dropped connection after the request left may
mean it already ran (Graph has no idempotency keys).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EFFECT_READ = "read"
EFFECT_MODIFY = "modify"
EFFECT_EXTERNAL = "external"
EFFECT_DESTRUCTIVE = "destructive"
EFFECTS = (EFFECT_READ, EFFECT_MODIFY, EFFECT_EXTERNAL, EFFECT_DESTRUCTIVE)

# (enabled on the connection, policy) a newly discovered tool starts with.
EFFECT_DEFAULTS: Dict[str, Tuple[bool, str]] = {
    EFFECT_READ: (True, "allow"),
    EFFECT_MODIFY: (False, "ask"),
    EFFECT_EXTERNAL: (False, "ask"),
    EFFECT_DESTRUCTIVE: (False, "deny"),
}

# Policies an admin, an agent or a user may choose, by effect. External
# actions leave the system and cannot be undone, so a model judge (auto) is
# not enough; destructive ones always need a human or stay off.
ALLOWED_POLICIES: Dict[str, Tuple[str, ...]] = {
    EFFECT_READ: ("allow", "ask", "auto", "deny"),
    EFFECT_MODIFY: ("allow", "ask", "auto", "deny"),
    EFFECT_EXTERNAL: ("allow", "ask", "deny"),
    EFFECT_DESTRUCTIVE: ("ask", "deny"),
}


def allowed_policies(effect: Optional[str]) -> Tuple[str, ...]:
    return ALLOWED_POLICIES.get(effect or "", ALLOWED_POLICIES[EFFECT_READ])


def effect_from_hints(metadata: Any) -> Optional[str]:
    """An MCP tool's effect from its annotations (readOnlyHint / destructiveHint)."""
    hints = (metadata or {}).get("annotations") if isinstance(metadata, dict) else None
    if not isinstance(hints, dict):
        return None
    if hints.get("readOnlyHint") is True:
        return EFFECT_READ
    if hints.get("destructiveHint") is True:
        return EFFECT_DESTRUCTIVE
    if hints.get("openWorldHint") is True:
        return EFFECT_EXTERNAL
    if hints.get("readOnlyHint") is False:
        return EFFECT_MODIFY
    return None


class ActionError(Exception):
    """A refusal or failure to report back to the agent as-is."""


@dataclass(frozen=True)
class ConnectorAction:
    name: str
    description: str
    effect: str
    input_schema: Dict[str, Any]
    # Each inner tuple is satisfied by any one of its scopes.
    required_scopes: Tuple[Tuple[str, ...], ...] = ()
    # Method on the client: (self, **arguments) -> dict (the result payload).
    handler: str = ""
    preview: Callable[[Dict[str, Any]], str] = field(default=lambda args: "")

    def scope_names(self) -> List[str]:
        return [alts[0] for alts in self.required_scopes]

    def to_tool(self) -> Dict[str, Any]:
        """The ConnectionTool shape (name/description/schemas + metadata)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "effect": self.effect,
            "metadata": {
                "native": True,
                "effect": self.effect,
                "required_scopes": [list(a) for a in self.required_scopes],
                "annotations": {
                    "readOnlyHint": self.effect == EFFECT_READ,
                    "destructiveHint": self.effect == EFFECT_DESTRUCTIVE,
                    "openWorldHint": self.effect == EFFECT_EXTERNAL,
                },
            },
        }


def _jwt_claims(token: Optional[str]) -> Dict[str, Any]:
    """The unverified claims of a JWT — only to read which scopes it carries.

    Never used to authorize anything: the API verifies the token itself; this
    only lets BOW explain a missing permission before (or instead of) a 403.
    """
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return {}


def scopes_in_token(token: Optional[str]) -> Optional[set]:
    """Delegated ``scp`` and app ``roles`` of a token; None when unreadable
    (an opaque token — permissions unknown, so nothing is refused up front)."""
    claims = _jwt_claims(token)
    if not claims:
        return None
    out = set(str(claims.get("scp") or "").split())
    roles = claims.get("roles")
    if isinstance(roles, list):
        out |= {str(r) for r in roles}
    return out


def missing_scopes(action: ConnectorAction, granted: Optional[Iterable[str]]) -> List[str]:
    if granted is None:
        return []
    have = {g.lower() for g in granted}
    return [alts[0] for alts in action.required_scopes if not any(a.lower() in have for a in alts)]


class ConnectorActionsMixin:
    """Gives a built-in client the tool-provider surface execute_mcp calls."""

    actions: ClassVar[Sequence[ConnectorAction]] = ()

    @classmethod
    def action_by_name(cls, name: str) -> Optional[ConnectorAction]:
        return next((a for a in cls.actions if a.name == name), None)

    @classmethod
    def action_tools(cls) -> List[Dict[str, Any]]:
        return [a.to_tool() for a in cls.actions]

    def granted_scopes(self) -> Optional[set]:
        token = getattr(self, "access_token", None)
        return scopes_in_token(token)

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        action = self.action_by_name(tool_name)
        if action is None:
            return {"success": False, "error": f"Unknown action '{tool_name}'.", "content_type": "text"}
        try:
            # Mint (or read) the token first, so the permission check sees it.
            if hasattr(self, "_token"):
                self._token()
            missing = missing_scopes(action, self.granted_scopes())
            if missing:
                raise ActionError(
                    f"The connection's Microsoft sign-in does not include {', '.join(missing)}. "
                    "An admin must grant it in the Entra app registration, then the user signs in again "
                    "from the connection's Tools tab."
                )
            data = getattr(self, action.handler)(**(arguments or {}))
            return {"success": True, "data": data, "content_type": "json", "error": None}
        except ActionError as e:
            return {"success": False, "error": str(e), "content_type": "text", "error_code": "refused"}
        except TypeError as e:
            return {"success": False, "error": f"Invalid arguments for {tool_name}: {e}", "content_type": "text"}
        except Exception as e:  # surfaced to the agent, never retried
            logger.warning("connector action %s failed: %s", tool_name, e)
            return {"success": False, "error": f"{tool_name} failed: {e}", "content_type": "text"}

    async def acall_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self.call_tool, tool_name, arguments)


def client_actions(client_cls) -> Sequence[ConnectorAction]:
    return getattr(client_cls, "actions", ()) or ()


# Kept explicit (not discovered by importing every client): adding actions to a
# connector means adding its type here, next to the client change.
_ACTION_TYPES_HINT = {"outlook_mail", "teams_messages"}


def actions_for_type(conn_type: str) -> Sequence[ConnectorAction]:
    if conn_type == "outlook_mail":
        from app.data_sources.clients.graph_mail_client import GraphMailClient
        return client_actions(GraphMailClient)
    if conn_type == "teams_messages":
        from app.data_sources.clients.graph_teams_client import GraphTeamsClient
        return client_actions(GraphTeamsClient)
    return ()


NATIVE_ACTION_TYPES = frozenset(_ACTION_TYPES_HINT)

# Connection types whose ConnectionTool rows the agent may call through
# execute_mcp: external tool providers plus built-in connectors with actions.
TOOL_PROVIDER_TYPES = ("mcp", "custom_api", "remote_ssh", "remote_smb")
CALLABLE_TOOL_CONNECTION_TYPES = TOOL_PROVIDER_TYPES + tuple(sorted(NATIVE_ACTION_TYPES))
