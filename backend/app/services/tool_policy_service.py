"""Per-user MCP/custom-API tool policy resolution.

Three layers stack for every connection tool (MCP or custom API):

  1. ``ConnectionTool.policy``            — org-wide default (admin)
  2. ``DataSourceConnectionTool.policy``  — per-agent overlay (admin)
  3. ``UserConnectionToolPreference``     — per-user preference (any member)

Resolution: the admin policy is the overlay when present, else the default.
The user preference then wins over the admin policy — except that an admin
``deny`` is absolute and cannot be relaxed by a user.

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


def resolve_effective_policy(admin_policy: Optional[str], user_policy: Optional[str]) -> str:
    """User preference wins over the admin policy, except admin deny is absolute."""
    admin = normalize_tool_policy(admin_policy)
    if admin == TOOL_POLICY_DENY:
        return TOOL_POLICY_DENY
    user = normalize_tool_policy(user_policy, default=None)
    return user or admin


@dataclass
class ToolPolicyResolution:
    """Everything a caller needs to enforce + explain a decision."""

    effective: str
    admin_policy: str          # overlay-resolved admin policy
    user_policy: Optional[str] # raw user preference (normalized), if any
    is_enabled: bool


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
            )
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

        ``data_source_ids`` are the agents in scope (a report's data sources);
        the first per-agent overlay found for the tool is used. ``user`` is the
        run's requesting user, whose preference applies.
        """
        admin_policy = tool.policy
        is_enabled = bool(tool.is_enabled)
        if data_source_ids:
            overlay = (
                await db.execute(
                    select(DataSourceConnectionTool).where(
                        DataSourceConnectionTool.data_source_id.in_(
                            [str(i) for i in data_source_ids]
                        ),
                        DataSourceConnectionTool.connection_tool_id == str(tool.id),
                    )
                )
            ).scalars().first()
            if overlay is not None:
                admin_policy = overlay.policy
                is_enabled = bool(overlay.is_enabled)

        user_policy = None
        if user is not None:
            prefs = await self.get_user_preferences(db, str(user.id), [str(tool.id)])
            user_policy = prefs.get(str(tool.id))

        return ToolPolicyResolution(
            effective=resolve_effective_policy(admin_policy, user_policy),
            admin_policy=normalize_tool_policy(admin_policy),
            user_policy=user_policy,
            is_enabled=is_enabled,
        )

    @staticmethod
    def is_interactive_run(runtime_ctx: dict) -> bool:
        """True when a human can answer an 'ask' prompt for this run.

        Interactive = a web-UI run kicked off by a real user: not an eval, not
        a Slack/Teams/email platform run, not a scheduled prompt. Anything
        headless must fail closed (ask behaves like deny).
        """
        if runtime_ctx.get("is_eval_run"):
            return False
        if runtime_ctx.get("platform"):
            return False
        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        if user is None:
            return False
        head = runtime_ctx.get("head_completion")
        if head is not None and getattr(head, "scheduled_prompt_id", None):
            return False
        return True
