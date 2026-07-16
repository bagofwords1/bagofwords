---
name: deploy-bow-runtime
description: Deploy BoW runtime for development process. Use when user ask you to run bow app or data edge agent.
---

# Deploy BoW Runtime
This skill works in conjunctions with bow-k8s MCP server.
bow-k8s MCP deploys the runtime using the `bow_runtime_deploy` tool.
The tool deploys only the bow-runtime container.
Your goal is to copy the sources from the local location
to the destination container. To copy the sources, the runtime container
exposes an HTTP PUT method which accepts a tar archive of the sources.

## Process
1. **Deploy the runtime** Use `bow_runtime_deploy`. It creates the Deployment,
   Service and ConfigMap, and waits for the rollout.
2. **Locate the sources root directory** Locate the `start.sh` file, this will be
   your sources root directory.
3. **Archive the sources** archive all the files located at the application root
   directory into a single tar archive. On macOS prefix the command with
   `COPYFILE_DISABLE=1`, otherwise tar adds AppleDouble `._*` entries that get
   uploaded alongside the real files:

       COPYFILE_DISABLE=1 tar -czf /tmp/sources.tgz -C <sources root> .

4. **Discover the upload endpoint** use the bow-k8s MCP tool
   `bow_runtime_status` and read the "Upload URL" line. It reports the live
   Service name, so use it rather than assuming the address.
5. **Upload archive** PUT the archive at that URL. The Service DNS name only
   resolves inside the cluster; from a developer machine, port-forward first:

       kubectl port-forward -n bow-test svc/bow-runtime 9191:9191
       curl -T /tmp/sources.tgz http://localhost:9191/sources.tgz

## What the runtime does with the archive
The body is saved under `/tmp`, unpacked there, and each extracted file is
copied into `/sandbox/app` — the app root — keeping its path within the archive.
A file is copied only when it is absent from the target or its contents differ,
so re-uploading an unchanged archive is a no-op. The response reports counts and
lists each path as `+` new, `~` changed or `=` unchanged.

Note the sync only adds and updates: a file deleted from your sources stays in
`/sandbox/app` from an earlier upload.

Only port 9191 serves HTTP. Ports 8080 and 3000 are published on the Service but
have no listener behind them, so an upload aimed at either is refused.
