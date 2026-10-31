"""move Atlassian MCP connections off the retired /v1/sse endpoint

Revision ID: atlv201
Revises: connapps01
Create Date: 2026-10-31 20:30:00.000000

Atlassian retired https://mcp.atlassian.com/v1/sse after 2026-06-30: sign-in
still succeeds but every MCP handshake fails. Connections created from the
"Jira / Atlassian" preset move to the Streamable HTTP endpoint
https://mcp.atlassian.com/v1/mcp/authv2 (same authorization server and DCR
client, so no re-registration). The stored token audience is re-derived from
the server on the next sign-in (ensure_mcp_oauth_config).

Only `config` (plain JSON) is touched; credentials stay encrypted and as-is.
Downgrade is a no-op: the old endpoint no longer works.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "atlv201"
down_revision: Union[str, None] = "connapps01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY = "https://mcp.atlassian.com/v1/sse"
CURRENT = "https://mcp.atlassian.com/v1/mcp/authv2"


def upgrade() -> None:
    bind = op.get_bind()
    connections = sa.table(
        "connections",
        sa.column("id", sa.String),
        sa.column("type", sa.String),
        sa.column("config", sa.JSON),
    )
    rows = bind.execute(
        sa.select(connections.c.id, connections.c.config).where(connections.c.type == "mcp")
    ).fetchall()
    for row in rows:
        config = row.config
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        if not isinstance(config, dict):
            continue
        url = (config.get("server_url") or "").rstrip("/")
        if url != LEGACY:
            continue
        config = {**config, "server_url": CURRENT, "transport": "streamable_http"}
        bind.execute(
            connections.update().where(connections.c.id == row.id).values(config=config)
        )


def downgrade() -> None:
    pass
