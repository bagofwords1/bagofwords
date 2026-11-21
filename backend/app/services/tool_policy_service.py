"""Per-user MCP/custom-API tool policy resolution.

Three layers stack for every connection tool (MCP or custom API):

  1. ``ConnectionTool.policy``            — org-wide default (admin)
  2. ``DataSourceConnectionTool.policy``  — per-agent overlay (admin)
  3. ``UserConnectionToolPreference``     — per-user preference (any member)

Resolution: the connection is the ceiling — a tool off there is off for every
agent, and a locked connection policy can only be tightened. Within that, the
agent's overlay applies when present, else the connection default. The user
preference then wins over the admin policy — except that an admin ``deny`` is
absolute and a locked policy cannot be relaxed by a user.

Several agents in one run: a call goes through ONE agent (``pick_via_agent``,
the least strict agent that enables the tool) and that agent's rule applies.
Agents' settings are never merged.

Policies: ``allow`` (run silently), ``ask`` (pause the run and ask the user in
the report UI; headless runs treat this as deny), ``deny`` (never run),
``auto`` (a small-model LLM judge reviews the specific call and decides).
The legacy value ``confirm`` is normalized to ``ask`` everywhere.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.user_connection_tool_preference import UserConnectionToolPreference

logger = logging.getLogger(__name__)

TOOL_POLICY_ALLOW = "allow"
TOOL_POLICY_ASK = "ask"
TOOL_POLICY_DENY = "deny"
TOOL_POLICY_AUTO = "auto"

VALID_TOOL_POLICIES = {TOOL_POLICY_ALLOW, TOOL_POLICY_ASK, TOOL_POLICY_DENY, TOOL_POLICY_AUTO}


def normalize_tool_policy(value: Optional[str], default: Optional[str] = TOOL_POLICY_ALLOW) -> Optional[str]:
    """Map any stored policy value onto the current enum ('confirm' → 'ask')."""
    v = (value or "").strip().lower()
    if v == "confirm":  # legacy value, pre-'ask'
        return TOOL_POLICY_ASK
    if v in VALID_TOOL_POLICIES:
        return v
    return default


def resolve_effective_policy(admin_policy: Optional[str], user_policy: Optional[str],
                             locked: bool = False) -> str:
    """User preference wins over the admin policy, except admin deny is
    absolute and a locked admin policy can only be made stricter."""
    admin = normalize_tool_policy(admin_policy)
    if admin == TOOL_POLICY_DENY:
        return TOOL_POLICY_DENY
    user = normalize_tool_policy(user_policy, default=None)
    if user and locked and _STRICTNESS[user] < _STRICTNESS[admin]:
        return admin
    return user or admin


_STRICTNESS = {TOOL_POLICY_DENY: 3, TOOL_POLICY_ASK: 2, TOOL_POLICY_AUTO: 1, TOOL_POLICY_ALLOW: 0}


def clamp_to_effect(tool, policy: Optional[str]) -> str:
    """The policy a call actually gets once the tool's effect is applied.

    External actions are never left to a model judge (auto → ask) and
    destructive ones never run silently (allow / auto → ask). Applied wherever
    a policy is resolved, so a preference stored before or around these rules
    (an old row, an "Always allow" click) cannot bypass them.
    """
    from app.data_sources.clients.connector_actions import allowed_policies

    p = normalize_tool_policy(policy)
    effect = getattr(tool, "effect", None)
    if not effect or p in allowed_policies(effect):
        return p
    return TOOL_POLICY_ASK if TOOL_POLICY_ASK in allowed_policies(effect) else TOOL_POLICY_DENY


def strictness(policy: Optional[str]) -> int:
    return _STRICTNESS.get(normalize_tool_policy(policy), 0)


def is_policy_locked(tool) -> bool:
    """True when the connection admin locked this tool's policy: agents and
    users may make it stricter, never looser."""
    return bool(getattr(tool, "policy_locked", False))


def agent_admin_setting(tool, overlay=None):
    """(is_enabled, admin_policy) for ``tool`` as one agent sees it.

    The connection is the ceiling: a tool switched off on the connection is
    off for every agent, whatever the agent's own row says. Without an agent
    row the agent uses the connection's setting. With one, the agent's policy
    applies, except that a locked connection policy cannot be loosened.
    """
    conn_enabled = bool(tool.is_enabled)
    conn_policy = normalize_tool_policy(tool.policy)
    if overlay is None:
        return conn_enabled, conn_policy
    policy = normalize_tool_policy(overlay.policy)
    if is_policy_locked(tool) and strictness(policy) < strictness(conn_policy):
        policy = conn_policy
    return conn_enabled and bool(overlay.is_enabled), policy


@dataclass
class AgentPick:
    """Which of the run's agents a tool call goes through, and its setting."""

    agent_id: Optional[str]
    is_enabled: bool
    admin_policy: str
    effective: str


def pick_via_agent(tool, agent_ids: Iterable[str], overlays_by_agent: Dict[str, object],
                   user_policy: Optional[str] = None, effective_fn=None) -> AgentPick:
    """Pick the agent a call to ``tool`` goes through: each agent's own rule,
    never a merge of several agents' rules.

    Among the run's agents that carry the tool's connection and enable the
    tool, the one whose effective policy (after the user's preference) is
    least strict wins; ties keep the order of ``agent_ids``. When no agent
    enables it, the result is disabled. ``effective_fn(admin, user)`` lets a
    caller substitute its own effective-policy rule (remote shell does).
    """
    effective_fn = effective_fn or (
        lambda admin, user: resolve_effective_policy(admin, user, locked=is_policy_locked(tool)))
    ids = [str(a) for a in agent_ids]
    best: Optional[AgentPick] = None
    for agent_id in ids or [None]:
        enabled, admin = agent_admin_setting(tool, overlays_by_agent.get(agent_id) if agent_id else None)
        if not enabled:
            continue
        effective = clamp_to_effect(tool, effective_fn(admin, user_policy))
        if best is None or strictness(effective) < strictness(best.effective):
            best = AgentPick(agent_id, True, admin, effective)
    if best is not None:
        return best
    enabled, admin = agent_admin_setting(tool, overlays_by_agent.get(ids[0]) if ids else None)
    return AgentPick(ids[0] if ids else None, False, admin,
                     clamp_to_effect(tool, effective_fn(admin, user_policy)))


@dataclass
class ToolPolicyResolution:
    """Everything a caller needs to enforce + explain a decision."""

    effective: str
    admin_policy: str          # overlay-resolved admin policy
    user_policy: Optional[str] # raw user preference (normalized), if any
    is_enabled: bool
    via_agent_id: Optional[str] = None  # the agent whose rule applied


class ToolPolicyService:
    async def get_user_preferences(
        self, db: AsyncSession, user_id: str, connection_tool_ids: Iterable[str]
    ) -> Dict[str, str]:
        """Map connection_tool_id -> normalized user policy for the given user."""
        ids = [str(i) for i in connection_tool_ids]
        if not ids or not user_id:
            return {}
        rows = await db.execute(
            select(UserConnectionToolPreference).where(
                UserConnectionToolPreference.user_id == str(user_id),
                UserConnectionToolPreference.connection_tool_id.in_(ids),
                UserConnectionToolPreference.deleted_at.is_(None),
            ).execution_options(populate_existing=True)
        )
        return {
            str(p.connection_tool_id): normalize_tool_policy(p.policy, default=None)
            for p in rows.scalars().all()
            if normalize_tool_policy(p.policy, default=None)
        }

    async def set_user_preference(
        self, db: AsyncSession, user_id: str, connection_tool_id: str, policy: str
    ) -> UserConnectionToolPreference:
        """Upsert the user's policy for one tool. Caller commits."""
        normalized = normalize_tool_policy(policy, default=None)
        if normalized is None:
            raise ValueError(f"policy must be one of {sorted(VALID_TOOL_POLICIES)}")
        row = (
            await db.execute(
                select(UserConnectionToolPreference).where(
                    UserConnectionToolPreference.user_id == str(user_id),
                    UserConnectionToolPreference.connection_tool_id == str(connection_tool_id),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = UserConnectionToolPreference(
                user_id=str(user_id),
                connection_tool_id=str(connection_tool_id),
                policy=normalized,
            )
            db.add(row)
        else:
            row.policy = normalized
            row.deleted_at = None
            db.add(row)
        return row

    async def clear_user_preference(
        self, db: AsyncSession, user_id: str, connection_tool_id: str
    ) -> bool:
        """Remove the user's preference (revert to admin policy). Caller commits."""
        row = (
            await db.execute(
                select(UserConnectionToolPreference).where(
                    UserConnectionToolPreference.user_id == str(user_id),
                    UserConnectionToolPreference.connection_tool_id == str(connection_tool_id),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        return True

    async def resolve_for_run(
        self,
        db: AsyncSession,
        *,
        tool,                       # ConnectionTool row
        data_source_ids: Optional[List[str]] = None,
        user=None,
    ) -> ToolPolicyResolution:
        """Resolve the effective policy for one tool in the context of a run.

        ``data_source_ids`` are the run's agents that carry the tool's
        connection. The call goes through one of them (``pick_via_agent``):
        that agent's own rule applies, never a merge of several agents'.
        ``user`` is the run's requesting user, whose preference applies.
        """
        ds_ids = [str(i) for i in (data_source_ids or [])]
        overlays = {}
        if ds_ids:
            for o in (
                await db.execute(
                    select(DataSourceConnectionTool).where(
                        DataSourceConnectionTool.data_source_id.in_(ds_ids),
                        DataSourceConnectionTool.connection_tool_id == str(tool.id),
                    )
                )
            ).scalars().all():
                overlays[str(o.data_source_id)] = o

        user_policy = None
        if user is not None:
            prefs = await self.get_user_preferences(db, str(user.id), [str(tool.id)])
            user_policy = prefs.get(str(tool.id))

        pick = pick_via_agent(tool, ds_ids, overlays, user_policy)
        return ToolPolicyResolution(
            effective=pick.effective,
            admin_policy=pick.admin_policy,
            user_policy=user_policy,
            is_enabled=pick.is_enabled,
            via_agent_id=pick.agent_id,
        )

    # Machine turns posted in the report: their approval card shows there and
    # the report's owner answers it, as for a turn they typed (as on main).
    WATCHED_TRIGGERS = frozenset({"wait", "checkin", "sub_agent_message", "sub_agent_wave"})
    # A sub-agent's own turn: its card is hidden, so it cannot ask, but it
    # works for the report's owner, so the report's grants apply ("for this
    # report" answers, report shell rules, Allow all).
    GRANTED_TRIGGERS = frozenset({"sub_agent"})

    @staticmethod
    def _run_kind(runtime_ctx: dict) -> str:
        """``watched`` (a person can answer a card), ``granted`` (a hidden
        sub-agent turn of a watched report), ``machine`` (nobody is in the
        loop: scheduled, webhook, eval, API key, an unknown trigger),
        ``channel`` (a person on Slack/Teams/email, who can't answer a card)
        or ``nobody`` (no user)."""
        if runtime_ctx.get("is_eval_run"):
            return "machine"
        from app.core.request_origin import CHANNEL_STARTED

        head = runtime_ctx.get("head_completion")
        # started_via: 'api_key' (an API-key prompt), or 'unattended' / 'api_key'
        # on a machine turn started by an unattended run (wait wake, check-in,
        # sub-agent, wave or message reply), so chains of turns inherit it;
        # 'channel' on a machine turn started by a platform run.
        started_via = getattr(head, "started_via", None) if head is not None else None
        if head is not None and (
            getattr(head, "scheduled_prompt_id", None)
            or getattr(head, "webhook_id", None)
            or (started_via and started_via != CHANNEL_STARTED)
        ):
            return "machine"
        if runtime_ctx.get("platform") or started_via == CHANNEL_STARTED:
            return "channel"
        trigger = getattr(head, "trigger_source", None) if head is not None else None
        if trigger and trigger not in ToolPolicyService.WATCHED_TRIGGERS:
            # eval_run, and any trigger this code does not know, fail closed.
            return "granted" if trigger in ToolPolicyService.GRANTED_TRIGGERS else "machine"
        if (runtime_ctx.get("user") or runtime_ctx.get("current_user")) is None:
            return "nobody"
        return "watched"

    @staticmethod
    def is_machine_run(runtime_ctx: dict) -> bool:
        """A run no person is in the loop for: scheduled, webhook, eval, a
        hidden sub-agent turn, or started with an API key (an integration;
        nobody is there to answer an approval). Narrower than "not
        interactive": a Slack or artifact-chat run has a person on the other
        end even though they can't answer an approval card."""
        return ToolPolicyService._run_kind(runtime_ctx) in ("machine", "granted")

    @staticmethod
    def is_interactive_run(runtime_ctx: dict) -> bool:
        """True when a human can answer an 'ask' prompt for this run.

        Interactive = a web-UI run kicked off by a real user, or a machine
        turn posted in their report (wait wake, check-in, sub-agent message or
        wave reply), whose card they see. Not an eval, a Slack/Teams/email
        platform run, a scheduled prompt, a webhook, a hidden sub-agent turn,
        or a run started with an API key (read from the head row, so a queued
        API prompt dispatched later stays unattended). Anything headless must
        fail closed: a prompt there follows the org's unattended setting
        (refuse by default).
        """
        return ToolPolicyService._run_kind(runtime_ctx) == "watched"

    @staticmethod
    def report_grants_apply(runtime_ctx: dict) -> bool:
        """Whether the report's grants apply to this run: "for this report"
        answers, report shell rules and Allow all. True for a watched run and
        for a hidden sub-agent turn, which works for the report's owner but
        cannot show a card."""
        return ToolPolicyService._run_kind(runtime_ctx) in ("watched", "granted")


def policy_not_allowed_for(tool, policy: Optional[str]) -> Optional[str]:
    """A reason ``policy`` may not be set on ``tool`` given its effect, else None.

    External actions (others see them, they cannot be undone) may not be left
    to a model judge (``auto``); destructive ones never run silently (``allow``).
    """
    from app.data_sources.clients.connector_actions import allowed_policies

    effect = getattr(tool, "effect", None)
    p = normalize_tool_policy(policy, default=None)
    if not effect or p is None:
        return None
    allowed = allowed_policies(effect)
    if p not in allowed:
        return f"'{p}' is not allowed for a {effect} tool ({getattr(tool, 'name', '')}); choose one of {', '.join(allowed)}."
    return None
