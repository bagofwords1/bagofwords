# devex-mcp

Developer-experience MCP server. Its tools run interactive flows **inside an Agent
Sandbox** by driving `sandboxd`'s gRPC `ProcessService` under a PTY (the Python SDK's
`commands.run()` is one-shot and cannot answer prompts).

## Tools

| Tool | What it does |
|---|---|
| `create_sandbox` | Creates a Bag of Words sandbox from a user-chosen name (plus a random 4-char suffix), applies the SandboxClaim, Service and two HTTPRoutes, waits for Ready, and returns `app_url`, `code_server_url`, `claude_logged_in` and `next_steps`. |
| `list_sandboxes` | Lists sandboxes created this way with readiness, URLs and `claude_logged_in`. The way to recover a `claim_name` in a later conversation. |
| `sandbox_status` | Readiness of one sandbox and whether Claude Code inside it is signed in, plus `next_steps`. |
| `delete_sandbox` | Removes everything `create_sandbox` created for a claim name. The pod and its volume go with it. |
| `claude_login` | Starts `claude auth login` inside an existing sandbox (`claim_name`); returns the sign-in `url` and a `session_id`. |
| `claude_login_submit_code` | Types the code the browser showed into that session; returns `success`, `exit_code`, `output`. |
| `claude_login_cancel` | Aborts a pending session. The sandbox keeps running. |
| `claude_login_pending` | Lists sessions still waiting for a code. |
| `claude_remote_control_start` | Starts `claude remote-control --name <name>` detached inside a sandbox (needs a prior `claude_login`); returns the claude.ai/code `url`. Offered to the user right after a successful login. |
| `claude_remote_control_status` | Whether Remote Control is running in a sandbox and its URL. |
| `claude_remote_control_stop` | Stops the Remote Control daemon in a sandbox. |

The login is split in two because an MCP tool call cannot block on a human mid-call: the
host shows the URL to the user, the user signs in and copies the code, then the host calls
the second tool. Pending sessions expire after 15 minutes.

**How the tools connect.** `create_sandbox` returns a `claim_name`; the host model passes it
to `claude_login`, `sandbox_status` and `delete_sandbox`. If it is no longer in context,
`list_sandboxes` recovers it. Every result carries `next_steps` the host should relay, so the
user learns that Claude Code in a fresh sandbox is not signed in and can ask for the login.

**Remote Control.** After `claude_login_submit_code` succeeds its `next_steps` tell the host
to ask the user whether to start Remote Control. `claude_remote_control_start` then runs
`claude remote-control --name <claim>` inside the sandbox under a PTY (`script`), detached with
`setsid` so it outlives the tool call and the MCP server, logging to
`/app/workspace/run/remote-control.log`. The tool polls that log until the daemon prints
`Ready` and returns the `https://claude.ai/code?environment=...` URL. The sandbox then shows up
as an environment in the user's claude.ai account.

**How the sandbox is reached.** The server runs in the cluster. A claim name resolves to the
bound Sandbox and its pod IP (from the Sandbox status), and sandboxd's gRPC ProcessService on
port 9090 of that pod is dialled directly. Outside the cluster, for local development, a
`kubectl port-forward` to the pod is used instead. Nothing is created or terminated by the
login tools.

## Sandbox creation

`create_sandbox(name)` turns `name` into a unique claim name (`alice` -> `alice-k7x2`)
and applies the same objects as `devex/agent-sandbox/sandbox-claim.yaml`, all keyed by
that claim name:

| Object | Name | Purpose |
|---|---|---|
| SandboxClaim | `<claim>` | adopts a warm-pool Sandbox, stamps `sandbox.users.io/claim=<claim>` on its pod |
| Service | `<claim>` | selects the pod by that label; ports 3000 (app) and 8080 (code-server) |
| HTTPRoute | `sandbox-<claim>-app` | `app-<claim>.<domain>` -> `<claim>:3000` |
| HTTPRoute | `sandbox-<claim>-code` | `code-server-<claim>.<domain>` -> `<claim>:8080` |

Cluster access uses in-cluster credentials when running as a pod, else the local
kubeconfig. Defaults come from env: `SANDBOX_NAMESPACE` (default), `SANDBOX_WARMPOOL`
(bow-warmpool), `SANDBOX_DOMAIN` (sndbx.bagofwords.com), `SANDBOX_GATEWAY_NAME` (eg),
`SANDBOX_GATEWAY_NAMESPACE` (default), `SANDBOX_GATEWAY_SECTION` (https-wildcard).
Pass `dry_run=true` to get the manifests without applying them.

## Run

```sh
cd tools/agentic-dev/devex-mcp
uv sync

# stdio (what MCP hosts spawn)
uv run devex-mcp

# streamable HTTP, e.g. for a remote host -> http://localhost:3400/mcp
uv run devex-mcp --transport streamable-http --port 3400

# behind a public hostname (container / Gateway): bind all interfaces and name
# the hostnames clients will use, so Host/Origin validation accepts them
uv run devex-mcp --transport streamable-http --host 0.0.0.0 --port 3400 \
  --allowed-host mcp.sndbx.bagofwords.com
# ($MCP_ALLOWED_HOSTS=a,b works too.) Without --allowed-host on a non-loopback
# bind, Host validation is disabled; put authentication in front in that case.
```

Register with Claude Code (local, stdio):

```sh
claude mcp add devex -- uv run --directory /ABS/PATH/tools/agentic-dev/devex-mcp devex-mcp
```

## Deploy in the cluster

`Dockerfile` here builds `bagofwords/devex-mcp`. The Kubernetes manifests (ServiceAccount,
Role, Deployment, Service, HTTPRoute and the Gateway API-key SecurityPolicy) live in the
devex repo under `devex-mcp/`, with step-by-step instructions in its README. Once deployed,
clients register it over HTTP:

```sh
claude mcp add --transport http devex https://mcp.sndbx.bagofwords.com/mcp \
  --header "X-API-Key: <key>"
```

## Notes

- `sandboxd` resolves the command with the **daemon's** PATH, not the `PATH` passed as
  process env, so `claude` must be installed on the daemon's PATH in the sandbox image.
- In-cluster the ServiceAccount needs: get/list/create/patch/delete `sandboxclaims`,
  get `sandboxes` (both `*.agents.x-k8s.io`), create/patch/delete `services`, and
  create/patch/delete `httproutes.gateway.networking.k8s.io` in the sandbox namespace.
  `SANDBOXD_GRPC_PORT` overrides the sandboxd gRPC port (default 9090).
- `home` controls where the session is stored (`$HOME/.claude`), `config_dir` sets
  `CLAUDE_CONFIG_DIR` instead.
- Layout: `devex_mcp/sandbox.py` (PTY process wrapper + target resolution),
  `devex_mcp/claude_login.py` (two-phase login + session registry),
  `devex_mcp/sandbox_claim.py` (claim + Service + HTTPRoute builder and apply),
  `devex_mcp/server.py` (FastMCP tools and CLI).
