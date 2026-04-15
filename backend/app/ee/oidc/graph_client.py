# Microsoft Graph API helper for OIDC group sync
# Licensed under the Business Source License 1.1

import logging
from typing import Dict

import httpx

logger = logging.getLogger(__name__)

GRAPH_MEMBER_OF_URL = "https://graph.microsoft.com/v1.0/me/memberOf"


async def resolve_group_names(access_token: str) -> Dict[str, str]:
    """Call MS Graph /me/memberOf to get group ID → displayName mapping.

    Requires the delegated permission GroupMember.Read.All on the Entra app registration.

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

            # Handle pagination
            url = data.get("@odata.nextLink")

    return groups
