# Microsoft Graph API helper for OIDC group sync
# Licensed under the Business Source License 1.1

import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_MEMBER_OF_URL = "https://graph.microsoft.com/v1.0/me/memberOf"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"


async def resolve_user_profile(
    access_token: str,
    fields: List[str],
) -> Dict[str, object]:
    """Call MS Graph /me and return the requested profile fields.

    Uses the signed-in user's delegated token. Every field the caller may pass
    is readable with the default-granted ``User.Read`` scope (no admin consent),
    so this needs no elevated permission. ``$select`` keeps the payload small and
    guarantees non-default fields (department, employeeId, …) are returned.

    Resilience: Graph evaluates ``$select`` all-or-nothing — if the tenant
    forbids even one selected property, the whole request returns 403. To avoid
    losing every field over a single restricted one, a 400/403 falls back to the
    default ``/me`` projection (no ``$select``) and returns whatever requested
    fields it contains. Fields that are only available via ``$select`` (e.g.
    department, employeeId) are simply absent in that case rather than fatal.

    Returns:
        Dict of field name → value for each requested field that Graph returned
        (missing/unset/inaccessible fields are omitted). ``employeeOrgData`` is a
        nested object.
    """
    if not fields:
        return {}

    select = ",".join(fields)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{GRAPH_ME_URL}?$select={select}", headers=headers)

        if resp.status_code in (400, 403):
            # A restricted field in $select failed the whole request. Retry the
            # default projection so the readable fields still come through.
            logger.warning(
                "Graph /me $select rejected (%s); retrying without $select so "
                "available fields still sync", resp.status_code
            )
            resp = await client.get(GRAPH_ME_URL, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return {f: data[f] for f in fields if f in data}

        resp.raise_for_status()
        data = resp.json()

    return {f: data.get(f) for f in fields}


async def resolve_group_names(access_token: str) -> Dict[str, Optional[str]]:
    """Call MS Graph /me/memberOf to get group ID → displayName mapping.

    Requires a delegated token with GroupMember.Read.All permission.

    Returns:
        Dict mapping group object ID → display name. Only includes security groups,
        not directory roles or other object types. A group Graph returns without a
        readable displayName maps to ``None`` rather than to its own id: the caller
        still learns the user is a member, but sees the name as unresolved instead
        of mistaking a bare GUID for a display name.
    """
    groups: Dict[str, Optional[str]] = {}
    url = GRAPH_MEMBER_OF_URL

    async with httpx.AsyncClient(timeout=10) as client:
        while url:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("value", []):
                if item.get("@odata.type") != "#microsoft.graph.group":
                    continue
                groups[item["id"]] = (item.get("displayName") or "").strip() or None

            url = data.get("@odata.nextLink")

    return groups


async def resolve_group_names_by_ids(
    group_ids: List[str],
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> Dict[str, Optional[str]]:
    """Look up group display names using client credentials (app-level token).

    Requires Application permission Group.Read.All on the Entra app registration.

    Not every id in a ``groups`` claim is a readable group: directory roles and
    administrative units come through the same claim, and a tenant can withhold
    individual groups from the app registration. ``getByIds`` simply omits those.

    Returns:
        Dict mapping group ID → display name, with ``None`` for every id that did
        not resolve. Unresolved ids must NOT be mapped to their own GUID — that
        makes an unreadable object indistinguishable from a group genuinely named
        after a GUID, and the raw id then surfaces as the group's name in the UI.
    """
    if not group_ids:
        return {}

    # Get app-level token via client credentials grant
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        })
        token_resp.raise_for_status()
        app_token = token_resp.json()["access_token"]

        # Every id starts unresolved; getByIds fills in the ones it can read.
        result: Dict[str, Optional[str]] = dict.fromkeys(group_ids)
        batch_size = 15
        url = "https://graph.microsoft.com/v1.0/directoryObjects/getByIds"
        for i in range(0, len(group_ids), batch_size):
            batch = group_ids[i:i + batch_size]
            resp = await client.post(
                url,
                json={"ids": batch, "types": ["group"]},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            resp.raise_for_status()
            for obj in resp.json().get("value", []):
                display_name = (obj.get("displayName") or "").strip()
                if display_name:
                    result[obj["id"]] = display_name

    return result
