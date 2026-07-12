# Feedback Loop — "Add Connection" modal: categorize sources, filter chips, MCP badge

The add-connection picker rendered every registry entry (~55 types) plus a
separate "Connectors" catalog in one flat, cramped `sm:max-w-xl` grid. This
change groups sources into domain **categories**, adds **filter chips** at the
top, spreads MCP-backed presets across their domain category with an **"MCP"
badge** (instead of a transport-named section), and pins the generic escape
hatches (raw MCP / Custom API) plus sample databases into a single frozen
footer. The claim validated here: the picker renders every source under the
right category, chips filter live, and presets show the MCP badge.

## Root cause / design (validated)

Grouping rides on a single new field rather than inferred heuristics — a
`services` app (Salesforce) and a `databases` warehouse (Postgres) are both
`data_shape="tables"`, so the domain split can't come from the existing axes.

- `DataSourceRegistryEntry.category` — added at
  `backend/app/schemas/data_source_registry.py:245` (default `"databases"`).
  Enum: `databases | bi | infra | services | files | custom`.
- `McpPreset.category` — added at
  `backend/app/schemas/data_source_registry.py:271` (default `"services"`), so a
  branded MCP server (Notion → services, Sentry → infra, Google Drive → files)
  slots into a domain category and is flagged by transport, not filed under one.
- `category` is surfaced to the frontend in `list_available_data_sources()`
  (`backend/app/schemas/data_source_registry.py:1419`); `mcp_presets()` already
  `model_dump()`s it.
- Frontend `AddConnectionModal.vue` merges both streams (registry entries +
  catalog presets) into one tile list, groups by category, renders filter chips
  (All + non-empty categories), stamps an MCP badge where `type === 'mcp'`, and
  pins `category === 'custom'` entries + sample DBs to the frozen footer. Modal
  widened `sm:max-w-xl → sm:max-w-3xl`, grid `3/4 → 4/5` columns.

## Loop A — backend categorization (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python - <<'PY'
from app.schemas.data_source_registry import list_available_data_sources, mcp_presets
from collections import defaultdict
g = defaultdict(list)
for e in list_available_data_sources():
    g[e["category"]].append(e["type"])
assert set(g) <= {"databases","bi","infra","services","files","custom"}, g.keys()
assert "postgresql" in g["databases"] and "tableau" in g["bi"]
assert "splunk" in g["infra"] and "salesforce" in g["services"]
assert "sharepoint" in g["files"]
assert set(g["custom"]) == {"mcp","custom_api"}          # escape hatches → footer
pg = {p["key"]: p["category"] for p in mcp_presets()}
assert pg["notion"] == "services" and pg["sentry"] == "infra" and pg["google_drive"] == "files"
print("PASS: every source categorized; presets spread across domains")
PY
```

Observed: `PASS: every source categorized; presets spread across domains`.
`uv run pytest tests/unit/test_mcp_presets.py -q` → `16 passed`.

## Loop B — live UI (running stack)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run --with httpx python ../tools/agent/seed_org.py
# admin@example.com / Password123!
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node <capture script>   # /agents/new opens the picker inline
```

Observed (screenshots in `media/pr/`):

- `add-connection-chips-top.png` — wider modal; filter chips row
  (All · Databases & warehouses · BI & analytics · Infrastructure · Services ·
  Files & object store); "Databases & warehouses" section first; frozen footer
  in two titled columns — **Custom Connectors** (MCP Server · Custom API) left,
  **Sample databases** (Music Store · Financial Market Agent) right.
- `add-connection-chip-services.png` — Services chip active: native services
  (NetSuite, Salesforce, ServiceNow, PostHog, Outlook Mail) beside MCP-badged
  presets (Monday, Notion, Jira/Atlassian, Linear, GitHub, Gmail, X). Section
  header hidden when a single chip is active.
- `add-connection-search-mcp-badge.png` — search "notion" resets chips to All
  and filters globally to Services → Notion with the MCP badge.

## What this proves / regression notes

The picker categorizes 100% of active registry entries into six known buckets,
presets carry their own category (verified for notion/sentry/google_drive),
chips filter without hiding the escape hatches, and the MCP badge renders for
`type === 'mcp'` tiles. The `category` field is additive with safe defaults, so
new connectors land in `databases` until explicitly categorized. The 210
`DeprecationWarning`s from `datetime.utcnow()` in Alembic migrations are
pre-existing and unrelated to this change.
