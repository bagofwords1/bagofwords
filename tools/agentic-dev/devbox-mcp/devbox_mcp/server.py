"""devbox MCP server: developer-experience tools that act inside Agent Sandboxes.

Run over stdio (what `claude mcp add` / Claude Desktop expect)::

    uv run --directory tools/agentic-dev/devbox-mcp devbox-mcp

or over streamable HTTP for a browser-side or remote client::

    uv run --directory tools/agentic-dev/devbox-mcp devbox-mcp --transport streamable-http --port 3400
    # then point the client at http://localhost:3400/mcp
"""
from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import claude_login, devbox, remote_control
from .sandbox import SandboxTarget

mcp = FastMCP(
    "devbox",
    instructions=(
        "Bag of Words (bow) devbox: personal, remote development environments for Bag of Words "
        "engineers, each a Kubernetes sandbox running the bagofwords app, code-server and Claude Code. "
        "Use these tools when the user talks about a devbox, bow devbox or bagofwords devbox: create one, "
        "list or check them, sign Claude Code in inside one, start Remote Control, or delete one. "
        "A devbox is identified by its claim_name (e.g. alice-k7x2), returned by devbox_create."
    ),
)


def _env(namespace: str | None = None, warmpool: str | None = None) -> devbox.SandboxEnv:
    return devbox.SandboxEnv(
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
@mcp.tool(name="devbox_create")
def create_devbox_tool(
    name: str,
    namespace: str | None = None,
    warmpool: str | None = None,
    wait_ready_seconds: int = 90,
    dry_run: bool = False,
) -> dict:
    """Create a new Bag of Words (bow) devbox and expose it through the Gateway.

    Ask the user for a short name for the devbox first if they did not give one.
    A random 4-character suffix is appended to make the claim name unique, e.g.
    name "alice" -> claim "alice-k7x2". That claim name identifies the sandbox in
    every other tool (devbox_claude_login, devbox_status, devbox_delete).

    Applies four objects: the SandboxClaim (adopts a warm-pool sandbox), a Service
    selecting the pod by label with ports 3000 and 8080, and two HTTPRoutes:

        app_url          https://app-<claim>.<domain>          Bag of Words
        code_server_url  https://code-server-<claim>.<domain>  code-server IDE

    Relay `next_steps` to the user verbatim; it says whether the sandbox is ready
    and whether Claude Code inside it still needs a sign-in (devbox_claude_login).

    Args:
        name: user-chosen name; lower-cased, non-alphanumerics become '-'.
        namespace: Kubernetes namespace (default $DEVBOX_NAMESPACE or "default").
        warmpool: SandboxWarmPool to claim from (default $DEVBOX_WARMPOOL or "bow-warmpool").
        wait_ready_seconds: wait up to this long for the claim to be Ready (0 = return at once).
        dry_run: build and return the manifests without applying them.
    """
    env = _env(namespace, warmpool)
    result = devbox.create_devbox(name, env=env, wait_ready_seconds=wait_ready_seconds, dry_run=dry_run)
    if dry_run:
        return result
    ready = bool(result.get("status", {}).get("ready"))
    logged_in = _login_state(result["claim_name"], env.namespace, "/root") if ready else None
    result["claude_logged_in"] = logged_in
    hosts = devbox.hostnames(result["claim_name"], env.domain)
    result["next_steps"] = devbox.next_steps(result["claim_name"], hosts, ready, logged_in)
    return result


@mcp.tool(name="devbox_list")
def list_devboxes_tool(namespace: str | None = None, check_login: bool = True) -> list[dict]:
    """List sandboxes created by devbox_create, newest last.

    Each entry has `claim_name` (the identifier other tools take), `ready`, the
    bound `sandbox` pod name, `app_url`, `code_server_url`, and, when
    `check_login` is true and the sandbox is Ready, `claude_logged_in`.
    Use this to find the right claim_name when the user refers to an existing
    sandbox, or to tell them none exist yet.
    """
    env = _env(namespace)
    items = devbox.list_devboxes(env.namespace, env.domain)
    if check_login:
        for it in items:
            it["claude_logged_in"] = _login_state(it["claim_name"], env.namespace, "/root") if it["ready"] else None
    return items


@mcp.tool(name="devbox_status")
def sandbox_status_tool(claim_name: str, namespace: str | None = None, home: str = "/root") -> dict:
    """Readiness of a Bag of Words (bow) devbox and whether Claude Code inside it is signed in.

    Returns the claim's Ready condition, the bound sandbox pod, both URLs,
    `claude_logged_in`, and `next_steps` to relay to the user.
    """
    env = _env(namespace)
    st = devbox.claim_status(claim_name, env.namespace)
    if not st.get("exists"):
        return {"claim_name": claim_name, "exists": False,
                "next_steps": ["No such sandbox. Call devbox_list to see existing ones or devbox_create."]}
    hosts = devbox.hostnames(claim_name, env.domain)
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
        "next_steps": devbox.next_steps(claim_name, hosts, bool(st.get("ready")), logged_in),
    }


@mcp.tool(name="devbox_delete")
def delete_devbox_tool(claim_name: str, namespace: str | None = None) -> dict:
    """Delete a Bag of Words (bow) devbox: its claim, Service and HTTPRoutes.

    The Sandbox pod and its persistent volume are garbage-collected with the claim,
    so the sandbox's data is lost. Confirm with the user before calling this.
    """
    return devbox.delete_devbox(claim_name, namespace=_env(namespace).namespace)


# ---------------------------------------------------------------------------
# Claude Code sign-in inside a sandbox
# ---------------------------------------------------------------------------
@mcp.tool(name="devbox_claude_login")
def claude_login_tool(
    claim_name: str,
    namespace: str | None = None,
    home: str = "/root",
    config_dir: str | None = None,
    email: str | None = None,
    console: bool = False,
) -> dict:
    """Sign Claude Code in inside a Bag of Words (bow) devbox: start `claude auth login` and return the sign-in URL.

    `claim_name` is the sandbox identifier returned by devbox_create (or found via
    devbox_list). The sandbox must be Ready. Show the returned `url` to the user
    and ask them to open it, sign in, and copy the code the page shows. Then call
    `devbox_claude_login_submit_code` with the `session_id` and that code to finish. The
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
                     "devbox_claude_login_submit_code(session_id, code).",
    }


@mcp.tool(name="devbox_claude_login_submit_code")
def claude_login_submit_code_tool(session_id: str, code: str) -> dict:
    """Finish a `devbox_claude_login` session by typing the code from the browser into the devbox.

    Returns `success`, the process `exit_code`, and the cleaned terminal `output`.
    """
    result = claude_login.submit_code(session_id, code)
    if result.get("success"):
        claim = result.get("claim_name")
        result["next_steps"] = [
            "Claude Code in the sandbox is now signed in.",
            f"Ask the user: 'Do you want to start Remote Control for this sandbox, so you can drive it "
            f"from claude.ai/code or the mobile app?' If yes, call "
            f"devbox_remote_control_start(claim_name={claim!r}) and give the user the returned url.",
        ]
    return result


@mcp.tool(name="devbox_claude_login_cancel")
def claude_login_cancel_tool(session_id: str) -> dict:
    """Abort a pending `devbox_claude_login` session (the devbox itself is left running)."""
    return {"cancelled": claude_login.cancel(session_id)}


@mcp.tool(name="devbox_claude_login_pending")
def claude_login_pending_tool() -> list[dict]:
    """List `devbox_claude_login` sessions still waiting for a code."""
    return claude_login.pending()


# ---------------------------------------------------------------------------
# Claude Code Remote Control inside a sandbox
# ---------------------------------------------------------------------------
@mcp.tool(name="devbox_remote_control_start")
def claude_remote_control_start_tool(
    claim_name: str,
    session_name: str | None = None,
    namespace: str | None = None,
    cwd: str = remote_control.DEFAULT_CWD,
    home: str = "/root",
    mode: str = "daemon",
    wait_seconds: int = 60,
) -> dict:
    """Start Claude Code Remote Control inside a Bag of Words (bow) devbox so the user can drive it from
    claude.ai/code or the Claude mobile app.

    Only call this after the user agreed to start Remote Control (offer it right after
    a successful devbox_claude_login). Claude Code in the sandbox must already be signed in.

    Runs `claude remote-control --name <session_name>` detached in the sandbox (it
    keeps running after this call) and waits until it reports Ready. Returns
    `state` (ready | starting | failed), the `url` to open on claude.ai, and the last
    log lines. On `starting`, call devbox_remote_control_status a little later.

    Args:
        claim_name: the sandbox's claim name.
        session_name: name shown in claude.ai/code (default: the claim name).
        namespace: Kubernetes namespace of the claim.
        cwd: directory the session works in (default: the sandbox's source checkout).
        home: HOME inside the sandbox (where the claude sign-in lives).
        mode: "daemon" runs `claude remote-control --name`; "interactive" runs
            `claude --remote-control <name>` (a full interactive session) instead.
        wait_seconds: how long to wait for Ready before returning `starting`.
    """
    result = remote_control.start(
        _target(claim_name, namespace), name=session_name, cwd=cwd, home=home, mode=mode,
        wait_seconds=wait_seconds,
    )
    if result["state"] == "ready":
        result["next_steps"] = [
            f"Remote Control is running. Tell the user to open {result['url']} "
            f"(or the Claude mobile app) to start a session named '{result['session_name']}' in this sandbox."
        ]
    elif result["state"] == "starting":
        result["next_steps"] = [f"Still starting; call devbox_remote_control_status(claim_name={claim_name!r}) in a few seconds."]
    else:
        result["next_steps"] = [
            "Remote Control failed to start. If the log mentions sign-in, run devbox_claude_login first. "
            "Otherwise show the user the log_tail."
        ]
    return result


@mcp.tool(name="devbox_remote_control_status")
def claude_remote_control_status_tool(claim_name: str, namespace: str | None = None, home: str = "/root") -> dict:
    """Whether Remote Control is running in a Bag of Words (bow) devbox, and its claude.ai URL if ready."""
    return remote_control.status(_target(claim_name, namespace), home=home)


@mcp.tool(name="devbox_remote_control_stop")
def claude_remote_control_stop_tool(claim_name: str, namespace: str | None = None, home: str = "/root") -> dict:
    """Stop the Remote Control daemon in a Bag of Words (bow) devbox. Confirm with the user first."""
    return remote_control.stop(_target(claim_name, namespace), home=home)


def _transport_security(bind_host: str, allowed_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection matching how the server is actually reached.

    FastMCP("devbox") is built at import time with the default bind host 127.0.0.1,
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
             "devbox.sndbx.bagofwords.com. Enables Host/Origin validation for those names. "
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
