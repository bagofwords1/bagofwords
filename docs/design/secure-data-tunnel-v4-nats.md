# Secure Data Tunnel — Design Document (NATS Transport)

## Context

Bow needs to query data sources that sit in different networks from the Bow instance. This applies to **any deployment model**:

- **SaaS**: Bow cloud → customer databases behind firewall
- **Self-hosted, multi-site**: Bow in HQ → databases in branch offices
- **Hybrid cloud**: Bow in AWS → databases in Azure or on-premise
- **Multi-subsidiary**: each business unit has its own data sources in its own network

The solution: split the system into a **control plane** (the Bow instance) and one or more **edge agents** (deployed in each remote network). The control plane runs everything Bow runs today — LLM inference, planning, code generation, the sandbox, the UI. Each edge agent holds credentials for its local data sources and answers client operations against them.

A single Bow instance supports **multiple edge agents simultaneously**. Connections are individually configured as `tunnel_mode=True` (routed through an edge agent) or `tunnel_mode=False` (direct, same-network access). A single report — and a single generated function — can mix both.

### The requirement: credential isolation

The property this design must deliver is that **the control plane never holds a credential for a tunneled data source**. It is not "customer data never leaves the customer network" — result sets cross the tunnel by construction, since the whole point is to get query results back to the AI agent. Bandwidth and memory are engineering concerns addressed in A6 and G; they are not the security boundary.

Stating this precisely matters, because it is what makes the design in section B viable: the tunnel branch is taken *before* credential resolution, so there is no code path on which a tunneled connection's system credentials reach the control plane.

### Why Application-Level (not TCP proxy)

A TCP proxy (e.g., Envoy) solves network connectivity but **not credential exposure** — the Bow instance still needs database passwords to authenticate over the forwarded TCP connection. With the application-level approach, the edge agent holds credentials and connects to the database locally. This also enables edge-agent-side query audit logging, scope limiting (allowed schemas, denied tables), and support for non-TCP data sources (SharePoint, Google Drive, MCP).

### Why a Client Proxy (not remote code execution)

There are two ways to build the application-level tunnel:

- **Client proxy (chosen).** Tunneled connections produce a `TunneledClient` on the control plane. Generated code runs on the control plane exactly as it does today; each `execute_query` / `read_file` / `call_tool` call becomes one NATS request/reply. The edge agent is a credential-holding client proxy with no Bow runtime in it.
- **Remote code execution.** The control plane ships the generated code string to the edge agent, which runs it in its own sandbox against locally-constructed clients and returns the final DataFrame.

Remote code execution was the earlier proposal in this document. It was reconsidered because its advantages are narrower than they first appeared, and it carries a correctness defect the client proxy does not have.

| Argument for remote execution | Weight | Assessment |
|---|---|---|
| **Fewer round trips** | High, but conditional | The one argument that fully survives. N queries in one function = N WAN round trips, serialized. But generated code typically issues 1–3 queries, so the multiplier is usually small — and the benefit is void in exactly the case remote execution cannot handle (code spanning two edge agents). |
| **Real clients run unchanged; no proxy client to maintain** | Medium, partly self-defeating | A `TunneledClient` must reproduce a real client's surface (see B1). But remote execution has its own duplication, and a worse one: it requires forking `validate_python_code` and `CodeSecurityVisitor` into the edge agent, because their dependency chain pulls in the full ORM. Duplicating a client interface is safer than duplicating a sandbox. |
| **Lower latency from local pandas transforms** | Medium | Largely a restatement of the round-trip argument plus payload size. The transform itself is CPU-local either way; what matters is whether the data had to cross first. |
| **All data stays local** | Dropped | Not a requirement (see above), and not true as stated — result DataFrames cross by construction. Reduces to bandwidth and control-plane memory — and control-plane memory is unchanged either way, since the same rows land in the same worker (A6). |

Against that, remote code execution costs:

1. **No answer for multi-connection code.** `ds_clients` is one flat dict spanning every data source on the report; the model resolves a connection by dict-key lookup *inside* the sandbox. Remote execution must pick one destination *before* `exec()`, but the information needed — which keys the code touches — only exists as string literals in the already-generated function. Mixed tunneled/direct code has no answer at all. This is a correctness defect, not a performance one.
2. **A forked sandbox.** Two copies of the code that decides what generated Python may do, drifting independently.
3. **A second Bow runtime in the customer's network.** Config store, client factory for 49 types, executor, result cache, file store, admin-UI upload path — all needed only because the sandbox moved.
4. **A harder operational sell.** Customers must run and trust a Python code executor rather than a credential-holding proxy.

The client proxy inverts all four. Routing is resolved per call, at the dict lookup, where the information actually is; the sandbox stays in one place; the edge agent holds credentials and network reach and nothing else.

The accepted cost is round-trip count, tracked in G. A whole-code fast path — ship the entire function when every client key it touches resolves to a single edge agent — remains available as a later optimization and is not foreclosed by anything here.

### Why NATS (not a custom Tunnel Router)

NATS is a lightweight message broker with **native WebSocket support** and **built-in request/reply**. Edge agents connect to NATS via WSS (port 443, firewall-friendly). Workers connect via TCP (internal). NATS handles routing, response correlation, and reconnection — eliminating the need for a custom Tunnel Router process, SessionRegistry, and pending-requests logic.

| Custom Router responsibility | NATS equivalent |
|------------------------------|-----------------|
| Accept WebSocket connections from edge agents | NATS server WebSocket listener |
| Route requests to the right edge agent | Subject-based routing (`tunnel.<org_id>.<edge_agent_id>.conn.<name>`) |
| Correlate response IDs to pending requests | Built-in request/reply (inbox subjects) |
| Hold HTTP requests open while waiting | `nats_client.request()` blocks until reply |
| Reconnection on disconnect | NATS client auto-reconnect |
| Heartbeat / liveness detection | NATS built-in connection monitoring |
| Session registry | NATS subscriptions = implicit registry |
| Horizontal scaling | NATS clustering |

## Architecture

The system has three components:

| Component | What it does | Where it runs | Process model |
|-----------|-------------|---------------|---------------|
| **Bow Workers** | Everything Bow does today: LLM inference, planning, code generation, the sandbox, tool orchestration, UI. Constructs a `TunneledClient` for tunneled connections and publishes a NATS request per client call. | Bow instance | Uvicorn, any number of workers/pods |
| **NATS Server** | Routes messages between workers and edge agents. Handles request/reply correlation, reconnection, and connection monitoring. | Bow instance (sidecar or standalone) | Single process (clusterable for HA) |
| **Edge Agent** | Holds credentials, constructs real clients, answers proxied client calls. Connects outbound to NATS via WSS. Local admin UI for credential configuration. | Remote network (customer site, branch office, cloud VPC) | Single asyncio process |

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          CONTROL PLANE (Bow Instance)                        │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐     │
│  │                     Uvicorn Workers (N workers)                     │     │
│  │                                                                     │     │
│  │   AgentV2 · Planner · Coder · Tools · Sandbox (exec of generate_df) │     │
│  │                                                                     │     │
│  │   ds_clients = {                                                    │     │
│  │     "warehouse:prod-pg" : TunneledClient  ──┐  tunnel_mode=True     │     │
│  │     "local:analytics"   : PostgresqlClient  │  tunnel_mode=False    │     │
│  │   }                                         │                       │     │
│  │                                             ▼                       │     │
│  │                                    TunnelClient (NATS, TCP)         │     │
│  └──────────────────────────────────────────┬──────────────────────────┘     │
│                                             │                                │
│                       NATS protocol over TCP (internal)                      │
│                                             │                                │
│  ┌──────────────────────────────────────────▼──────────────────────────┐     │
│  │                         NATS Server                                 │     │
│  │  Listens on:                                                        │     │
│  │    - TCP :4222   (internal, workers connect here)                   │     │
│  │    - WS  :9443   (external via ingress/Caddy on :443, TLS there)    │     │
│  │  Subjects:                                                          │     │
│  │    tunnel.<org>.<edge_agent>.conn.<name>  → the owning edge agent        │     │
│  │    tunnel.<org>.<edge_agent>.control      → user-initiated cancel        │     │
│  │    tunnel.<org>.advertisements       → connection advertisement     │     │
│  │  Built-in: request/reply inboxes, reconnection, clustering          │     │
│  └──────────────┬─────────────────────┬────────────────────┬───────────┘     │
│                 │                     │                    │                 │
└─────────────────┼─────────────────────┼────────────────────┼─────────────────┘
                  │                     │                    │
                  │    NATS protocol over WebSocket (WSS, port 443)
                  │    outbound from edge agent, TLS
                  │    NATS auth: scoped user per edge agent (v1, static config)
                  │                     │                    │
    ┌─────────────▼───────┐ ┌───────────▼─────┐ ┌──────────▼────────┐
    │    Edge Agent A     │ │  Edge Agent B   │ │   Edge Agent C    │
    │    NYC Office       │ │  Tokyo DC       │ │   AWS VPC         │
    │                     │ │                 │ │                   │
    │  NATS Client (WSS)  │ │  NATS Client    │ │  NATS Client      │
    │  id: nyc-01         │ │ id: tky-01      │ │ id: aws-01        │
    │  subscribes to:     │ │  subscribes to: │ │  subscribes to:   │
    │  tunnel.<org>.nyc-01│ │ tunnel.<org>.   │ │ tunnel.<org>.     │
    │    .conn.prod-pg    │ │ tky-01.conn.    │ │ aws-01.conn.      │
    │    .conn.sharepoint │ │   snowflake     │ │   bigquery        │
    │                     │ │                 │ │                   │
    │  Credential store   │ │  Credentials    │ │  Credentials      │
    │  Client factory     │ │  Client factory │ │  Client factory   │
    │  Real clients       │ │  Real clients   │ │  Real clients     │
    │  Admin UI :9191     │ │  Admin UI :9191 │ │  Admin UI :9191   │
    └─────────────────────┘ └─────────────────┘ └───────────────────┘

    No sandbox, no code executor, no result cache, no file store on the edge agent.
```

### Data Flow: What Crosses the Tunnel

```
      Worker (sandbox)              NATS Server            Edge Agent
            │                          │                       │
   generate_df() runs here             │                       │
            │                          │                       │
  ds_clients["warehouse:prod-pg"]      │                       │
      .execute_query("SELECT …")       │                       │
            │                          │                       │
            │  request(                │                       │
            │   tunnel.<org>.nyc-01.conn.prod-pg ─▶ routes ─▶  │  real
            │    {operation:"execute_query"} │                 │  PostgresqlClient
            │  )                       │                       │  runs the query
            │                          │                       │
            │  ◀── reply ──────────────│──◀── msg.respond() ── │
            │      (Parquet result)    │                       │
            │                          │                       │
  ds_clients["local:analytics"]        │                       │
      .execute_query("SELECT …")  ─────┼── direct, no tunnel ──┼──▶ local DB
            │                          │                       │
   pd.merge(...) — control plane       │                       │
            │                          │                       │
            └──▶ DataFrame returned to the tool

    Crosses the tunnel:

    ~ SQL / method arguments      (query strings, file ids, tool arguments)
    ~ Result sets                 (Parquet DataFrames, file previews, tool output)
    ~ Per-user credentials        (OAuth tokens, per-user passwords — for
                                   user_required connections only. Used once,
                                   never stored on the edge agent. See A7.)

    NEVER crosses the tunnel:

    ✗ System credentials          (configured on the edge agent via admin UI;
                                   the control plane's tunnel branch is taken
                                   before resolve_credentials() — see B2)
```

## A. Protocol

### A1. Transport: NATS with dual listeners

NATS server runs as a sidecar or standalone service alongside Bow:

- **TCP :4222** — internal, workers connect here (standard NATS port)
- **WS :9443** — external, behind a TLS terminator. Edge agents reach it as `wss://tunnel.bow.com` (port 443 at the ingress), which is the firewall-friendly part; 9443 is the port the NATS process itself binds.

Both are the same NATS protocol — only the transport layer differs. A single NATS server handles both.

**One port pair, used consistently.** `4222` internal, `9443` websocket, `443` at the ingress — E1 and E3 use the same numbers, and the edge agent's configured URL (`wss://tunnel.bow.com`) always addresses the ingress, never the NATS container directly. Pointing `wss://` at the `no_tls` listener is the mistake this note exists to prevent.

NATS server config:
```
listen: 0.0.0.0:4222

websocket {
  listen: "0.0.0.0:9443"
  no_tls: true          # TLS terminated at Caddy / the K8s ingress
}

authorization {
  # Per-agent users with scoped subject permissions — see section A11
  users: [ ... ]
}
```

Caddy terminates TLS and proxies to the websocket listener. NATS serves its websocket at the root path, so this is a whole-host proxy, not a path prefix:
```
# Caddyfile
tunnel.bow.com {
    reverse_proxy nats:9443
}
```

To skip the terminator entirely, drop `no_tls` and give the websocket block a `tls { cert_file, key_file }` instead — but then the listener must be the port the edge agent's URL names.

### A2. Message format: JSON-RPC 2.0 over NATS

JSON-RPC 2.0 messages are the application protocol. NATS is the transport. NATS messages are opaque byte payloads — we put JSON-RPC inside them. For binary data (Parquet DataFrames), we use NATS's byte payload directly.

### A3. Subject scheme

Subjects are scoped by `<org_id>` then `<edge_agent_id>`. **`org_id` is read from the subject, never from a message payload** — and each edge agent's NATS credential may name exactly one org's subtree (A11), so an edge agent cannot declare its own tenancy. The subject is the attested part; the payload is not (see A10).

**`<edge_agent_id>` is in the subject so that a shared connection name cannot become a shared *route*.** Two sites will both call their production database `prod-pg`; without the agent token those are the *same subject by construction*, and NATS would fan every query out to both databases. The agent token makes that particular failure structurally impossible rather than something an admin has to avoid.

It does **not** make the names independent. `Connection` still carries `UniqueConstraint('organization_id', 'name')` (`connection.py:16`), so within one org the second agent to advertise `prod-pg` is rejected and an admin has to rename it (A10). Names remain org-unique; what the agent token buys is that a collision fails loudly at registration instead of silently fanning queries to two databases.

`edge_agent_id` is admin-supplied via `BOW_EDGE_AGENT_AGENT_ID` (E1) and must be stable for the lifetime of the agent — under v1's static config the admin has to name it in `nats.conf` *before* the agent can connect, so generating it at first boot would deadlock the bootstrap. It is an identifier, not a label: the human-readable agent name travels in the advertisement payload and lives on `DataEdgeAgent.label` (D2), where renaming it costs nothing. Putting a mutable name in the routing key would make every rename a coordinated rewrite of `nats.conf`, every `Connection` row, and every live subscription.

```
tunnel.<org_id>.<edge_agent_id>.conn.<connection_name>   → request/reply for a specific connection
tunnel.<org_id>.<edge_agent_id>.control                  → lifecycle messages (cancel)
tunnel.<org_id>.<edge_agent_id>.progress.<ref_id>        → progress for one request (A8)
tunnel.<org_id>.<edge_agent_id>.heartbeat                → liveness, for UI status (A10)
tunnel.<org_id>.advertisements                      → registration (org is a subject token — A10)
```

**Every subject is org-scoped, advertisements included.** There is no global subject anywhere in the scheme. That is not tidiness: the org token in the subject is the *only* thing that tells the control plane which tenant a message came from, because core NATS gives a subscriber no publisher identity at all (A10).

Each edge agent subscribes to subjects for the connections it serves:
```python
# Edge Agent A serves prod-pg and sharepoint
await nc.subscribe(f"tunnel.{org_id}.{edge_agent_id}.conn.prod-pg", cb=handler)
await nc.subscribe(f"tunnel.{org_id}.{edge_agent_id}.conn.sharepoint", cb=handler)
```

Workers publish to the connection-specific subject:
```python
# Any worker, any pod — org_id from the connection's organization_id
response = await nc.request(f"tunnel.{org_id}.{edge_agent_id}.conn.prod-pg", payload, timeout=60)
```

NATS routes the message to whichever edge agent is subscribed to that subject. With JWT+NKey auth (production), NATS accounts provide additional per-org isolation on top of subject scoping.

**One subscriber per connection subject.** NATS routes purely by *who is currently subscribed*. A subject is not an address of a machine — nothing in Bow's DB influences routing. If two processes subscribe to the same subject, NATS fans the request out to *both*, both execute it, and the worker takes whichever reply arrives first — duplicate queries against the customer database, nondeterministic results, and duplicated writes for operations like `write_file`.

The agent token removes the case that actually occurs in practice: **two different agents can no longer share a subject**, whatever their connections are named. What remains is narrower:

- **Across credentials (enforced).** Each edge agent's NATS user may subscribe only under its own `tunnel.<org_id>.<edge_agent_id>.>` prefix (A11). A misconfigured or foreign agent is refused at `subscribe` time with a permissions violation.
- **Same credential (v1: admin responsibility).** Two processes sharing one credential *and* one `edge_agent_id` — rolling redeploy overlap, replica scale-out, a cloned VM — are indistinguishable to NATS. v1 requires **one instance per credential, deployed stop-then-start**. Optionally set `max_connections: 1` on the account to make a second instance fail loudly at connect.

As a runtime guard, every response carries the responding `edge_agent_id`; the worker asserts it matches `Connection.edge_agent_id` and raises a hard error on mismatch (B3).

**Be clear about what that guard is worth.** It is a self-reported field checked against the expected owner, so it catches a *misrouted* response, not a *dishonest* one — and A11 already makes misrouting impossible at the broker. In the one case v1 does admit (two processes sharing a credential and an `edge_agent_id`) both answer with the correct value and the guard sees nothing. It is a consistency assertion against configuration drift and a wrong-subject bug during development, not a security control, and it protects the *result* rather than the *execution* — by the time it fires, the query has already run at the source. The registration lease in v2 (A11) is what actually closes the case.

### A4. Request/Reply — how NATS eliminates response correlation

With NATS, request/reply correlation is automatic:

```python
# Worker side — one line, blocking until response
response = await nc.request(
    f"tunnel.{org_id}.{edge_agent_id}.conn.prod-pg",
    json.dumps({
        "jsonrpc": "2.0",
        "id": "rpc_1",
        "method": "invoke",
        "params": {"operation": "execute_query", "kwargs": {"sql": "SELECT 1"}}
    }).encode(),
    timeout=60.0
)
result = json.loads(response.data)
```

Under the hood, NATS:
1. Creates a unique inbox subject (e.g., `_INBOX.bow.abc123`)
2. Sets the message's `reply` field to this inbox
3. Subscribes to the inbox
4. Publishes the message to `tunnel.<org_id>.<edge_agent_id>.conn.prod-pg`
5. Waits for a response on the inbox
6. Returns the response to the caller

```python
# Edge Agent side — subscribe and respond
async def handler(msg):
    request = json.loads(msg.data)
    result = await process_request(request)
    await msg.respond(json.dumps(result).encode())

await nc.subscribe(f"tunnel.{org_id}.{edge_agent_id}.conn.prod-pg", cb=handler)
```

No custom correlation logic. No pending futures dict. No session registry.

### A5. RPC Catalog — 19 proxied client operations

Every operation names a capability a real `DataSourceClient` has today. The edge agent constructs the real client and calls the method; the control plane's `TunneledClient` exposes the same surface. There is no `execute_code` — generated code runs on the control plane.

**The wire names the sync method; the control plane mostly calls the `a*` wrapper.** `base.py` defines async wrappers — `atest_connection`, `aget_schemas`, `aget_schema`, `aprompt_schema`, `aexecute_query`, `awarm_all`, `aread_file` — that `asyncio.to_thread` the sync implementation, and those are what callers actually use (`connection_indexing_service.py:615` calls `awarm_all`, not `warm_all`). Two consequences the table below would otherwise hide:

- `warm_all` **has no sync form at all** — `awarm_all` (`base.py:214`) is the method, and it is `async def` on the base class rather than a thread-wrapped sync one.
- `TunneledClient` must implement the `a*` names, since that is what the call sites reach for. Each one maps to the same wire operation as its sync twin: `aget_schemas` → `"get_schemas"`, `awarm_all` → `"warm_all"`. The edge agent resolves the wire name back to whichever form the real client implements — preferring `a*` when present, else `asyncio.to_thread` on the sync one (C3's `_dispatch_plain`).

The wire operation names below are therefore protocol identifiers, not Python attribute names on either side.

#### Query operations (7)

| # | Method | Args | Returns | Called from |
|---|--------|------|---------|-------------|
| 1 | `execute_query` | sql (str) | DataFrame | **generated code**, via `ds_clients[key]` |
| 2 | `test_connection` | — | `{success, message}` | `ConnectionService` |
| 3 | `get_schemas` | progress (streamed), prior_catalog?, prior_tables? | list of Table objects | Schema indexing pipeline, via `aget_schemas` |
| 4 | `get_schema` | table_name | single Table object | `FileService` |
| 5 | `prompt_schema` | — | string | `DataSourceService` |
| 6 | `warm_all` | progress (streamed), cancel (control subject) | list | `ConnectionIndexingService`, via `awarm_all` |
| 7 | `index_stats` | — | dict | `ConnectionIndexingService` — **returned with rows 3 and 6, never called on its own** (B1) |

Row 7 is the odd one: it is in the catalog because the edge agent must produce the numbers, but it is never a request of its own. Both callers invoke `index_stats()` synchronously from async code, so a proxy that answered it over the wire would deadlock the loop (B1) — instead the edge agent attaches its stats to the `get_schemas` / `warm_all` response, which is the moment those numbers exist anyway, and the proxy replays them.

Rows 3 and 6 are the awkward two, because their real signatures take **callables** — `progress_callback`, and for `awarm_all` a `cancel_check` the client polls between chunks. Neither can be serialized; both are reconstituted at the far end (A8, A9). Row 3 also carries `prior_catalog` / `prior_tables` *into* the edge agent, which is the one place a request payload is large enough to matter (A6).

`execute_query` is the hot path — it is the only one called from inside the sandbox, and the only one whose latency multiplies with the number of queries a generated function issues.

#### File operations (5)

| # | Method | Args | Returns | Called from |
|---|--------|------|---------|-------------|
| 8 | `list_files` | folder_id?, recursive? | list of file dicts | `file_reference` route, `list_files` tool |
| 9 | `read_file` | file_id, max_rows, max_chars | **preview only** (truncated) | `read_file` tool |
| 10 | `search_files` | query, **kwargs | list of file dicts | `search_files` tool |
| 11 | `grep_files` | pattern, **kwargs | match dicts | `grep_files.py:195`, via `agrep_files` |
| 12 | `file_version` | file_id | version token or `None` | `read_file.py:470`, via `afile_version` |
| 13 | `write_file` | filename, content, folder_id?, overwrite? | dict | `write_file` tool |
| 14 | `read_raw_bytes` | file_id | (bytes, filename, mime_type) | `attach_file` tool |

File operations are called from tools in async context, never from inside the sandbox — see B2.

Rows 11 and 12 are easy to miss because neither appears in the obvious file-tool path, and both fail quietly rather than loudly if the proxy omits them: `grep_files` falls through to `base.py:265`'s `NotImplementedError`, and `file_version` to `base.py:293`'s `return None`, which reads as "no cheap version available" and silently disables content caching for every tunneled file source.

`read_raw_bytes` is **not on the base class** — it is per-client, probed with `hasattr(client, "read_raw_bytes")` at `attach_file.py:135` before being called. A proxy that omits it does not error; the tool simply decides the source cannot supply bytes.

#### MCP / Tool provider operations (5)

| # | Method | Args | Returns | Called from |
|---|--------|------|---------|-------------|
| 15 | `list_tools` | — | list of tool dicts | `ConnectionService.refresh_tools()` |
| 16 | `call_tool` | tool_name, arguments | `{success, data, content_type, error}` | `execute_mcp` tool |
| 17 | `list_resources` | — | list of resource dicts | `list_mcp_resources` tool |
| 18 | `list_resource_templates` | — | list of template dicts | `list_mcp_resources` tool |
| 19 | `read_resource` | uri | `{success, contents, error}` | `read_mcp_resource` tool |

**These live on a different base class, and that has structural consequences.** `ToolProviderClient` (`tool_provider_base.py:6`) is an ABC *parallel to* `DataSourceClient`, not a subclass — so a single proxy class cannot stand in for both. B1 therefore defines two, and B2 picks between them by what `resolve_client_class` returned.

Rows 17–19 have **only async forms** (`alist_resources`, `alist_resource_templates`, `aread_resource` at `tool_provider_base.py:74-80`) — no sync twin, the same shape as `warm_all`.

#### Resolved locally, never tunneled

| Concern | Why no RPC is needed |
|---|---|
| `description` | LLM-facing syntax text. A class attribute — the control plane resolves the real client class from the advertised `type` and reads it locally. |
| `capabilities` | Same. Needed by `resolve_file_client` before any call is made. |
| `connect` | Internal to client implementations, on the edge agent side. |
| `excel_files` | User-uploaded files stay on the control plane and are passed into `generate_df` normally. |
| `load_step` / `load_entity` | Prior results live in Bow's DB and are resolved into in-memory closures on the control plane, unchanged. |

One attribute resolves neither way and has to be **advertised**: `catalog_identity_available` (`base.py:267`) is a property whose value depends on the client *instance* — false for delegated-only sources such as OneNote, which have no app-only mode. It cannot be read off `_client_class` like `capabilities`, and it cannot be an RPC because `connection_service.py:1428` reads it synchronously as `getattr(client, "catalog_identity_available", True)`.

That default is the danger: a proxy that simply lacks the attribute answers **True**, and the docstring says exactly what that costs — an empty `get_schemas()` from a delegated-only source gets treated as authoritative and "prunes every row the signed-in users had contributed." So the edge agent advertises it per connection (A10) and the proxy answers from `Connection.config`, the same route `query_timeout_seconds` takes.

#### Generic `invoke` envelope

All 19 operations use one envelope:

```json
{"jsonrpc": "2.0", "id": "<req_id>", "method": "invoke",
 "params": {
   "connection_name": "<name>",
   "operation": "<one of the 19 methods>",
   "kwargs": { ... },
   "timeout_ms": 60000,
   "user_credentials": { ... }
 }}
```

`user_credentials` is optional — included only for `user_required` connections (A7).

**The edge agent owns the query timeout.** It is configured there — an agent-level default plus an optional per-connection override (C4) — and enforced there, because the timeout must live wherever the statement can actually be cancelled. A control-plane timeout can stop *waiting*; only the edge agent can stop the *query* (C3).

`timeout_ms` is therefore the **caller's patience**, not the policy: the edge agent applies `min(timeout_ms, effective_timeout)`, so a caller may ask for less but never more. The control plane sizes it from the effective timeout the advertisement reported for that connection (A10) — so under normal operation the edge agent's own budget is what fires, and the NATS request timeout above it (`timeout + 5` in B3) is reached only when the edge agent is unreachable, which is a different failure with a different meaning.

**Two budgets, not one.** The query timeout governs `execute_query` and nothing else. `get_schemas` and `warm_all` index an entire source and legitimately run for minutes — clamping them to a query budget would abort every large index, and leaving them unbounded (as an earlier draft of C3 did, by computing a budget and then ignoring it for every non-query operation) leaves a wedged index holding a subscription forever. So the edge agent carries a second agent-level setting, `index_timeout_seconds` (C4, default 15m), advertised alongside the query timeout and applied to `get_schemas` / `warm_all`. Everything else — the per-call file and MCP operations — uses the query budget, which is the right order of magnitude for them.

Three numbers must be sized against each other, and it is worth stating the order once: `index_timeout_seconds` ≤ the NATS request timeout the control plane sets for those operations (B3) ≤ `allow_responses: { expires }` in A11. Get that backwards and the symptom is silent: the reply permission lapses mid-index, the edge agent finishes the work, and the response is dropped on the floor.

#### Wire format examples

**Query (system_only — no user credentials):**
```
Worker ──NATS request(tunnel.<org_id>.<edge_agent_id>.conn.prod-pg)──▶ NATS Server ──▶ Edge Agent

{"jsonrpc": "2.0", "id": "q_1", "method": "invoke",
 "params": {"connection_name": "prod-pg", "operation": "execute_query",
            "kwargs": {"sql": "SELECT product, SUM(revenue) FROM orders GROUP BY 1"},
            "timeout_ms": 60000}}

Edge Agent ──msg.respond()──▶ NATS Server ──▶ Worker:
{"jsonrpc": "2.0", "id": "q_1", "edge_agent_id": "nyc-01",
 "result": {"row_count": 42, "elapsed_ms": 234,
            "dataframe_b64": "<base64 Parquet bytes>"}}
```

**Query (user_required — with per-user credentials):**
```
{"jsonrpc": "2.0", "id": "q_2", "method": "invoke",
 "params": {"connection_name": "analytics-bq", "operation": "execute_query",
            "kwargs": {"sql": "SELECT ..."}, "timeout_ms": 60000,
            "user_credentials": {"auth_type": "oauth", "access_token": "eyJ..."}}}
```

**File read:**
```
{"jsonrpc": "2.0", "id": "f_1", "method": "invoke",
 "params": {"connection_name": "sharepoint", "operation": "read_file",
            "kwargs": {"file_id": "01ABC...", "max_rows": 50, "max_chars": 5000},
            "timeout_ms": 60000}}
```

**Error:**
```
{"jsonrpc": "2.0", "id": "q_3", "edge_agent_id": "nyc-01",
 "error": {"code": -32000, "message": "relation \"ordrs\" does not exist",
           "data": {"operation": "execute_query"}}}
```

Errors are re-raised on the control plane as the exception type the real client would have raised, so the existing retry loop and `_raise_if_query_errors_were_swallowed` logic behave identically to direct mode.

### A6. Result payloads and the message-size ceiling

NATS messages are bytes — no distinction between text and binary. DataFrame results are serialized as Parquet.

**Inline base64 (v1).** The JSON-RPC response carries `dataframe_b64`. Simple, one message, compatible with the `allow_responses: { max: 1 }` permission in A11. Costs ~33% encoding overhead — which is not just a CPU tax, it comes straight off the ceiling: with `max_payload` at 64MB, v1 fits about **48MB of Parquet**, not 64. The raw-payload optimization below is what recovers the other 16MB.

**Raw Parquet payload (optimization).** For `execute_query` the response is always a DataFrame, so the reply can be raw Parquet bytes with metadata in NATS headers, skipping base64 entirely:

```python
await msg.respond(parquet_bytes, headers={"X-Row-Count": "42", "X-Elapsed-Ms": "234"})
```

Start with base64; move to raw payload if encoding overhead shows up in profiles. A two-message response (metadata then binary) is **not** available — `allow_responses: { max: 1 }` permits exactly one reply per request.

NATS's `max_payload` default is 1MB; E3 raises it to 64MB — which is also as high as it goes. 64MB is the NATS server's own upper bound for the setting, so there is no headroom to raise later; the escape hatch is chunking (v2), not a bigger number. See the ceiling discussion below.

**The message-size ceiling — a transport limit, not a policy.**

Bow imposes no limit on result size today and this design introduces none. `PostgresqlClient.execute_query` is `pd.read_sql(text(sql), conn)` with no cap; a 5M-row result is fetched whole into worker memory. The only bound on a direct query is *time* (`QueryTimeoutError`, `resolve_query_timeout` at `code_execution.py:264`), and `format_df_for_widget(max_rows)` (`:1446`) caps display only — not what the query fetched or what generated code operates on. Memory pressure is identical tunneled or direct: the same rows land in the same worker's RAM, having arrived by Parquet decode rather than `pd.read_sql`.

What *is* different is that **NATS enforces a maximum message size**. `max_payload` (E3: 64MB; NATS default 1MB) is a protocol ceiling, not a product decision, and raising it without bound is not an option — large messages cost broker-wide memory and cause head-of-line blocking for every other tenant sharing the connection.

So the check is on **serialized bytes, not rows**. Rows cannot predict the outcome: 5M rows × 5 numeric columns is perhaps 10–30MB of Parquet and passes; 5M rows × 20 string columns exceeds 64MB comfortably. The edge agent knows the exact size the moment serialization finishes, so it checks there. **This is derived, not configured** — there is no per-connection knob, because there is no value an admin could set more correctly than the transport already implies. `nats-py` exposes the server's limit as `nc.max_payload`, learned from the INFO frame at connect, so the edge agent reads the real number from the broker it is actually attached to rather than being told one.

**Check the encoded size, not the Parquet size.** The thing `max_payload` measures is the bytes that go on the wire — in v1 that is the JSON envelope wrapping a base64 string, so the budget is `nc.max_payload - envelope_overhead`, and the Parquet frame must fit in roughly three quarters of it. Checking the pre-encoding size would pass frames the broker then rejects, which surfaces as a transport error with none of the actionable message below.

**Requests have the same ceiling — but not the same place.** It is a symmetric protocol limit, and one operation gets near it: `get_schemas` sends `prior_catalog` / `prior_tables` into the edge agent for incremental indexing (A5), and on a large catalog those dicts are not small.

The check cannot live where the result check does. An oversized *request* is refused by `nats-py` at **publish**, so the message never reaches the edge agent and there is nothing for it to inspect or degrade. The check therefore runs on the **control plane, before publishing** — `TunnelClient.invoke` measures the encoded payload against its own `nc.max_payload` and raises `TunnelPayloadTooLarge` (B3).

For `get_schemas` that error is recoverable, and only there: `prior_catalog` / `prior_tables` are an optimization input, not the request. `TunneledClient.aget_schemas` catches it, drops them, and retries once — a full re-extract is slower than an incremental one but still correct, whereas failing the index is neither. Every other operation lets it propagate, because for them an oversized request means oversized arguments the caller has to fix.

On exceed, **error rather than truncate**. A silently truncated frame produces a confidently wrong answer; an error feeds the existing codegen retry loop, which is well-placed to respond by aggregating in SQL. The message names the actual size:

> Result serialized to 89MB (119MB base64-encoded), exceeding the 64MB tunnel message limit. Aggregate in SQL, select fewer columns, or narrow the filter.

Report both numbers when they differ. A user told "89MB exceeds 64MB" and then handed a 70MB result that also fails has been told something that isn't quite true.

**Consequence worth stating plainly: a query that succeeds against a direct connection can fail against a tunneled one.** That is a real behavioral difference, not a strict improvement — though it cuts both ways, since the same query run directly consumes gigabytes silently and may OOM the worker instead of returning a legible error.

**Size is not the only way a tunneled result can differ.** Parquet is a typed columnar format and `pd.read_sql` is not: it returns whatever the driver produced, including `object` columns pyarrow cannot serialize — mixed types in one column, JSONB decoded to `dict`, raw `bytes` / `memoryview`, `Decimal` alongside `float`, driver-specific geometry and interval objects. Those pass straight through a direct connection into `generate_df` and fail at the serialization boundary here.

Handle it at the serializer, not by hoping: coerce what has an unambiguous representation (`dict` / `list` → JSON string, `Decimal` → float or string by column, `bytes` → base64) and fail with the offending column name and dtype when there is no honest coercion. The dtype-fidelity test in Step 3 must cover this set, not just int/float/str/datetime/None — a roundtrip suite that only exercises the easy dtypes will pass on day one and the first JSONB column will find the gap in production.

If the ceiling proves tight in practice, the fix is chunked transfer (v2) — splitting the Parquet payload across messages with reassembly on the worker — which removes the ceiling rather than raising it. That requires relaxing `allow_responses: { max: 1 }` in A11, so it is deliberately out of v1.

### A7. Per-user authentication (`user_required` connections)

Bow supports two auth policies per connection:

- **`system_only`**: a shared service account. Credentials configured once on the edge agent via admin UI. Never crosses the tunnel — and, on the control plane, `resolve_credentials()` is never reached (B2).
- **`user_required`**: each user has their own credentials (OAuth tokens, personal passwords, delegated Kerberos tickets). These are managed by the control plane — the user authenticated through the Bow UI and their credentials live in `UserConnectionCredentials` in Bow's DB.

For `user_required` connections in tunnel mode, the control plane **forwards per-user credentials per-request**. The edge agent merges them with local system config to construct the client for that call, then **discards them** — no persistence.

This is a deliberate, bounded exception to the credential-isolation property: it applies only to `user_required` connections, only to per-user credentials, and never to a connection's system credentials. It is also the same trust model as today — Bow already holds these tokens and uses them to query databases on behalf of users.

#### What stays where

```
                        system_only            user_required
                        connection             connection

System config           Edge agent             Edge agent
(host, port, db)        (admin UI)             (admin UI)

System credentials      Edge agent             Edge agent
(service account)       (admin UI, encrypted)  (admin UI — used as fallback
                                                for indexing/schema sync)

Per-user credentials    N/A                    Crosses tunnel per-request
(OAuth token, password)                        (ephemeral, never stored
                                                on edge agent)
```

#### Security properties

- Per-user tokens are **ephemeral** — OAuth access tokens expire in minutes/hours
- They cross the tunnel **encrypted** (TLS on the WebSocket hop)
- The edge agent **never stores them** — used for one call, then discarded
- The control plane **refreshes expired tokens** before sending (existing `resolve_credentials()` already handles this)
- Audit log records that per-user auth was used but **does not log token values**

#### Supported per-user auth types

| Auth type | Examples | What crosses the tunnel |
|-----------|----------|------------------------|
| OAuth (delegated/OBO) | BigQuery, Entra-backed DBs | `access_token` (short-lived) |
| Per-user password | PostgreSQL, Snowflake | `user` + `password` |
| Kerberos delegated | MSSQL | Delegated ticket |

### A8. Streaming progress

For long operations (`get_schemas`, `warm_all`), the edge agent publishes progress notifications to a separate subject:

**Progress is per-request, so the subject is too.** `aget_schemas` and `awarm_all` take a `progress_callback` the caller supplies (`base.py:173`, `:216`) — the control plane's job is to turn published notifications back into calls to that callable, and only *the worker that issued the request* holds it. A single per-agent progress subject would deliver every report's progress to all 20 workers (B4), each of which would then have to filter on `ref_id` and discard almost everything. Putting `ref_id` in the subject lets NATS do that filtering:

```python
# Edge Agent — during a long operation
await nc.publish(
    f"tunnel.{org_id}.{edge_agent_id}.progress.{ref_id}",
    json.dumps({
        "jsonrpc": "2.0",
        "method": "progress",
        "params": {"stage": "indexing", "done": 12, "total": 340}
    }).encode()
)
```

On the worker side, `TunnelClient.invoke_streaming` (B3) subscribes to `…progress.{ref_id}` for the life of one request, turns each notification back into a call to the caller's `progress_callback`, and unsubscribes in a `finally`. It lives on the transport rather than on `TunneledClient` because it owns subscription lifetime against `self._nc` — and for the same reason it carries the cancel poll (A9). **The signature and body are in B3 and only there**; two copies of one method is how this document previously ended up specifying two different event loops.

The subscription must be created *before* the request is published, or the first notifications race the subscribe and are lost.

Progress is fire-and-forget (NATS publish, not request/reply). If nobody is listening — no `progress_callback` was supplied, or the worker died — the messages are dropped, with no backpressure and no effect on the operation. `execute_query` does not stream; it is a single request/reply.

`progress_callback` is invoked on the worker's main loop, inside a NATS callback, so it must not block. The existing indexing callbacks write progress rows and are already async-friendly; a slow one would stall the same loop everything else on this connection runs on.

### A9. Cancellation (user-initiated)

This path carries **user-initiated** cancellation — someone stops a running report — which genuinely originates on the control plane. Query *timeouts* do not use it: they are enforced on the edge agent, next to the connection that can be cancelled (A5, C3). Routing timeouts through here would mean a message travelling control plane → NATS → edge agent while the query keeps running, and racing its completion.

To cancel an in-flight operation, the worker publishes to the agent's control subject:

```python
# Worker
await nc.publish(
    f"tunnel.{org_id}.{edge_agent_id}.control",
    json.dumps({"method": "cancel", "params": {"ref_id": "rpc_1"}}).encode()
)

# Edge Agent — subscribed to its control subject
async def control_handler(msg):
    cmd = json.loads(msg.data)
    if cmd["method"] == "cancel":
        _cancel_in_flight(cmd["params"]["ref_id"])

await nc.subscribe(f"tunnel.{org_id}.{edge_agent_id}.control", cb=control_handler)
```

**A flag is not enough for a query — and is exactly right for an index.** The two long operations fail to stop for opposite reasons, so there are two mechanisms, chosen by what is actually in flight:

- **`execute_query`** sits inside a blocking driver call in a daemon thread (C3). Nothing in that thread polls anything, so a flag leaves the query running exactly as an uncancelled timeout would. It must be cancelled *at the source*, via the same `query_cancellation.cancel_thread` the timeout path uses.
- **`warm_all`** is the reverse. `awarm_all` honors a `cancel_check` callable that the client polls between chunks (`base.py:214`); there is no single statement to kill, and `cancel_thread` would find nothing in the registry and return `"not_running"` while the warm kept going. Here the flag *is* the mechanism — the control-plane cancel sets it, and the running operation reads it on its next chunk boundary. `get_schemas` behaves the same way where the client accepts a cancel check, and is uninterruptible where it does not.

One registry, two entry shapes:

```python
# ref_id -> ("query", client, thread_ident) | ("cooperative", threading.Event)
_in_flight: dict[str, tuple] = {}

def _cancel_in_flight(ref_id):
    entry = _in_flight.get(ref_id)
    if not entry:
        return "not_running"               # already finished — genuinely benign here
    kind, *rest = entry
    if kind == "query":
        client, thread_ident = rest
        return query_cancellation.cancel_thread(client, thread_ident)
    (event,) = rest
    event.set()                            # awarm_all's cancel_check reads this
    return "cancel_requested"
```

`dispatch` (C3) registers the entry before starting the work and clears it in a `finally`, so timeout and user-cancel share one registry. For the query path the abandoned thread is reaped identically in both cases; for the cooperative path the operation returns normally, having stopped early. Either way the response carries the outcome, so the audit log distinguishes *cancelled* from *finished first* from *stopped between chunks*.

**None of this works if the control subject cannot be serviced.** The cancel arrives as a NATS callback on the edge agent's event loop, so any handler that blocks that loop for the duration of the operation makes cancellation structurally dead — the message sits in the socket buffer until the thing it was meant to cancel has already finished. This is why C3's dispatch must not block the loop, and it is the single most load-bearing detail in that section.

**The control plane has to poll for the cancel to ever be published.** `cancel_check` is a local callable on *this* side — indexing passes a `threading.Event`'s `is_set` (`connection_indexing_service.py:617`) — and the edge agent cannot read a Python callable. `TunnelClient.invoke_streaming` therefore polls it for the life of the request and publishes one cancel when it first returns true (B3). Without that poll the near half is missing and the symptom is worse than silence: `request_cancel` (`:202`) optimistically marks the row `CANCELLED`, so the UI reports success while the index runs to completion.

The poll runs in the worker that issued the request, which is the same worker holding the event, so it needs no leader gate — unlike registration (B4).

Cancellation is best-effort by nature: the request may complete before the message lands. That is why `"not_running"` is a legitimate answer **here** — unlike the timeout path (C3), where it would mean the safeguard silently did nothing.

### A10. Lifecycle — connection advertisement

**Edge agents publish their connections; users do not create them.** A tunneled connection cannot be created through the Bow UI. When an edge agent connects, it advertises what it serves:

```python
await nc.publish(f"tunnel.{org_id}.advertisements", json.dumps({
    "edge_agent_id": "nyc-01",                 # stable; appears in subjects (A3)
    "edge_agent_name": "NYC Office",           # admin-configured label; display only
    "version": "1.0.0",
    "index_timeout_seconds": 900,         # agent-level budget for get_schemas / warm_all (A5)
    "connections": [
        {"name": "prod-pg",    "type": "postgresql", "label": "NYC Production",
         "query_timeout_seconds": 300,    # effective value — override or agent default
         "catalog_identity_available": true},
        {"name": "sharepoint", "type": "sharepoint", "label": "NYC Documents",
         "query_timeout_seconds": 120,
         "catalog_identity_available": false},   # delegated-only source (A5)
    ],
}).encode())
```

`edge_agent_name` is the admin-configured human label; `edge_agent_id` is the stable identifier that appears in subjects (A3). Only the latter affects routing.

**Every advertisement is persisted, whether or not it produces a `Connection` row.** The handler writes the full payload to `DataEdgeAgent.last_advertisement` with `last_advertised_at` (D2), then for each advertised connection attempts to create or update a `Connection` with `tunnel_mode=True`, `edge_agent_id=<edge_agent_id>`, and no credentials, recording a per-connection outcome in the stored payload.

**On name conflict, reject — do not upsert.** `Connection` carries `UniqueConstraint('organization_id', 'name')` (`connection.py:16`), so a second agent advertising a name another agent already owns violates it. The handler does what the interactive path already does at `data_source_service.py:769` — catch the `IntegrityError`, roll back, and record a conflict — rather than upserting on `(org_id, name)`, which would silently collapse two physical databases into one row with `edge_agent_id` flapping between them.

A rejected connection simply gets no row, so no worker ever publishes to its subject. The edge agent stays subscribed to `tunnel.<org>.<edge_agent>.conn.<name>` and sits inert. The failure is contained to that one connection; everything else the agent advertised keeps working.

**The conflict surfaces in the Bow UI, because only the control plane can describe it.** The Tokyo edge agent has no idea NYC exists — the most it could ever report is "rejected." Bow knows both sides and renders the actionable message: *"`prod-pg` is already claimed by agent `nyc-01`. Rename it in this edge agent's admin UI."* The edge agents page (H15) lists each agent with its last advertisement, per-connection status, and any conflicts.

**Advertisement repeats on a timer, not only on connect.** `nc.publish` is fire-and-forget and NATS core is at-most-once with no persistence, so a single advertisement is lost if the control plane is down or restarting when it arrives — and re-advertising only on NATS reconnect would miss the case where the Bow app restarts while the edge agent's NATS connection stays up. Re-advertising periodically (alongside the heartbeat) makes registration a converging state sync instead of a one-shot event, so the system heals itself without JetStream.

**`organization_id` comes from the subject, never from the payload — and the subject is why advertisements are org-scoped.** The advertisement says what an edge agent serves, not who it belongs to.

The mechanism has to be stated precisely, because the obvious phrasing — "the handler resolves the org from the publisher's authenticated NATS identity" — describes something NATS does not offer. **Core NATS gives a subscriber no publisher identity.** A message delivered to a callback carries a subject, headers, a reply subject and bytes; there is no field naming the user that published it, and no server option that adds one. A handler subscribed to a single global `tunnel.advertisements` would therefore have nothing but the payload to go on, which is exactly what this design forbids. Worse, that subject is one every edge agent must be granted publish on, so permissions constrain nothing there either: any agent could advertise into any org by editing a JSON field.

So tenancy is carried by the **subject**, which the broker does attest. The advertisement subject is `tunnel.<org_id>.advertisements`; each edge agent's NATS user is granted publish on exactly one such subject (A11), so naming another org's is refused at publish time. The handler subscribes to `tunnel.*.advertisements` and reads `org_id` out of `msg.subject`, not out of `msg.data`:

```python
async def on_advertisement(msg):
    org_id = msg.subject.split(".")[1]        # broker-attested; the payload is not
    payload = json.loads(msg.data)
    payload.pop("org_id", None)               # ignore it if present at all
    await register_agent(org_id, payload)
```

A forged `org_id` field changes nothing because nothing reads it. An edge agent that names a subject outside its org — to advertise or to subscribe — is refused by the broker, not by the handler.

This is the same property the connection subjects already had; the earlier global advertisement subject was the one place in the scheme where it silently did not hold.

The advertised `type` is what makes the rest work: `resolve_client_class(type)` on the control plane yields the real client class, from which `capabilities` and `description` are read locally (A5).

**A registered connection is not yet a reachable one — an admin still has to put it in a data source.** `Connection` is many-to-many with `DataSource` through `domain_connection` (`connection.py:132`), and `construct_clients` iterates `data_source.connections`, keying each client `f"{data_source.name}:{conn.name}"` (`data_source_service.py:2507`). Advertisement creates a `Connection` row with **no** domain membership, so on its own it appears on the edge agents page and in the connections list and nowhere else: no `ds_clients` entry, no agent context, nothing generated code can reach.

That gap is deliberate, and naming it matters because "edge agents publish their connections; users do not create them" reads as if registration were the whole story. An edge agent can say *what it serves*; it cannot decide which analytical domain that belongs to, or which users may query it. Membership stays an org-admin action in the Bow UI, exactly as it is for a direct connection — the advertisement just means the admin is choosing from connections that already exist rather than typing credentials. What D1 makes read-only is the credential and config surface of a tunneled connection, **not** its data-source membership.

The edge agents page should therefore show membership state per connection, because "registered but in no data source" is the state a new edge agent sits in until someone acts, and it is otherwise indistinguishable from working.

**A connection the agent stops advertising is deactivated, not deleted.** Re-advertisement is a converging state sync, so a name that disappears from the payload means the edge agent no longer serves it — but that is equally what a mid-edit admin UI save, a renamed connection, or a partially-restored config store looks like. Deleting the row would take its data-source membership, RBAC grants, indexed schema and query history with it, and re-adding the name later would not bring them back. So the handler sets `is_active = False` and records the connection as withdrawn in `last_advertisement`; the existing filter in `construct_clients` drops it from `ds_clients` immediately, and a later advertisement carrying the name again reactivates the same row with everything still attached. Actual deletion is an admin action in the Bow UI, which is where the consequences are visible.

**`catalog_identity_available` is advertised because it cannot be derived.** It is a property of the constructed client instance, not of its class (`base.py:267`), so the control plane cannot read it off `_client_class` — and its caller uses `getattr(..., True)`, so an absent value is not just missing but wrong in the expensive direction (A5). The edge agent reads it from the real client it built and reports it; the handler stores it in `Connection.config`.

**`query_timeout_seconds` is the connection's *effective* budget** — its per-connection override if set, otherwise the agent default (C4) — already resolved by the edge agent so the control plane never re-derives it. The handler stores it in `Connection.config`, which gives it three uses: `TunneledClient` sizes its NATS request timeout from it (B1), the edge agents page can display what each connection is actually configured for, and `_bow_connection_query_timeout` stops being `None` for tunneled clients. It is reported, not negotiated — Bow cannot raise it.

**Heartbeat.** NATS handles connection liveness natively. When an edge agent disconnects, its subscriptions are removed and NATS stops routing to it; worker `request()` calls then time out. For UI status, the edge agent periodically publishes to `tunnel.<org_id>.<edge_agent_id>.heartbeat`. A missed heartbeat window flips `DataEdgeAgent.status` and sets `Connection.is_active = False`, at which point the existing filters in `construct_clients` and `AgentV2` drop those clients from `ds_clients` and their schemas from the AI agent's context — no tunnel-specific handling needed.

Writing to `is_active` from a liveness signal is consistent with what that column already means: it is documented at `data_source_service.py:2540` as "a cached reachability flag, not a config toggle", set `False` by a failed connection test and flipped back by a later success. A heartbeat is the same fact arriving by a different route, and a returning agent's next advertisement restores it. It is *not* an admin's enable/disable switch, so nothing an admin chose is being overwritten here.

Both the advertisement and heartbeat handlers are **leader-gated**, for the reason in B4: they run in every worker otherwise.

**Reconnection.** NATS clients have built-in reconnection with configurable backoff:
```python
nc = await nats.connect(
    servers=["wss://tunnel.bow.com"],      # 443 at the ingress, proxied to :9443 (A1)
    reconnect_time_wait=5,
    max_reconnect_attempts=-1,
    user="edge-cust-b-nyc",
    password="...",
)
```

On reconnect, the edge agent re-subscribes and re-advertises.

### A11. Authentication — v1: static config, admin-managed

**v1 uses static NATS users with scoped subject permissions, managed by the system admin in `nats.conf`.** Each edge agent gets its own NATS user whose permission list names exactly the subjects it may use. NATS enforces this at `subscribe`/`publish` time — a credential physically cannot attach to another agent's or another org's subjects, so subject scoping stops being a naming convention and becomes a broker-enforced boundary.

```
authorization {
  users: [
    # Control plane workers — internal TCP only
    { user: "bow-worker", password: "$2a$11$..."
      permissions: {
        publish:   ["tunnel.>"]
        subscribe: ["_INBOX.bow.>", "tunnel.*.advertisements",
                    "tunnel.*.*.progress.*", "tunnel.*.*.heartbeat"]
      }}

    # One entry per edge agent
    { user: "edge-cust-b-nyc", password: "$2a$11$..."
      permissions: {
        subscribe: ["tunnel.cust-b.nyc-01.>"]
        publish:   ["tunnel.cust-b.advertisements", "tunnel.cust-b.nyc-01.>"]
        allow_responses: { max: 1, expires: "20m" }
      }}
  ]
}
```

**The user → org binding is the tenancy boundary, and the subject is how it is observed.** `edge-cust-b-nyc` can only name `tunnel.cust-b.*` subjects — including `tunnel.cust-b.advertisements`, which is why the advertisement subject carries the org token. The handler reads tenancy off `msg.subject`, which the broker refused to let the publisher lie about, rather than off the payload, which it would have been free to forge (A10). Grant each edge agent exactly one advertisement subject; a wildcard here would hand back the forgery it exists to prevent.

Note the edge agent's publish grant also covers its own `conn.*` subjects, which lets it publish requests to itself. Harmless — it is the only subscriber, it already holds the credentials, and narrowing the grant would mean enumerating connections in `nats.conf`, which is exactly the drift the single wildcard eliminates.

**One wildcard per agent, not one line per connection.** Because `edge_agent_id` is a subject token (A3), an agent's entire surface is `tunnel.<org>.<edge_agent_id>.>`. The admin never enumerates connections in `nats.conf`, so the permission list cannot drift as connections are added and removed on the edge agent — it is written once when the agent is provisioned. This is why `edge_agent_id` must be admin-supplied and known before first boot: it is the thing the permission grant is written against.

**Why `allow_responses` rather than publish on `_INBOX.>`.** A responder needs to publish to the requester's inbox to reply. Granting blanket `_INBOX.>` publish would also let an edge agent inject messages into arbitrary inboxes, including other agents'. `allow_responses` instead grants a temporary, one-shot publish permission to the reply subject of each message the client actually received — so an edge agent can only ever answer requests genuinely routed to it. `expires` must exceed the **longest** operation, not the typical one. `execute_query` is bounded by its own budget (A5, C3), but `warm_all` and `get_schemas` index an entire source and can run for many minutes — and when the grant expires mid-operation the edge agent loses permission to reply, so the work completes and the response is silently dropped.

This is why `index_timeout_seconds` exists as a real setting rather than an unbounded wait (A5, C4): `expires` has to be sized against *something*, and an operation with no budget cannot be sized against. Keep the ordering `index_timeout_seconds` (15m default) < the control plane's NATS request timeout for those operations < `expires` (20m), so the edge agent's own budget always fires first and the reply grant is still alive when it does. Raising the index budget means raising both of the others. `max: 1` means one reply per request, which is why A6 uses a single-message response.

Workers set `inbox_prefix="_INBOX.bow"` on their NATS client so their subscribe permission is scoped rather than the global `_INBOX.>` wildcard. An edge agent is never granted subscribe on any `_INBOX` subject — otherwise it could read replies (including other orgs' result sets) destined for workers.

**Operating it:**

- Hash passwords with `nats server passwd` (bcrypt) — no plaintext in `nats.conf`.
- Adding or changing an agent: edit `nats.conf`, then `nats-server --signal reload` (or `docker kill -s HUP nats`). Existing connections stay up with re-applied permissions; a removed user is disconnected.
- Permissions are evaluated at connect time. Reassigning a connection between agents requires the affected edge agent to reconnect.
- `nats.conf` names *agents*; Bow's DB holds *connections*, populated by advertisement (A10). Connections never appear in `nats.conf` — an agent's grant is a single wildcard — so adding or removing a connection on an edge agent requires no broker change at all.

**Transport security.** TLS is terminated at Caddy or the K8s ingress, and the websocket listener itself is `no_tls` (A1, E3) — edge agents reach it as `wss://tunnel.bow.com` on 443. Enable TLS on the internal `:4222` listener too — per-user OAuth tokens and result sets cross that hop. Note that NATS decrypts at the broker: it sees every payload in plaintext. That is acceptable while the broker is operated by the same party as the control plane; it is the property to revisit if a third party ever runs it.

**v1 assumptions (explicit).** The system admin is responsible for: (a) provisioning one NATS user per edge agent with a correct, non-overlapping subject list; (b) ensuring no two edge agents are configured for the same connection subject; (c) running one instance per credential, stop-then-start. v1 is intended for **single-tenant / self-hosted** deployments — with static credentials the trust boundary already includes the admin. Multi-tenant SaaS requires v2.

**v2 (planned): JWT + NKey and auth callout.** Per-organization NATS accounts with `nsc`-minted credentials give cross-account isolation stronger than subject permissions. An auth callout — a small NATS client subscribed to `$SYS.REQ.USER.AUTH` that validates the Bow API key and mints a scoped user JWT from the DB — makes Bow's DB the single source of truth and removes config drift, at the cost of one new service and nkey management. A registration lease (request/reply grant before subscribing) closes the same-credential duplicate case that permissions cannot see. Not required for v1.

## B. Control Plane Changes (Bow Workers)

### B1. `TunneledClient` — `backend/app/data_sources/clients/tunneled_client.py` (new)

A `DataSourceClient` subclass that forwards method calls over NATS instead of opening a database connection. It is constructed with the real client class (resolved from the advertised `type`) so that class-level attributes resolve locally.

```python
class TunneledClient(DataSourceClient):
    """Proxies client operations to an edge agent over NATS.

    Holds no credentials. Constructed by the three client-construction sites
    (B2) when Connection.tunnel_mode is True.
    """

    def __init__(self, connection, client_class, tunnel, user_credentials=None):
        self._connection = connection
        self._client_class = client_class      # real class — for capabilities/description
        self._tunnel = tunnel                  # TunnelClient (B3)
        self._user_credentials = user_credentials

        # Attribute surface the rest of the codebase reads off clients.
        self._bow_connection = connection
        cfg = connection.config or {}
        self._bow_connection_query_timeout = cfg.get("query_timeout_seconds")
        # Two budgets (A5): the per-connection query budget, and the agent's
        # index budget for get_schemas / warm_all. Both advertised, neither
        # negotiable from this side.
        self._query_budget = self._bow_connection_query_timeout or TUNNEL_FALLBACK_TIMEOUT_SECONDS
        self._index_budget = cfg.get("index_timeout_seconds", TUNNEL_FALLBACK_INDEX_SECONDS)

    @property
    def capabilities(self):
        return self._client_class.capabilities

    @property
    def description(self):
        return self._client_class.description

    # --- Sync surface, called from inside the sandbox ---

    def execute_query(self, sql: str = None, **kwargs) -> pd.DataFrame:
        # Budget comes from the edge agent, reported by advertisement and
        # stashed in Connection.config (A10). We are bounding our own wait,
        # not setting policy: the edge agent clamps with
        # min(timeout_ms, effective), so nothing sent from here can raise its
        # budget. The fallback covers exactly one state — a Connection row
        # whose first advertisement has not landed yet — and because of that
        # clamp it can only ever shorten our wait, never the edge agent's.
        return self._tunnel.invoke_sync(
            self._connection, "execute_query", {"sql": sql, **kwargs},
            timeout=self._query_budget,
            user_credentials=self._user_credentials,
        )

    # --- Async surface: what the call sites actually use ---

    async def aexecute_query(self, *args, **kwargs):
        return await self._invoke("execute_query", self._query_kwargs(*args, **kwargs),
                                  timeout=self._query_budget)

    async def atest_connection(self):
        return await self._invoke("test_connection", {})

    async def aget_schemas(self, progress_callback=None, prior_catalog=None,
                           prior_tables=None):
        # Forward the kwargs verbatim; the far side introspects. See below.
        try:
            return await self._invoke_streaming(
                "get_schemas",
                {"prior_catalog": prior_catalog, "prior_tables": prior_tables},
                progress_callback=progress_callback, timeout=self._index_budget,
            )
        except TunnelPayloadTooLarge:
            # Prior state is an optimization, not the request (A6). A full
            # re-extract is slower; a failed index is worse.
            logger.warning("tunnel.get_schemas.prior_state_too_large",
                           extra={"connection": self._connection.name})
            return await self._invoke_streaming(
                "get_schemas", {}, progress_callback=progress_callback,
                timeout=self._index_budget,
            )

    async def awarm_all(self, progress_callback=None, cancel_check=None):
        return await self._invoke_streaming(
            "warm_all", {}, progress_callback=progress_callback,
            cancel_check=cancel_check, timeout=self._index_budget,
        )

    async def aread_file(self, file_id: str, **kwargs):
        return await self._invoke("read_file", {"file_id": file_id, **kwargs})

    # --- Helpers ---

    @staticmethod
    def _query_kwargs(*args, **kwargs):
        # execute_query signatures vary across clients (bigquery_client.py:90
        # adds maximum_bytes_billed / use_query_cache), so normalise the one
        # positional every client shares and forward the rest untouched.
        if args:
            kwargs = {"sql": args[0], **kwargs}
        return {k: v for k, v in kwargs.items() if v is not None}

    # --- Thin binders: every call carries the same connection + credentials.
    #     The streaming implementation itself lives on TunnelClient, which
    #     owns the NATS subscription lifetime it needs (B3).

    async def _invoke(self, operation, kwargs, **rest):
        return await self._tunnel.invoke(
            self._connection, operation, kwargs,
            user_credentials=self._user_credentials, **rest)

    async def _invoke_streaming(self, operation, kwargs, **rest):
        result = await self._tunnel.invoke_streaming(
            self._connection, operation, kwargs,
            user_credentials=self._user_credentials, **rest)
        # get_schemas / warm_all carry the index stats back with them, which is
        # what lets index_stats() answer without a round trip (see below).
        if isinstance(result, dict) and "index_stats" in result:
            self._last_index_stats = result["index_stats"]
            return result["value"]
        return result
```

**The five methods above are a sample, not the surface.** Every operation in A5's catalog needs a proxy method, because anything left unimplemented inherits a base that answers *plausibly and wrongly* rather than failing: `grep_files` raises `NotImplementedError` (`base.py:265`), `file_version` returns `None` — read as "no cheap version available", silently disabling content caching — and `read_raw_bytes` is absent, so `attach_file.py:135`'s `hasattr` check simply decides the source has no bytes. The rule, applied uniformly:

| Base method | Proxy form |
|---|---|
| `aget_schema`, `aprompt_schema`, `alist_files`, `asearch_files`, `agrep_files`, `afile_version`, `awrite_file` | `await self._invoke("<op>", {…})` with the query budget |
| `aexecute_query`, `atest_connection`, `aread_file` | as shown above |
| `aget_schemas`, `awarm_all` | `_invoke_streaming`, index budget |
| `read_raw_bytes` | sync — `invoke_sync`, which is safe because `attach_file.py:137` calls it through `asyncio.to_thread`. It must *exist* for the `hasattr` probe at `:135` to pass |
| `index_stats` | **no round trip** — see below |
| `execute_query` | sync, sandbox-facing (above) |

**`index_stats` is the one sync method that must not touch the network.** Its two callers invoke it directly from async code with no thread in between — `connection_indexing_service.py:619` (`extra_stats = client.index_stats() or {}`, unguarded, right after `awarm_all`) and `connection_service.py:1456` (behind a `hasattr`). A proxy implementing it with `invoke_sync` would schedule a coroutine onto the running loop and then block that loop awaiting it: the coroutine can never run, because the thread that would run it is the thread waiting. That is a deadlock, not a slow call, and no timeout below the request timeout makes it safe.

So the proxy answers from memory. The edge agent returns its stats in the `get_schemas` / `warm_all` response — it has just finished the work those numbers describe — and the proxy caches and replays them:

```python
def index_stats(self) -> dict:
    # Never a round trip: both callers are on the event loop (B1). Populated
    # from the last streaming response, which is exactly when the numbers are
    # produced anyway.
    return dict(self._last_index_stats)

@property
def catalog_identity_available(self) -> bool:
    # Advertised (A5, A10). Absent, getattr() defaults to True on the caller's
    # side (connection_service.py:1428) and an empty delegated-only crawl gets
    # treated as authoritative — pruning rows real users contributed.
    return (self._connection.config or {}).get("catalog_identity_available", True)
```

### A second proxy for tool providers

`ToolProviderClient` is an ABC **parallel to** `DataSourceClient` (`tool_provider_base.py:6`), and `codegen_clients` filters on `isinstance(client, ToolProviderClient)` (`:95-99`). A `TunneledClient(DataSourceClient)` wrapping an MCP connection is not an instance of it, so it **passes the filter** and lands in `ds_clients` advertised to the coder as a queryable client.

That is precisely the failure `codegen_clients` exists to prevent, in its own words: the model "reached for the MCP connection and emitted `ds_clients["Agent:Conn"].execute_mcp(...)` — a method no client has", failing the whole code attempt. Tunneling an MCP connection through the wrong base class reintroduces a bug that is already fixed.

```python
class TunneledToolProviderClient(ToolProviderClient):
    """Same transport, correct base — so codegen_clients still filters it out."""

    async def alist_tools(self):                  return await self._invoke("list_tools", {})
    async def acall_tool(self, tool_name, arguments):
        return await self._invoke("call_tool",
                                  {"tool_name": tool_name, "arguments": arguments})
    async def alist_resources(self):              return await self._invoke("list_resources", {})
    async def alist_resource_templates(self):     return await self._invoke("list_resource_templates", {})
    async def aread_resource(self, uri):          return await self._invoke("read_resource", {"uri": uri})
    async def atest_connection(self):             return await self._invoke("test_connection", {})
```

The two classes share `__init__`, `_invoke`, and the attribute surface — factor those into a `_TunnelProxyBase` mixin rather than duplicating them, but keep the class *hierarchy* split, since the `isinstance` check is the whole point. Fixing this by teaching `codegen_clients` to unwrap the proxy would work too, and is worse: it puts tunnel awareness into shared code that B2 otherwise promises not to touch.

`TUNNEL_FALLBACK_TIMEOUT_SECONDS` and `TUNNEL_FALLBACK_INDEX_SECONDS` are module constants in `tunneled_client.py`, and `_ENVELOPE_HEADROOM` is one in `tunnel_client.py`. The exception types — `TunnelOwnershipError`, `TunnelNotConnectedError`, `RemoteQueryTimeout`, `TunnelPayloadTooLarge`, `TunnelSyncOnLoopError` — and `translate_remote_error` live in `backend/app/services/tunnel_errors.py`, imported by both the transport and the edge agent's error translation.

**Implement the `a*` methods, not just the sync ones.** The call sites reach for the async wrappers (`connection_indexing_service.py:615` calls `awarm_all`; the file tools call `aread_file`), and `warm_all` has no sync form at all (A5). A `TunneledClient` that defined only sync methods would inherit `base.py`'s wrappers, which `asyncio.to_thread` a sync call — putting a blocking `invoke_sync` on a pool thread for an operation that was already async. Override them.

**Do not let the base class introspect the proxy.** `aget_schemas` decides whether to pass `prior_catalog` / `prior_tables` by inspecting the *bound* method's signature — `_accepts_kwarg(self.get_schemas, "prior_catalog")` (`base.py:191-196`). On a proxy that inspects the proxy: a generic `**kwargs` signature makes every check answer yes, an explicit one makes it answer no, and neither answer has anything to do with the real client on the far side. The failure is silent and expensive — file sources use `prior_catalog` to skip re-extracting unchanged files, so a wrong "no" means every tunneled index re-extracts everything, forever, with no error anywhere.

The fix is not to reproduce 49 clients' signatures on the control plane. It is to **move the introspection to the side that has the real signature**: `TunneledClient.aget_schemas` forwards the kwargs verbatim (dropping `None`s at the wire), and the edge agent invokes the real client's own `aget_schemas`, whose `_accepts_kwarg` runs against the real `get_schemas`. C3's dispatcher already prefers the `a*` method when the client has one, which is exactly what makes this work.

Same reasoning for `execute_query`: signatures vary across clients (`bigquery_client.py:90` takes `maximum_bytes_billed` and `use_query_cache`), so the proxy accepts `**kwargs` and forwards them rather than pinning the Postgres signature. The `query` / `aquery` aliases (`base.py:144`) are inherited from the base and need no proxy of their own — they call through `execute_query`.

**The attribute surface matters as much as the methods.** Verified reads against constructed clients that a `TunneledClient` must satisfy:

| Attribute | Read by |
|---|---|
| `_bow_connection_query_timeout` | `resolve_query_timeout` (`code_execution.py:272`) |
| `execute_query` (presence) | `wrap_clients_for_capture` (`code_execution.py:1003`) |
| `capabilities` | `resolve_file_client` (`_file_tool_common.py:342`) |
| `description` | coder context builders |
| `_bow_connection` | file tools inspecting auth policy |
| quota metadata | `_attach_client_quota_metadata` (`data_source_service.py:2593`) |
| stored table metadata | `_attach_stored_table_metadata` (`data_source_service.py:2594`) |

Quota and stored-table metadata are attached by the existing call sites and work unchanged — quota is enforced on the control plane, and stored table metadata comes from Bow's own schema cache.

### B2. The three client-construction sites

Every client in Bow is born at one of three places, all running the identical sequence: `resolve_client_class(type)` → merge config + resolved credentials → narrow to constructor signature → `ClientClass(**allowed)`. Tunneling is one branch inserted at each, immediately after class resolution:

```python
ClientClass = resolve_client_class(connection.type)

if connection.tunnel_mode:
    # Pick the proxy by which base the real client has. A tool provider wrapped
    # in a DataSourceClient proxy slips past codegen_clients' isinstance filter
    # and is offered to the coder as queryable (B1).
    Proxy = (TunneledToolProviderClient
             if issubclass(ClientClass, ToolProviderClient)
             else TunneledClient)
    return Proxy(connection, ClientClass, tunnel_client,
                 user_credentials=await self._resolve_user_credentials(...))

# existing path — unchanged
creds = await self.resolve_credentials(db, connection, current_user)
...
```

| Site | Scope | Downstream callers that get tunneling for free |
|---|---|---|
| `ConnectionService.construct_client` (`connection_service.py:1658`) | one connection | `execute_mcp:617`, `read_mcp_resource:115`, `list_mcp_resources:143`, `_file_tool_common:339`, `file_reference_service:50`, `connection_tool_gateway:334`, `custom_query_service:306,530`, `connection_indexing_service:614`, `routes/data_source:594`, `routes/file_reference:99`, and internal `test_connection` / `refresh_tools` / indexing at `:996,1079,1354,2107` |
| `DataSourceService.construct_clients` (`data_source_service.py:2507`) | all connections; builds the `ds_clients` dict for the sandbox | `AgentV2`, `agent_focus_common:124`, MCP tool context, `step_service`, `entity_service`, `query_service` |
| `DataSourceService.construct_client` (`data_source_service.py:2274`) | first connection; **deprecated** | `user_data_source_credentials_service` ×3, internal ×2. Branched for safety rather than assumed unreachable. |

**The branch is taken before `resolve_credentials()`.** For a `system_only` tunneled connection there is no code path on which the control plane obtains the credential — the isolation property is structural, not a policy the code chooses to honor. `user_required` connections resolve per-user credentials only (A7); system credentials are still never fetched.

**That depends entirely on `_resolve_user_credentials` being narrow, so it is defined here rather than left to the reader.** The obvious implementation — delegate to `ConnectionService.resolve_credentials` — would break the property, because that method returns *system* credentials in two cases: unconditionally for `system_only` (`connection_service.py:1786-1787`), and as a fallback for `user_required` when `current_user is None` (`:1790-1796`), which is exactly how the indexing path calls it.

```python
async def _resolve_user_credentials(self, db, connection, current_user):
    """Per-user credentials for a tunneled connection, and nothing else.

    Deliberately NOT resolve_credentials(): that answers with system
    credentials for system_only, and falls back to them for user_required
    on the current_user=None indexing path. Either would put a tunneled
    connection's system credentials on the control plane, which is the one
    thing this branch exists to prevent.
    """
    if connection.auth_policy != "user_required" or current_user is None:
        return None                      # never reaches a system credential
    return await self._user_credential_store.get(db, connection, current_user)
```

It reads `UserConnectionCredentials` directly — the same store `resolve_credentials`'s user branch reads, reached without the system fallbacks wrapped around it. For `user_required` on the indexing path (`current_user is None`) it returns `None` and the edge agent uses its own locally-stored system credentials, which is what A7's "used as fallback for indexing/schema sync" row already describes.

**Do not let the property rest on the column happening to be empty.** Today `connection.decrypt_credentials()` would return `{}` for a tunneled row because advertisement stores none — so even the wrong implementation would look correct. That makes the guarantee data-dependent rather than structural, and one populated column away from silently forwarding secrets: a migration, a restore, or a pre-tunnel connection later converted. Two cheap guards keep it structural:

- D1's read-only rule is enforced in `connection_schema.py` and the route, not only in the UI form.
- The advertisement handler asserts `Connection.credentials` is empty when it sets `tunnel_mode=True`, and refuses the registration if not — a tunneled connection with credentials in Bow is a bug, and the loudest place to discover it is registration.

**No tool changes.** The v3 design modified `create_data`, `inspect_data`, `read_file`, `execute_mcp`, `_file_tool_common`, and `connection_service` to branch on tunnel mode. None of that is needed: tools receive a client and call methods on it, and the proxies answer those methods — `TunneledClient` for data sources, `TunneledToolProviderClient` for `execute_mcp`'s tool providers (B1).

**FAST acceleration works over a tunnel unchanged — and is the recommended mitigation for round-trip cost.** `construct_clients` also attaches a `{key}::fast` DuckDB sibling for activated custom queries (`:2602-2608`). Neither half of that feature needs tunnel awareness:

- **Serving** reads encrypted DuckDB artifacts from `backend/uploads/fast/<connection_id>/` and never touches the source (`fast_client.py:85`), so the sibling behaves identically whether its parent connection is tunneled or not.
- **Extraction** resolves a streaming adapter via `source_for(client)` (`extractor.py:105`), which requires a client that can hand over a SQLAlchemy Connection (`sources.py:106`). A `TunneledClient` has none, so extraction lands in the existing non-streaming fallback (`extractor.py:288`) — a single `client.execute_query(bounded_sql(sql, max_rows))`, already bounded by an injected row limit, which the proxy supports directly.

This matters strategically rather than incidentally. Round-trip count is the client proxy's one accepted cost (see "Why a Client Proxy"), and acceleration is its direct answer: *move the working set, not the query*, applied to a WAN hop instead of a slow source. An admin materializes the working set on a schedule; the AI agent's many small exploratory queries then run against local DuckDB at zero tunnel cost. Accelerating tunneled connections should be encouraged, not blocked.

Two implementation notes, neither blocking:

- The fallback at `extractor.py:288` carries the comment "Unreachable for the dialects in `ACCELERABLE_TYPES`, all of which stream." A tunneled connection is its first real caller, so it needs a test rather than an assumption.
- An accelerated tunneled source leaves a persistent materialized copy of customer data on the Bow instance, encrypted at rest with a per-artifact key. Permitted under credential isolation, but a posture change worth disclosing to anyone who deployed an edge agent specifically to keep data in their network.

**Multi-connection code works without a routing decision.** Because resolution happens per call at the dict lookup inside the sandbox, this executes correctly with `prod-pg` tunneled to NYC, `snowflake` tunneled to Tokyo, and `analytics` direct:

```python
def generate_df(ds_clients, excel_files):
    a = ds_clients["warehouse:prod-pg"].execute_query("SELECT id, amount FROM orders")
    b = ds_clients["tokyo:snowflake"].execute_query("SELECT id, region FROM dim_customer")
    c = ds_clients["local:analytics"].execute_query("SELECT id, tier FROM accounts")
    return a.merge(b, on="id").merge(c, on="id")
```

### B3. `TunnelClient` — `backend/app/services/tunnel_client.py` (new)

The NATS transport shared by all `TunneledClient` instances in a worker.

```python
class TunnelClient:
    """NATS request/reply transport. One per worker process."""

    async def connect(self, nats_url: str):
        # Capture the loop that owns the connection, in the same statement
        # that creates it — see "Loop ownership" below. Mirrors
        # execute_code_async stashing its loop on usage_context (:1310-1311).
        self._loop = asyncio.get_running_loop()
        self._nc = await nats.connect(
            nats_url,
            inbox_prefix="_INBOX.bow",   # required — see A11
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,
        )

    async def invoke(self, connection, operation, kwargs, *, ref_id=None,
                     timeout=60.0, user_credentials=None):
        # 60s suits the per-call file and MCP operations. execute_query passes
        # the connection's advertised query budget and get_schemas / warm_all
        # the agent's advertised index budget (B1) — no operation relies on
        # this default for anything long-running, which is what keeps the
        # ordering in A11 (index budget < request timeout < allow_responses
        # expiry) true rather than aspirational.
        #
        # ref_id is supplied by invoke_streaming (A8) so progress notifications
        # and a later cancel address the same operation; otherwise it is fresh.
        payload = {
            "jsonrpc": "2.0", "id": ref_id or str(uuid.uuid4()), "method": "invoke",
            "params": {
                "connection_name": connection.name,
                "operation": operation,
                "kwargs": kwargs,
                "timeout_ms": int(timeout * 1000),
            },
        }
        if user_credentials:
            payload["params"]["user_credentials"] = user_credentials

        # Requests share the result ceiling, and an oversized one is refused
        # at publish — so it is checked here, not on the far side (A6).
        encoded = json.dumps(payload).encode()
        if len(encoded) > self._nc.max_payload - _ENVELOPE_HEADROOM:
            raise TunnelPayloadTooLarge(len(encoded), self._nc.max_payload, operation)

        msg = await self._nc.request(
            f"tunnel.{connection.organization_id}.{connection.edge_agent_id}.conn.{connection.name}",
            encoded,
            timeout=timeout + 5,
        )
        return self._unwrap(msg, connection, operation)

    async def invoke_streaming(self, connection, operation, kwargs, *, timeout,
                               progress_callback=None, cancel_check=None,
                               user_credentials=None):
        """Long operations (get_schemas, warm_all): progress flows back,
        cancel flows forward. Lives here rather than on TunneledClient because
        it owns subscription lifetime against `self._nc` (A8, A9)."""
        ref_id = str(uuid.uuid4())
        base = f"tunnel.{connection.organization_id}.{connection.edge_agent_id}"

        sub = None
        if progress_callback is not None:
            # Subscribe BEFORE publishing, or the first notifications race it.
            sub = await self._nc.subscribe(
                f"{base}.progress.{ref_id}",
                cb=lambda msg: progress_callback(**json.loads(msg.data)["params"]),
            )
        watcher = None
        if cancel_check is not None:
            watcher = asyncio.create_task(
                self._watch_cancel(base, ref_id, cancel_check))
        try:
            return await self.invoke(
                connection, operation, kwargs, ref_id=ref_id, timeout=timeout,
                user_credentials=user_credentials,
            )
        finally:
            if watcher is not None:
                watcher.cancel()
            if sub is not None:
                await sub.unsubscribe()

    async def _watch_cancel(self, base, ref_id, cancel_check, interval=1.0):
        """Bridge a local cancel_check callable to a remote cancel.

        `cancel_check` is a plain callable on this side — indexing passes a
        threading.Event's `is_set` (`connection_indexing_service.py:617`) — and
        the edge agent cannot read it. Nothing polls it unless we do, which is
        the difference between "cancel works" and "the UI says cancelled while
        the index runs to completion" (A9).
        """
        while True:
            await asyncio.sleep(interval)
            if cancel_check():
                await self._nc.publish(
                    f"{base}.control",
                    json.dumps({"method": "cancel",
                                "params": {"ref_id": ref_id}}).encode(),
                )
                return

    def _unwrap(self, msg, connection, operation):
        body = json.loads(msg.data)

        # A3 runtime guard: the responder must be the expected owner.
        if body.get("edge_agent_id") != connection.edge_agent_id:
            raise TunnelOwnershipError(
                f"{connection.name} answered by {body.get('edge_agent_id')!r}, "
                f"expected {connection.edge_agent_id!r}"
            )

        if "error" in body:
            raise translate_remote_error(body["error"], operation)

        result = body["result"]
        if "dataframe_b64" in result:
            return pd.read_parquet(io.BytesIO(base64.b64decode(result["dataframe_b64"])))
        return result
```

**Loop ownership and the sync bridge.**

Eighteen of the nineteen operations (A5) are called from async contexts — file tools, MCP tools, `ConnectionService`, the advertisement handler — and simply `await tunnel.invoke(...)` on the main loop. Only `execute_query` is different, because only it is reached from inside the sandbox.

Generated code runs in `_CODE_EXEC_POOL` via `run_in_executor` (`code_execution.py:1331`), and **those threads have no event loop at all** — stated outright at `safe_client.py:4-6`, "the code-exec worker thread has no [running loop]." Being a plain `def`, `generate_df` also cannot `await`. So the pool thread must hand its coroutine to a loop running elsewhere and block on the result.

**Bow already solves this exact problem on this exact call path.** `execute_code_async` captures its loop and stashes it before handing off to the pool (`code_execution.py:1310-1311`):

```python
loop = asyncio.get_running_loop()
if self.usage_context is not None:
    self.usage_context.loop = loop
```

and `UsageLimitContext.run_blocking` (`usage_policy_service.py:572`) consumes it from the pool thread, so a synchronous quota check inside `execute_query` can reach an async DB call. `TunnelClient` follows the same pattern:

```python
def invoke_sync(self, *args, timeout=60.0, **kwargs):
    if not (self._nc and self._loop and self._loop.is_running()):
        raise TunnelNotConnectedError("Tunnel client is not connected")
    # Refuse to block the loop we are about to schedule onto — that is a
    # deadlock, not a stall: the coroutine cannot run because the thread that
    # would run it is the thread waiting. Same guard, same reason, as
    # run_blocking's refusal at usage_policy_service.py:575-580.
    try:
        if asyncio.get_running_loop() is self._loop:
            raise TunnelSyncOnLoopError(
                "invoke_sync called from the event loop; await invoke() instead")
    except RuntimeError:
        pass                      # no running loop here — the expected case
    future = asyncio.run_coroutine_threadsafe(
        self.invoke(*args, timeout=timeout, **kwargs), self._loop
    )
    # Outermost of three nested budgets, and the only one that protects the
    # pool thread: edge agent budget (C3) < NATS request timeout (timeout + 5)
    # < this. Left unbounded, a bug anywhere below pins a _CODE_EXEC_POOL
    # thread forever; set equal to the NATS timeout, it races it and reports
    # the wrong failure. On expiry, cancel the future so the coroutine does not
    # outlive the caller that abandoned it.
    try:
        return future.result(timeout=timeout + 10)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise
```

`self._nc` is checked alongside the loop because B4 installs the client even when `connect()` raised — a boot with an unreachable broker must fail per call with `TunnelNotConnectedError`, not with an `AttributeError` on `None`.

`self._loop` is set by `connect()` in the same statement that creates `self._nc`, so the loop that owns the connection and the loop `invoke_sync` targets **cannot disagree** — one line establishes both. No dedicated background loop is needed; the pool thread borrows the loop that is already servicing the connection's reader and flusher tasks.

**One deliberate divergence from `run_blocking`: do not copy its `asyncio.run(coroutine)` fallback.** That branch is safe there because the quota coroutine is self-contained and owns nothing across loops. It is fatal here — a fresh loop touching `self._nc`'s futures and locks, which belong to the main loop, produces exactly the "Lock is bound to a different event loop" failure documented at `agent_v2.py:843-848`. The same trap sits next door in `SafeHttpClient`, which legitimately calls `asyncio.run` (`:350`, `:374`) because it creates its session *inside* the coroutine. That pattern is the house style and it is wrong for a shared, long-lived connection.

**Operational notes.** Neither of these is a design question; both are pre-existing characteristics of the sandbox execution model whose magnitude the tunnel changes.

- **Thread stacking.** `QueryCapturingClientWrapper` wraps a `TunneledClient` like any other client, so `_call_with_timeout` (`code_execution.py:810`) would apply and a tunneled query would carry two independent timeout layers. **`TunneledClient` opts out of the wrapper's timeout.** That wrapper exists to bound a query it can cancel; against a tunneled client it would be timing a network round trip while holding no connection to cancel. The authoritative budget is the edge agent's (C3), which is the only place the statement can actually be stopped; the NATS timeout in `invoke` is the backstop above it. Query capture and per-query timings are unaffected — those come from the wrapper's recording, not its timeout.
- **Pool sizing.** `_CODE_EXEC_POOL` is capped at `min(8, cpu*2)` (`code_execution.py:173`). A tunneled query holds a pool thread for a WAN round trip rather than a LAN one, and a function issuing five queries holds it for five. Saturation surfaces as *cancelled queued futures* (`:170-172`), not slow ones — so watch for cancellations, not latency. Direct queries already occupy pool threads the same way; the tunnel changes the duration, not the mechanism.

### B4. Worker NATS connection lifecycle

One `TunnelClient` per worker process, holding **one** NATS connection shared by every `TunneledClient` in that worker — across all reports, users, and orgs it serves. NATS multiplexes: `nc.request()` allocates a unique inbox subject per call (A4), so concurrent requests over one socket are correlated independently. Note this *reduces* socket count versus direct mode, where five connections mean five SQLAlchemy engines and five pools.

This section owns the whole lifecycle. `TunnelClient` (B3) provides transport methods; when and where the connection is created, recovered, and closed is decided here — the two were previously split, which is how B3 and B4 came to specify different owning loops.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    client = TunnelClient()
    try:
        await client.connect(settings.nats_url)      # captures the main loop (B3)
        # Registration is global state, so exactly one worker owns it.
        if is_scheduler_leader:
            await start_advertisement_handler(client)   # + heartbeat sweeper
    except Exception:
        logger.exception("Tunnel unavailable; tunneled connections will fail")
    app.state.tunnel_client = client
    set_tunnel_client(client)                        # module accessor — see below
    yield
    await client.drain()                             # finish in-flight, then close
```

**The transport is per-worker; the registration handler is not.** Every worker needs its own NATS connection — it is how that worker's `TunneledClient`s reach an edge agent, and it must be created on that worker's own loop (B3). Advertisement and heartbeat handling are the opposite: they mutate shared rows.

`backend/main.py:736` runs uvicorn with `workers=20`, and core NATS fans a subject out to *every* subscriber (a plain `subscribe`, not a queue group). A single advertisement subscribed in `lifespan` therefore lands in all 20 workers, each of which upserts the same `DataEdgeAgent`, writes the same `last_advertisement`, races the same `create-or-update` on each `Connection`, and produces 20 concurrent `IntegrityError` rollbacks on a name conflict instead of one recorded conflict. The heartbeat sweeper is worse: 20 independent timers flipping `is_active` on the same rows.

Bow already has both the mechanism and the precedent. `try_acquire_scheduler_leader()` (`main.py:441`) exists because *"N-workers × every scheduled tick becomes an N-way resource storm"*, and it is what gates the Slack Socket Mode listener and the email poller — long-lived external subscriptions with exactly this shape. The advertisement handler joins that list.

A NATS queue group (`queue="bow-advertisements"`) would also work and needs no leader lock, at the cost of a second, differently-shaped answer to a question the codebase has already settled. Prefer the leader gate; reach for the queue group only if advertisement volume ever outgrows one worker, which for a per-agent timer it will not.

One consequence to keep in mind: the leader holds the *only* advertisement subscription, so if that worker restarts, registrations arriving in the gap are lost. That is precisely why advertisement repeats on a timer (A10) — the next cycle heals it — and it is the reason that timer is not optional.

**Loop.** Lifespan runs on the uvicorn main loop, so that is the loop `connect()` captures and the loop `invoke_sync` targets. No second loop exists. The main loop is not blocked by this: pool threads block on a future while the loop runs the coroutine, and `execute_code_async` awaits `run_in_executor` rather than blocking (`code_execution.py:1331`).

**Startup failure is non-fatal.** If NATS is unreachable at boot, the app still starts — direct connections are unaffected and most of Bow works. Tunneled connections then fail per call with a clear `TunnelNotConnectedError` rather than the app refusing to serve. Note that the client is installed either way, so it is installed in a *disconnected* state: both `invoke` and `invoke_sync` check `self._nc` before touching it (B3), or the promised clear error is an `AttributeError` on `None` instead. The alternative — refusing to boot — would let a broker outage take down instances that have no tunneled connections at all.

**Reconnection** is handled by `nats-py` (`reconnect_time_wait=2`, `max_reconnect_attempts=-1`, B3). In-flight requests fail during the gap and surface to the retry loop; new ones queue until the connection returns. Because the connection is shared, an outage fails **every tunneled query in that worker at once** — a wider blast radius than a single bad direct connection, and the reason infinite retry rather than a bounded attempt count is the right default.

**Shutdown drains rather than closes.** Pool threads may be blocked in `invoke_sync` when the lifespan exits; `drain()` lets in-flight requests finish and their replies land before the socket goes away, so a shutdown mid-query surfaces as a completed query rather than a spurious error.

**Reaching the client from the construction sites.** `DataSourceService.construct_clients(db, data_source, current_user)` has no route to `app.state`, so B2's branch resolves it through a module-level accessor (`get_tunnel_client()`) set during lifespan. All three construction sites use the same accessor; it returns `None` before startup completes, which the branch treats as "not connected."

### B5. What does NOT change

- `AgentV2` loop, `Planner`, `Coder` — unchanged
- `code_execution.py`, `validate_python_code`, `CodeSecurityVisitor` — the sandbox stays on the control plane, in one copy
- `QueryCapturingClientWrapper` — wraps a `TunneledClient` exactly as it wraps a real one; query capture and per-query timings work unchanged. **Its timeout is the one exception**: a tunneled client opts out, because the budget must be enforced where the statement can be cancelled (B3, C3)
- `create_data`, `inspect_data`, `read_file`, `execute_mcp`, `_file_tool_common` — no tunnel awareness
- `excel_files`, `load_step`, `load_entity` — control-plane resident, unchanged
- All auth, RBAC, report management, scheduling

## C. Edge Agent

A credential-holding client proxy. No sandbox, no code executor, no result cache, no file store.

### C1. Package structure

```
data_plane/
├── pyproject.toml            # nats-py, pandas, pyarrow, cryptography, fastapi
├── Dockerfile
│
├── data_edge_agent/
│   ├── __init__.py
│   ├── main.py ················ entry point (NATS connection + admin UI)
│   ├── tunnel.py ·············· NATS client (subscribe, respond, advertise)
│   ├── protocol.py ············ JSON-RPC 2.0 messages
│   ├── dispatcher.py ·········· operation whitelist + client method dispatch
│   ├── client_factory.py ······ constructs real clients from local config —
│   │                             both DataSourceClient and ToolProviderClient
│   │                             types (imports them from backend/ — see below)
│   ├── models.py ·············· Table, TableColumn (Pydantic)
│   ├── serializers.py ········· DataFrame↔Parquet, Table↔JSON, previews
│   ├── config_store.py ········ SQLite + Fernet encrypted credential storage
│   ├── security.py ············ query scope validation, payload size check
│   ├── audit.py ··············· JSON-lines audit log
│   └── admin/
│       ├── app.py ············· FastAPI admin UI (localhost:9191)
│       ├── routes.py
│       └── static/
│
└── tests/
```

**The edge agent is not dependency-free, and the package layout should not pretend otherwise.** It imports the real client classes and `query_cancellation` from `backend/` (Step 5, C3), which pulls in SQLAlchemy and the drivers — that is why `pyproject.toml` above already lists them. The claim this design actually makes is narrower than "no Bow runtime": the edge agent runs **no sandbox, no code executor, no ORM models, no config store of Bow's own, and no generated Python** — the dependency chain that forced a fork in the remote-execution proposal. Client classes are a leaf: they take a config dict and return DataFrames.

Practically, `backend/app/data_sources/` has to be on the image and on `PYTHONPATH`, which the Dockerfile in Step 9 must copy. Extracting those clients into a package both sides depend on is the tidy end state; the `PYTHONPATH` arrangement is the v1 expedient, and the thing to check is that no import of a client drags in `app.models` transitively.

### C2. NATS connection and subscription

```python
async def run_edge_agent(config, store, factory):
    nc = await nats.connect(
        servers=[config.nats_url],          # wss://tunnel.bow.com (ws://nats:9443 in compose)
        user=config.nats_user,              # scoped NATS user — A11
        password=config.nats_password,
        reconnect_time_wait=5,
        max_reconnect_attempts=-1,
    )

    # Bound, not free: the handler needs self.config.edge_agent_id on both response
    # paths (A3's ownership field), self.client_factory, and self._nc for the
    # A6 payload ceiling — see C3.
    plane = RequestHandler(nc, config, store, factory)

    base = f"tunnel.{config.org_id}.{config.edge_agent_id}"
    for conn in config.connections:
        await nc.subscribe(f"{base}.conn.{conn.name}",
                           cb=lambda msg, c=conn: plane.handle_request(msg, c))
    await nc.subscribe(f"{base}.control", cb=plane.handle_control)

    # Both timers are load-bearing, not housekeeping (A10):
    #   - re-advertisement is what heals a registration lost while the leader
    #     worker was restarting, which is why it is "not optional" in B4;
    #   - the heartbeat is what drives DataEdgeAgent.status and is_active, so
    #     without it every agent looks stale a few minutes after connecting.
    await advertise(nc, config, store)          # immediately, then on a timer
    asyncio.create_task(_advertise_loop(nc, config, store, every=60))
    asyncio.create_task(_heartbeat_loop(nc, config, every=30))
```

A reconnect re-runs the subscriptions and re-advertises (A10); the timers survive it, since they publish on whatever connection `nc` currently holds.

`config.org_id` is used only to build subject strings — the connection subjects above, and the advertisement subject `tunnel.<org_id>.advertisements` (A10). It carries no authority: the NATS user's permission list (A11) is what constrains which subjects this process may name, and the control plane reads tenancy off the subject a message arrived on, which the broker attested (A10). A wrong value here fails loudly — at `subscribe` time for connections, at `publish` time for the advertisement — rather than quietly registering into someone else's org.

### C3. Request handler

```python
async def handle_request(self, msg, conn_config):
    # Bound before the try, so the error and audit paths always have something
    # to report. A malformed envelope must still produce a JSON-RPC error: if
    # it escapes the callback instead, the worker learns nothing until its
    # request times out, and the audit log records the failure not at all.
    started = time.monotonic()
    ref_id, operation, connection_name = None, "<unparsed>", None
    try:
        request = json.loads(msg.data)
        params = request["params"]
        ref_id = request.get("id")
        operation = params["operation"]
        connection_name = params["connection_name"]

        if operation not in ALLOWED_OPERATIONS:      # the 19 from A5, across both bases
            raise ValueError(f"Operation not permitted: {operation}")

        client = await self.client_factory.aget_client(
            connection_name,
            user_credentials=params.get("user_credentials"),
        )

        kwargs = params.get("kwargs", {})
        if operation == "execute_query":
            security.validate_query_scope(kwargs["sql"], conn_config.security)

        result = await dispatch(
            client, operation, kwargs, ref_id=ref_id,
            timeout=self._budget_for(operation, conn_config, params),
        )
        # max_payload comes from the broker (nc.max_payload, learned at
        # connect), not from conn_config — A6 is explicit that the ceiling is
        # derived from the transport rather than configured per connection.
        result = serialize_result(operation, result, conn_config.security,
                                  max_payload=self._nc.max_payload)

        await msg.respond(json.dumps({
            "jsonrpc": "2.0", "id": ref_id,
            "edge_agent_id": self.config.edge_agent_id, "result": result,
        }).encode())

    except Exception as e:
        await msg.respond(json.dumps({
            "jsonrpc": "2.0", "id": ref_id,
            "edge_agent_id": self.config.edge_agent_id,
            "error": {"code": -32000, "message": str(e),
                      "data": {"operation": operation}},
        }).encode())
    finally:
        audit.log(operation, connection_name,
                  duration_ms=(time.monotonic() - started) * 1000)
```

The handler is a bound method rather than a free function because it needs `self.config.edge_agent_id` on both response paths (A3's ownership field) and `self.client_factory` — the sketch that read a module-global `config` was hiding a dependency the process actually has to thread through.

**Which budget applies depends on the operation** (A5). One helper, so the rule lives in one place:

```python
def _budget_for(self, operation, conn_config, params):
    # The edge agent owns the policy; timeout_ms is only the caller's
    # patience, so it can lower the budget but never raise it.
    if operation in ("get_schemas", "warm_all"):
        effective = self.config_store.index_timeout_seconds       # agent-level (C4)
    else:
        effective = self.config_store.effective_query_timeout(conn_config)
    asked = params.get("timeout_ms")
    return min(asked / 1000, effective) if asked else effective
```

An earlier draft computed a single query budget for every operation and then quietly ignored it for the eighteen non-query ones — which is how an index could run unbounded while a `timeout_ms` sat in the payload doing nothing.

`client_factory.get_client()` merges credentials:

```python
async def aget_client(self, connection_name, user_credentials=None):
    conn = self.config_store.get_connection(connection_name)
    if user_credentials:                    # ephemeral: never cached, never stored
        params = {**conn.config, **user_credentials}
        return await asyncio.to_thread(construct_client, conn.type, params)

    cached = self._clients.get((connection_name, conn.config_version))
    if cached is None:
        params = {**conn.config, **conn.credentials}    # local encrypted store
        cached = await asyncio.to_thread(construct_client, conn.type, params)
        self._clients[(connection_name, conn.config_version)] = cached
    return cached
```

**System-credential clients are cached per connection; user-credential clients never are.** Constructing a client builds a SQLAlchemy engine and its pool, so a fresh one per request means a fresh pool per request — every query paying a full TCP and auth handshake, and the pool it just built discarded before it can be reused. Caching keyed on `(connection_name, config_version)` keeps C4's promise that "credential changes take effect immediately": editing a connection in the admin UI bumps the version and the next request builds a new client, while the old one ages out. Per-user clients are excluded by construction — caching them would be the persistence A7 says does not happen.

Construction is blocking (DNS, TCP, driver handshake) and therefore goes through `asyncio.to_thread`, for the same reason as the join above.

One interaction with cancellation is worth naming: `query_cancellation` keys its registry on `(id(client), thread_ident)`, so caching makes `id(client)` stable across requests — which is what the registry expects, and what the per-request-construction sketch would have made unnecessarily fragile.

#### Query timeout and cancellation live here

**The timeout must be enforced in the process that can cancel the statement.** On a direct connection Bow already does this: `_call_with_timeout` (`code_execution.py:810`) runs the query in a daemon thread, abandons it on expiry, and — before raising — cancels it at the source, because, in its own words, *"abandoning the thread frees BOW but not the source: the statement keeps running there until it completes on its own."*

That machinery is process-local. `query_cancellation` keys its registry on `(id(client), thread_ident)` (`query_cancellation.py:64`), populated by `track()` inside each pooled client's `connect()`. Under tunneling the real client runs **here**, so the registry is populated **here** — and works unmodified. Step 5 already puts `backend/` on `PYTHONPATH`, so the edge agent imports `app.data_sources.query_cancellation` as-is — which also means the container image has to actually contain it (Step 9).

So the edge agent ports the same pattern:

```python
async def dispatch(client, operation, kwargs, *, timeout, ref_id):
    if operation in ("get_schemas", "warm_all"):
        return await _dispatch_cooperative(client, operation, kwargs,
                                           timeout=timeout, ref_id=ref_id)
    if operation != "execute_query":
        return await asyncio.wait_for(
            _dispatch_plain(client, operation, kwargs), timeout)

    holder, thread = {}, _run_in_daemon_thread(client, kwargs, holder)
    _in_flight[ref_id] = ("query", client, thread.ident)   # A9 cancels through this
    try:
        # NOT thread.join(timeout). See below — this is the load-bearing await
        # in the whole edge agent.
        await asyncio.to_thread(thread.join, timeout)
        if thread.is_alive():                        # expired — abandon and CANCEL
            outcome = query_cancellation.cancel_thread(client, thread.ident)
            raise RemoteQueryTimeout(timeout, outcome=outcome)
        if "error" in holder:
            raise holder["error"]                    # re-raise in the handler's context
        return holder["result"]
    finally:
        _in_flight.pop(ref_id, None)
```

**`thread.join(timeout)` must not be called directly.** It is a synchronous block, and `dispatch` is reached from a NATS message callback — so a bare join freezes the edge agent's event loop for the entire query budget, up to five minutes on a default configuration. Everything that loop owns stops with it:

- **the control subject**, so a user-initiated cancel (A9) sits unread in the socket buffer until the query it was meant to cancel has finished on its own — cancellation is not merely unreliable, it is structurally impossible;
- **NATS PING/PONG**, so the broker concludes the client is dead and disconnects it mid-query, after which the reply has nowhere to go;
- **progress publication** (A8), so long operations appear frozen;
- **every other connection this agent serves**, since they share the one loop.

`await asyncio.to_thread(thread.join, timeout)` keeps the same abandon-and-cancel semantics and costs one extra thread. The same rule applies anywhere else in the edge agent: no blocking call — client construction, driver connect, Parquet serialization of a large frame — belongs directly on the loop.

`_dispatch_plain` prefers the client's `a*` method when it has one and falls back to `asyncio.to_thread` on the sync one, which is what lets the real `aget_schemas` do its own `_accepts_kwarg` introspection against its own signature (B1).

`_dispatch_cooperative` wraps its return as `{"value": …, "index_stats": client.index_stats()}` — calling the real client's own `index_stats()` here, locally and cheaply, so the proxy never has to (A5, B1). It handles the two long operations, which stop by flag rather than by statement cancel (A9): it registers `("cooperative", threading.Event())` in `_in_flight`, passes a `cancel_check` reading that event plus a `progress_callback` that publishes to `tunnel.<org>.<edge_agent>.progress.<ref_id>`, and bounds the whole thing with the index budget.

`_in_flight` is the single registry both cancellation paths use. A9's user-initiated cancel looks up the same `(client, thread_ident)` pair and calls the same `query_cancellation.cancel_thread`. Without it a user pressing stop would set a flag that nothing reads — the blocking driver call in the daemon thread polls nothing — and the query would run to completion exactly as an uncancelled timeout does.

The control plane translates `RemoteQueryTimeout` back into `QueryTimeoutError` (B3), so the codegen retry loop sees exactly what it sees in direct mode — with the difference that the abandoned query is genuinely stopped rather than left running.

**Report the real outcome.** `cancel_thread` returns `"not_running"`, `"cancelled"`, or a failure string. Pass it back in the error payload and record it in the audit log. `"not_running"` legitimately means "finished between expiry and cancel" — it must never be what a tunneled timeout reports by default, or a query left running looks identical to one that completed.

**The control plane's NATS timeout is a backstop**, set above this budget (`timeout + 5` in B3's `invoke`). It fires only when the edge agent is unreachable — a genuinely different failure, and one no cancellation could help with anyway.

### C4. Admin UI

Localhost-only web UI (`http://localhost:9191`):

- **Setup wizard**: NATS URL and credentials on first launch
- **Connection CRUD**: add/edit/remove connections with credentials
- **Test connection**: verify connectivity locally before advertising
- **Query timeout**: an **agent-level default** applied to every connection, plus an optional **per-connection override**. This is the authoritative budget for `execute_query` — Bow cannot raise it, and the effective value is reported upward in the advertisement (A10) so the control plane can size its own wait and display it
- **Index timeout**: a separate agent-level budget for `get_schemas` / `warm_all`, which legitimately run for minutes and would be aborted by any sane query budget (A5). Also advertised, and the number `allow_responses: { expires }` in A11 must be sized above
- **Security constraints**: allowed schemas, denied tables per connection
- **Dashboard**: tunnel status, connection health, what is currently advertised
- **Audit log**: every operation with timestamp, duration, row counts
- **Live config updates**: credential changes take effect immediately; connection add/remove triggers re-advertisement

No file upload path — user files live on the control plane.

### C5. Security

The edge agent sees SQL strings and method arguments, never Python. Per-connection controls: `allowed_schemas`, `denied_tables`, and `query_timeout_seconds` (per-connection override over an agent-level default, C4) — the timeout is owned here, not on the control plane. The serialized-size check (A6) is derived from the broker's `max_payload`, not configured per connection. Full local audit trail. The AST sandbox stays on the control plane, in one copy.

## D. Data Model Changes

### D1. Connection model — `backend/app/models/connection.py`

```python
tunnel_mode = Column(Boolean, nullable=False, default=False)
edge_agent_id = Column(String(255), nullable=True)
```

The flag is on `Connection`, not `DataSource`: clients are constructed per connection and keyed `f"{ds.name}:{conn.name}"` (`data_source_service.py:2559`), so connection-level granularity matches how clients are built and lets one data source span planes.

Tunneled connections are **created only by advertisement** (A10). `Connection.config` for one is populated from the advertisement and holds `query_timeout_seconds` (and the agent's `index_timeout_seconds`), the effective budgets the edge agent reported. On this side that is display-and-sizing information: editing it in Bow would change nothing, so those fields are read-only, as are the credential fields, which have no value here at all.

**Read-only means the credential and config surface, not the whole row.** Everything that decides *who may use the connection and where it appears* stays a normal Bow admin action: which data sources it belongs to (`domain_connection`, without which no agent can reach it at all — A10), RBAC, and deletion. The edge agent declares what it serves; it does not get to decide what the org does with it.

### D2. DataEdgeAgent model — `backend/app/models/data_edge_agent.py` (new)

```python
class DataEdgeAgent(BaseSchema):
    __tablename__ = "data_edge_agents"
    __table_args__ = (
        # One row per agent per org, updated in place by each advertisement.
        # The constraint is what makes the handler's upsert safe under the
        # timer-driven re-advertisement in A10.
        UniqueConstraint('organization_id', 'edge_agent_id', name='uq_data_edge_agents_org_agent'),
    )

    edge_agent_id = Column(String(255), nullable=False)
    organization_id = Column(String(36), ForeignKey('organizations.id'))
    status = Column(String(50), default="offline")   # online/offline/stale
    last_connected_at = Column(DateTime, nullable=True)
    last_heartbeat_at = Column(DateTime, nullable=True)
    client_version = Column(String(50), nullable=True)
    label = Column(String(255), nullable=True)          # advertised edge_agent_name

    # Last advertisement, stored whole — including connections the handler
    # rejected. Without this a conflicting advertisement leaves no trace and
    # the UI cannot explain why a connection is missing (A10).
    last_advertisement = Column(JSON, nullable=True)
    last_advertised_at = Column(DateTime, nullable=True)
```

`last_advertisement` holds the payload plus a per-connection outcome:

```json
{"edge_agent_name": "NYC Office", "version": "1.0.0",
 "connections": [
   {"name": "prod-pg",    "type": "postgresql", "status": "registered"},
   {"name": "sharepoint", "type": "sharepoint", "status": "conflict",
    "reason": "name already claimed by agent 'tokyo-01'"}
 ]}
```

A JSON column rather than a second table: the edge agents page renders per agent, so nothing needs to query across agents, and it avoids a migration for a shape that is still settling. `(organization_id, edge_agent_id)` is unique — an agent is one row, updated in place on each advertisement.

### D3. Alembic migration

Single migration adding `tunnel_mode` / `edge_agent_id` to `connections` and creating `data_edge_agents`.

## E. NATS Deployment

### E1. Docker Compose

```yaml
services:
  nats:
    image: nats:latest
    ports:
      - "4222:4222"     # internal TCP (workers)
      - "9443:9443"     # websocket (edge agents) — plaintext; TLS at the ingress
    volumes:
      - ./nats.conf:/etc/nats/nats.conf
    command: ["-c", "/etc/nats/nats.conf"]
    networks:
      - bow-network

  app:
    environment:
      BOW_NATS_URL: nats://nats:4222
    # ...

  edge-agent:
    build:
      context: .
      dockerfile: data_plane/Dockerfile
    ports:
      - "9191:9191"
    environment:
      BOW_EDGE_AGENT_SECRET_KEY:    ${BOW_EDGE_AGENT_SECRET_KEY}
      BOW_EDGE_AGENT_NATS_URL:      ws://nats:9443    # prod: wss://tunnel.bow.com
      BOW_EDGE_AGENT_NATS_USER:     edge-cust-b-nyc
      BOW_EDGE_AGENT_NATS_PASSWORD: ${EDGE_NATS_PASSWORD}
      BOW_EDGE_AGENT_ORG_ID:        cust-b
      BOW_EDGE_AGENT_AGENT_ID:      nyc-01          # appears in subjects; must match nats.conf
      BOW_EDGE_AGENT_AGENT_NAME:    "NYC Office"    # display label only (A10)
      BOW_EDGE_AGENT_ADMIN_PORT:    9191
```

**Scheme and port have to match the listener that is actually there.** E3's websocket block is `no_tls: true` because TLS is terminated at Caddy or the K8s ingress (A1), so inside compose — where no terminator exists — the edge agent speaks `ws://nats:9443`. Pointing `wss://` straight at that listener, as an earlier draft did, fails the handshake with an error that looks like a certificate problem and is not one.

In production the URL is `wss://tunnel.bow.com` (443 at the ingress, proxied to 9443), and the edge agent's config is the only thing that changes. If you want compose to exercise the real shape, add the Caddy service from A1 and point the edge agent at it; what does not work is naming a scheme the compose topology cannot provide.

### E2. Kubernetes

NATS has an official Helm chart (`nats/nats`). Run as a StatefulSet alongside the Bow deployment. Workers connect via the K8s Service `nats://nats:4222`. Edge agents connect via an Ingress on WSS. For HA, run 3 NATS nodes; edge agent subscriptions survive a single node failure.

### E3. NATS config

```
# nats.conf
listen: 0.0.0.0:4222
max_payload: 67108864   # 64MB — large result sets

websocket {
  listen: "0.0.0.0:9443"
  no_tls: true           # TLS terminated at Caddy/ingress
}

authorization {
  # v1: static users with scoped subject permissions (see A11).
  # Admin-managed; reload with `nats-server --signal reload` after edits.
  users: [
    { user: "bow-worker", password: "$2a$11$..."
      permissions: {
        publish:   ["tunnel.>"]
        subscribe: ["_INBOX.bow.>", "tunnel.*.advertisements",
                    "tunnel.*.*.progress.*", "tunnel.*.*.heartbeat"]
      }}

    { user: "edge-cust-b-nyc", password: "$2a$11$..."
      permissions: {
        subscribe: ["tunnel.cust-b.nyc-01.>"]
        publish:   ["tunnel.cust-b.advertisements", "tunnel.cust-b.nyc-01.>"]
        allow_responses: { max: 1, expires: "20m" }
      }}
  ]
}

# Clustering (production)
# cluster {
#   listen: 0.0.0.0:6222
#   routes: ["nats://nats-1:6222", "nats://nats-2:6222"]
# }
```

## F. Files Summary

### New files

| File | Purpose |
|------|---------|
| `backend/app/data_sources/clients/tunneled_client.py` | `TunneledClient` and `TunneledToolProviderClient` — one proxy per client base, so `codegen_clients`' `isinstance` filter keeps working (B1) |
| `backend/app/services/tunnel_client.py` | NATS transport, sync bridge, streaming + cancel bridge for workers |
| `backend/app/services/tunnel_errors.py` | `TunnelOwnershipError`, `TunnelNotConnectedError`, `RemoteQueryTimeout`, `TunnelPayloadTooLarge`, `TunnelSyncOnLoopError`, `translate_remote_error` — shared by the transport and the edge agent |
| `backend/app/services/tunnel_advertisement.py` | Advertisement + heartbeat handler (subscribes to `tunnel.*.advertisements`; leader-gated, B4) |
| `backend/app/models/data_edge_agent.py` | DB model for edge agent agents |
| `backend/app/schemas/tunnel_schema.py` | Pydantic schemas |
| `backend/app/routes/data_edge_agents.py` | REST endpoints for edge agent listing |
| `backend/alembic/versions/xxx_add_tunnel_support.py` | Migration |
| `frontend/pages/settings/edge-agents.vue` | Tunnel agents admin page |
| `frontend/composables/useDataEdgeAgents.ts` | API composable |
| `nats.conf` | NATS server configuration |
| `data_plane/` | Data plane project — the data edge agent (its own pyproject; not a package under backend/) |

### Modified files

| File | Change |
|------|--------|
| `backend/app/services/connection_service.py` | Tunnel branch in `construct_client` (`:1658`) |
| `backend/app/services/data_source_service.py` | Tunnel branch in `construct_clients` (`:2507`) and `construct_client` (`:2274`) |
| `backend/app/models/connection.py` | Add `tunnel_mode`, `edge_agent_id` |
| `backend/app/schemas/connection_schema.py` | Add tunnel fields (read-only) |
| `backend/main.py` | Lifespan: `TunnelClient` + advertisement handler |
| `docker-compose.yaml` | Add NATS server + edge-agent services |
| `frontend/components/connections/` | Show tunnel badge + owning agent; hide credential fields |

### NOT modified

| File | Why unchanged |
|------|--------------|
| `backend/app/ai/agent_v2.py` | Receives `ds_clients`; indifferent to what's in it |
| `backend/app/ai/agents/coder/coder.py` | Code generation unchanged |
| `backend/app/ai/code_execution/code_execution.py` | Sandbox stays on the control plane |
| `backend/app/ai/tools/implementations/*.py` | Tools call client methods; the proxy answers them |
| `backend/app/data_sources/clients/*.py` (existing) | Real clients run unchanged on the edge agent. The new `tunneled_client.py` sits beside them but modifies none of them |

## G. Known Gaps

Status values:

- 🔴 **Not solved** — the design does not answer the question; an implementer would have to invent an answer.
- ⚪ **Won't be solved** — a deliberate, documented v1 limitation.
- 🟢 **Solved** — a real concern this design answers; the row records where.

| Name | Description | Status |
|------|-------------|--------|
| NATS connection loop ownership | Generated code runs in `_CODE_EXEC_POOL` threads that have **no event loop** (`safe_client.py:4-6`), and `generate_df` is a plain `def`, so the sandbox cannot await — it must hand its coroutine to a loop elsewhere. Resolved by following the pattern already on this call path: `execute_code_async` captures its loop and stashes it (`code_execution.py:1310-1311`) so `UsageLimitContext.run_blocking` (`usage_policy_service.py:572`) can route a sync quota check back to it. `TunnelClient.connect()` captures `self._loop` in the same statement that creates `self._nc`, so the owning loop and the target loop cannot disagree, and `invoke_sync` uses `run_coroutine_threadsafe` against it (B3). No dedicated background loop; B4 now owns the whole connection lifecycle so the two cannot drift apart again. The one thing **not** to copy is `run_blocking`'s `asyncio.run` fallback — safe for a self-contained coroutine, fatal for a shared connection owned by another loop, and the same trap `SafeHttpClient` sits next to. | 🟢 Solved |
| Query timeout must cancel at the source | On a direct query `_call_with_timeout` (`code_execution.py:810`) abandons the worker thread **and** cancels the statement via `query_cancellation`, because "abandoning the thread frees BOW but not the source." That registry is process-local (`query_cancellation.py:64`), so a control-plane timeout against a `TunneledClient` finds nothing to cancel and returns `"not_running"` — the benign outcome — while the query keeps running in the customer's network, and each codegen retry starts another. Resolved by moving both the policy and the enforcement to where the connection is: the edge agent owns the budget (agent-level default plus per-connection override, C4), reports the effective value upward in the advertisement (A10), abandons and cancels locally with `query_cancellation` used unmodified, and returns the real outcome rather than `"not_running"` (A5, C3). `timeout_ms` is only the caller's patience and can lower the budget, never raise it. `TunneledClient` opts out of the wrapper's timeout (B3) and the NATS timeout becomes a backstop for an unreachable edge agent. A9 keeps user-initiated cancellation, which does originate on the control plane. | 🟢 Solved |
| Duplicate connection names across agents | Resolved in three parts. **Transport:** `edge_agent_id` is a subject token (A3), so two agents advertising `prod-pg` occupy different subjects and NATS cannot fan a query out to both databases — the collision is structurally impossible rather than something an admin must avoid. **Database:** `uq_connections_org_name` (`connection.py:16`) still governs, and the handler rejects on `IntegrityError` exactly as the interactive path does at `data_source_service.py:769`, rather than upserting on `(org_id, name)` and silently merging two databases into one row. A rejected connection gets no row, so no worker publishes to it and the edge agent sits inert — the failure is contained to that connection. **Feedback:** the advertisement is persisted whole in `DataEdgeAgent.last_advertisement` including rejected entries, and the edge agents page names both sides — only the control plane knows that `nyc-01` already holds the name (A10, D2, H15). | 🟢 Solved |
| Result larger than one NATS message | Bow enforces no result-size limit today — `execute_query` is `pd.read_sql` with no cap (`postgresql_client.py:83`) and the only bound on a direct query is time. This design adds no policy limit either. But NATS caps message size (`max_payload`, E3), so a result serializing above it is refused by the broker. The edge agent therefore checks **serialized bytes** after Parquet encoding — derived from `max_payload`, not admin-configured, since rows cannot predict the outcome — and errors with the actual size rather than truncating, feeding the codegen retry loop toward SQL aggregation. The check is on the **encoded** size — v1's base64 envelope means a 64MB `max_payload` fits ~48MB of Parquet — and 64MB is NATS's own maximum for that setting, so there is no headroom to raise. Accepted consequence: a query that succeeds direct can fail tunneled. Chunked transfer removes the ceiling in v2, but needs `allow_responses: { max: 1 }` relaxed (A6, A11). | ⚪ Won't be solved |
| Round trips scale with query count | Each `execute_query` in a generated function is one WAN round trip, serialized. A function issuing five queries pays five. This is the accepted cost of the client proxy (see "Why a Client Proxy"). The mitigation — shipping the whole function when every client key resolves to one edge agent — is a deliberate v2 optimization, not a v1 requirement. | ⚪ Won't be solved |
| `user_required` breaks the structural credential guarantee | For `user_required` connections the control plane resolves and forwards per-user credentials (A7), so the "never reaches `resolve_credentials`" property holds for system credentials only. Deliberate: it is the same trust model as today, the tokens are ephemeral, and the alternative is not supporting per-user auth over the tunnel at all. | ⚪ Won't be solved |
| Same-credential duplicate subscriber | Two processes sharing one NATS credential are indistinguishable to the broker; both receive and answer the same request. v1 mitigates by convention (one instance per credential, stop-then-start, optional `max_connections: 1`) plus the runtime `edge_agent_id` assertion in B3. The registration lease that would enforce it is v2 (A3, A11). | ⚪ Won't be solved |
| `nats.conf` agent provisioning is manual | Adding or removing a *edge agent* means editing `nats.conf` and reloading the broker; the `edge_agent_id` there must match `BOW_EDGE_AGENT_AGENT_ID` on the agent. Connection-level drift is gone — an agent's grant is one wildcard (`tunnel.<org>.<edge_agent_id>.>`), so connections never appear in `nats.conf` and adding one needs no broker change. A mismatched `edge_agent_id` fails loudly at `subscribe`, not silently. The v2 auth callout removes the manual step entirely by minting credentials from Bow's DB (A11). | ⚪ Won't be solved |
| Broker sees payloads in plaintext | NATS terminates TLS and decrypts at the broker, so it observes per-user OAuth tokens and result sets. Acceptable while the broker is operated by the same party as the control plane; the property to revisit if a third party ever runs it (A11). | ⚪ Won't be solved |
| Multi-tenant SaaS isolation | v1 isolates orgs by subject naming plus per-user permission lists, weaker than account-level separation. v1 targets single-tenant / self-hosted, where the trust boundary already includes the admin. Per-org NATS accounts with JWT+NKey are v2 (A11). | ⚪ Won't be solved |
| Multi-connection routing | Resolved by the client proxy: routing happens per call at the `ds_clients` dict lookup, inside the sandbox, where the connection identity is known. Code spanning two edge agents — or mixing tunneled and direct connections in one `generate_df` — executes correctly with no routing decision to make (B2). | 🟢 Solved |
| Control plane holding system credentials | The tunnel branch is taken immediately after `resolve_client_class` and before `resolve_credentials()` at all three construction sites (B2). For `system_only` tunneled connections there is no code path on which the control plane obtains the credential. | 🟢 Solved |
| `org_id` provenance | Read from the **subject**, never from the advertisement payload (A3, A10, A11). The earlier form of this row said "from the authenticated NATS identity", which is not something core NATS offers: a subscriber receives subject, headers, reply and bytes, with no field naming the publishing user — so a handler on a single global `tunnel.advertisements` would have had nothing but the payload to trust, on a subject every edge agent must be granted publish. Fixed by scoping the subject: `tunnel.<org_id>.advertisements`, one granted per edge agent, org read out of `msg.subject`. The connection subjects always had this property; the advertisement subject was the one place it silently did not. | 🟢 Solved |
| `get_description` / capability RPCs | Not needed. The advertisement carries the connection `type`, so the control plane resolves the real client class locally and reads `description` and `capabilities` off it (A5, A10) — including before any call is made, which is what `resolve_file_client:342` requires. | 🟢 Solved |
| Sandbox code duplication | Avoided. The sandbox, `validate_python_code`, and `CodeSecurityVisitor` stay on the control plane in one copy; the edge agent never executes generated Python and needs none of that dependency chain (C5). | 🟢 Solved |
| Cross-credential subject hijacking | A foreign or misconfigured agent subscribing to another agent's or org's connection subject is refused by the broker at `subscribe` time (A11). Subject scoping is broker-enforced, not a naming convention. | 🟢 Solved |
| Edge agent reading other orgs' replies | An edge agent is granted no subscribe permission on any `_INBOX` subject, so it cannot read replies destined for workers. It replies via `allow_responses`, a one-shot grant to the reply subject of a message it actually received (A11). Workers set `inbox_prefix="_INBOX.bow"` (B3) so their own subscribe permission is scoped. | 🟢 Solved |
| Response correlation and session registry | NATS request/reply generates inbox subjects and correlates responses natively, removing the pending-futures dict, SessionRegistry, and custom Tunnel Router process (A4). | 🟢 Solved |
| Two-message binary response vs `allow_responses` | Incompatible with `allow_responses: { max: 1 }`. Resolved by using a single-message response — base64 inline in v1, raw Parquet payload with NATS headers as the optimization (A6). | 🟢 Solved |
| An advertised connection reaches no AI agent | `Connection` is M:N with `DataSource` via `domain_connection` (`connection.py:132`), and `construct_clients` iterates `data_source.connections` (`data_source_service.py:2507`). Advertisement creates a row with no membership, so a freshly registered connection is visible in settings and reachable by nothing — no `ds_clients` entry, nothing in the AI agent's context. Resolved by naming the boundary rather than automating past it: the edge agent declares *what it serves*; an org admin still places it in a data source and grants access, exactly as for a direct connection, and D1's read-only rule covers the credential and config surface only. The edge agents page shows "registered, in no data source" so the state is visible rather than looking like a silent failure (A10, D1, H15). | 🟢 Solved |
| A running query freezes the edge agent | `dispatch` bounded `execute_query` with a bare `thread.join(timeout)` inside an `async def` reached from a NATS callback — blocking the event loop for the whole budget. That takes the control subject with it, so A9's cancel cannot be read until the query it targets has already finished; it also stops PING/PONG (the broker disconnects mid-query), progress publication, and every other connection the agent serves. Resolved with `await asyncio.to_thread(thread.join, timeout)`, same semantics, one extra thread — and the general rule that no blocking call, including client construction and Parquet serialization, sits directly on the loop (C3). | 🟢 Solved |
| Advertisement handled by every worker | `backend/main.py:736` runs `workers=20`, and core NATS fans a subject out to every subscriber. An advertisement handler started in `lifespan` therefore ran 20 times per message: 20 upserts of one `DataEdgeAgent`, 20 racing create-or-updates per connection, 20 rollbacks on a name conflict, and 20 heartbeat sweepers flipping `is_active`. Resolved by leader-gating registration with the existing `try_acquire_scheduler_leader()` (`main.py:441`) — the same gate the Slack listener and email poller already use for long-lived external subscriptions — while the transport stays per-worker because each worker's clients need their own connection on their own loop (B4). | 🟢 Solved |
| Streaming operations take callables | `aget_schemas` and `awarm_all` accept a `progress_callback`, and `awarm_all` a `cancel_check` it polls between chunks (`base.py:173,216`); neither can be serialized. Resolved by reconstituting both at the far end: progress is published to `tunnel.<org>.<edge_agent>.progress.<ref_id>` so NATS routes it to the one worker holding the callback (A8), and cancel arrives on the control subject and sets an event the `cancel_check` reads (A9). Note this is the one place where a flag *is* the right mechanism — the opposite of `execute_query`, which must be cancelled at the statement. | 🟢 Solved |
| Per-user credential resolution could reach a system credential | B2's branch calls `_resolve_user_credentials`, and the obvious implementation — delegate to `ConnectionService.resolve_credentials` — returns *system* credentials unconditionally for `system_only` (`connection_service.py:1786`) and as a fallback for `user_required` when `current_user is None` (`:1790`), which is how indexing calls it. Today that returns `{}` because advertisement stores no credentials, which makes the guarantee **data-dependent rather than structural** and one populated column away from silently forwarding secrets. Resolved by defining the helper narrowly — `None` for anything but `user_required` with a real user, reading `UserConnectionCredentials` directly rather than through the wrapper carrying the fallbacks — plus enforcing D1's read-only rule at the schema and route layer, and asserting the credentials column is empty at registration (B2, A10). | 🟢 Solved |
| Edge agent timers left unstarted | A10 requires two periodic behaviours — re-advertisement, which heals a registration lost while the leader worker restarted (B4), and the heartbeat that drives `DataEdgeAgent.status` and `Connection.is_active`. C2 showed a one-shot `advertise()` and no heartbeat loop, so an implementer following it would ship an edge agent that registers once and then looks stale forever. Resolved by starting both as tasks in C2, with a step 7 test that asserts a second advertisement arrives **without** a reconnect — and that heartbeats keep flowing during a long query, which doubles as a cheaper detector for the C3 blocking-join rule. | 🟢 Solved |
| Oversized request payload | `get_schemas` sends `prior_catalog` / `prior_tables` for incremental indexing, and on a large catalog they approach `max_payload`. A6 originally said "same check, same place" as the result ceiling, which cannot work: an oversized request is refused by `nats-py` at **publish**, so the edge agent never sees it and can degrade nothing. Resolved by checking on the control plane before publishing (`TunnelClient.invoke`, raising `TunnelPayloadTooLarge`) and having `aget_schemas` — and only it — catch that error, drop the prior state, and retry once, since prior state is an optimization input rather than the request (A6, B1, B3). | 🟢 Solved |
| `invoke_sync` from the event loop deadlocks | `index_stats` is called directly from async code with no thread between — `connection_indexing_service.py:619` unguarded, `connection_service.py:1456` behind a `hasattr`. A proxy implementing it with `invoke_sync` schedules a coroutine onto the running loop and then blocks that loop waiting for it, so the coroutine can never run: a deadlock rather than a slow call, and no timeout makes it safe. Resolved two ways — `index_stats` answers from stats cached out of the last `get_schemas` / `warm_all` response and never touches the network, and `invoke_sync` now refuses outright when its caller is already on the owning loop (`TunnelSyncOnLoopError`), mirroring `run_blocking`'s identical refusal at `usage_policy_service.py:575-580` (B1, B3). | 🟢 Solved |
| Tunneled MCP client escapes the codegen filter | `ToolProviderClient` is an ABC **parallel to** `DataSourceClient` (`tool_provider_base.py:6`), and `codegen_clients` drops tool providers by `isinstance` (`:95-99`). A single `TunneledClient(DataSourceClient)` wrapping an MCP connection is not an instance of it, so it passes the filter into `ds_clients` and is advertised to the coder as queryable — reintroducing the exact bug that function documents, where the model emits `ds_clients["Agent:Conn"].execute_mcp(...)` and the code attempt fails. Resolved with a second proxy, `TunneledToolProviderClient(ToolProviderClient)`, selected in B2 by `issubclass(ClientClass, ToolProviderClient)`; the two share a mixin but not a hierarchy, because the `isinstance` check is the point (A5, B1, B2). | 🟢 Solved |
| Catalog left an incomplete operation surface | A5 listed 17 operations and B1 sketched five, which reads as a complete design and is not one. Two live operations were missing entirely — `agrep_files` (`grep_files.py:195`) and `afile_version` (`read_file.py:470`) — and the unimplemented remainder fails *quietly*: `grep_files` raises `NotImplementedError`, `file_version` returns `None` (read as "no cheap version", silently disabling content caching), and `read_raw_bytes` is absent so `attach_file.py:135`'s `hasattr` probe decides the source has no bytes. Resolved by taking the catalog to 19 and giving B1 a rule covering every one rather than a sample (A5, B1). | 🟢 Solved |
| `catalog_identity_available` defaults the dangerous way | A property of the client *instance* (`base.py:267`), false for delegated-only sources like OneNote, read by its caller as `getattr(client, ..., True)` (`connection_service.py:1428`). A proxy that simply omits it answers True, and an empty `get_schemas()` from such a source is then treated as authoritative — pruning every row signed-in users contributed, which is the failure its own docstring describes. It cannot be resolved from `_client_class` (instance-dependent) nor fetched by RPC (read synchronously), so the edge agent advertises it and the proxy answers from `Connection.config` (A5, A10, B1). | 🟢 Solved |
| Cancelling a tunneled index | `awarm_all` takes a `cancel_check` callable, and indexing supplies a local `threading.Event`'s `is_set` (`connection_indexing_service.py:617`). A9 gave the far side an event to read but nothing polled the near one, so a user cancel set a local flag the edge agent never heard — and because `request_cancel` (`:202`) optimistically marks the row `CANCELLED`, the UI reported success while the index ran to completion. Resolved by `TunnelClient.invoke_streaming` polling `cancel_check()` for the life of the request and publishing one cancel on the control subject when it first goes true (B3, A9). The mirror of the progress problem: progress is a callable the *far* side needs, cancel is one the *near* side must translate. | 🟢 Solved |
| Signature introspection against a proxy | `aget_schemas` passes `prior_catalog` / `prior_tables` only if `_accepts_kwarg(self.get_schemas, …)` says the client accepts them (`base.py:191-196`). Against a `TunneledClient` that introspects the proxy, not the real client, and a wrong answer silently disables incremental indexing — every tunneled index re-extracting everything, with no error. Resolved by moving the introspection to the side that has the real signature: the proxy forwards kwargs verbatim and the edge agent invokes the real client's own `a*` method, which introspects its own sync form (B1, C3). | 🟢 Solved |
| Long operations had no budget | The query timeout is the wrong scale for `get_schemas` / `warm_all`, and C3's original dispatch computed a budget then ignored it for every non-query operation — leaving indexing unbounded while `allow_responses: { expires }` was nonetheless sized against it. Resolved with a second advertised budget, `index_timeout_seconds` (A5, C4), and the stated ordering: index budget < NATS request timeout < `expires`, so the edge agent's own budget always fires first and the reply grant is still alive when it does (A11). | 🟢 Solved |
| Result dtypes Parquet cannot represent | `pd.read_sql` returns whatever the driver produced, including `object` columns pyarrow refuses — mixed types, JSONB `dict`, `bytes`, `Decimal` beside `float`, driver geometry. Direct connections pass these into `generate_df` untouched; tunneled ones hit a serialization boundary. Mitigated at the serializer — coerce what has an unambiguous representation, fail with the column name and dtype when it does not — so it is a legible error rather than a stack trace from pyarrow. Residual: it remains a second way a query can succeed direct and fail tunneled, alongside size (A6). | ⚪ Won't be solved |
