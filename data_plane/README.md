# Data Plane

Components that run **outside** the Bow instance, in the customer's own network.

The control plane — everything else under `backend/` — never holds a credential
for a tunneled data source. The components here do.

## `data_edge_agent`

The process an admin installs on their site. It holds credentials for its local
data sources, connects **outbound** to NATS (so nothing has to route inward),
and answers proxied client operations.

### Configure 
Before running data-edge-agent prepare `config.yaml`
1. cp data_edge_agent/config.example.yaml config.yaml
2. discover 


### Run

```bash
cd data_plane
uv sync --extra dev
cp data_edge_agent/config.example.yaml config.yaml   # then edit
uv run python -m data_edge_agent --config config.yaml
```



### Test

```bash
cd data_plane && uv run python -m pytest -q
```
