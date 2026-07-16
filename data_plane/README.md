# Data Plane

Components that run **outside** the Bow instance, in the customer's own network.

The control plane — everything else under `backend/` — never holds a credential
for a tunneled data source. The components here do.

## `data_edge_agent`

The process an admin installs on their site. It holds credentials for its local
data sources, connects **outbound** to NATS (so nothing has to route inward),
and answers proxied client operations.

Design: `docs/design/secure-data-tunnel-v4-nats.md`
Plan: `docs/design/secure-data-tunnel-implementaion-plan.md`

### Run

```bash
cd data_plane
uv sync --extra dev
cp data_edge_agent/config.example.yaml config.yaml   # then edit
uv run python -m data_edge_agent --config config.yaml
```

Credentials come from the environment, never the file:

```bash
export BOW_EDGE_AGENT_NATS_TOKEN=...
```

Any config field can be overridden as `BOW_EDGE_AGENT_<FIELD>`; environment wins
over file.

### Test

```bash
cd data_plane && uv run python -m pytest -q
```

### Phase 1 scope — what this does and does not do

**Does:** load config, validate it, connect to NATS with infinite reconnect,
subscribe to this agent's subjects, log every request that arrives, and answer
each one so a caller using `nc.request()` gets an error instead of a timeout.

**Does not:** construct real clients, execute anything, advertise its
connections, publish heartbeats, or serve an admin UI. Every operation currently
answers JSON-RPC `-32601`.

### Subjects

```
tunnel.<org_id>.<edge_agent_id>.conn.<connection_name>   requests (subscribed)
tunnel.<org_id>.<edge_agent_id>.control                  cancel   (subscribed)
tunnel.<org_id>.advertisements                           registration (later phase)
```

`org_id`, `edge_agent_id` and each connection `name` become subject tokens, so
they are validated at load: no dots, spaces, `*` or `>`. A dot would silently
create an extra token and route nowhere.

`edge_agent_id` must be assigned **before first start** and match the user
granted in the Bow instance's `nats.conf` — the broker grants permissions
against it, so an id invented at boot could never connect.
