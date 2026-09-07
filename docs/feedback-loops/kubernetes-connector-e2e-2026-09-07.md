# Kubernetes connector — end-to-end test plan and results (2026-09-07)

Derived from `docs/design/kubernetes-connector.md`. Target: the developer's
default cluster (`~/.kube/config` current context `microk8s`, Kubernetes
v1.35.6, one node, no metrics-server, 49 CRDs) reached through the connector's
own service-account access file printed by `tools/kubernetes/print_access_file.sh`.
Every call the connector makes is a `GET`; nothing on the cluster is modified.

App under test: the local sandbox (backend `main.py` on :8000 with a stable
`BOW_ENCRYPTION_KEY` and a sandbox license, `nuxt dev` on :3000, fresh seeded
database, Anthropic Haiku configured).

## A. Backend — `KubernetesClient` directly against the cluster

| # | Case (design section) | Expectation |
|---|---|---|
| A1 | `test_connection()` (Authentication) | success; message names server version, node and namespace counts, and says no metrics-server |
| A2 | `get_schemas()` catalog (Data model) | all 27 fixed tables; `logs` present; `pod_metrics`/`node_metrics` absent (not served) with the note on `nodes`; `crd::` tables only for populated kinds, ≤ `max_crd_tables`; `labels`/`annotations` on object tables only; FK chains (pod→node, rs→deployment, job→cronjob, pvc→pv→sc→csi, ingress→class, slice→service) |
| A3 | Every fixed table queried, all namespaces | a DataFrame with the table's columns even at zero rows (jobs, cronjobs, ingresses, ingress_classes, HPAs, resource_quotas, csi_drivers are empty on this cluster) |
| A4 | Row semantics | pods `ready` is `n/m`, `restarts` int, owner resolved, `app` promoted, `labels` parses as JSON, `last-applied` stripped; containers ≥ pods with `state`/normalized resources; nodes capacity in millicores/bytes and `kubelet_version`; deployments counters; endpoint slices `ready_count`; network policy rules JSON; PVC→PV chain (`volume_name` ↔ `claim_name`), PV `source_type`; storage class `is_default`; configmaps keys only |
| A5 | CRDs (Custom resources) | `custom_resource_definitions` = 49 rows; a `crd::gateway.networking.k8s.io/Gateway` query returns rows with `spec`/`status` JSON |
| A6 | Logs (Runtime signals) | by pod name; `container`; `tail_lines` honoured; `since`; `grep`; `label_selector` fan-out; `previous=true` on a never-restarted container yields a `<no logs…>` row, not an exception; timestamps parsed |
| A7 | Query spec | namespace list; single-object `name`; `label_selector`/`field_selector`; `limit` paginates; `raw` column with env redaction; escape hatch list path; refused paths (`secrets`, `exec`, `proxy`) raise `ValueError` |
| A8 | Namespace allowlist (Config) | client built with `namespaces="bow-test"` scopes an unqualified query and refuses `kube-system` |
| A9 | Error surfaces | laptop kubeconfig with `exec` → `ValueError` naming the stanza; tampered token → 401 message pointing at the setup guide; unknown table → `ValueError`; `pod_metrics` on this cluster → clear "no metrics-server" error |
| A10 | Security | no `secrets` table; `configmaps` exposes no values; `raw` never contains a `last-applied` annotation |

## B. Backend — through the app API

| # | Case | Expectation |
|---|---|---|
| B1 | `GET /data_sources/kubernetes/fields` | one visible config field (`namespaces`), `access_file` textarea, single auth variant, 3-step `setup_guide` with the inline manifest heredoc, `supports_user_auth=false` |
| B2 | `POST /data_sources/test_connection` with the access file | success, "Found N tables" |
| B3 | Connection created through the UI (C) is indexed | connection lists the same table count; `POST /connections/{id}/test` succeeds |

## C. Frontend — Playwright through the real UI

| # | Case | Expectation |
|---|---|---|
| C1 | Catalog | Kubernetes tile under Infrastructure, unlocked |
| C2 | Connect form | setup guide with 3 steps and 2 copy buttons; step 1 copies the exact `rbac.yaml` heredoc; only Namespaces in Configuration; no "Require user authentication" toggle |
| C3 | Wrong paste | laptop kubeconfig rejected with the `exec` message |
| C4 | Right paste | Test connection → "Connected successfully. Found N tables."; Save and Continue |
| C5 | Discovery + tables | "Discovered N tables" → Connect → Select Tables lists `pods`, `deployments`, `logs`, a `crd::` table; select all; Set Context reached |
| C6 | Agent prompts (real LLM) | three prompts answered from live cluster data: deployments in `bow-test`, last log lines of the `bow-runtime-app` pod, restarted pods + warning events — each answer names real objects |

## D. Regression suites

| # | Case | Expectation |
|---|---|---|
| D1 | `tests/unit/test_kubernetes_client.py` | green |
| D2 | `tests/e2e/test_data_source.py`, `tests/e2e/test_connection.py` (sqlite) | green |

## Results

Scripts (copied into the repo so the loop is re-runnable): `tools/agent/k8s_e2e_backend.py`
(section A, prints a PASS/FAIL table), `tools/agent/k8s_e2e_frontend.mjs` (C1–C5,
run from a copy in `frontend/.agent-tmp/`), `tools/agent/k8s_e2e_prompts.mjs`
(C6, real LLM). Evidence in `media/pr/feature-k8s-connector/e2e/`.

| # | Result | Observed |
|---|---|---|
| A1 | PASS | `Connected to Kubernetes v1.35.6: 1 nodes, 11 namespaces; no metrics-server (usage tables hidden).` |
| A2 | PASS | 43 tables: 27 fixed + `logs` + 18 `crd::` (Calico, Gateway API, MetalLB, Envoy Gateway, agents.x-k8s.io); 49 CRD probes reported via progress; metadata columns and FK chains as designed |
| A3 | PASS | rows: pods 22, containers 30, deployments 14, replicasets 21, statefulsets 3, daemonsets 2, configmaps 24, events 9, services 18, endpoint_slices 18, network_policies 2, PVC 4, PV 4, storage_classes 1, CRDs 49, namespaces 11, nodes 1; empty with columns intact: jobs, cronjobs, HPAs, resource_quotas, ingresses, ingress_classes, csi_drivers |
| A4 | PASS | node 8000 millicores; all 4 PVCs Bound and joined to their PVs by name; default storage class `microk8s-hostpath`; configmaps expose keys only |
| A5 | PASS | `crd::gateway.networking.k8s.io/Gateway`: 2 rows, spec keys `gatewayClassName`, `listeners` |
| A6 | PASS | logs by pod (5 rows, timestamps parsed), `grep` PUT/GET (8 rows), `since -24h`, label-selector fan-out, `previous=true` on the restarted app pod returned its previous run's lines |
| A7 | PASS | namespace list 12 pods, `name` → 1, selectors → 1 Running, `limit 3` → 3, `raw` without last-applied, escape hatch 2 rows, `secrets`/`exec`/`proxy` paths refused |
| A8 | PASS | allowlist `bow-test`: 8 pods; `kube-system` refused |
| A9 | PASS | exec kubeconfig rejected naming `exec`; tampered token → 401 message; unknown table and `pod_metrics` refused with clear messages |
| A10 | PASS | no secrets table; configmap values never returned |
| B1 | PASS | visible config `namespaces` only; credential `access_file`; single auth variant; 3-step guide starting with the inline manifest heredoc; `supports_user_auth=false` |
| B2 | PASS | `Connected successfully. Found 43 tables.` |
| B3 | PASS | the connection created in C4/C5 indexed 43 tables (`Discovered 43 tables · 1s`) |
| C1 | PASS | tile under Infrastructure, unlocked (sandbox license) |
| C2 | PASS | 3 steps, 2 copy buttons, step-1 clipboard equals the `rbac.yaml` heredoc byte-for-byte; labels Connection Name / Namespaces / Cluster access file; no "Require user authentication" toggle |
| C3 | PASS | `…user carries a \`exec\` stanza, which is not accepted…` |
| C4 | PASS | `Connected successfully. Found 43 tables.` |
| C5 | PASS | discovery modal, Select Tables lists pods/deployments/logs/crd::, Set Context reached and the LLM generated the overview + starters ("Failing Workloads", "Recent Warning Events", …) |
| C6 deployments | PASS (on rerun) | 55 s: table of the 4 `bow-test` deployments with ready/desired and images, flagging two with `unavailable=1`. First attempt: the agent's code-generation LLM call stalled ~17 min before returning (see findings) |
| C6 logs | PASS | 110 s, 2 steps (`logs`, `pods`): the pod's 9 available lines summarised — startup on :9191, four GET/PUT pairs, no errors |
| C6 restarts | PASS | 75 s: 20-row "Pods with Restarts" table plus the Warning events |
| D1 | PASS | 113 unit tests |
| D2 | PASS | 11 e2e tests (sqlite) |

### Findings outside the connector (not fixed here)

1. **No timeout on the agent's LLM call.** The first C6 prompt sat at
   "Creating Data · deployments · Generating Code" for ~17 minutes until the
   provider stream finally returned (`agent_execution_done +84901ms` on the
   resumed step); the UI showed "Working 15m 23s" with no way to tell a slow
   provider from a hang. The connector had not been queried yet. A per-request
   timeout with retry in the LLM client would bound this.
2. **Session-concurrency errors in `query_context_builder`** during every run:
   `Failed to load queries for report …: This session is provisioning a new
   connection; concurrent operations are not permitted`, plus
   `focus-on-use: commit failed`. The runs still completed, so the errors are
   swallowed, but they are logged at ERROR on every prompt.
3. **`bow-runtime-app` pod restarts / `unavailable=1`** on two deployments are
   real cluster state the agent surfaced correctly, not test artefacts.
