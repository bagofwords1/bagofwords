"""devbox MCP server: developer-experience tools that act inside Agent Sandboxes.

Run over stdio (what `claude mcp add` / Claude Desktop expect)::

    uv run --directory tools/devbox-mcp devbox-mcp

or over streamable HTTP for a browser-side or remote client::

    uv run --directory tools/devbox-mcp devbox-mcp --transport streamable-http --port 3400
    # then point the client at http://localhost:3400/mcp
"""
from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import claude_login, devbox, github_login, remote_control, workspace
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


def _url_block(url: str) -> str:
    """A fenced bash block holding only the URL, as plain text.

    Chat UIs (claude.ai, the desktop app) render a bare URL as a link and
    truncate the visible text, which breaks copy/paste of long OAuth URLs.
    Inside a code block the full string is shown verbatim and copyable.
    Any explanation belongs in the surrounding chat message, not in the block.
    """
    return f"```bash\n{url}\n```"


def _remote_control_steps(result: dict, claim_name: str) -> list[str]:
    """next_steps for a remote_control.start() result (also sets url_block when ready)."""
    if result["state"] == "ready":
        result["url_block"] = _url_block(result["url"])
        steps = [
            f"Remote Control is running. Tell the user in plain text to open this URL on claude.ai/code "
            f"(or use the Claude mobile app) to start a session named '{result['session_name']}' in this "
            f"devbox, then show `url_block` verbatim as a fenced code block containing only the URL."
        ]
        mcp_info = result.get("devbox_mcp", {})
        if mcp_info.get("registered"):
            steps.append(f"The '{mcp_info['server_name']}' MCP server is registered in that devbox, so remote "
                         f"sessions can manage devboxes too.")
        elif mcp_info.get("reason") != "disabled":
            steps.append("Registering the devbox MCP server inside the devbox failed: "
                         f"{mcp_info.get('output') or mcp_info.get('reason')}.")
        return steps
    if result["state"] == "starting":
        return [f"Remote Control is still starting; call devbox_remote_control_status(claim_name={claim_name!r}) in a few seconds."]
    return ["Remote Control failed to start. If the log mentions sign-in, run devbox_claude_login first. "
            "Otherwise show the user the log_tail."]


def _login_state(claim_name: str, namespace: str | None, home: str) -> bool | None:
    """True/False when the devbox is reachable, None when it is not Ready yet."""
    try:
        return claude_login.status(_target(claim_name, namespace), home=home)["logged_in"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Devboxes
# ---------------------------------------------------------------------------
@mcp.tool(name="devbox_create")
def create_devbox_tool(
    name: str,
    git_user_name: str | None = None,
    git_user_email: str | None = None,
    namespace: str | None = None,
    warmpool: str | None = None,
    wait_ready_seconds: int = 90,
    dry_run: bool = False,
) -> dict:
    """Create a new Bag of Words (bow) devbox and expose it through the Gateway.

    Ask the user for a short name for the devbox first if they did not give one.

    A random 4-character suffix is appended to make the claim name unique, e.g.
    name "alice" -> claim "alice-k7x2". That claim name identifies the devbox in
    every other tool (devbox_claude_login, devbox_status, devbox_delete).

    The devbox is adopted from the warm pool and starts on the branch the cluster's
    SandboxTemplate sets (BOW_REPO_REF, normally `main`). Switching branches is done inside the devbox by the agent in a Remote Control
    session (git checkout; `bow-stack reset-db` if the branch's migrations are older
    than the database's), not through this server. Pushing from the devbox needs the
    user's own GitHub sign-in: offer devbox_github_login afterwards (it also sets the
    commit identity from their GitHub profile). The git_user_* arguments only preset a
    commit identity.

    Applies four objects: the SandboxClaim (adopts a warm-pool sandbox), a Service
    selecting the pod by label with ports 3000 and 8080, and two HTTPRoutes:

        app_url          https://app-<claim>.<domain>          Bag of Words
        code_server_url  https://code-server-<claim>.<domain>  code-server IDE

    Relay `next_steps` to the user verbatim; it says whether the devbox is ready
    and whether Claude Code inside it still needs a sign-in (devbox_claude_login).

    Args:
        name: user-chosen name; lower-cased, non-alphanumerics become '-'.
        git_user_name: commit author name inside the devbox (optional preset).
        git_user_email: commit author email inside the devbox (optional preset).
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
    admin = devbox.admin_credentials(env.namespace)
    result["bow_login"] = {"email": admin["email"], "password": admin["password"]}
    result["next_steps"] = devbox.next_steps(result["claim_name"], hosts, ready, logged_in, admin)

    target = _target(result["claim_name"], env.namespace)
    if ready and (git_user_name or git_user_email):
        try:
            result["git"] = workspace.configure_git(target, user_name=git_user_name, user_email=git_user_email)
        except Exception as e:
            result["git"] = {"changed": False, "error": str(e)}
    if ready:
        git = workspace.current_branch(target)
        result["git"] = {**result.get("git", {}), **{k: v for k, v in git.items() if k != "error"}}
        branch = git.get("branch")
        result["next_steps"].append(
            f"The devbox is on branch '{branch}' at {git.get('commit', '?')}." if branch else
            "The devbox's branch could not be read yet.")
    result["next_steps"].append("Branch switching happens inside the devbox (Remote Control session), not through this server.")
    result["next_steps"].append(
        f"To commit and push from this devbox the user must sign in to GitHub inside it: offer "
        f"devbox_github_login(claim_name={result['claim_name']!r}) (one-time code + URL, no secrets in chat).")
    return result


@mcp.tool(name="devbox_git_config")
def git_config_tool(claim_name: str, git_user_name: str | None = None, git_user_email: str | None = None,
                    namespace: str | None = None) -> dict:
    """Set the git commit identity (user.name / user.email) inside a Bag of Words (bow) devbox.

    Push credentials are not set here: they are per user and come from
    devbox_github_login, which also sets the identity from the GitHub profile.
    """
    return workspace.configure_git(_target(claim_name, namespace), user_name=git_user_name, user_email=git_user_email)


# ---------------------------------------------------------------------------
# GitHub sign-in inside a devbox (per user, device flow)
# ---------------------------------------------------------------------------
@mcp.tool(name="devbox_github_login")
def github_login_tool(claim_name: str, namespace: str | None = None, home: str = "/root") -> dict:
    """Sign the user in to GitHub inside a Bag of Words (bow) devbox so they can push.

    Starts `gh auth login` (GitHub device flow) inside the devbox and returns a
    one-time `code` and the `url` https://github.com/login/device. Tell the user in
    plain text to open the URL, enter the code, and approve; show the code and the
    `url_block` verbatim (fenced code block, URL only). gh waits inside the devbox for
    up to 15 minutes and completes by itself. Afterwards call
    devbox_github_login_status, which wires git to the token (`gh auth setup-git`) and
    sets user.name/email from the GitHub profile. The token never leaves the devbox.
    """
    r = github_login.start(_target(claim_name, namespace), home=home)
    if r["state"] == "waiting_for_user":
        r["url_block"] = _url_block(r["url"])
        r["next_step"] = (
            f"Tell the user: open the URL below, enter the one-time code {r['code']}, and approve "
            f"access for the GitHub CLI; then call devbox_github_login_status(claim_name={claim_name!r}). "
            f"Show `url_block` verbatim as a fenced code block containing only the URL."
        )
    else:
        r["next_step"] = "gh did not produce a device code; show the user the log_tail."
    return r


@mcp.tool(name="devbox_github_login_status")
def github_login_status_tool(claim_name: str, namespace: str | None = None, home: str = "/root") -> dict:
    """Whether the devbox is signed in to GitHub; finalises git on first success.

    Returns `logged_in`, `github_login`, whether the login process is still waiting,
    and after success `git_identity` as configured for commits.
    """
    r = github_login.status(_target(claim_name, namespace), home=home)
    if r["logged_in"]:
        r["next_steps"] = [f"GitHub sign-in done as {r.get('github_login')}; git in the devbox commits as "
                           f"{r.get('git_identity')} and can push over HTTPS."]
    elif r["login_process"] == "running":
        r["next_steps"] = ["Not signed in yet; the device-code login is still waiting for the user. Call again after they approve."]
    else:
        r["next_steps"] = [f"Not signed in and no login in progress. Call devbox_github_login(claim_name={claim_name!r})."]
    return r


@mcp.tool(name="devbox_github_login_cancel")
def github_login_cancel_tool(claim_name: str, namespace: str | None = None) -> dict:
    """Abort a pending devbox_github_login in a devbox."""
    return github_login.cancel(_target(claim_name, namespace))


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
    admin = devbox.admin_credentials(env.namespace)
    logged_in = None
    login: dict = {}
    if st.get("ready"):
        try:
            login = claude_login.status(_target(claim_name, namespace), home=home)
            logged_in = login["logged_in"]
        except Exception as e:  # sandboxd unreachable, pod restarting, ...
            login = {"error": str(e)}
    git = workspace.current_branch(_target(claim_name, namespace)) if st.get("ready") else {}
    gh = {}
    if st.get("ready"):
        try:
            gh = github_login.status(_target(claim_name, namespace), finalize=False)
            gh = {k: gh.get(k) for k in ("gh_installed", "logged_in", "github_login", "login_process")}
        except Exception as e:
            gh = {"error": str(e)}
    return {
        "claim_name": claim_name, "namespace": env.namespace, **st, "git": git, "github": gh,
        "app_url": f"https://{hosts['app']}", "code_server_url": f"https://{hosts['code_server']}",
        "claude_logged_in": logged_in, "claude": login,
        "bow_login": {"email": admin["email"], "password": admin["password"]},
        "next_steps": devbox.next_steps(claim_name, hosts, bool(st.get("ready")), logged_in, admin),
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
    devbox_list). The sandbox must be Ready. Show the returned `url_block` to the user verbatim (a fenced code block, so the long URL is not truncated into a link),
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
        "url_block": _url_block(session.url),
        "next_step": (
            "Tell the user in plain text: 'Here is your Claude sign-in URL, copy the whole line into "
            "your browser', then show `url_block` VERBATIM as a fenced code block containing only the "
            "URL (do not turn it into a link or shorten it: chat clients truncate long URLs). Ask them "
            "to sign in, copy the code the page shows, then call "
            "devbox_claude_login_submit_code(session_id, code)."
        ),
    }


@mcp.tool(name="devbox_claude_login_submit_code")
def claude_login_submit_code_tool(session_id: str, code: str) -> dict:
    """Finish a `devbox_claude_login` session by typing the code from the browser into the devbox.

    On success it immediately starts Claude Code Remote Control in that devbox (with the
    devbox MCP server registered inside it) and returns the claude.ai/code URL under
    `remote_control`. Returns `success`, `exit_code`, `output`, `remote_control` and
    `next_steps`; relay `next_steps` to the user.
    """
    result = claude_login.submit_code(session_id, code)
    if not result.get("success"):
        return result
    claim, ns = result.get("claim_name"), result.get("namespace")
    result["next_steps"] = ["Claude Code in the devbox is now signed in."]
    try:
        rc = remote_control.start(SandboxTarget(claim_name=claim, namespace=ns))
    except Exception as e:  # login succeeded; report the follow-up failure instead of hiding the success
        result["remote_control"] = {"state": "failed", "error": str(e)}
        result["next_steps"].append(
            f"Starting Remote Control failed: {e}. Retry with devbox_remote_control_start(claim_name={claim!r}).")
        return result
    result["remote_control"] = rc
    result["next_steps"] += _remote_control_steps(rc, claim)
    result["next_steps"].append(
        f"Ask the user whether to also sign in to GitHub inside the devbox so Claude can push: "
        f"devbox_github_login(claim_name={claim!r}).")
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
    permission_mode: str | None = None,
    register_devbox_mcp: bool = True,
    wait_seconds: int = 60,
) -> dict:
    """Start Claude Code Remote Control inside a Bag of Words (bow) devbox so the user can drive it from
    claude.ai/code or the Claude mobile app.

    Normally not needed: devbox_claude_login_submit_code starts it automatically after a
    successful sign-in. Use this to restart it or to start it for a devbox that was
    signed in earlier. Claude Code in the devbox must already be signed in.

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
        permission_mode: Claude Code permission mode for the sessions: default,
            acceptEdits, auto, dontAsk, bypassPermissions or plan. Unset uses the
            server's DEVBOX_REMOTE_CONTROL_PERMISSION_MODE (default "default", which
            prompts in the claude.ai UI for each non-allowlisted action).
        register_devbox_mcp: register the devbox MCP server (in-cluster URL, no API key)
            in the devbox's Claude Code before launching, so remote sessions have the
            devbox tools (default true).
        wait_seconds: how long to wait for Ready before returning `starting`.
    """
    result = remote_control.start(
        _target(claim_name, namespace), name=session_name, cwd=cwd, home=home, mode=mode,
        permission_mode=permission_mode, register_devbox_mcp=register_devbox_mcp,
        wait_seconds=wait_seconds,
    )
    result["next_steps"] = _remote_control_steps(result, claim_name)
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
