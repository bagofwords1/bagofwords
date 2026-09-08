# Plan: Kubernetes connector — `KubernetesClient`

## Mission
Add a new data source type `kubernetes` to bagofwords that lets the agent
investigate a Kubernetes cluster — workloads, nodes, networking, storage,
custom resources, events, live resource usage, and pod logs — through the
Kubernetes API server. Model it on the existing **Zabbix** and **Aria
Operations** connectors: an HTTP API with no query language, where
`execute_query` takes a **JSON query spec** and `get_schemas` exposes a curated
catalog of virtual tables, plus **discovered** tables for whatever custom
resource definitions (CRDs) the cluster actually runs.

This follows `.agents/skills/add-connection-type/SKILL.md`. The connector is
registry-driven; the frontend form derives from the config/credentials schemas.

## Why Zabbix + Aria are the template (not Prometheus / CloudWatch)
- **Not SQL, not a metrics store.** The API server is typed REST
  (`/api/v1/pods`, `/apis/apps/v1/deployments`, …). There is no query language
  to pass through, so `execute_query` takes a JSON spec exactly like
  `zabbix_client.py`, not a query string.
- **Fixed + discovered catalog.** Built-in kinds are stable and get curated
  columns declared in code (Zabbix `_CATALOG`). CRDs are per-cluster and get
  discovered tables named by prefix (Aria's `metrics::<Adapter>/<Kind>`), so
  Argo, cert-manager, Prometheus Operator, Crossplane … become queryable with
  zero vendor-specific code.
- **One connection, several signals.** Inventory, events, usage and logs live
  on one connection for the CloudWatch reason: an investigation crosses them
  (the pod that is `CrashLoopBackOff` in inventory is the one whose last log
  lines and OOM events explain why). Splitting them would put them on
  separate connections with no way for the planner to correlate.
- **Read-only by construction.** The client only issues `GET`s. No exec, no
  port-forward, no writes, no Secrets — see [Security](#security-and-rbac).

## Data model → the repo's `Table`/`TableColumn`

Every fixed table has curated columns (≤ ~14), RFC3339 timestamps parsed to
UTC datetimes, and Kubernetes *quantities* normalized to numbers (`cpu` →
millicores, `memory`/storage → bytes) with the unit in the column name. `pk`
is `name` for cluster-scoped kinds and (`namespace`, `name`) for namespaced
ones. `fks` wire the object graph so the planner walks pod → node, pod →
owner, claim → volume → class → driver, ingress → class, event → involved
object without guessing.

### Metadata columns (uniform on every object-backed table)

Labels are the join key of Kubernetes — a Service finds its pods, a
Deployment owns its pods and a NetworkPolicy targets its pods by **label
selector**, not by foreign key — and they are where ownership and
attribution live (`team`, `env`, `app.kubernetes.io/*`). Annotations carry
operator configuration (ingress-controller and cloud-LB settings,
cert-manager issuers, rollout causes). So every table that maps to a
Kubernetes object carries, after its curated columns and **not repeated in
the per-table rows below**:

| Column | Type | Rule |
|---|---|---|
| `labels` | JSON string (`{"app":"checkout",…}`) | Always present, `{}` when empty. |
| `annotations` | JSON string | Always present, with noise stripped: `kubectl.kubernetes.io/last-applied-configuration` is dropped unconditionally (it can be tens of KB and embeds the full spec, container env literals included — it would bypass the env redaction), and the remaining map is capped at 4 KB per object (truncated values marked `…`). |

Excluded from the rule: `containers`, `logs`, `pod_metrics`, `node_metrics`
(derived rows — join to `pods`/`nodes` for metadata).

**Promoted well-known labels** — real columns where they change how the
agent reasons, so it does not have to parse JSON to get at them:

| Table | Column | Source label |
|---|---|---|
| `nodes` | `zone` | `topology.kubernetes.io/zone` (fallback `failure-domain.beta.kubernetes.io/zone`) |
| `nodes` | `region` | `topology.kubernetes.io/region` |
| `nodes` | `instance_type` | `node.kubernetes.io/instance-type` |
| `pods` | `app` | `app.kubernetes.io/name`, fallback `app` |

Everything else stays in the JSON column. **Filtering is server-side**:
`system_prompt()` tells the agent to select with `label_selector` in the
query spec (cheaper, always exact) and to use the `labels` column for
reading, grouping and display — never to reimplement a selector in pandas.
`raw: true` still returns the untouched metadata.

### Core / workloads

| Table | API | Key columns | FKs |
|---|---|---|---|
| `namespaces` | `core/v1` | name, phase, created | — |
| `nodes` | `core/v1` | name, ready, unschedulable, roles, zone, region, instance_type, kubelet_version, os_image, container_runtime, internal_ip, cpu_capacity_millicores, memory_capacity_bytes, cpu_allocatable_millicores, memory_allocatable_bytes, conditions (JSON), taints (JSON), created | — |
| `pods` | `core/v1` | name, namespace, app, node, phase, ready (`2/3`), restarts, owner_kind, owner_name, qos_class, pod_ip, service_account, priority_class, started_at, created | node → `nodes`, (owner_kind, owner_name) → workload tables |
| `containers` | flattened from pods | pod, namespace, name, image, ready, state (`running`/`waiting`/`terminated`), reason (`CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff` …), exit_code, restarts, last_state_reason, cpu_request_millicores, cpu_limit_millicores, memory_request_bytes, memory_limit_bytes | pod → `pods` |
| `deployments` | `apps/v1` | name, namespace, replicas, ready, updated, available, unavailable, strategy, selector (JSON), images, conditions (JSON), created | — |
| `replicasets` | `apps/v1` | name, namespace, owner_name, replicas, ready, available, revision, created | owner_name → `deployments` |
| `statefulsets` | `apps/v1` | name, namespace, replicas, ready, current, updated, service_name, update_strategy, created | service_name → `services` |
| `daemonsets` | `apps/v1` | name, namespace, desired, current, ready, available, misscheduled, created | — |
| `jobs` | `batch/v1` | name, namespace, owner_name, active, succeeded, failed, completions, parallelism, start_time, completion_time, conditions (JSON) | owner_name → `cronjobs` |
| `cronjobs` | `batch/v1` | name, namespace, schedule, suspend, concurrency_policy, active_count, last_schedule_time, last_successful_time | — |
| `horizontal_pod_autoscalers` | `autoscaling/v2` | name, namespace, target_kind, target_name, min_replicas, max_replicas, current_replicas, desired_replicas, current_metrics (JSON), conditions (JSON) | (target_kind, target_name) → workload tables |
| `configmaps` | `core/v1` | name, namespace, keys (names only, **never values**), created | — |
| `resource_quotas` | `core/v1` | name, namespace, hard (JSON), used (JSON), scopes | — |
| `events` | `core/v1` | namespace, type (`Normal`/`Warning`), reason, message, involved_kind, involved_name, involved_namespace, source_component, reporting_controller, count, first_seen, last_seen | (involved_kind, involved_name) → the involved table |

### Networking

| Table | API | Key columns | FKs |
|---|---|---|---|
| `services` | `core/v1` | name, namespace, type, cluster_ip, external_ips, ports (JSON), selector (JSON), load_balancer_ingress, created | — |
| `endpoint_slices` | `discovery.k8s.io/v1` | name, namespace, service, address_type, addresses, ready_count, not_ready_count, ports (JSON) | service → `services` |
| `ingresses` | `networking.k8s.io/v1` | name, namespace, ingress_class, hosts, rules (JSON: host/path → service:port), tls_hosts, load_balancer_ingress, created | ingress_class → `ingress_classes` |
| `ingress_classes` | `networking.k8s.io/v1` | name, controller, is_default, parameters (JSON) | — |
| `network_policies` | `networking.k8s.io/v1` | name, namespace, pod_selector (JSON), policy_types, ingress_rules (JSON), egress_rules (JSON) | — |

`endpoint_slices` is how the agent verifies a Service *actually has ready
backends* instead of trusting its selector; `network_policies` is the only
way to answer "A cannot reach B"; `ingress_classes` answers "the ingress does
nothing" (no default class, or a class whose controller is not running).

### Storage

| Table | API | Key columns | FKs |
|---|---|---|---|
| `persistent_volume_claims` | `core/v1` | name, namespace, phase, volume_name, storage_class, access_modes, requested_bytes, capacity_bytes, volume_mode, created | volume_name → `persistent_volumes`, storage_class → `storage_classes` |
| `persistent_volumes` | `core/v1` | name, phase, capacity_bytes, access_modes, reclaim_policy, storage_class, volume_mode, claim_namespace, claim_name, source_type (`csi`/`nfs`/`hostPath`/…), csi_driver, csi_volume_handle, created | storage_class → `storage_classes`, csi_driver → `csi_drivers`, (claim_namespace, claim_name) → `persistent_volume_claims` |
| `storage_classes` | `storage.k8s.io/v1` | name, provisioner, reclaim_policy, volume_binding_mode, allow_volume_expansion, is_default, parameters (JSON) | provisioner → `csi_drivers` |
| `csi_drivers` | `storage.k8s.io/v1` | name, attach_required, pod_info_on_mount, volume_lifecycle_modes, storage_capacity, fs_group_policy, requires_republish | — |

The chain claim → volume → class → driver is the whole storage RCA path
(`Pending` pod on a `WaitForFirstConsumer` class, a `Released` volume with
`Retain`, a provisioner whose driver is not installed). `CSINode` and
`CSIStorageCapacity` are per-node and noisy — deliberately out of v1; note in
`system_prompt()` that the raw-path escape hatch reaches them.

### Custom resources (discovered)

| Table | Source | Columns |
|---|---|---|
| `custom_resource_definitions` | `apiextensions.k8s.io/v1` | name, group, kind, plural, scope, served_versions, storage_version, established, created |
| `crd::<group>/<Kind>` (one per **populated** CRD) | the CRD's own list endpoint | name, namespace, generation, created, conditions (JSON, from `status.conditions` when present), spec (JSON), status (JSON) |

Discovery: list CRDs (one call), then probe each with `limit=1` and only emit
a table for kinds with ≥ 1 object (Aria's "populated" rule), capped by
`max_crd_tables` (default 40) with the remainder named in the
`custom_resource_definitions` description so the agent knows they exist and can
reach them through the raw-path escape hatch. Each discovered table's
description carries the top-level `spec` keys seen on the probe object so the
planner knows what to filter on in pandas.

### Runtime signals

| Table | Source | Columns | Notes |
|---|---|---|---|
| `pod_metrics` | `metrics.k8s.io/v1beta1` | namespace, pod, container, cpu_millicores, memory_bytes, timestamp, window_seconds | Current snapshot only (metrics-server keeps no history). Table is emitted only when the API group is served; otherwise the catalog description says the cluster has no metrics-server. |
| `node_metrics` | `metrics.k8s.io/v1beta1` | node, cpu_millicores, memory_bytes, timestamp, window_seconds | Join to `nodes` for utilization %. |
| `logs` | `GET /api/v1/namespaces/{ns}/pods/{pod}/log` | namespace, pod, container, timestamp, line | **Requires `pod`** (or `label_selector`, fanned out to at most `max_log_pods` pods). Params: `container`, `since` (`-1h`, `-15m`, or seconds), `tail_lines` (default 500, max `log_tail_max`), `previous` (the crashed container's last run), `grep` (client-side regex). Always requested with `timestamps=true` so lines parse to a datetime column. |

This connector answers "what is it doing *right now*" and "what did it just
log"; metric history is out of scope for v1.

## Query spec (what `execute_query` accepts)

A JSON string (or dict), mirroring the Zabbix spec:

```json
{"table": "pods",
 "namespace": "payments",
 "label_selector": "app=checkout,tier!=canary",
 "field_selector": "spec.nodeName=ip-10-0-1-5",
 "limit": 500}
```

- `table` (required): a fixed table, a `crd::<group>/<Kind>` table, or
  `logs`.
- `namespace` (optional): a name, a list, or omitted for **all namespaces**
  (server-side `…/pods` across namespaces, then filtered by the connection's
  `namespaces` allowlist when one is set). Ignored for cluster-scoped kinds.
- `name` (optional): fetch a single object (`GET …/{name}`).
- `label_selector` / `field_selector` (optional): passed straight to the API,
  so filtering happens server-side.
- `limit` (optional, default 500, hard cap `MAX_ROWS` 50 000): implemented with
  the API's `limit` + `continue` pagination so large clusters never trip the
  server's response-size limits.
- `since` (optional): for `events` (client-side on `last_seen`, the API has no
  time filter) and `logs` (server-side `sinceSeconds`).
- `raw: true` (optional): adds a `raw` column with the full object JSON for
  fields the curated columns leave out. Env-var literals are redacted (see
  Security).
- **Escape hatch** — `{"path": "/apis/apps/v1/namespaces/x/deployments",
  "params": {"labelSelector": "app=x"}}` issues a raw `GET` and returns
  `items` flattened one level. Any path that is not a `GET` list/get, or that
  targets `secrets`, is rejected.

Results are DataFrames — one row per object (per container / per log line /
per metric sample for those tables). Aggregation happens in pandas, as with
every API connector.

`system_prompt()` documents the spec plus the Kubernetes specifics the model
gets wrong: filter by label with `label_selector` (server-side), never by
parsing the `labels` column; quantities are normalized (`250m` → 250 millicores, `512Mi` →
bytes), `restarts` on `pods` is the sum across containers, `events` are
retained only ~1 h by default (`--event-ttl`) so "why did it crash yesterday"
must come from `logs … previous=true`, `logs`
requires a pod, the owner chain is pod → replicaset → deployment (two hops),
and the storage chain claim → volume → class → driver.

`relative_date_hint`: "`since` takes relative offsets (`-1h`, `-24h`) or
seconds — never hard-code a timestamp; events/logs are read at execution time."

## Authentication

One mechanism, chosen for operational simplicity: the connector runs inside
the bagofwords backend the way the PostgreSQL client does, and the credential
is a static one that does not expire — the **bearer token of a long-lived
service-account token Secret**, sent as `Authorization: Bearer <token>` on
every request. `scopes=["system"]`.

What the admin pastes, though, is not the token by itself. A Postgres admin
already knows host, port, user and password; a Kubernetes admin has to
*produce* the API server URL, the CA certificate and the token, and three
separate copy-pastes (one of them a PEM block in a textarea) is where setups
go wrong. So the form takes **one artifact — an access file printed by a
script we ship** — and the backend takes URL, CA and token out of it.

### Connection setup UX (the UI part of this design)

The connect form for `kubernetes` shows a numbered **setup guide** above the
fields, each step with copy-to-clipboard code, then a single paste field:

1. **Grant read access on the cluster** — the full `rbac.yaml` manifest
   (namespace, ServiceAccount, cluster-wide read-only ClusterRole, binding,
   token Secret — see [Security](#security-and-rbac)) shown inline as
   `kubectl apply -f - <<'EOF' … EOF`, so the admin reads exactly what a
   cluster-wide read role grants before applying it, with nothing fetched
   from a remote URL.
2. **Print the access file** — run `tools/kubernetes/print_access_file.sh`
   (shown inline; ~15 lines, no `curl | bash`). It waits until the token
   controller has populated the Secret, then prints one self-contained file
   built from the Secret's `token` + `ca.crt` and the server URL of the
   admin's current context:
   ```yaml
   apiVersion: v1
   kind: Config
   clusters:
   - name: bagofwords
     cluster: {server: https://…:6443, certificate-authority-data: <base64 CA>}
   users:
   - name: bagofwords-reader
     user: {token: <service-account JWT>}
   contexts:
   - name: bagofwords
     context: {cluster: bagofwords, user: bagofwords-reader}
   current-context: bagofwords
   ```
   The admin can verify it before pasting:
   `kubectl --kubeconfig access.yaml get nodes`.
3. **Paste it** into the form's **Cluster access file** field and click
   **Test connection**.

Why the file is kubeconfig-*shaped*: it is the one format every Kubernetes
admin recognises and can test with `kubectl`, and its error messages are
familiar. It is still exactly one mechanism — the backend parser accepts
**only** what the script produces (one cluster with `server` +
`certificate-authority-data`, one user with `token`) and rejects, with a
message naming the offending stanza, anything else: `exec` credential
plugins, `client-certificate`, `auth-provider`, multiple contexts, or a token
that is not a service-account JWT. Pasting a laptop kubeconfig therefore
fails immediately and explains why, instead of hanging on a missing `aws`
binary.

**Form fields** (the schemas ARE the form):

| Field | Where | UI | Purpose |
|---|---|---|---|
| `access_file` | credentials | textarea, masked after save | The file printed in step 2. Description: "Paste the output of `print_access_file.sh` (see the steps above)." |
| `namespaces` | config | text, optional | Comma-separated allowlist (`payments, inventory`); empty = every namespace. The only visible config field. |

Everything else (`discover_crds`, caps, timeouts) keeps a safe default and is
`ui:hidden` in v1 — the goal is a form with one required input.

**Generic extension needed**: `DataSourceRegistryEntry` gains an optional
`setup_guide: list[SetupStep]` (`title`, `body`, optional `code` +
`language`), served through `GET /available_data_sources` and rendered by
`ConnectForm.vue` as a collapsible numbered panel above the fields with a
copy button per code block. Titles and bodies are i18n keys
(`connectors.kubernetes.setup.*` in every `locales/*.json`, per the
localization skill); code blocks are not localized. Zabbix (mint an API
token), Splunk and Elastic (API key) can adopt the same block later — it is
not Kubernetes-specific.

**Edit mode**: the access file is a credential, so it is never returned by
the API; the edit form shows the field empty with "leave blank to keep the
current file", the way password fields behave today.

`test_connection()` does `GET /version` (confirms endpoint + TLS, reports the
server version), then `GET /api/v1/nodes` and `GET /api/v1/namespaces`
(confirms the token really is cluster-wide — a 403 on `nodes` means someone
bound a namespaced or `view` role instead of `rbac.yaml`, and is reported as
such — and reports node / namespace counts). It also probes
`/apis/metrics.k8s.io` and reports whether the usage tables will be
available. A URL that the admin's laptop can reach but the backend cannot
(private endpoint) surfaces here as a connection error naming the host.

## Config (`KubernetesConfig`) and credentials (`KubernetesAccessFileCredentials`)

| Field | Schema | UI | Default | Purpose |
|---|---|---|---|---|
| `access_file` | credentials | textarea (required) | — | The access file from the setup guide. Parsed into `api_server_url`, `ca_certificate`, `token` at client construction; the parsed parts are never stored separately. |
| `namespaces` | config | text | empty | Comma-separated allowlist. Empty = every namespace. Applied to every namespaced table and to `logs`. (A plain text field: the generic form renders list-typed fields as a JSON editor, which is the wrong UX for two namespace names.) |
| `verify_ssl` | config | hidden | `True` | The CA from the access file is always used; this exists only for a dev cluster whose CA is missing from the file. |
| `discover_crds` | config | hidden | `True` | Emit `crd::` tables. |
| `max_crd_tables` | config | hidden | 40 | Cap on discovered CRD tables (rest are listed in `custom_resource_definitions`). |
| `log_tail_default` | config | hidden | 500 | `tail_lines` when a `logs` query omits it. |
| `log_tail_max` | config | hidden | 5000 | Hard cap per pod per `logs` query. |
| `max_log_pods` | config | hidden | 10 | Fan-out cap when `logs` is queried by `label_selector`. |
| `request_timeout` | config | hidden | 60 | Seconds per API call. |

Hidden fields use `json_schema_extra={"ui:hidden": True}` (existing
convention) so they are settable through the API and can be surfaced in the
form later without a migration. Field titles and descriptions are the connect
form — write them as product copy.

## Security and RBAC

**The token is cluster-wide read-only** — cluster-admin minus the write
verbs. One wildcard rule, so every built-in kind, every CRD group and every
aggregated API (metrics-server) is covered without manifest churn when the
cluster gains an operator. `tools/kubernetes/rbac.yaml`, applied in step 1
of the setup guide:

```yaml
apiVersion: v1
kind: Namespace
metadata: { name: bagofwords }
---
apiVersion: v1
kind: ServiceAccount
metadata: { name: bagofwords-reader, namespace: bagofwords }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: bagofwords-cluster-reader }
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: ["get", "list", "watch"]
  - nonResourceURLs: ["*"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: bagofwords-cluster-reader }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: bagofwords-cluster-reader }
subjects:
  - { kind: ServiceAccount, name: bagofwords-reader, namespace: bagofwords }
---
apiVersion: v1
kind: Secret
metadata:
  name: bagofwords-reader-token
  namespace: bagofwords
  annotations: { kubernetes.io/service-account.name: bagofwords-reader }
type: kubernetes.io/service-account-token
```

RBAC has no "everything except" syntax, so a wildcard read role **can read
Secrets and exec into nothing** (`pods/exec`, `pods/portforward` and
`pods/attach` need `create`, which is not granted). The consequences:

- **The token must be handled like a database superuser password**: it is
  Fernet-encrypted at rest like every credential, never logged, never
  returned by the API, and rotated by deleting the Secret.
- **The connector is the guard against Secrets, not RBAC.** No `secrets`
  table; the escape hatch rejects any path whose resource segment is
  `secrets`; `configmaps` exposes key names only; `raw: true` redacts
  `spec.containers[].env[].value` (keeps `name` and `valueFrom` refs); the
  `kubectl.kubernetes.io/last-applied-configuration` annotation is dropped
  everywhere, `raw` included, because it embeds the full spec with env
  literals. These are enforced in code and pinned by unit tests, because
  nothing else stops a wildcard token from reading them.
- **Read-only.** The client issues `GET` only — no writes, no subresource
  `create`s. Capability set is `{Capability.QUERY}`.

## Dependency: the official `kubernetes` client, used thinly

`cd backend && uv add kubernetes` (pure Python; its transitive deps —
`urllib3`, `pyyaml`, `google-auth`, `websocket-client` — are already present
or trivial). Use it for the two things that are painful to hand-roll:

1. **Configuration** — a `kubernetes.client.Configuration` built from
   URL + bearer token + CA (`host`, `api_key`, `ssl_ca_cert` written to a
   temp file, `verify_ssl`), so TLS and CA bundles are handled the way
   `kubectl` handles them.
2. **Raw JSON transport** — `ApiClient.call_api(path, "GET", …,
   response_type="object", _preload_content=…)` returning plain dicts.

**Do not use the typed API classes** (`CoreV1Api.list_pod_for_all_namespaces`
→ model objects). One raw `_get(path, params)` method serves built-ins, CRDs,
metrics and logs alike, every table is a pure function `dict → row`, and the
unit test fakes a single boundary (path → JSON fixture) instead of dozens of
model constructors. 

## Files to create / modify (in order)

1. **Create** `backend/app/data_sources/clients/kubernetes_client.py`
   - `class KubernetesClient(DataSourceClient)`, `capabilities = {Capability.QUERY}`.
   - `__init__(access_file, namespaces=None, verify_ssl=True,
     discover_crds=True, max_crd_tables=40, log_tail_default=500,
     log_tail_max=5000, max_log_pods=10, request_timeout=60, **kwargs)` —
     config + credentials splatted together by `Connection.get_client()`.
   - `parse_access_file(text) -> (api_server_url, ca_pem, token)` — a pure
     function, strict as described in [Authentication](#authentication);
     raises `ValueError` naming the offending stanza.
   - `_configuration()` → a `kubernetes.client.Configuration`;
     `connect()` context manager yielding an `ApiClient`; `_get(path,
     params)` with pagination (`limit`/`continue`), readable errors on
     401/403 (name the missing verb/resource from the API's status message),
     404 (API group not served — how missing metrics-server / a CRD shows
     up), and TLS failures (point at the CA inside the access file — re-run
     `print_access_file.sh` after a cluster CA rotation).
   - `_CATALOG` dict (fixed tables → path, scope, columns, pk, fks, desc,
     row-builder) in the Zabbix shape; `_flatten_pod`, `_flatten_containers`,
     `_parse_quantity`, `_parse_rfc3339` helpers; `_discover_crds()`.
   - `test_connection()`, `get_schemas(progress_callback=…)` (CRD probing is
     the slow part — report progress per CRD), `get_schema()`,
     `execute_query()`, `prompt_schema()` (via `ServiceFormatter`),
     `system_prompt()`, `description`, `relative_date_hint`.
   - Keep every call sync — the base provides async wrappers.
2. **Edit** `backend/app/schemas/data_sources/configs.py` — `KubernetesConfig`
   (table above, knobs `ui:hidden`) and `KubernetesAccessFileCredentials`
   (`access_file`, textarea, description pointing at the setup guide).
3. **Edit** `backend/app/schemas/data_source_registry.py` — a `"kubernetes"`
   entry: `category="infra"`, `config_schema=KubernetesConfig`,
   `credentials_auth=AuthOptions(default="access_file", by_auth={"access_file":
   AuthVariant(title="Cluster access file", …, scopes=["system"])})`,
   `setup_guide=[…]` (the three steps above), **explicit**
   `client_path="app.data_sources.clients.kubernetes_client.KubernetesClient"`,
   `version="beta"`, `requires_license="enterprise"`, `dev_only=True` while
   incubating. Add `"kubernetes"` to `ENTERPRISE_DATASOURCES` in
   `backend/app/ee/license.py` (matching zabbix/splunk/aria_operations).
4. **Driver** — `uv add kubernetes` (pure Python; no `Dockerfile` change).
5. **Icon** — `frontend/public/data_sources_icons/kubernetes.png`.
   `DataSourceIcon.vue` resolves `/data_sources_icons/<type>.png` from the
   type token, so no frontend code change is expected.
6. **Setup guide plumbing (generic)** — `SetupStep` model + `setup_guide`
   field on `DataSourceRegistryEntry`; include it in
   `list_available_data_sources()`; render it in
   `frontend/components/datasources/ConnectForm.vue` (collapsible numbered
   panel, copy buttons); i18n keys in all `locales/*.json`.
7. **Scripts** — `tools/kubernetes/rbac.yaml` and
   `tools/kubernetes/print_access_file.sh` (the two artifacts the guide
   embeds verbatim; both are pinned to the registry constants by a unit
   test so the guide can never drift from the files).
8. **Tests** — see Verification.
9. **Sandbox** — `tools/kubernetes/` (see Verification §5).

## Verification (sandbox-feedback-loop)

### 1. Registry + import resolves
```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('kubernetes'))"
```

### 2. Unit test (fake the `_get` boundary — always green, no cluster)
`backend/tests/unit/test_kubernetes_client.py`, in the style of
`test_zabbix_client.py`: a fake session maps API paths to JSON fixtures
(`tests/fixtures/kubernetes/*.json`, captured from a kind cluster). Assert:
- `get_schemas()` catalog shape — every fixed table, pks/fks, storage and
  networking chains present; `crd::` tables only for populated CRDs and
  capped by `max_crd_tables`; `pod_metrics`/`node_metrics` present iff the
  metrics group is served.
- Metadata: `labels`/`annotations` present on every object-backed table
  and absent on derived ones; `last-applied-configuration` stripped (also
  under `raw`); the 4 KB annotation cap; promoted `zone`/`region`/
  `instance_type`/`app` columns with their fallbacks.
- Row builders: pod `ready`/`restarts`/owner resolution, container
  state/reason/exit_code, quantity parsing (`250m`, `1`, `512Mi`, `2Gi`,
  `1e3`), RFC3339 parsing, PV `source_type`/`csi_driver`, ingress rule
  flattening, endpoint-slice ready counts.
- `execute_query()` dispatch: namespace fan-out vs all-namespaces path,
  label/field selectors passed through, pagination via `continue`, `limit`
  cap, `name` → single-object path, `raw` column + env redaction, `logs`
  requires a pod / respects `tail_lines` and `max_log_pods`, `since`
  filtering on events.
- Security: escape hatch rejects `secrets` paths and non-GET; no `secrets`
  table; `configmaps` carries keys only; `raw` env redaction.
- `parse_access_file`: accepts the script's output (with and without the
  optional `contexts`/`current-context`); rejects `exec`, `client-certificate`,
  `auth-provider`, `insecure-skip-tls-verify`, multiple clusters/users, a
  missing `certificate-authority-data`, a non-JWT token, and non-YAML input —
  each with a message naming the stanza. The Bearer header is attached to
  every request and the CA is materialized for TLS.
- Registry: the `kubernetes` entry exposes a `setup_guide` with three steps
  and it serializes through `list_available_data_sources()`.
- `test_connection()` success / 401 / 403 / TLS-error surfacing.

### 3. Generic data-source e2e still green
```bash
cd backend
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

### 4. Integration test against a real cluster (testcontainers k3s)
`testcontainers.k3s.K3SContainer` is already installed in the backend venv
and yields an admin kubeconfig (client certificate). The test uses that
admin identity **only to seed**: `seed_fn` applies `tools/kubernetes/rbac.yaml`
+ `manifests/`, creates the long-lived token Secret, waits for the workloads
to settle, and assembles the access file from the Secret's `token` +
`ca.crt` and the container's API URL (what `print_access_file.sh` does) — the
connector under test then authenticates exactly the way a customer's
connection does. Add `"kubernetes"` to `DATA_SOURCES` in
`backend/tests/integrations/ds_clients.py` with a `CONTAINER_REGISTRY` entry
whose `get_kwargs` returns `access_file`.
Asserts run `pods`, `events`, the storage chain, a `crd::` table and `logs`
against the live API.

### 5. The sandbox — `tools/kubernetes/`
- `kind-config.yaml` — one control-plane + one worker so `nodes`, taints and
  scheduling failures are real.
- `manifests/` — namespaces `payments` and `inventory`; a healthy
  Deployment + Service + EndpointSlices; a `CrashLoopBackOff` Deployment
  (exits 1) and an `OOMKilled` one (tiny memory limit); a Pod stuck `Pending`
  on a PVC whose StorageClass has no provisioner (exercises claim → class →
  driver); a Pod with an unsatisfiable `nodeSelector`; a CronJob with one
  failed Job; an Ingress referencing a missing IngressClass; a NetworkPolicy
  that isolates `payments`; an HPA; a ResourceQuota; a sample CRD
  (`widgets.example.com`) with instances so `crd::` discovery has data.
- `rbac.yaml` — the ServiceAccount / cluster-wide read-only ClusterRole /
  binding / token Secret from [Security](#security-and-rbac).
- `print_access_file.sh` — the script the setup guide shows (waits for the
  Secret's `token` to be populated, assembles the access file from the
  Secret + the current context's server URL, prints it).
- `seed_kubernetes.sh` — creates the kind cluster, installs metrics-server
  (with `--kubelet-insecure-tls` for kind), applies `rbac.yaml` +
  `manifests/`, waits for the failure states to appear, and prints the path
  of the access file it wrote with `print_access_file.sh` on the last line
  (the Zabbix/Splunk seed convention).

### 6. Live UI pass
`tools/agent/boot_stack.sh` + `seed_org.py`, then: open the Kubernetes tile
→ the setup guide renders with three steps and working copy buttons → paste
the access file from the sandbox → **Test connection** reports the server
version, node/namespace counts and metrics availability → paste a laptop
kubeconfig with an `exec` stanza instead and confirm the rejection message →
the tables selector lists the
fixed tables grouped by domain plus the `crd::widgets` table → prompts:
"which pods are crash-looping and why" (pods + containers + events + logs),
"why is the payments claim pending" (claim → class → driver), "does the
checkout service have ready backends" (services + endpoint_slices). Capture
the connect form (guide + paste field, before/after since `ConnectForm.vue`
changes), the rejection message, and the tables list (**ui-evidence**
skill) — the form is schema-generated, so this doubles as the review of the
schemas and the guide copy.

### 7. Record the loop
`docs/feedback-loops/kubernetes-connector.md` (**sandbox-feedback-loop**
skill), mirroring `docs/feedback-loops/zabbix-connector.md`.

## Pitfalls
- **Send the full `Authorization: Bearer <token>` value, not
  `api_key` + `api_key_prefix`** — observed on `kubernetes` 36.x: the prefix
  was dropped, the raw JWT went out alone, and the API server treated every
  call as `system:anonymous` (a confusing 403 on nodes, while `/version`
  still worked). Setting `api_key["authorization"] = "Bearer …"` verbatim is
  version-proof and pinned by a unit test.
- **Pod logs need `Accept: text/plain, */*`, not a bare `text/plain`** —
  observed on microk8s v1.35.6: the `/log` subresource answered a bare
  `text/plain` with **406** "only application/json, application/yaml,
  application/vnd.kubernetes.protobuf are accepted", while `*/*` (what
  kubectl sends) returned the text; kind v1.36 accepted either. The wildcard
  fallback is pinned by a unit test.
- **The Secrets guard must reject encoding, not just compare segments** —
  review finding, confirmed on a real apiserver: `/namespaces/default/%73ecrets`
  and `default%2Fsecrets` passed a literal segment check and returned the
  Secret (urllib3 sends escapes verbatim; the apiserver routes on the decoded
  path). Every path segment and every `name`/`namespace`/`pod` value must be
  a plain Kubernetes name (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `%`, `/`, `?`,
  `#`, `..`) — rejecting is simpler and safer than decoding.
- **Refuse streaming query parameters on the escape hatch** — `watch=true`
  on a list or `follow=true` on a log path turns the request into an
  open-ended chunked stream; urllib3's read timeout only measures gaps
  between chunks, so the worker thread is pinned and the body buffered for
  the apiserver's watch timeout (30–60 min). `watch`, `follow`,
  `timeoutSeconds`, `allowWatchBookmarks`, `sendInitialEvents` are refused,
  as is any `?` in `path`.
- **Users will paste their laptop kubeconfig** — `parse_access_file`
  rejects `exec`/client-cert/auth-provider stanzas by name and the message
  points back at step 2 of the guide. A hand-built file carrying a
  `kubectl create token` JWT parses fine, works for an hour, then 401s; the
  401 message says "the token has expired or been revoked — re-run the
  setup guide" so that failure is self-explaining too.
- **The server URL in the admin's context may be private** (VPC-only
  endpoint) — reachable from the laptop that ran the script, not from the
  backend. `test_connection()` names the host in the error; the guide's
  step 2 says to edit `server:` to the endpoint the bagofwords backend can
  reach if they differ.
- **Token Secret population is asynchronous** — the token controller fills
  `data.token` a moment after the Secret is created; the print script polls
  for it instead of printing an empty token.
- **Private API endpoints** — most managed clusters are not reachable from
  outside their VPC. v1 assumes the backend can reach the API server. Keep
  `_get()` the only transport seam so a future relay is a local change.
- **Pagination** — an unbounded `GET /api/v1/pods` on a large cluster is
  slow and can exceed the API server's response limits; always paginate with
  `limit`/`continue`, and prefer server-side selectors over pandas filters.
- **Events are ephemeral** (1 h TTL by default) and unsorted; sort by
  `last_seen` client-side and say so in the prompt.
- **metrics-server is optional** and its API returns 404 when absent; hide
  the usage tables rather than failing discovery, and say so in
  `test_connection()`.
- **CRD discovery cost** — one `limit=1` probe per CRD; a cluster with 200
  CRDs is 200 calls. Cap, report progress, and skip CRDs whose group is a
  known-internal one (e.g. `metrics.k8s.io`, `*.internal.*`).
- **Quantities** — `cpu: 0.5`, `500m`, `memory: 1Gi`, `1000000000`, `1e9`
  are all legal; parse them once, centrally, and test every form.
- **Ready/restarts semantics** — `ready` is per container, `restarts` must be
  summed, `phase=Running` does not mean healthy. The `containers` table
  exists so the agent does not have to reason about this from `pods`.
- **Always set `client_path`**; `is_connection` stays unset (True); registry
  and config descriptions are product copy.

## Scope summary
1 new client + unit test + fixtures, config/registry/license/icon edits, one
`uv add`, `rbac.yaml` + `print_access_file.sh`, a kind-based sandbox under
`tools/kubernetes/`, a k3s testcontainer entry, and one **generic** frontend
change: the `setup_guide` panel in `ConnectForm.vue` (+ i18n keys). No SQL,
no native driver, no Dockerfile change. Catalog: **27 fixed tables** (14 core, 5 networking,
4 storage, 1 CRD index, 3 runtime) plus discovered `crd::` tables.

## Decisions
- **Auth**: one mechanism, system scope only — the bearer token of a
  long-lived service-account token Secret. Nothing else in v1.
- **Setup UX**: one input. The admin applies `rbac.yaml`, runs
  `print_access_file.sh`, pastes the access file; the backend parses URL +
  CA + token out of it and rejects any other kubeconfig content. The form
  shows the steps via a new generic `setup_guide` registry field.
- **RBAC**: cluster-wide read-only (`get`/`list`/`watch` on `*`); the
  connector code, not RBAC, keeps Secrets out of the agent's reach.
- **Dependency**: official `kubernetes` package, for `Configuration` + raw
  `ApiClient.call_api` transport only; no typed API models.
- **Logs are in v1**; current usage via metrics-server is in v1 when the
  cluster serves it. Metric history is out of scope.
- **Reachability**: the backend talks to the API server directly.
- **Test rig**: kind sandbox + k3s testcontainer.
- **License**: enterprise, category infra, `version="beta"`.
