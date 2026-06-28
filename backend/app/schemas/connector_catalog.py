"""Curated catalog of pre-built MCP integrations.

Distinct from the data_source *type* registry: these are named **instances**
(preset server_url + icon + default auth) an org can enable. An admin adds them
from the "Add connection" catalog; nothing is seeded automatically. Used by
`GET /connectors/catalog` (the catalog tiles) and the DCR SSRF allowlist.

All `auth="oauth"` entries connect via **per-user OAuth with Dynamic Client
Registration** (no admin setup) — verified DCR-capable by live probe (2026-06).
`oauth_app` entries need a registered client (bundled or admin). `bearer` entries
take a per-user token/PAT.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import List, Optional


@dataclass(frozen=True)
class ConnectorCatalogEntry:
    key: str
    title: str
    icon: str
    server_url: str
    transport: str = "streamable_http"     # "streamable_http" | "sse"
    auth: str = "oauth"                     # oauth(DCR) | oauth_app | bearer | api_key | none
    data_shape: str = "tools"
    auto_seed: bool = False                 # marks the recommended zero-setup DCR set
    ready_out_of_box: bool = True           # no admin action before a user can connect
    description: str = ""


CATALOG: List[ConnectorCatalogEntry] = [
    # --- Auto-seeded, zero admin setup (DCR) ---
    ConnectorCatalogEntry("monday", "Monday", "monday",
        "https://mcp.monday.com/mcp", auto_seed=True,
        description="Boards, items and updates from monday.com."),
    ConnectorCatalogEntry("notion", "Notion", "notion",
        "https://mcp.notion.com/mcp", auto_seed=True,
        description="Pages, databases and search across your Notion workspace."),
    ConnectorCatalogEntry("atlassian", "Jira / Atlassian", "atlassian",
        "https://mcp.atlassian.com/v1/sse", transport="sse", auto_seed=True,
        description="Jira issues and Confluence pages."),
    ConnectorCatalogEntry("linear", "Linear", "linear",
        "https://mcp.linear.app/mcp", auto_seed=True,
        description="Issues, projects and cycles from Linear."),
    ConnectorCatalogEntry("sentry", "Sentry", "sentry",
        "https://mcp.sentry.dev/mcp", auto_seed=True,
        description="Errors, issues and releases from Sentry."),

    # --- Available on demand (need a client/token; not auto-seeded) ---
    ConnectorCatalogEntry("github", "GitHub", "github",
        "https://api.githubcopilot.com/mcp/", auth="oauth_app",
        ready_out_of_box=False, auto_seed=False,
        description="Repos, issues and PRs (needs a GitHub OAuth app — bundled or admin)."),
    ConnectorCatalogEntry("gmail", "Gmail", "gmail",
        "", auth="oauth_app", ready_out_of_box=False, auto_seed=False,
        description="Gmail (needs a Google OAuth client + Workspace approval)."),
    ConnectorCatalogEntry("supabase", "Supabase", "supabase",
        "https://mcp.supabase.com/mcp", auth="bearer",
        ready_out_of_box=True, auto_seed=False,
        description="Supabase project access via a personal access token."),
]


def list_catalog() -> List[dict]:
    return [e.__dict__.copy() for e in CATALOG]


def get_catalog_entry(key: str) -> Optional[ConnectorCatalogEntry]:
    return next((e for e in CATALOG if e.key == key), None)


def auto_seed_entries() -> List[ConnectorCatalogEntry]:
    return [e for e in CATALOG if e.auto_seed]


def allowed_dcr_hosts() -> set:
    """Hostnames DCR discovery/registration is allowed to target (SSRF guard).

    Includes every catalog server_url host plus the discovered authorization-server
    hosts we know about. Non-seeded custom URLs require an explicit admin allowlist
    (not implemented here)."""
    hosts = set()
    for e in CATALOG:
        if e.server_url:
            h = urlsplit(e.server_url).netloc
            if h:
                hosts.add(h)
    # Authorization-server hosts that differ from the resource host.
    hosts.update({"auth.atlassian.com", "cf.mcp.atlassian.com", "github.com"})
    return hosts
