# devbox deployment

Everything needed to run Bag of Words (bow) devboxes on the sandbox cluster: the
SandboxTemplate and warm pool that define a devbox, the Secret they boot with, and the
devbox MCP server that creates and manages them for users.

| File | Purpose |
|---|---|
| `sandbox-template.yaml` | `SandboxTemplate` **bow-sandbox-template**: the devbox pod (runtime image, ports, probes, persistent workspace volume, node-wide dependency cache, config Secret). |
| `sandbox-warmpool.yaml` | `SandboxWarmPool` **bow-warmpool**: pre-booted devboxes waiting to be claimed. |
| `devbox-mcp.yaml` | The devbox MCP server: Namespace `devbox`, ServiceAccount with cluster-admin, Deployment, Service, HTTPRoute and the Gateway API-key SecurityPolicy. |
| `README.md` | This file. |

Related code: the MCP server in [`..`](../README.md), the sandbox runtime image in
[`../sandbox-runtime`](../sandbox-runtime/) (`Dockerfile`, `entrypoint.sh`, `bow-stack.sh`).

```
  user (claude.ai / Claude Code)
     |  devbox_create "alice"
     v
  devbox-mcp (ns devbox) --kube API--> SandboxClaim alice-k7x2  --adopts-->  warm Sandbox (bow-warmpool-xxxxx)
                                       Service alice-k7x2                     pod: bow app :3000, backend :8000,
                                       HTTPRoute app-alice-k7x2.sndbx...  --> code-server :8080, sandboxd :8888/:9090
                                       HTTPRoute code-server-alice-k7x2... -> PVC workspace-<sandbox>
```

## Prerequisites

- The cluster from the devex repo's `sandbox-cluster/`: MicroK8s, Envoy Gateway with the
  Gateway `eg` in `default` (wildcard listener `*.sndbx.bagofwords.com` with a Let's Encrypt
  certificate), MetalLB, hostpath storage.
- The agent-sandbox controller and extensions (`install.sh` in the devex repo's
  `agent-sandbox/`, or the upstream release manifest) **with the warm pool readiness grace
  period raised**: the controller replaces any pool Sandbox that is not Ready within the
  grace period (default 5 min), and a devbox takes 5-8 min to boot. The install script
  sets `--sandbox-warm-pool-readiness-grace-period=20m`; to do it by hand:

  ```bash
  kubectl -n agent-sandbox-system patch deployment agent-sandbox-controller --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--sandbox-warm-pool-readiness-grace-period=20m"}]'
  ```
- The DNS-only wildcard A record `*.sndbx.bagofwords.com` pointing at the Gateway address.

Check:

```bash
kubectl get gateway eg -n default
kubectl -n agent-sandbox-system get deploy agent-sandbox-controller
kubectl get storageclass
```

## 1. Sandbox runtime image

The devbox pod image. Built from the **repository root**, because the Dockerfile also copies
`tools/qvd2parquet`:

```bash
docker build --platform linux/amd64 \
  -f tools/devbox-mcp/sandbox-runtime/Dockerfile \
  -t bagofwords/bagofwords-sandbox-runtime:latest .
docker push bagofwords/bagofwords-sandbox-runtime:latest
```

`entrypoint.sh` in that image clones the repo into the persistent workspace, installs
dependencies (through the node-wide `/cache` volume), starts the backend and frontend via
`bow-stack`, provisions the admin user, organization and LLM provider, and starts
code-server and sandboxd. See the comments at the top of `entrypoint.sh` for every knob.

## 2. Devbox config Secret (`bow-sandbox-config`)

Injected into every devbox pod as environment variables (`envFrom` on the template,
`optional: true`, so a missing Secret only means "no LLM provider"). Read at pod creation.
Lives in the sandbox namespace (`default`), next to the template.

```bash
kubectl -n default create secret generic bow-sandbox-config \
  --from-literal=BOW_ADMIN_EMAIL='admin@example.com' \
  --from-literal=BOW_ADMIN_PASSWORD='<password>' \
  --from-literal=BOW_LICENSE_KEY='<enterprise license key>' \
  --from-literal=OPENAI_API_KEY='sk-...' \
  --from-literal=OPENAI_MODEL_ID='<model id>' \
  --from-literal=OPENAI_MODEL_NAME='<display name>'
```

| Key | Effect in the devbox |
|---|---|
| `BOW_ADMIN_EMAIL`, `BOW_ADMIN_PASSWORD` | the Bag of Words admin the entrypoint creates (defaults `admin@example.com` / `Password123!`). The MCP server reads these to tell users the login. |
| `BOW_LICENSE_KEY` | enterprise license for the backend. It reads the key straight from its environment (the `license.key` placeholder in bow-config), and the entrypoint also records it in `run/stack.env` so `bow-stack restart backend` keeps the license. Unset means community edition. |
| `ANTHROPIC_KEY` | seeds an Anthropic provider with one model (`ANTHROPIC_MODEL_ID`, default Claude 4.5 Haiku) as default and small-default. Takes precedence over OpenAI. |
| `OPENAI_API_KEY`, `OPENAI_MODEL_ID`, `OPENAI_MODEL_NAME` | same with an OpenAI provider, used when no Anthropic key is set. |
| `GIT_USER_NAME`, `GIT_USER_EMAIL` | placeholder commit identity until the user signs in (defaults "Bag of Words devbox" / devbox@bagofwords.com). Push credentials are **per user**: `devbox_github_login` runs the GitHub device flow inside the devbox and the token never leaves it. No shared git token in this Secret. |
| `BOW_FRONTEND_MODE`, … | any other `entrypoint.sh` setting. (`BOW_REPO_REF` is set explicitly on the template's `env`, which takes precedence over this Secret.) |

Every devbox can read these values from its environment, so use a shared test key here,
never a production one. After changing the Secret, recycle the warm pool (below) so new
devboxes pick it up; existing devboxes keep what they were seeded with.

## 3. SandboxTemplate and warm pool

```bash
kubectl apply -f tools/devbox-mcp/deployment/sandbox-template.yaml
kubectl apply -f tools/devbox-mcp/deployment/sandbox-warmpool.yaml
kubectl -n default get sandboxtemplate,sandboxwarmpool,sandbox
```

**What the template defines**

- `spec.service: true`: a headless Service per Sandbox, used by the sandbox-router's DNS lookup.
  The Gateway reaches devboxes through the per-claim Service the MCP server creates instead.
- One container from the runtime image with ports 3000 (frontend), 8000 (backend), 8080
  (code-server) and 8888 (sandboxd REST). sandboxd gRPC on 9090 is dialled by pod IP.
- Startup and readiness probes on `GET /` of the frontend. Readiness is tolerant on purpose
  (10 s timeout, 6 failures): in dev mode the Nuxt server is single-threaded and goes quiet
  while it compiles modules for a first visit; a strict probe cuts the pod out of the Service
  and the Gateway answers 503 mid page load. No liveness probe: agents stop and restart the
  stack inside the devbox with `bow-stack`.
- `volumeClaimTemplates`: a 5Gi PVC per Sandbox mounted at `/app/workspace` (sources, venv,
  node_modules, SQLite database). Deleted with the Sandbox.
- A node-wide hostPath cache at `/cache` shared by all devboxes. Besides the download
  caches (`uv`, `yarn`, `ms-playwright`, the `git` mirror) it holds the devboxes' Python
  venv and `node_modules` themselves, because hard links only work within one mount:
  `deps.sh` in the runtime creates `/cache/venv/<sandbox>` hard-linked from the uv cache and
  `/cache/node_modules/<sandbox>` as a hard-link clone of a golden tree
  `/cache/node_modules/golden-<yarn.lock hash>` (built by the first devbox that needs it),
  then symlinks it from `frontend/node_modules`. Dependency setup drops from minutes to
  seconds. Each devbox touches `/cache/live/<sandbox>` every 5 min; at boot, per-devbox dirs
  whose heartbeat is older than `DEPS_PRUNE_AFTER` (default 24 h) are pruned. Golden trees and
  the download caches are never pruned, so pruning never costs the next claim anything.
  `BOW_LINKED_DEPS=0` falls back to keeping both on the PVC.
- `envFrom` the `bow-sandbox-config` Secret.
- `networkPolicyManagement: Unmanaged`: no NetworkPolicy. The controller's default policy would
  only admit the sandbox-router and block cluster DNS.
- `envVarsInjectionPolicy: Allowed`: a claim may add env vars, but any claim that does so
  cold-starts instead of adopting a warm Sandbox. The MCP server's claims set none.

**What the warm pool does**

`bow-warmpool` keeps `replicas` idle Sandboxes booted and Ready. A claim adopts one in
seconds instead of waiting several minutes for clone, install and provisioning. The adopted
Sandbox keeps its pool-generated name (`bow-warmpool-xxxxx`); the MCP server never depends
on that name, it selects the pod by the label the claim stamps on it. `replicas` is the idle
count, not a cap on devboxes: when the pool is empty a claim cold-starts a new Sandbox.

**Idle Sandboxes must stay Ready.** The pool controller deletes an *unclaimed* Sandbox whose
pod stops being Ready for longer than the readiness grace period (logged as "Deleting stuck
warm pool sandbox") and starts a replacement; that same timer covers the initial boot, hence
the 20 min setting above. Restarting the frontend in an idle pod, for example, gets that pod recycled.
Claimed devboxes are never touched by the pool, so agents may restart the stack freely there.

**Branches.** Every devbox starts on the branch in the template's `BOW_REPO_REF` env
(`main` by default; a tag works too). Change it there and recycle the pool to move all new
devboxes to another branch. The MCP server has no branch option and reports the branch a
devbox is actually on. Switching is done inside the devbox by the agent in a Remote Control session:
`git checkout <branch>`, then `uv sync` / `yarn install` if lockfiles changed, and
`bow-stack reset-db` when the branch's migrations are older than the database's (alembic
"Can't locate revision"): it moves the SQLite file aside as `app.db.<timestamp>.bak`,
migrates a fresh one and re-provisions the admin and LLM. `bow-stack provision` re-runs the
API provisioning alone.

**Changing the template.** Idle warm Sandboxes are not updated in place (`updateStrategy:
OnReplenish`). After editing probes, image, env or the Secret, apply and recycle the idle ones:

```bash
kubectl apply -f tools/devbox-mcp/deployment/sandbox-template.yaml
kubectl -n default delete sandbox -l agents.x-k8s.io/warm-pool-sandbox
```

Claimed devboxes are unaffected; they would need to be recreated to pick the change up.

**Disk on the node.** `/cache` grows with every new `yarn.lock` (one golden tree, ~1 GB,
per hash) and every dependency version seen by uv. Clear old goldens by hand when needed:
`ls -lt /var/cache/bow-sandbox/node_modules/` on the node and remove `golden-*` entries that
no checkout uses any more.

**Troubleshooting a devbox pod**

```bash
POD=<sandbox name>                                   # kubectl -n default get sandbox
kubectl -n default logs $POD -c bagofwords | grep -E '\[entrypoint\]|\[bow-stack\]|\[provision\]'
kubectl -n default exec $POD -c bagofwords -- bow-stack status
kubectl -n default exec $POD -c bagofwords -- tail -50 /app/workspace/run/backend.log
```

A pod can be recreated on top of its existing workspace volume (claim adopted a pool
sandbox after the template was re-applied, `kubectl delete pod`, node reboot). The
entrypoint then finds the sources and database already there and only re-provisions
what is missing: `provision.py` logs in before it tries to register, because the
backend answers `Sign-up is disabled` once any user exists. A provisioning error is
not fatal either; the boot summary shows `provisioning FAILED` and `bow-stack
provision` or `bow-stack reset-db` repairs it from inside the devbox. Runtime images
built before 2026-10-11 crash-loop in this situation (`POST /api/auth/register ->
400`); to unblock such a pod move `data/app.db*` aside on its volume and let it
restart. The readiness and startup probes never restart the container: there is no
liveness probe, and the startup probe allows 15 minutes.

## 4. MCP server

The server that users talk to. It lives in its own `devbox` namespace with a cluster-admin
ServiceAccount, is exposed at `https://devbox.sndbx.bagofwords.com/mcp` behind the Gateway,
and is protected by an API key checked at the Gateway.

```
  Claude Code / MCP host  --X-API-Key-->  https://devbox.sndbx.bagofwords.com/mcp
                                                   |
                                        Envoy Gateway (eg)
                                        SecurityPolicy: API key check
                                                   |
                                        Service devbox-mcp:3400
                                                   |
                                        Pod devbox-mcp  (namespace devbox, SA devbox-mcp)
                                          |  kube API: SandboxClaims, Services, HTTPRoutes
                                          |  gRPC 9090: sandboxd in each sandbox pod
```

### 4.1 Build and push the image

From `tools/devbox-mcp` (the Dockerfile's build context):

```bash
cd tools/devbox-mcp
docker build --platform linux/amd64 -t bagofwords/devbox-mcp:latest .
docker push bagofwords/devbox-mcp:latest
```

### 4.2 Create the API-key Secret

One entry per client, created in the `devbox` namespace. The entry's key is the client id, forwarded to the server as
`X-Client-Id`; its value is the API key the client sends in `X-API-Key`.

```bash
kubectl -n devbox create secret generic devbox-mcp-apikeys \
  --from-literal=dima="$(openssl rand -hex 32)"
kubectl -n devbox get secret devbox-mcp-apikeys -o jsonpath='{.data.dima}' | base64 -d; echo
```

Add or rotate a client later with `kubectl -n devbox patch secret devbox-mcp-apikeys --type=merge -p '{"stringData":{"<client>":"<key>"}}'`.
Envoy picks up Secret changes without a restart.

### 4.3 Deploy

```bash
kubectl apply -f tools/devbox-mcp/deployment/devbox-mcp.yaml
kubectl -n devbox rollout status deployment/devbox-mcp
kubectl -n devbox get httproute devbox-mcp securitypolicy devbox-mcp
```

DNS: `devbox.sndbx.bagofwords.com` is covered by the existing `*.sndbx.bagofwords.com`
record and wildcard certificate; nothing to add.

### 4.4 Verify

```bash
# no key -> 401 from the Gateway, never reaches the server
curl -si https://devbox.sndbx.bagofwords.com/mcp | head -1

# with key -> 406 (the MCP endpoint wants a JSON-RPC POST), proving the key passed
curl -si -H "X-API-Key: <key>" https://devbox.sndbx.bagofwords.com/mcp | head -1
```

### 4.5 Register the client

```bash
claude mcp add --transport http devbox https://devbox.sndbx.bagofwords.com/mcp \
  --header "X-API-Key: <key>"
```

## Notes on the MCP server

- **One replica only.** `claude_login` sessions live in the server's memory and
  streamable-HTTP sessions are bound to the instance that created them. The
  Deployment uses `Recreate` so a rollout never runs two.
- **Namespace.** Everything here lives in the `devbox` namespace, including the API-key
  Secret (the SecurityPolicy can only reference a Secret in its own namespace). The
  devboxes themselves are created in the sandbox namespace, `default` by default
  (`DEVBOX_NAMESPACE`).
- **RBAC.** The ServiceAccount is bound to the built-in `cluster-admin` ClusterRole, so
  the server can manage sandbox objects in any namespace. Anyone holding a valid API key
  therefore has cluster-wide reach through the tools the server exposes; keep the key set
  small and rotate on departures.
- **Reaching sandboxes.** From inside the cluster the server dials sandboxd's gRPC
  port 9090 on the sandbox pod IP directly. No router, no port-forward, no kubectl
  in the image.
- **Host validation.** `MCP_ALLOWED_HOSTS` must list the public hostname, or the
  server answers 421 to requests arriving through the Gateway.
