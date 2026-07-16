# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Evaluation/deployment scripts and a Helm chart for running NVIDIA OpenShell on Kubernetes. OpenShell is a runtime environment for autonomous agents — the gateway manages sandbox pods where agents execute.

This is a local eval environment, not the upstream OpenShell repo. The numbered shell scripts provision a Kind cluster, install prerequisites, and deploy the OpenShell Helm chart from the upstream OCI registry.

## Cluster Setup (run scripts in order)

```bash
source 01-exports.sh          # sets KIND_KUBECONFIG, KIND_CLUSTER_NAME, NIP_IO_IP (via colima)
./10-setup-cluster.sh          # creates Kind cluster with port mappings + niplb
./20-install-openshell-prereq.sh  # installs Agent Sandbox CRDs (v0.5.2) + Envoy Gateway (v1.8.1)
./30-install-openshell-helm.sh    # helm install from oci://ghcr.io/nvidia/openshell/helm-chart
```

All kubectl/helm commands use `--kubeconfig="${KIND_KUBECONFIG}"` (the `kind-cluster` file in this directory). The cluster name has a typo: `openshll-eval` (missing 'e').

Cleanup: `./clenaup.sh` (also has a typo in filename) deletes the Kind cluster.

## Environment Requirements

- **colima** must be running — `NIP_IO_IP` is derived from `colima status --json`
- **kind**, **kubectl**, **helm** on PATH
- Port mappings: host 15021->30001, 80->30002, 443->30003

## Helm Chart (`helm-chart/`)

Local copy of the OpenShell Helm chart (appVersion 0.0.86). Key architecture:

- **Workload kind**: StatefulSet (default, for SQLite) or Deployment (requires `server.externalDbSecret` for PostgreSQL)
- **Gateway pod template**: shared via `_gateway-workload.tpl`, used by both `statefulset.yaml` and `deployment.yaml`
- **TLS bootstrap**: pre-install/pre-upgrade hook Job (`certgen.yaml`) runs `openshell-gateway generate-certs`; alternatively cert-manager owns TLS when `certManager.enabled=true`
- **Gateway API**: optional GRPCRoute + Gateway resources via `grpcRoute.enabled`
- **Supervisor sideloading**: auto-detects `image-volume` (K8s >= 1.35) vs `init-container`

### Helm Template Rendering

```bash
helm template openshell ./helm-chart -f values.yaml
```

### Helm Tests

Tests live in `helm-chart/tests/` and use `helm-unittest` format (not standard `helm test`). They validate template output for specific values combinations.

### Local `values.yaml` Override

The root `values.yaml` enables `grpcRoute` with gateway creation using the `eg` GatewayClass — this is the eval-specific override on top of the chart defaults.