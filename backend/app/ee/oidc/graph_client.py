# Microsoft Graph API helper for OIDC group sync
# Licensed under the Business Source License 1.1

import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_MEMBER_OF_URL = "https://graph.microsoft.com/v1.0/me/memberOf"


async def resolve_group_names(access_token: str) -> Dict[str, str]:
    """Call MS Graph /me/memberOf to get group ID → displayName mapping.

    Requires a delegated token with GroupMember.Read.All permission.

    Returns:
        Dict mapping group object ID → display name. Only includes security groups,
        not directory roles or other object types.
    """
    groups: Dict[str, str] = {}
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
                if item.get("@odata.type") == "#microsoft.graph.group":
                    groups[item["id"]] = item.get("displayName", item["id"])

            url = data.get("@odata.nextLink")

    return groups


async def resolve_group_names_by_ids(
    group_ids: List[str],
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> Dict[str, str]:
    """Look up group display names using client credentials (app-level token).

    Requires Application permission Group.Read.All on the Entra app registration.

    Returns:
        Dict mapping group ID → display name. Groups that fail to resolve
        keep their ID as the name.
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

        # Batch lookup using $filter with 'in' operator (up to 15 IDs per request)
        result: Dict[str, str] = {}
        batch_size = 15
        for i in range(0, len(group_ids), batch_size):
            batch = group_ids[i:i + batch_size]
            ids_filter = ",".join(f"'{gid}'" for gid in batch)
            url = (
                f"https://graph.microsoft.com/v1.0/directoryObjects/getByIds"
            )
            resp = await client.post(
                url,
                json={"ids": batch, "types": ["group"]},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            resp.raise_for_status()
            for obj in resp.json().get("value", []):
                result[obj["id"]] = obj.get("displayName", obj["id"])

        # Fill in any IDs that didn't resolve
        for gid in group_ids:
            if gid not in result:
                result[gid] = gid

    return result
