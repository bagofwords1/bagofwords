"""Per-agent Slack channel scope.

A ``slack_messages`` connection is shared by every agent linked to it; each
agent may narrow it to the channels it is about (and choose whether it also
reads the user's DMs). The choice lives in ``data_source_connection_settings``.

When a report runs with several agents that carry the same Slack connection,
the agent reads the UNION of their scopes — exactly like attaching several
agents to a report widens what it can read. An agent with no channel choice
contributes every channel, so the union is then unrestricted.

The scope only ever narrows what the signed-in user's own Slack membership
already allows; it is a focus setting, not a grant.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source_connection_setting import DataSourceConnectionSetting

SLACK_TYPE = "slack_messages"
#: Most channels one agent may pick (keeps the in:#x search scoping and the
#: settings row small; an agent "about" more than this is better unscoped).
MAX_CHANNELS = 200


def normalize_scope(raw: Optional[dict]) -> Dict[str, Any]:
    """``{"allowed_channels": [{"id", "name"}], "include_direct_messages": bool}``
    with defaults filled and junk dropped."""
    raw = raw or {}
    channels: List[Dict[str, str]] = []
    seen = set()
    for item in raw.get("allowed_channels") or []:
        if isinstance(item, str):
            item = {"id": item, "name": ""}
        cid = str((item or {}).get("id") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        channels.append({"id": cid, "name": str(item.get("name") or "").strip().lstrip("#")})
    include_dms = raw.get("include_direct_messages")
    return {
        "allowed_channels": channels[:MAX_CHANNELS],
        "include_direct_messages": True if include_dms is None else bool(include_dms),
    }


async def get_scope(db: AsyncSession, data_source_id: str, connection_id: str) -> Dict[str, Any]:
    row = (await db.execute(
        select(DataSourceConnectionSetting).where(
            DataSourceConnectionSetting.data_source_id == str(data_source_id),
            DataSourceConnectionSetting.connection_id == str(connection_id),
        )
    )).scalar_one_or_none()
    return normalize_scope(row.settings if row is not None else None)


async def set_scope(db: AsyncSession, data_source_id: str, connection_id: str, scope: dict) -> Dict[str, Any]:
    scope = normalize_scope(scope)
    row = (await db.execute(
        select(DataSourceConnectionSetting).where(
            DataSourceConnectionSetting.data_source_id == str(data_source_id),
            DataSourceConnectionSetting.connection_id == str(connection_id),
        )
    )).scalar_one_or_none()
    if row is None:
        row = DataSourceConnectionSetting(
            data_source_id=str(data_source_id), connection_id=str(connection_id), settings=scope,
        )
        db.add(row)
    else:
        row.settings = {**(row.settings or {}), **scope}
    await db.commit()
    return scope


def combine_scopes(scopes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Union of several agents' scopes. Any agent without a channel choice
    makes the union unrestricted; DMs are in when any agent includes them."""
    scopes = [normalize_scope(s) for s in scopes]
    if not scopes:
        return normalize_scope(None)
    include_dms = any(s["include_direct_messages"] for s in scopes)
    if any(not s["allowed_channels"] for s in scopes):
        return {"allowed_channels": [], "include_direct_messages": include_dms}
    merged: List[Dict[str, str]] = []
    seen = set()
    for s in scopes:
        for ch in s["allowed_channels"]:
            if ch["id"] not in seen:
                seen.add(ch["id"])
                merged.append(ch)
    return {"allowed_channels": merged, "include_direct_messages": include_dms}


async def scope_for_agents(db: AsyncSession, connection_id: str, data_source_ids: Iterable[str]) -> Dict[str, Any]:
    ids = [str(i) for i in data_source_ids if i]
    if not ids:
        return normalize_scope(None)
    rows = (await db.execute(
        select(DataSourceConnectionSetting).where(
            DataSourceConnectionSetting.connection_id == str(connection_id),
            DataSourceConnectionSetting.data_source_id.in_(ids),
        )
    )).scalars().all()
    by_ds = {str(r.data_source_id): r.settings for r in rows}
    return combine_scopes(by_ds.get(i) for i in ids)


def client_overrides(scope: Dict[str, Any]) -> Dict[str, Any]:
    """``construct_client`` config overrides that apply a scope to the client.
    The channel list is passed only when it narrows (an empty list would also
    be merged, and mean the same as none)."""
    out: Dict[str, Any] = {"include_direct_messages": bool(scope.get("include_direct_messages", True))}
    ids = [c["id"] for c in scope.get("allowed_channels") or []]
    if ids:
        out["allowed_channels"] = ids
    return out


def describe_scope(scope: Dict[str, Any]) -> Optional[str]:
    """One line for the planner context, or None when the agent reads all of
    the user's Slack."""
    channels = scope.get("allowed_channels") or []
    dms = scope.get("include_direct_messages", True)
    if not channels and dms:
        return None
    if not channels:
        return "This agent reads the user's Slack channels but not their DMs."
    names = ", ".join(f"#{c['name']}" if c.get("name") else c["id"] for c in channels)
    tail = " and the user's DMs" if dms else ""
    return (f"This agent reads only these Slack channels{tail}: {names}. "
            "Other channels are not readable here.")
