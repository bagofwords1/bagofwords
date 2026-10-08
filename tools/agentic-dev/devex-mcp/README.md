# devex-mcp

Developer-experience MCP server. Its tools run interactive flows **inside an Agent
Sandbox** by driving `sandboxd`'s gRPC `ProcessService` under a PTY (the Python SDK's
`commands.run()` is one-shot and cannot answer prompts).

## Tools

| Tool | What it does |
|---|---|
| `claude_login` | Starts `claude auth login` in the sandbox; returns the sign-in `url` and a `session_id`. |
| `claude_login_submit_code` | Types the code the browser showed into that session; returns `success`, `exit_code`, `output`. |
| `claude_login_cancel` | Aborts a pending session and releases its sandbox. |
| `claude_login_pending` | Lists sessions still waiting for a code. |

The login is split in two because an MCP tool call cannot block on a human mid-call: the
host shows the URL to the user, the user signs in and copies the code, then the host calls
the second tool. Pending sessions expire after 15 minutes.

**Where the sandbox is** (per call, or via env): omit `warmpool` to talk to a local
`sandboxd` at `grpc` / `$SANDBOXD_GRPC_ADDR` (default `localhost:9090`); pass
`warmpool` (or set `$SANDBOX_WARMPOOL`) to claim a sandbox through the SDK's pod tunnel.
The claimed sandbox is terminated when the session ends.

## Run

```sh
cd tools/agentic-dev/devex-mcp
uv sync

# stdio (what MCP hosts spawn)
uv run devex-mcp

# streamable HTTP, e.g. for a remote host -> http://localhost:3400/mcp
uv run devex-mcp --transport streamable-http --port 3400
```

Register with Claude Code:

```sh
claude mcp add devex -- uv run --directory /ABS/PATH/tools/agentic-dev/devex-mcp devex-mcp
```

## Notes

- `sandboxd` resolves the command with the **daemon's** PATH, not the `PATH` passed as
  process env, so `claude` must be installed on the daemon's PATH in the sandbox image.
- `home` controls where the session is stored (`$HOME/.claude`), `config_dir` sets
  `CLAUDE_CONFIG_DIR` instead.
- Layout: `devex_mcp/sandbox.py` (PTY process wrapper + target resolution),
  `devex_mcp/claude_login.py` (two-phase login + session registry), `devex_mcp/server.py`
  (FastMCP tools and CLI).
