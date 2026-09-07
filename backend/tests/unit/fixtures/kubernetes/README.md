# Kubernetes fixtures — captured from the seeded kind sandbox

Captured 2026-09-06 from `tools/kubernetes/seed_kubernetes.sh` (kind v0.32,
Kubernetes v1.36.1, metrics-server, kind's local-path provisioner) **through
the connector's own service-account access file**, so every body is exactly
what the connector sees in production. `managedFields` is stripped; everything
else — including `kubectl.kubernetes.io/last-applied-configuration`
annotations and the deliberately planted `PAYMENT_API_KEY` env literal — is
kept because the redaction tests depend on it. All data is synthetic.

| File | API path |
|---|---|
| `api_v1_<plural>.json` | `/api/v1/<plural>` (`-A` list) |
| `apis_<group>_<version>_<plural>.json` | `/apis/<group>/<version>/<plural>` (`-A` list) |
| `apis_metrics.k8s.io_v1beta1_{pods,nodes}.json` | metrics-server usage snapshot |
| `version.json` | `/version` |
| `log_<namespace>_<pod>_<container>.txt` | `…/pods/<pod>/log?timestamps=true` (`--previous` for the crashed ones) |

The fake `ApiClient` in `tests/unit/test_kubernetes_client.py` serves
namespaced paths, named gets, label/field selectors and `limit`/`continue`
pagination from these all-namespace lists, so one capture per kind covers
every path shape the client builds.

Seeded states worth knowing when reading the data:

- `payments/fraud-scorer-*` — CrashLoopBackOff (exit 1), log says the model file is missing
- `payments/report-builder-*` — OOMKilled (exit 137) under a 32Mi limit
- `payments/gpu-batch` — Pending, unsatisfiable nodeSelector
- `payments/archiver` + PVC `ledger-archive` — Pending on StorageClass `slow-nfs` whose provisioner is not installed
- `payments/nightly-reconcile` CronJob — every run fails (exit 2)
- `payments/checkout` — healthy Deployment + Service + EndpointSlices + HPA; Ingress references the absent class `public-nginx`; NetworkPolicy isolates the namespace
- `inventory/catalog-db` StatefulSet — bound PV via the default class (the storage chain)
- `inventory` — ResourceQuota; ConfigMap with a planted connection string (must never surface)
- `widgets.example.com` CRD with two instances → `crd::example.com/Widget`

Re-capture: run the seed script, then the capture loop recorded in
`docs/feedback-loops/kubernetes-connector.md`.
