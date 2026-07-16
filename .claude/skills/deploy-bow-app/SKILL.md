---
name: deploy-bow-app
description: Deploy and run the BoW app (backend + frontend) in its k8s runtime pod for development. Use when the user asks to run or deploy the bow app. NOT for the data edge agent, which has its own pod and tools.
---

# Deploy BoW Application

Deploys and runs the **bow app** — the control plane (backend + frontend) — in
its own runtime pod on the bow-k8s cluster. This skill is for the app only.

**Not the data edge agent.** That is a separate pod with its own tools
(`bow_runtime_data_edge_agent_deploy` / `_start` / `_status` / `_delete`). Do
not use this skill or the `bow_runtime_app_*` tools for it.

The runtime pod (`bow-runtime-app`) runs a small HTTP server that accepts the
sources as a tar archive over PUT; a separate start step then boots the app
inside the pod. Both the app and the agent pods use the same upload mechanism,
so the flow below is: deploy the pod → upload sources → start the app.

## Process

1. **Check / deploy the pod.** `bow_runtime_app_status` shows whether
   `bow-runtime-app` is running. If not, `bow_runtime_app_deploy` creates its
   Deployment, Service and ConfigMap and waits for the rollout.

2. **Locate the sources root.** The directory containing `start.sh` at the repo
   root is the sources root.

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
| `data_plane/.venv`                                                    | 125 MB   | same                                             |
| `.git`                                                                | 120 MB   | history, never needed at runtime                 |
| `media`                                                               | 58 MB    | README/marketing screenshots                     |
| `docs`                                                                | 18 MB    | product documentation                            |
| `node_modules`, `frontend/.output`, `frontend/.nuxt`                  | varies   | installed / built in the container               |
| `backend/db`, `backend/uploads`                                       | varies   | local state, not sources                         |
| `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache` | varies   | caches                                           |

With those excluded the archive is **17 MB / 3060 entries** (measured), down
from 97 MB / 3879 with only the build artifacts excluded. Keep source, configs
(`*.yaml`), `locales/`, `scripts/`, `tools/` and tests — the runtime is a dev
environment, so do not reuse `.dockerignore`, which additionally drops `*.yaml`,
`tests/` and all `*.md`.

4. **Discover the upload endpoint.** `bow_runtime_app_status` reports the live
   Service name and the "Upload URL" line — use it rather than assuming the
   address (the Service is `bow-runtime-app`).

5. **Upload the archive.** PUT it at that URL. The Service DNS name only resolves
   inside the cluster; from a developer machine, port-forward first:

       kubectl port-forward -n bow-test svc/bow-runtime-app 9191:9191
       curl -T /tmp/sources.tgz http://localhost:9191/sources.tgz

6. **Start the app.** `bow_runtime_app_start` execs into the pod and launches
   `tools/agent/boot_stack.sh --dev` from `/sandbox/app`, returning immediately
   with output going to `/tmp/boot_stack.log` in the pod. Boot takes a couple of
   minutes (uv sync, migrations, yarn install, nuxt build). Follow it with:

       kubectl exec -n bow-test <pod> -- tail -f /tmp/boot_stack.log

   It is ready when the log prints `stack is up`. The frontend then serves on
   port 3000 through the Service (boot_stack.sh binds it with `--host`); the
   backend listens on 8000 inside the pod.

## What the runtime does with the archive
The body is saved under `/tmp`, unpacked there, and each extracted file is
copied into `/sandbox/app` — the app root — keeping its path within the archive.
A file is copied only when it is absent from the target or its contents differ,
so re-uploading an unchanged archive is a no-op. The response reports counts and
lists each path as `+` new, `~` changed or `=` unchanged.

Note the sync only adds and updates: a file deleted from your sources stays in
`/sandbox/app` from an earlier upload. To reset, `bow_runtime_app_delete` then
`bow_runtime_app_deploy` gives a clean pod.

Before the app is started, only port 9191 (the upload server) serves HTTP; 8080
and 3000 are published on the Service but have nothing behind them until
`bow_runtime_app_start` boots the stack.
