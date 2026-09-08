# Feedback Loop — Kubernetes connector (`kubernetes`)

Validates the design in `docs/design/kubernetes-connector.md`: a read-only
Kubernetes data source with 27 fixed tables, discovered `crd::` tables,
metrics-server usage tables and pod logs, authenticated by ONE mechanism (the
bearer token of a long-lived service-account token Secret, pasted as an
access file), with a numbered setup guide rendered above the connect form.

## What shipped

- `backend/app/data_sources/clients/kubernetes_client.py` — `KubernetesClient`
  (JSON query spec, `_CATALOG` of fixed tables, CRD discovery, metrics, logs,
  escape hatch, `parse_access_file`).
- `KubernetesConfig` / `KubernetesAccessFileCredentials` in
  `backend/app/schemas/data_sources/configs.py`; registry entry
  (`category="infra"`, `version="beta"`, `requires_license="enterprise"`,
  explicit `client_path`, `setup_guide=[…]`) and the new generic
  `SetupStep` model + `setup_guide` field in
  `backend/app/schemas/data_source_registry.py`; `kubernetes` in
  `ENTERPRISE_DATASOURCES`; `setup_guide` surfaced by
  `GET /data_sources/{type}/fields`.
- `frontend/components/datasources/ConnectForm.vue` — generic **Setup steps**
  panel (numbered, collapsible, copy button per code block, i18n via
  `data.setupGuides.<type>[i]`), plus one behavioural fix (below).
- `tools/kubernetes/` — `rbac.yaml`, `print_access_file.sh`,
  `kind-config.yaml`, `manifests/`, `seed_kubernetes.sh`;
  `tools/agent/kubernetes_ui_flow.mjs`.
- Tests: `backend/tests/unit/test_kubernetes_client.py` (112 tests) over
  fixtures captured from the sandbox
  (`backend/tests/unit/fixtures/kubernetes/`); k3s testcontainer entry in
  `backend/tests/integrations/ds_clients.py`.
- Dependency: `kubernetes>=36.0.3` (pure Python). Icon:
  `frontend/public/data_sources_icons/kubernetes.png`.

## Root causes found and fixed while building

1. **Bearer prefix dropped by the generated client** — `Configuration.api_key
   + api_key_prefix` sent the raw JWT with no `Bearer `, so the API server saw
   `system:anonymous` (`/version` worked, `nodes` 403). Fixed by setting
   `api_key["authorization"] = "Bearer <token>"` verbatim
   (`kubernetes_client.py:_configuration`, pinned by
   `TestConnection.test_bearer_header_and_ca_configured`).
2. **Rejection order** — a laptop kubeconfig usually fails both the CA and the
   user-stanza checks; the parser now checks the user stanza first so the
   admin sees "carries an `exec` stanza" rather than a CA message
   (`TestParseAccessFile.test_exec_plugin_reported_even_with_a_bad_ca`).
3. **Connect form swallowed the first click after a failed test** —
   `@change="clearTestResult()"` fired on the textarea's blur at mousedown, the
   error line vanished, the buttons shifted up and the mouseup missed. A real
   user needed two clicks after correcting a paste (any connector). Now
   `@input` — the result clears while typing, before the click
   (`ConnectForm.vue`, 4 wrappers).
4. **Log tail clamp** — a `log_tail_max` below the default raised the max
   instead of clamping the default; the cap now wins.
5. **Empty results lost their columns** — `_frame(rows, columns)` keeps the
   table's columns on zero rows.
6. **406 on pod logs against microk8s v1.35.6** (reported from the cluster
   deployment): the `/log` subresource rejected a bare `Accept: text/plain`
   ("only application/json, application/yaml,
   application/vnd.kubernetes.protobuf are accepted") while `*/*` returned the
   text; kind v1.36 accepted both. Reproduced with `kubectl proxy` + curl
   across four Accept values, fixed by sending `text/plain, */*`
   (`LOG_ACCEPT`), pinned by `TestLogs.test_single_pod_all_containers_and_params`,
   and re-verified through the client against both clusters.

## Review fixes (PR #1087)

7. **Percent-encoded Secrets bypass** (reviewer confirmed on a real apiserver:
   `/namespaces/default/%73ecrets` and `default%2Fsecrets` returned the Secret):
   every path segment and every `name`/`namespace`/`pod` value must now be a
   plain Kubernetes name; `?`, `#`, `%` and `..` are refused before any request.
8. **Unbounded streams through the escape hatch**: `watch`, `follow`,
   `timeoutSeconds`, `allowWatchBookmarks`, `sendInitialEvents` (any case) and
   non-object `params` are refused.
9. **CRD probe ignored the namespace allowlist** and advertised tables that
   always returned empty — the probe is scoped like every other query.
10. **Invalid `grep`** raised a raw `re.error`; now a `ValueError` naming `grep`,
    with no log request issued.
11. **Clipboard fallback** removes its temp textarea in a `finally`, so a
    throwing `execCommand` cannot leak one per click; `UToggle` listener back
    to `@change` (Nuxt UI's Toggle never emits `input`; the watcher already
    clears the result); the no-op `KubernetesClient = KubernetesClient` alias
    is gone.
   Verified by 19 new unit tests (132 total), the kind cluster (legitimate
   paths/names/logs/CRDs/escape hatch unchanged) and a Playwright run on an
   insecure-context origin (0 leaked textareas even when `execCommand` throws).

## Loop A — deterministic (no cluster)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('kubernetes'))"
uv run pytest tests/unit/test_kubernetes_client.py -q                         # 112 passed (68s)
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q   # 11 passed
```

Observed 2026-09-06: `112 passed`, `11 passed`. The fake `ApiClient` replays
the captured fixtures and asserts the connector only ever issues `GET`.

Registry-touching suites (every `tests/unit/test_*_client.py` plus
`tests/e2e/test_data_source.py`) re-run after adding the entry and the
`setup_guide` field: `840 passed, 1 skipped`.

## Loop B — real clusters

### B1. testcontainers k3s (CI-shaped)

```bash
cd backend
printf '{"kubernetes": {"enabled": true, "container": true}}\n' > tests/integrations/integrations.json   # gitignored
# colima / Docker Desktop on macOS: point the SDK at the real socket and the
# in-VM path (ryuk mounts it); plain Linux needs neither.
export DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}')" \
       TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
uv run pytest tests/integrations/ds_clients.py -k kubernetes -v
```

Observed: `1 passed in 20.94s` — k3s `v1.31.4-k3s1` booted, `rbac.yaml` +
manifests applied inside the container via its own `kubectl`, the access file
assembled from the token Secret exactly as `print_access_file.sh` does,
`test_connection()` succeeded and `get_schemas()` returned the catalog.

### B2. kind sandbox + the running product

```bash
tools/kubernetes/seed_kubernetes.sh          # kind bow-k8s (2 nodes) + metrics-server + seeded states
# last line: /tmp/bow-agent/kubernetes-access.yaml
kubectl --kubeconfig /tmp/bow-agent/kubernetes-access.yaml get nodes            # Ready ×2
kubectl --kubeconfig /tmp/bow-agent/kubernetes-access.yaml auth can-i create pods -A   # no
```

Client against the live cluster (`KubernetesClient(access_file=…)`):
`test_connection` → *Connected to Kubernetes v1.36.1: 2 nodes, 8 namespaces;
metrics-server available*; `get_schemas` → 28 tables incl.
`crd::example.com/Widget`, `pod_metrics`, `node_metrics`; `containers` shows
`fraud-scorer` `terminated/Error` exit 1 and `report-builder`
`terminated/OOMKilled` exit 137 with normalized limits; the Pending claim on
`slow-nfs` and the Bound PV of the StatefulSet; Warning events; usage rows;
the crashed container's previous-run log lines; `raw` redacts the planted
`PAYMENT_API_KEY` literal and drops `last-applied-configuration`.

Product UI (backend `main.py` with a sandbox license from
`scripts/gen_sandbox_license.py`, `yarn dev` frontend, `seed_org.py --demo`;
macOS note: `boot_stack.sh` needs `setsid`, so both were started by hand):

```bash
cd frontend && cp ../tools/agent/kubernetes_ui_flow.mjs .agent-tmp/
node .agent-tmp/kubernetes_ui_flow.mjs ../media/pr/feature-k8s-connector /tmp/bow-agent/kubernetes-access.yaml
```

Observed (screenshots in `media/pr/feature-k8s-connector/`):

| Step | Evidence |
|---|---|
| Catalog tile under *Infrastructure* | `01-catalog-kubernetes-tile.png` |
| Connect form: **Setup steps** panel, 3 steps, 2 copy buttons, one visible config field (Namespaces), one credential textarea | `02-connect-form-setup-guide.png` |
| Laptop kubeconfig (exec plugin) pasted → *"…user carries a `exec` stanza, which is not accepted…"* | `03-laptop-kubeconfig-rejected.png` |
| Real access file → *Connected successfully. Found 28 tables.*, Save enabled | `04-test-connection-ok.png` |
| Discovery modal *Discovered 28 tables* | `05-schema-discovery.png` |
| Select Tables lists pods, containers, persistent_volumes, csi_drivers, ingress_classes, network_policies, crd::example.com/Widget, pod_metrics, logs | `06-select-tables.png`, `07-select-tables-full.png` |
| PostgreSQL form unchanged (no panel for types without a guide) | `08-postgres-form-unchanged.png` |

Not covered: an agent prompt through a real LLM (no provider key in this
environment). The query surface the agent would use is exercised directly
against the cluster above and by the unit suite.

## Re-capturing fixtures

```bash
KC=/tmp/bow-agent/kubernetes-access.yaml; D=backend/tests/unit/fixtures/kubernetes
cap() { out="$D/$1.json"; shift; kubectl --kubeconfig $KC get "$@" -o json | python3 -c "
import json,sys; d=json.load(sys.stdin)
for it in d.get('items',[]): (it.get('metadata') or {}).pop('managedFields', None)
json.dump(d, open('$out','w'), indent=1, sort_keys=True)"; }
cap api_v1_pods pods -A            # …one per table; see the README in the fixtures dir for the full list
```

## Regression notes

- `tests/integrations/integrations.json` is gitignored and local only.
- `scripts/gen_sandbox_license.py` swaps `app/ee/license_public_key.pem`;
  restore it (`mv …pem.orig …pem`) before committing — done here.
- Teardown: `tools/kubernetes/seed_kubernetes.sh --stop`.
