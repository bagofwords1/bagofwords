---
name: deploy-bow-data-edge-agent
description: Deploy and run the BoW data edge agent in its k8s runtime pod for development. Use when the user asks to run or deploy the data edge agent. NOT for the bow app, which has its own pod and tools.
---

# Deploy BoW Data Edge Agent

Deploys and runs the **data edge agent** — the process that holds credentials
for local data sources and answers proxied queries over NATS — in its own
runtime pod on the bow-k8s cluster. This skill is for the agent only.

**Not the bow app.** The app (backend + frontend) is a separate pod with its own
tools (`bow_runtime_app_deploy` / `_start` / `_status` / `_delete`). Do not use
this skill or the `bow_runtime_data_edge_agent_*` tools for it.

The runtime pod (`bow-runtime-data-edge-agent`) runs a small HTTP server that
accepts the sources as a tar archive over PUT; a separate start step then boots
the agent inside the pod. The flow is: deploy the pod → upload sources → start
the agent.

## Prerequisites
The agent connects out to **NATS** and a **data source** (the LEGO PostgreSQL by
default). Both must already be running in the namespace, or the agent starts and
then fails to connect:

- NATS — `nats_status`, and `nats_deploy` if absent.
- PostgreSQL — `postgresql_status`, and `postgresql_deploy` if absent.

In-cluster the agent reaches them by their service DNS names
(`nats.bow-test.svc.cluster.local:9443`,
`postgresql.bow-test.svc.cluster.local:5432`); no port-forwarding is involved.

## Process

1. **Check / deploy the pod.** `bow_runtime_data_edge_agent_status` shows whether
   `bow-runtime-data-edge-agent` is running. If not,
   `bow_runtime_data_edge_agent_deploy` creates its Deployment, Service and
   ConfigMap and waits for the rollout.

2. **Locate the sources root.** The directory containing `start.sh` at the repo
   root is the sources root. (The agent itself lives under `data_plane/`, which
   is included by the archive below.)

3. **Archive the sources**, excluding everything the runtime does not need (see
   *What to exclude*). On macOS prefix with `COPYFILE_DISABLE=1`, or tar adds
   AppleDouble `._*` entries that get uploaded alongside the real files:

       COPYFILE_DISABLE=1 tar -czf /tmp/sources.tgz \
         --exclude='./.git' \
         --exclude='./.github' \
         --exclude='./.idea' \
         --exclude='./.vscode' \
         --exclude='./media' \
         --exclude='./docs' \
         --exclude='./.venv' \
         --exclude='./backend/.venv' \
         --exclude='./data_plane/.venv' \
         --exclude='*/node_modules' \
         --exclude='./frontend/.output' \
         --exclude='./frontend/.nuxt' \
         --exclude='./frontend/dist' \
         --exclude='__pycache__' \
         --exclude='*.pyc' \
         --exclude='./backend/db' \
         --exclude='./backend/uploads' \
         --exclude='.pytest_cache' \
         --exclude='.mypy_cache' \
         --exclude='.ruff_cache' \
         --exclude='*.log' \
         --exclude='.DS_Store' \
         --exclude='*.swp' \
         -C <sources root> .

   Check the result before uploading — tens of MB, not hundreds:

       du -h /tmp/sources.tgz && tar -tzf /tmp/sources.tgz | wc -l

### What to exclude, and why
Do not upload the whole root verbatim. Measured on this repo, the root is
**2.1 GB** and almost all of it is not source:

| exclude                                                               | size     | why                                              |
|-----------------------------------------------------------------------|----------|--------------------------------------------------|
| `backend/.venv`                                                       | 1.7 GB   | rebuilt in the container; also platform-specific |
| `data_plane/.venv`                                                    | 125 MB   | rebuilt in the container (uv sync)               |
| `.git`                                                                | 120 MB   | history, never needed at runtime                 |
| `media`                                                               | 58 MB    | README/marketing screenshots                     |
| `docs`                                                                | 18 MB    | product documentation                            |
| `node_modules`, `frontend/.output`, `frontend/.nuxt`                  | varies   | installed / built in the container               |
| `backend/db`, `backend/uploads`                                       | varies   | local state, not sources                         |
| `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache` | varies   | caches                                           |

With those excluded the archive is **17 MB / 3060 entries** (measured), down
from 97 MB / 3879 with only the build artifacts excluded. Keep source, configs
(`*.yaml`), `data_plane/`, `scripts/`, `tools/` and tests — the runtime is a dev
environment, so do not reuse `.dockerignore`, which additionally drops `*.yaml`,
`tests/` and all `*.md`.

4. **Discover the upload endpoint.** `bow_runtime_data_edge_agent_status` reports
   the live Service name and the "Upload URL" line — use it rather than assuming
   the address (the Service is `bow-runtime-data-edge-agent`).

5. **Upload the archive.** PUT it at that URL. The Service DNS name only resolves
   inside the cluster; from a developer machine, port-forward first:

       kubectl port-forward -n bow-test svc/bow-runtime-data-edge-agent 9191:9191
       curl -T /tmp/sources.tgz http://localhost:9191/sources.tgz

6. **Start the agent.** `bow_runtime_data_edge_agent_start` execs into the pod
   and launches `tools/agent/boot_data_edge_agent.sh` from `/sandbox/app` —
   in-cluster by default — returning immediately with output going to
   `/tmp/boot_data_edge_agent.log` in the pod. It runs `uv sync` for
   `data_plane` on first start, then connects. Follow it with:

       kubectl exec -n bow-test <pod> -- tail -f /tmp/boot_data_edge_agent.log

   It is ready when the log prints `edge agent is up`, after which it has logged
   `nats.connected`, subscribed to its subjects and `advertised`. The agent
   serves no HTTP port — it connects out to NATS and answers requests over the
   tunnel — so there is nothing to curl to check it; read the log.

## What the runtime does with the archive
The body is saved under `/tmp`, unpacked there, and each extracted file is
copied into `/sandbox/app` — the app root — keeping its path within the archive.
A file is copied only when it is absent from the target or its contents differ,
so re-uploading an unchanged archive is a no-op. The response reports counts and
lists each path as `+` new, `~` changed or `=` unchanged.

Note the sync only adds and updates: a file deleted from your sources stays in
`/sandbox/app` from an earlier upload. To reset, `bow_runtime_data_edge_agent_delete`
then `bow_runtime_data_edge_agent_deploy` gives a clean pod.

On this pod, port 9191 is the upload server. 8080 and 3000 are published on the
Service but nothing ever listens on them — the agent has no HTTP listener — so
do not point a health check or an uploader at them.
