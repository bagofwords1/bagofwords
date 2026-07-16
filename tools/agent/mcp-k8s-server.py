#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2,<3"]
# ///
"""
MCP server for managing K8s test infrastructure (NATS, PostgreSQL).

Stdio (default):
    python3 tools/agent/mcp-k8s-server.py

SSE (network):
    python3 tools/agent/mcp-k8s-server.py --sse [--host 0.0.0.0] [--port 8080]
"""

import argparse
import asyncio
import logging
import os
import shlex

from mcp.server.mcpserver import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bow-k8s")

mcp = MCPServer("bow-k8s")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


async def _run(cmd: list[str], env_overrides: dict[str, str] | None = None,
               timeout: float = 300) -> str:
    cmd_str = " ".join(cmd)
    log.info("exec: %s", cmd_str)
    env = {**os.environ, **(env_overrides or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        log.error("timeout after %ss: %s", timeout, cmd_str)
        return f"Command timed out after {timeout}s"

    output = stdout.decode()
    if proc.returncode != 0:
        log.warning("exit %d: %s", proc.returncode, cmd_str)
        output += f"\nSTDERR:\n{stderr.decode()}"
        output += f"\nExit code: {proc.returncode}"
    else:
        log.info("ok: %s", cmd_str)
    return output


async def _run_script(script: str, args: list[str] | None = None,
                      env_overrides: dict[str, str] | None = None) -> str:
    cmd = ["bash", os.path.join(SCRIPT_DIR, script)] + (args or [])
    return await _run(cmd, env_overrides)


def _bow_runtime_env(namespace: str, app_name: str, image: str = "",
                 app_port: str = "", exposed_ports: str = "") -> dict[str, str]:
    """Env for deploy-bow-runtime.sh, omitting anything the caller left empty.

    An env override always beats the script's own default, so forwarding a
    blank would replace a working default with nothing.
    """
    env = {"NAMESPACE": namespace, "APP_NAME": app_name}
    for key, value in (("IMAGE", image), ("APP_PORT", app_port),
                       ("EXPOSED_PORTS", exposed_ports)):
        if value:
            env[key] = value
    return env


# ── NATS tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def nats_deploy(
    namespace: str = "bow-test",
    release_name: str = "nats",
    nats_token: str = "bow-test-token",
) -> str:
    """Deploy NATS server on Kubernetes for testing.

    Returns connection info (TCP and WSS endpoints, auth token).
    """
    log.info("tool:nats_deploy namespace=%s release=%s", namespace, release_name)
    return await _run_script("deploy-nats.sh", env_overrides={
        "NAMESPACE": namespace,
        "RELEASE_NAME": release_name,
        "NATS_TOKEN": nats_token,
    })


@mcp.tool()
async def nats_delete(
    namespace: str = "bow-test",
    release_name: str = "nats",
) -> str:
    """Delete the NATS server deployment from Kubernetes."""
    log.info("tool:nats_delete namespace=%s release=%s", namespace, release_name)
    return await _run_script("deploy-nats.sh", args=["--delete"], env_overrides={
        "NAMESPACE": namespace,
        "RELEASE_NAME": release_name,
    })


@mcp.tool()
async def nats_status(
    namespace: str = "bow-test",
    release_name: str = "nats",
) -> str:
    """Check status of the NATS server deployment (pods, services, helm release)."""
    log.info("tool:nats_status namespace=%s release=%s", namespace, release_name)
    return await _run_script("deploy-nats.sh", args=["--status"], env_overrides={
        "NAMESPACE": namespace,
        "RELEASE_NAME": release_name,
    })


# ── PostgreSQL tools ────────────────────────────────────────────────────────


@mcp.tool()
async def postgresql_deploy(
    namespace: str = "bow-test",
    release_name: str = "postgresql",
    pg_username: str = "lego",
    pg_password: str = "lego",
    pg_postgres_password: str = "lego",
    pg_database: str = "lego",
    pg_storage: str = "",
) -> str:
    """Deploy PostgreSQL on Kubernetes for testing (Bitnami chart).

    Defaults to a 'lego' database owned by 'lego'/'lego', seeded with the LEGO
    sample dataset. Override any pg_* argument to deploy something else.

    Returns the connection details for the deployed release.
    """
    log.info("tool:postgresql_deploy namespace=%s release=%s db=%s", namespace, release_name, pg_database or "(default)")

    # These defaults are declared here so the tool schema shows what a bare call
    # deploys, which means they are a second copy of the values in
    # deploy-postgresql.sh - an env override always beats the script's default,
    # so changing them there alone has no effect on this tool. Keep the two in
    # step. An argument left empty is not forwarded and does inherit the script.
    env = {"NAMESPACE": namespace, "RELEASE_NAME": release_name}
    for key, value in (
        ("PG_USERNAME", pg_username),
        ("PG_PASSWORD", pg_password),
        ("PG_POSTGRES_PASSWORD", pg_postgres_password),
        ("PG_DATABASE", pg_database),
        ("PG_STORAGE", pg_storage),
    ):
        if value:
            env[key] = value

    return await _run_script("deploy-postgresql.sh", env_overrides=env)


@mcp.tool()
async def postgresql_delete(
    namespace: str = "bow-test",
    release_name: str = "postgresql",
) -> str:
    """Delete the PostgreSQL deployment from Kubernetes.

    Destructive: the PVC goes with the release, so the database contents are
    gone permanently and a redeploy starts from an empty data directory. That
    is also what lets a redeploy pick up new credentials, since the chart only
    initialises the database and user on first boot against empty storage.
    """
    log.info("tool:postgresql_delete namespace=%s release=%s", namespace, release_name)
    return await _run_script("deploy-postgresql.sh", args=["--delete"], env_overrides={
        "NAMESPACE": namespace,
        "RELEASE_NAME": release_name,
    })


@mcp.tool()
async def postgresql_status(
    namespace: str = "bow-test",
    release_name: str = "postgresql",
) -> str:
    """Check status of the PostgreSQL deployment (pods, services, helm release)."""
    log.info("tool:postgresql_status namespace=%s release=%s", namespace, release_name)
    return await _run_script("deploy-postgresql.sh", args=["--status"], env_overrides={
        "NAMESPACE": namespace,
        "RELEASE_NAME": release_name,
    })


# ── Bow runtime pods ────────────────────────────────────────────────────────
#
# Two independent runtime pods, one per workload, each deployed from the same
# generic deploy-bow-runtime.sh (parameterised by APP_NAME) and each running the
# small upload server so its sources can be PUT in as a tar archive:
#
#   bow-runtime-app               -> tools/agent/boot_stack.sh --dev
#   bow-runtime-data-edge-agent   -> tools/agent/boot_data_edge_agent.sh
#
# Keeping them separate means the control plane and the edge agent restart,
# fail and get their sources updated independently.

APP_POD = "bow-runtime-app"
EDGE_POD = "bow-runtime-data-edge-agent"


async def _runtime_deploy(namespace, app_name, image, app_port, exposed_ports,
                          expose_external=False):
    env = _bow_runtime_env(namespace, app_name, image, app_port, exposed_ports)
    if expose_external:
        # Add a Gateway + HTTPRoute so the app is reachable from outside the
        # cluster (the app pod only; the edge agent needs no inbound access).
        env["EXPOSE_EXTERNAL"] = "1"
    return await _run_script("deploy-bow-runtime.sh", env_overrides=env)


async def _runtime_delete(namespace, app_name):
    return await _run_script("deploy-bow-runtime.sh", args=["--delete"],
                             env_overrides=_bow_runtime_env(namespace, app_name))


async def _runtime_status(namespace, app_name):
    return await _run_script("deploy-bow-runtime.sh", args=["--status"],
                             env_overrides=_bow_runtime_env(namespace, app_name))


async def _runtime_start(namespace, app_name, boot_script, boot_args, boot_log):
    # BOOT_* are set directly, not via _bow_runtime_env, because BOOT_ARGS must
    # be forwarded even when empty (boot_data_edge_agent.sh takes no arguments,
    # and the script would otherwise fall back to its '--dev' default).
    env = {
        "NAMESPACE": namespace,
        "APP_NAME": app_name,
        "BOOT_SCRIPT": boot_script,
        "BOOT_ARGS": boot_args,
        "BOOT_LOG": boot_log,
    }
    return await _run_script("deploy-bow-runtime.sh", args=["--start"], env_overrides=env)


# ── bow-runtime-app: the control plane (backend + frontend) ──────────────────


@mcp.tool()
async def bow_runtime_app_deploy(
    namespace: str = "bow-test",
    image: str = "",
    app_port: str = "",
    exposed_ports: str = "",
) -> str:
    """Deploy the bow-runtime-app pod on Kubernetes for testing.

    Runs the small upload server from a config map: PUT a tar archive to its
    upload URL and it is unpacked into /sandbox/app (new-or-changed files only);
    then start the app with bow_runtime_app_start. Publishes 8080, 3000 and 9191;
    9191 is the upload/HTTP listener.

    Also creates a Gateway + HTTPRoute (Gateway API) so the app is reachable from
    outside the cluster; the external URL is reported by bow_runtime_app_status
    once the app is started (the route targets the frontend on port 3000).

    Returns the endpoint and example curl commands.
    """
    log.info("tool:bow_runtime_app_deploy namespace=%s", namespace)
    return await _runtime_deploy(namespace, APP_POD, image, app_port, exposed_ports,
                                 expose_external=True)


@mcp.tool()
async def bow_runtime_app_start(
    namespace: str = "bow-test",
    boot_args: str = "--dev",
    boot_log: str = "/tmp/boot_stack.log",
) -> str:
    """Start the app (backend + frontend) inside the bow-runtime-app pod.

    kubectl execs into the pod and launches tools/agent/boot_stack.sh --dev from
    /sandbox/app, returning as soon as it is launched rather than waiting for it
    to exit, with output redirected to /tmp/boot_stack.log in the pod.

    Requires the pod deployed and its sources uploaded. Read the log with
    kubectl_proxy: "exec -n bow-test <pod> -- tail -100 /tmp/boot_stack.log".

    Returns the pod name, the launched pid and the log path.
    """
    log.info("tool:bow_runtime_app_start namespace=%s", namespace)
    return await _runtime_start(namespace, APP_POD, "tools/agent/boot_stack.sh",
                                boot_args, boot_log)


@mcp.tool()
async def bow_runtime_app_status(namespace: str = "bow-test") -> str:
    """Check status of the bow-runtime-app pod (deployment, pods, service, endpoint)."""
    log.info("tool:bow_runtime_app_status namespace=%s", namespace)
    return await _runtime_status(namespace, APP_POD)


@mcp.tool()
async def bow_runtime_app_delete(namespace: str = "bow-test") -> str:
    """Delete the bow-runtime-app deployment, service and config map.

    Uploads live in an emptyDir, so they go with the pod.
    """
    log.info("tool:bow_runtime_app_delete namespace=%s", namespace)
    return await _runtime_delete(namespace, APP_POD)


# ── bow-runtime-data-edge-agent: the data edge agent ─────────────────────────


@mcp.tool()
async def bow_runtime_data_edge_agent_deploy(
    namespace: str = "bow-test",
    image: str = "",
    app_port: str = "",
    exposed_ports: str = "",
) -> str:
    """Deploy the bow-runtime-data-edge-agent pod on Kubernetes for testing.

    Same upload server as bow-runtime-app, in its own pod: PUT a tar archive to
    its upload URL, then start the agent with bow_runtime_data_edge_agent_start.
    9191 is the upload listener.

    Returns the endpoint and example curl commands.
    """
    log.info("tool:bow_runtime_data_edge_agent_deploy namespace=%s", namespace)
    return await _runtime_deploy(namespace, EDGE_POD, image, app_port, exposed_ports)


@mcp.tool()
async def bow_runtime_data_edge_agent_start(
    namespace: str = "bow-test",
    boot_args: str = "",
    boot_log: str = "/tmp/boot_data_edge_agent.log",
) -> str:
    """Start the data edge agent inside the bow-runtime-data-edge-agent pod.

    kubectl execs into the pod and launches tools/agent/boot_data_edge_agent.sh
    from /sandbox/app - in-cluster by default, reaching NATS and PostgreSQL by
    their service DNS names. It returns as soon as the agent is launched, with
    output redirected to /tmp/boot_data_edge_agent.log in the pod.

    Requires the pod deployed and its sources uploaded. Read the log with
    kubectl_proxy: "exec -n bow-test <pod> -- tail -100 /tmp/boot_data_edge_agent.log".

    Returns the pod name, the launched pid and the log path.
    """
    log.info("tool:bow_runtime_data_edge_agent_start namespace=%s", namespace)
    return await _runtime_start(namespace, EDGE_POD,
                                "tools/agent/boot_data_edge_agent.sh", boot_args, boot_log)


@mcp.tool()
async def bow_runtime_data_edge_agent_status(namespace: str = "bow-test") -> str:
    """Check status of the bow-runtime-data-edge-agent pod (deployment, pods, service)."""
    log.info("tool:bow_runtime_data_edge_agent_status namespace=%s", namespace)
    return await _runtime_status(namespace, EDGE_POD)


@mcp.tool()
async def bow_runtime_data_edge_agent_delete(namespace: str = "bow-test") -> str:
    """Delete the bow-runtime-data-edge-agent deployment, service and config map."""
    log.info("tool:bow_runtime_data_edge_agent_delete namespace=%s", namespace)
    return await _runtime_delete(namespace, EDGE_POD)


# ── Combined infrastructure tools ──────────────────────────────────────────


@mcp.tool()
async def infra_deploy_all(
    namespace: str = "bow-test",
) -> str:
    """Deploy both NATS and PostgreSQL for a complete test environment.

    Returns connection info for both services.
    """
    log.info("tool:infra_deploy_all namespace=%s", namespace)
    nats_out = await nats_deploy(namespace=namespace)
    pg_out = await postgresql_deploy(namespace=namespace)
    return f"=== NATS ===\n{nats_out}\n\n=== PostgreSQL ===\n{pg_out}"


@mcp.tool()
async def infra_delete_all(
    namespace: str = "bow-test",
) -> str:
    """Delete both NATS and PostgreSQL deployments."""
    log.info("tool:infra_delete_all namespace=%s", namespace)
    nats_out = await nats_delete(namespace=namespace)
    pg_out = await postgresql_delete(namespace=namespace)
    return f"=== NATS ===\n{nats_out}\n\n=== PostgreSQL ===\n{pg_out}"


# ── Generic kubectl tools ──────────────────────────────────────────────────


@mcp.tool()
async def kubectl_proxy(command: str) -> str:
    """Run an arbitrary kubectl command and return its output.

    Pass it as you would type it, with or without the leading 'kubectl':
    "get pods -n bow-test", "kubectl describe pod nats-0 -n bow-test",
    "get svc nats -n bow-test -o yaml", "apply -f /path/manifest.yaml".

    Commands that stream or need a terminal are rejected, since this returns a
    single captured result: port-forward, proxy, attach, edit, 'logs -f',
    'get -w' and 'exec -i/-t'. Use 'logs --tail=N' for a log snapshot; a
    port-forward has to outlive a single tool call, so run it from a shell.
    """
    log.info("tool:kubectl_proxy command=%s", command)

    # shlex keeps a quoted argument (a jsonpath containing spaces, say) in one
    # piece without handing the string to a shell, so ';' or '$(...)' inside the
    # command reach kubectl as literal argument text instead of running.
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"Could not parse command: {e}"

    if not argv:
        return "No command given."

    return await _run_script("kubectl_proxy.sh", args=argv)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="bow-k8s MCP server")
    parser.add_argument("--sse", action="store_true", help="Run with SSE transport (network)")
    parser.add_argument("--host", default="0.0.0.0", help="SSE bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="SSE bind port (default: 8080)")
    args = parser.parse_args()

    if args.sse:
        log.info("starting SSE server on %s:%d", args.host, args.port)
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        log.info("starting stdio server")
        mcp.run(transport="stdio")
