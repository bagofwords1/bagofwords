"""devex MCP server: developer-experience tools that act inside Agent Sandboxes.

Run over stdio (what `claude mcp add` / Claude Desktop expect)::

    uv run --directory tools/agentic-dev/devex-mcp devex-mcp

or over streamable HTTP for a browser-side or remote client::

    uv run --directory tools/agentic-dev/devex-mcp devex-mcp --transport streamable-http --port 3400
    # then point the client at http://localhost:3400/mcp
"""
from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP

from . import claude_login
from .sandbox import SandboxTarget

mcp = FastMCP("devex")


def _target(grpc: str | None, warmpool: str | None, namespace: str) -> SandboxTarget:
    return SandboxTarget(
        grpc=grpc or os.environ.get("SANDBOXD_GRPC_ADDR", "localhost:9090"),
        warmpool=warmpool or os.environ.get("SANDBOX_WARMPOOL") or None,
        namespace=namespace,
    )


@mcp.tool(name="claude_login")
def claude_login_tool(
    warmpool: str | None = None,
    namespace: str = "default",
    grpc: str | None = None,
    home: str = "/root",
    config_dir: str | None = None,
    email: str | None = None,
    console: bool = False,
) -> dict:
    """Start `claude auth login` inside a sandbox and return the sign-in URL.

    Show the returned `url` to the user and ask them to open it, sign in, and copy the
    code the page shows. Then call `claude_login_submit_code` with the `session_id` and
    that code to finish. The sandbox process waits for the code (up to 15 minutes).

    Args:
        warmpool: SandboxWarmPool to claim a sandbox from (via the SDK's pod tunnel).
            Omit to talk to a local sandboxd at `grpc` ($SANDBOXD_GRPC_ADDR).
        namespace: Kubernetes namespace of the warm pool.
        grpc: sandboxd gRPC address for local mode (default localhost:9090).
        home: HOME inside the sandbox; the session lands in $HOME/.claude.
        config_dir: sets CLAUDE_CONFIG_DIR inside the sandbox instead.
        email: pre-populate the email on the login page.
        console: log in to the Anthropic Console (API billing) instead of a subscription.
    """
    session = claude_login.start(
        _target(grpc, warmpool, namespace),
        home=home, config_dir=config_dir, email=email, console=console,
    )
    return {
        "session_id": session.id,
        "url": session.url,
        "next_step": "Ask the user to open the URL, sign in, copy the code shown, then call "
                     "claude_login_submit_code(session_id, code).",
    }


@mcp.tool(name="claude_login_submit_code")
def claude_login_submit_code_tool(session_id: str, code: str) -> dict:
    """Finish a `claude_login` session by typing the code from the browser into the sandbox.

    Returns `success`, the process `exit_code`, and the cleaned terminal `output`.
    """
    return claude_login.submit_code(session_id, code)


@mcp.tool(name="claude_login_cancel")
def claude_login_cancel_tool(session_id: str) -> dict:
    """Abort a pending `claude_login` session and release its sandbox."""
    return {"cancelled": claude_login.cancel(session_id)}


@mcp.tool(name="claude_login_pending")
def claude_login_pending_tool() -> list[dict]:
    """List `claude_login` sessions still waiting for a code."""
    return claude_login.pending()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3400)
    args = parser.parse_args()

    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
