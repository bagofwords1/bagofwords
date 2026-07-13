# SaaS Secure Data Access — Design Investigation

## Context

Bow is an AI data analytics platform currently deployed **self-hosted** inside customer infrastructure, giving it direct network access to customer databases. The goal is to create a **SaaS version** running in bow's cloud, which introduces a fundamental challenge: how does bow SaaS securely access customer data sources that sit behind firewalls?

## How Bow Accesses Data Today

### Data Client Architecture

Bow supports **49 data source connectors** (Postgres, Snowflake, BigQuery, MongoDB, Tableau, Salesforce, MCP servers, etc.). All connectors implement a base class (`app/data_sources/clients/base.py`) with a uniform interface:

- `execute_query(sql)` → returns a pandas DataFrame
- `get_schemas()` → returns table/column metadata
- `test_connection()` → verifies connectivity
- `description` → LLM-facing text explaining query syntax
- `capabilities` → enum set (`QUERY`, `LIST_FILES`, `READ_FILE`, etc.)

### Client Construction Flow

When a user asks a question inside a report:

1. **Report → Data Sources**: the report has data sources attached (ORM relationship)
2. **Registry lookup**: `REGISTRY` dict in `app/schemas/data_source_registry.py` maps type strings (e.g. `"postgresql"`) to client classes via `resolve_client_class()`
3. **Instantiation**: `DataSourceService.construct_clients()` iterates each data source's connections, merges config + credentials, and instantiates client objects
4. **Keyed dict**: clients stored as `clients["data_source_name:connection_name"]`

### How the AI Agent Queries Data

The agent uses a **single-agent loop** (not multi-agent). The Planner LLM decides which tool to use; for database queries, it calls `create_data` or `inspect_data`, which invoke a **Coder sub-agent** that generates Python code:

```python
def generate_df(ds_clients, excel_files):
    df = ds_clients["Sales:postgresql-1"].execute_query(
        "SELECT product, SUM(revenue) FROM orders GROUP BY product"
    )
    return df
```

This code is executed server-side in a sandbox with the real client objects injected. The LLM learns the `execute_query()` interface through:
1. Hardcoded instructions in the Coder prompt
2. Each client's `.description` property (database-specific syntax docs)
3. Table schemas in `<ground_truth_schemas>` XML tags

### Other Data Access Paths (no code generation)

| Path | Tools | Mechanism |
|------|-------|-----------|
| Schema metadata | `describe_tables` | Reads cached schemas from bow's internal DB |
| File clients | `read_file`, `search_files`, `list_files` | Direct client API calls (SharePoint, Google Drive, etc.) |
| MCP | `execute_mcp`, `read_mcp_resource` | Direct MCP protocol calls |
| Web | `web_fetch` | Direct HTTP requests |
| Stored results | `read_query` | Reads previously-executed query data from bow's DB |
| Excel | `read_excel_range` | Office.js bridge to live Excel |

## The SaaS Problem

```
TODAY (self-hosted):                    GOAL (SaaS):
┌─── Customer Network ────┐           ┌─── Bow Cloud ──┐     ┌─── Customer Network ────┐
│                          │           │                 │     │                          │
│  ┌─────┐   ┌──────────┐ │           │  ┌─────┐       │     │  ┌──────────┐            │
│  │ Bow  │──▶│ Database │ │           │  │ Bow  │──?──▶│     │──▶│ Database │            │
│  └─────┘   └──────────┘ │           │  │ SaaS │       │     │  └──────────┘            │
│         direct access    │           │  └─────┘       │     │   behind firewall        │
└──────────────────────────┘           └─────────────────┘     └──────────────────────────┘
```

Self-hosted: bow and the database share a network. SaaS: bow is outside the firewall with no path to the database.

### What a CISO Would Reject

- **Exposing databases to the internet** — even with IP allowlisting. A database port open to the internet is a non-starter. IPs can be spoofed, allowlists leak, violates zero-trust.
- **Sending database credentials to bow's cloud** — credentials leaving the customer network is a compliance red flag (SOC2, HIPAA, GDPR).
- **VPN/peering** — too broad an access surface, complex to manage, hard to scope to just database traffic.

### What a CISO Would Accept

The industry-standard pattern: a **customer-deployed gateway agent** running inside the customer's network, creating an **outbound-only** tunnel to the SaaS platform.

```
┌─── Bow Cloud ──────────────┐       ┌─── Customer Network ──────────────────┐
│                             │       │                                       │
│  ┌──────────┐               │       │   ┌───────────┐     ┌──────────┐     │
│  │ Bow SaaS │◀──────────────│◀──────│───│ Bow Agent │────▶│ Database │     │
│  └──────────┘   tunnel      │       │   └───────────┘     └──────────┘     │
│              (outbound WSS) │       │    outbound only     local access     │
│                             │       │    no inbound ports  creds stay here  │
└─────────────────────────────┘       └───────────────────────────────────────┘
```

### Why This Satisfies a CISO

1. **No inbound firewall rules** — agent initiates outbound connections only (HTTPS/WSS port 443)
2. **Credentials never leave** — database passwords/tokens/certificates stay inside customer network
3. **Customer controls scope** — agent configured to allow only specific databases/schemas
4. **Full audit trail** — every query passes through the agent, logged locally
5. **Instant revocation** — stop the agent, access is gone
6. **Minimal attack surface** — even if bow's cloud is compromised, attacker gets a tunnel endpoint, not credentials
7. **Encrypted in transit** — mTLS/WSS between agent and SaaS
8. **Compliance-friendly** — same pattern used by Fivetran, Azure Data Factory (Self-hosted IR), Looker, Sigma, Atlan

## Key Files

| Area | Path |
|------|------|
| Base client interface | `backend/app/data_sources/clients/base.py` |
| Client registry | `backend/app/schemas/data_source_registry.py` |
| Client construction | `backend/app/services/data_source_service.py` (`construct_clients`) |
| Agent orchestration | `backend/app/ai/agent_v2.py` |
| Code generation | `backend/app/ai/agents/coder/coder.py` |
| Code execution | `backend/app/ai/code_execution/code_execution.py` |
| Tool implementations | `backend/app/ai/tools/implementations/` |
| Completion service (entry point) | `backend/app/services/completion_service.py` |
| Planner prompts | `backend/app/ai/agents/planner/prompt_builder_v3.py` |

## Status

Investigation phase — understanding the current architecture and defining the secure access problem. Next step: design the gateway agent protocol and determine what changes are needed in the bow backend to support tunneled connections alongside direct ones.
