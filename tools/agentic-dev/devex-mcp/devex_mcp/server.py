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
from mcp.server.transport_security import TransportSecuritySettings

from . import claude_login, sandbox_claim
from .sandbox import SandboxTarget

mcp = FastMCP("devex")


def _env(namespace: str | None = None, warmpool: str | None = None) -> sandbox_claim.SandboxEnv:
    return sandbox_claim.SandboxEnv(
        **({"namespace": namespace} if namespace else {}),
        **({"warmpool": warmpool} if warmpool else {}),
    )


def _target(claim_name: str, namespace: str | None) -> SandboxTarget:
    return SandboxTarget(claim_name=claim_name, namespace=_env(namespace).namespace)


def _login_state(claim_name: str, namespace: str | None, home: str) -> bool | None:
    """True/False when the sandbox is reachable, None when it is not Ready yet."""
    try:
        return claude_login.status(_target(claim_name, namespace), home=home)["logged_in"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------
@mcp.tool(name="create_sandbox")
def create_sandbox_tool(
    name: str,
    namespace: str | None = None,
    warmpool: str | None = None,
    wait_ready_seconds: int = 90,
    dry_run: bool = False,
) -> dict:
    """Create a new Bag of Words sandbox and expose it through the Gateway.

    Ask the user for a short name for the sandbox first if they did not give one.
    A random 4-character suffix is appended to make the claim name unique, e.g.
    name "alice" -> claim "alice-k7x2". That claim name identifies the sandbox in
    every other tool (claude_login, sandbox_status, delete_sandbox).

    Applies four objects: the SandboxClaim (adopts a warm-pool sandbox), a Service
    selecting the pod by label with ports 3000 and 8080, and two HTTPRoutes:

        app_url          https://app-<claim>.<domain>          Bag of Words
        code_server_url  https://code-server-<claim>.<domain>  code-server IDE

    Relay `next_steps` to the user verbatim; it says whether the sandbox is ready
    and whether Claude Code inside it still needs a sign-in (claude_login).

    Args:
        name: user-chosen name; lower-cased, non-alphanumerics become '-'.
        namespace: Kubernetes namespace (default $SANDBOX_NAMESPACE or "default").
        warmpool: SandboxWarmPool to claim from (default $SANDBOX_WARMPOOL or "bow-warmpool").
        wait_ready_seconds: wait up to this long for the claim to be Ready (0 = return at once).
        dry_run: build and return the manifests without applying them.
    """
    env = _env(namespace, warmpool)
    result = sandbox_claim.create_sandbox(name, env=env, wait_ready_seconds=wait_ready_seconds, dry_run=dry_run)
    if dry_run:
        return result
    ready = bool(result.get("status", {}).get("ready"))
    logged_in = _login_state(result["claim_name"], env.namespace, "/root") if ready else None
    result["claude_logged_in"] = logged_in
    hosts = sandbox_claim.hostnames(result["claim_name"], env.domain)
    result["next_steps"] = sandbox_claim.next_steps(result["claim_name"], hosts, ready, logged_in)
    return result


@mcp.tool(name="list_sandboxes")
def list_sandboxes_tool(namespace: str | None = None, check_login: bool = True) -> list[dict]:
    """List sandboxes created by create_sandbox, newest last.

    Each entry has `claim_name` (the identifier other tools take), `ready`, the
    bound `sandbox` pod name, `app_url`, `code_server_url`, and, when
    `check_login` is true and the sandbox is Ready, `claude_logged_in`.
    Use this to find the right claim_name when the user refers to an existing
    sandbox, or to tell them none exist yet.
    """
    env = _env(namespace)
    items = sandbox_claim.list_sandboxes(env.namespace, env.domain)
    if check_login:
        for it in items:
            it["claude_logged_in"] = _login_state(it["claim_name"], env.namespace, "/root") if it["ready"] else None
    return items


@mcp.tool(name="sandbox_status")
def sandbox_status_tool(claim_name: str, namespace: str | None = None, home: str = "/root") -> dict:
    """Readiness of a sandbox and whether Claude Code inside it is signed in.

    Returns the claim's Ready condition, the bound sandbox pod, both URLs,
    `claude_logged_in`, and `next_steps` to relay to the user.
    """
    env = _env(namespace)
    st = sandbox_claim.claim_status(claim_name, env.namespace)
    if not st.get("exists"):
        return {"claim_name": claim_name, "exists": False,
                "next_steps": ["No such sandbox. Call list_sandboxes to see existing ones or create_sandbox."]}
    hosts = sandbox_claim.hostnames(claim_name, env.domain)
    logged_in = None
    login: dict = {}
    if st.get("ready"):
        try:
            login = claude_login.status(_target(claim_name, namespace), home=home)
            logged_in = login["logged_in"]
        except Exception as e:  # sandboxd unreachable, pod restarting, ...
            login = {"error": str(e)}
    return {
        "claim_name": claim_name, "namespace": env.namespace, **st,
        "app_url": f"https://{hosts['app']}", "code_server_url": f"https://{hosts['code_server']}",
        "claude_logged_in": logged_in, "claude": login,
        "next_steps": sandbox_claim.next_steps(claim_name, hosts, bool(st.get("ready")), logged_in),
    }


@mcp.tool(name="delete_sandbox")
def delete_sandbox_tool(claim_name: str, namespace: str | None = None) -> dict:
    """Delete a sandbox created by `create_sandbox`: its claim, Service and HTTPRoutes.

    The Sandbox pod and its persistent volume are garbage-collected with the claim,
    so the sandbox's data is lost. Confirm with the user before calling this.
    """
    return sandbox_claim.delete_sandbox(claim_name, namespace=_env(namespace).namespace)


# ---------------------------------------------------------------------------
# Claude Code sign-in inside a sandbox
# ---------------------------------------------------------------------------
@mcp.tool(name="claude_login")
def claude_login_tool(
    claim_name: str,
    namespace: str | None = None,
    home: str = "/root",
    config_dir: str | None = None,
    email: str | None = None,
    console: bool = False,
) -> dict:
    """Start `claude auth login` inside an existing sandbox and return the sign-in URL.

    `claim_name` is the sandbox identifier returned by create_sandbox (or found via
    list_sandboxes). The sandbox must be Ready. Show the returned `url` to the user
    and ask them to open it, sign in, and copy the code the page shows. Then call
    `claude_login_submit_code` with the `session_id` and that code to finish. The
    login process waits inside the sandbox for up to 15 minutes. Credentials are
    stored in the sandbox ($HOME/.claude) and persist with it.

    Args:
        claim_name: the sandbox's claim name, e.g. "alice-k7x2".
        namespace: Kubernetes namespace of the claim.
        home: HOME inside the sandbox; the session lands in $HOME/.claude.
        config_dir: sets CLAUDE_CONFIG_DIR inside the sandbox instead.
        email: pre-populate the email on the login page.
        console: log in to the Anthropic Console (API billing) instead of a subscription.
    """
    session = claude_login.start(
        _target(claim_name, namespace), home=home, config_dir=config_dir, email=email, console=console,
    )
    return {
        "session_id": session.id,
        "claim_name": claim_name,
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
    """Abort a pending `claude_login` session (the sandbox itself is left running)."""
    return {"cancelled": claude_login.cancel(session_id)}


@mcp.tool(name="claude_login_pending")
def claude_login_pending_tool() -> list[dict]:
    """List `claude_login` sessions still waiting for a code."""
    return claude_login.pending()


def _transport_security(bind_host: str, allowed_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection matching how the server is actually reached.

    FastMCP("devex") is built at import time with the default bind host 127.0.0.1,
    which pins the allowlist to localhost. Changing settings.host later does not
    update that allowlist, so a server behind a public hostname would answer every
    request with 421. Rebuild it here from the real bind host and --allowed-host.
    """
    loopback = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    loopback_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if allowed_hosts:
        # The "host:*" pattern only matches a Host header that carries a port;
        # browsers and proxies send the bare name on 80/443, so list both forms.
        hosts, origins = list(loopback), list(loopback_origins)
        for h in allowed_hosts:
            hosts += [h, f"{h}:*"]
            origins += [f"https://{h}", f"https://{h}:*", f"http://{h}", f"http://{h}:*"]
        return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=hosts, allowed_origins=origins)
    if bind_host in ("127.0.0.1", "localhost", "::1"):
        return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=loopback, allowed_origins=loopback_origins)
    # Bound on a non-loopback interface with no allowlist: accept any Host.
    # Put authentication in front (e.g. a Gateway SecurityPolicy) in that case.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (use 0.0.0.0 in a container)")
    parser.add_argument("--port", type=int, default=3400)
    parser.add_argument(
        "--allowed-host", action="append", default=[], metavar="HOST",
        help="public hostname clients use to reach this server (repeatable), e.g. "
             "mcp.sndbx.bagofwords.com. Enables Host/Origin validation for those names. "
             "Also read from $MCP_ALLOWED_HOSTS (comma-separated).",
    )
    args = parser.parse_args()

    if args.transport != "stdio":
        allowed = args.allowed_host + [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.transport_security = _transport_security(args.host, allowed)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
