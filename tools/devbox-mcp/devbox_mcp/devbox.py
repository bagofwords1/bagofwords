"""Create a Bag of Words (bow) devbox: a SandboxClaim plus the Service and HTTPRoutes
that expose it through the Envoy Gateway.

Mirrors devex/agent-sandbox/sandbox-claim.yaml (a devbox is a SandboxClaim plus its routes). The claim name is the user's
name plus a random 4-character suffix so repeated requests never collide. That
claim name is used everywhere: the claim itself, the label the claim stamps on
the adopted pod, the Service that selects by that label, and the two
HTTPRoutes:

    app-<claim>.<domain>          -> Service <claim>:3000   (Bag of Words)
    code-server-<claim>.<domain>  -> Service <claim>:8080   (code-server)

Nothing depends on the pool-generated Sandbox name.
"""
from __future__ import annotations

import os
import re
import secrets
import string
import time
from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

LABEL_KEY = "sandbox.users.io/claim"
CLAIM_GROUP, CLAIM_VERSION, CLAIM_PLURAL = "extensions.agents.x-k8s.io", "v1beta1", "sandboxclaims"
SANDBOX_GROUP, SANDBOX_VERSION, SANDBOX_PLURAL = "agents.x-k8s.io", "v1beta1", "sandboxes"
ROUTE_GROUP, ROUTE_VERSION, ROUTE_PLURAL = "gateway.networking.k8s.io", "v1", "httproutes"
FIELD_MANAGER = "devbox-mcp"

APP_PORT = 3000
CODE_SERVER_PORT = 8080
SUFFIX_LEN = 4
# A hostname label is at most 63 chars; "code-server-" is the longest prefix.
MAX_CLAIM_LEN = 63 - len("code-server-")
MAX_NAME_LEN = MAX_CLAIM_LEN - SUFFIX_LEN - 1

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class SandboxEnv:
    """Cluster-side defaults; each resolvable from the environment."""

    namespace: str = os.environ.get("DEVBOX_NAMESPACE", "default")
    warmpool: str = os.environ.get("DEVBOX_WARMPOOL", "bow-warmpool")
    domain: str = os.environ.get("DEVBOX_DOMAIN", "sndbx.bagofwords.com")
    gateway_name: str = os.environ.get("DEVBOX_GATEWAY_NAME", "eg")
    gateway_namespace: str = os.environ.get("DEVBOX_GATEWAY_NAMESPACE", "default")
    gateway_section: str = os.environ.get("DEVBOX_GATEWAY_SECTION", "https-wildcard")


def normalize_name(name: str) -> str:
    """Lower-case, map anything that is not [a-z0-9] to '-', trim, validate."""
    n = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not n:
        raise ValueError("sandbox name must contain at least one letter or digit")
    if len(n) > MAX_NAME_LEN:
        raise ValueError(f"sandbox name too long ({len(n)} chars); max {MAX_NAME_LEN}")
    if not NAME_RE.match(n):
        raise ValueError(f"invalid sandbox name {n!r}")
    return n


def make_claim_name(name: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(SUFFIX_LEN))
    return f"{normalize_name(name)}-{suffix}"


def hostnames(claim: str, domain: str) -> dict[str, str]:
    return {"app": f"app-{claim}.{domain}", "code_server": f"code-server-{claim}.{domain}"}


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
def _route(claim: str, kind: str, host: str, port: int, env: SandboxEnv) -> dict:
    return {
        "apiVersion": f"{ROUTE_GROUP}/{ROUTE_VERSION}",
        "kind": "HTTPRoute",
        "metadata": {
            "name": f"devbox-{claim}-{kind}",
            "namespace": env.namespace,
            "labels": {LABEL_KEY: claim},
        },
        "spec": {
            "parentRefs": [{
                "name": env.gateway_name,
                "namespace": env.gateway_namespace,
                "sectionName": env.gateway_section,
            }],
            "hostnames": [host],
            "rules": [{
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "filters": [{
                    "type": "RequestHeaderModifier",
                    "requestHeaderModifier": {"set": [
                        {"name": "X-Forwarded-Host", "value": host},
                        {"name": "X-Forwarded-Proto", "value": "https"},
                    ]},
                }],
                "backendRefs": [{"kind": "Service", "name": claim, "port": port}],
            }],
        },
    }


def build_manifests(claim: str, env: SandboxEnv) -> list[dict]:
    hosts = hostnames(claim, env.domain)
    labels = {LABEL_KEY: claim}
    return [
        {
            "apiVersion": f"{CLAIM_GROUP}/{CLAIM_VERSION}",
            "kind": "SandboxClaim",
            "metadata": {"name": claim, "namespace": env.namespace, "labels": labels},
            "spec": {
                "warmPoolRef": {"name": env.warmpool},
                # Stamped onto the adopted pod; the Service selects on it.
                "additionalPodMetadata": {"labels": labels},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": claim, "namespace": env.namespace, "labels": labels},
            "spec": {
                "type": "ClusterIP",
                "selector": labels,
                "ports": [
                    {"name": "http", "port": APP_PORT, "targetPort": APP_PORT, "protocol": "TCP"},
                    {"name": "code-server", "port": CODE_SERVER_PORT, "targetPort": CODE_SERVER_PORT, "protocol": "TCP"},
                ],
            },
        },
        _route(claim, "app", hosts["app"], APP_PORT, env),
        _route(claim, "code", hosts["code_server"], CODE_SERVER_PORT, env),
    ]


# ---------------------------------------------------------------------------
# Cluster access
# ---------------------------------------------------------------------------
_loaded = False


def _load_config() -> None:
    global _loaded
    if _loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _loaded = True


def _apply(obj: dict) -> str:
    """Create the object, or merge-patch it if it already exists. Returns 'created'|'updated'."""
    _load_config()
    kind, meta = obj["kind"], obj["metadata"]
    ns, name = meta["namespace"], meta["name"]
    try:
        if kind == "Service":
            client.CoreV1Api().create_namespaced_service(ns, obj)
        else:
            group, version = obj["apiVersion"].split("/")
            plural = CLAIM_PLURAL if kind == "SandboxClaim" else ROUTE_PLURAL
            client.CustomObjectsApi().create_namespaced_custom_object(group, version, ns, plural, obj)
        return "created"
    except ApiException as e:
        if e.status != 409:
            raise RuntimeError(f"apply {kind}/{name} failed: {e.status} {e.reason}: {e.body}") from e
    if kind == "Service":
        client.CoreV1Api().patch_namespaced_service(name, ns, obj)
    else:
        group, version = obj["apiVersion"].split("/")
        plural = CLAIM_PLURAL if kind == "SandboxClaim" else ROUTE_PLURAL
        client.CustomObjectsApi().patch_namespaced_custom_object(group, version, ns, plural, name, obj)
    return "updated"


def claim_status(claim: str, namespace: str) -> dict:
    _load_config()
    try:
        obj = client.CustomObjectsApi().get_namespaced_custom_object(
            CLAIM_GROUP, CLAIM_VERSION, namespace, CLAIM_PLURAL, claim)
    except ApiException as e:
        if e.status == 404:
            return {"exists": False}
        raise
    status = obj.get("status", {})
    ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
    return {
        "exists": True,
        "ready": ready.get("status") == "True",
        "reason": ready.get("reason"),
        "message": ready.get("message"),
        "sandbox": status.get("sandbox", {}).get("name"),
    }


def sandbox_pod_ip(sandbox: str, namespace: str) -> str | None:
    """Pod IP of a Sandbox, from its status (no pods permission needed)."""
    _load_config()
    try:
        obj = client.CustomObjectsApi().get_namespaced_custom_object(
            SANDBOX_GROUP, SANDBOX_VERSION, namespace, SANDBOX_PLURAL, sandbox)
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    ips = obj.get("status", {}).get("podIPs") or []
    return ips[0] if ips else None


def _describe(obj: dict, domain: str) -> dict:
    meta, status = obj["metadata"], obj.get("status", {})
    claim = meta["name"]
    ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
    hosts = hostnames(claim, domain)
    return {
        "claim_name": claim,
        "namespace": meta["namespace"],
        "created": meta.get("creationTimestamp"),
        "ready": ready.get("status") == "True",
        "reason": ready.get("reason"),
        "sandbox": status.get("sandbox", {}).get("name"),
        "app_url": f"https://{hosts['app']}",
        "code_server_url": f"https://{hosts['code_server']}",
    }


def list_devboxes(namespace: str | None = None, domain: str | None = None) -> list[dict]:
    """Every sandbox created by create_devbox (claims carrying the claim label)."""
    _load_config()
    env = SandboxEnv()
    ns, dom = namespace or env.namespace, domain or env.domain
    objs = client.CustomObjectsApi().list_namespaced_custom_object(
        CLAIM_GROUP, CLAIM_VERSION, ns, CLAIM_PLURAL, label_selector=LABEL_KEY)
    return sorted((_describe(o, dom) for o in objs.get("items", [])), key=lambda d: d["created"] or "")


def next_steps(claim: str, hosts: dict[str, str], ready: bool, claude_logged_in: bool | None) -> list[str]:
    steps = [
        f"Bag of Words: https://{hosts['app']} (login admin@example.com / Password123! unless changed).",
        f"code-server IDE: https://{hosts['code_server']}",
    ]
    if not ready:
        steps.insert(0, "The devbox is still starting; the URLs answer once it is Ready "
                        "(seconds from a warm pool, a few minutes on a cold start).")
    if claude_logged_in is False:
        steps.append(f"Claude Code inside this devbox is NOT signed in. Offer to run "
                     f"devbox_claude_login(claim_name={claim!r}); it returns a sign-in link for the user.")
    elif claude_logged_in is None:
        steps.append(f"Once Ready, check Claude Code sign-in with devbox_status(claim_name={claim!r}) "
                     f"and offer devbox_claude_login if it is not signed in.")
    return steps


def create_devbox(name: str, *, env: SandboxEnv | None = None,
                   wait_ready_seconds: int = 0, dry_run: bool = False) -> dict:
    """Build, (optionally) apply, and describe a new sandbox for `name`."""
    env = env or SandboxEnv()
    claim = make_claim_name(name)
    manifests = build_manifests(claim, env)
    hosts = hostnames(claim, env.domain)

    result = {
        "claim_name": claim,
        "namespace": env.namespace,
        "app_url": f"https://{hosts['app']}",
        "code_server_url": f"https://{hosts['code_server']}",
    }
    if dry_run:
        return {**result, "applied": False, "manifests": manifests}

    result["applied"] = {f"{m['kind']}/{m['metadata']['name']}": _apply(m) for m in manifests}

    if wait_ready_seconds > 0:
        deadline = time.monotonic() + wait_ready_seconds
        while True:
            st = claim_status(claim, env.namespace)
            if st.get("ready"):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(3)
        result["status"] = st
    else:
        result["status"] = claim_status(claim, env.namespace)

    return result


def delete_devbox(claim: str, *, namespace: str | None = None) -> dict:
    """Remove the claim, Service and HTTPRoutes created by create_devbox."""
    _load_config()
    ns = namespace or SandboxEnv().namespace
    deleted: dict[str, str] = {}
    co, core = client.CustomObjectsApi(), client.CoreV1Api()
    for kind in ("app", "code"):
        rname = f"devbox-{claim}-{kind}"
        try:
            co.delete_namespaced_custom_object(ROUTE_GROUP, ROUTE_VERSION, ns, ROUTE_PLURAL, rname)
            deleted[f"HTTPRoute/{rname}"] = "deleted"
        except ApiException as e:
            deleted[f"HTTPRoute/{rname}"] = "not found" if e.status == 404 else f"error {e.status}"
    try:
        core.delete_namespaced_service(claim, ns)
        deleted[f"Service/{claim}"] = "deleted"
    except ApiException as e:
        deleted[f"Service/{claim}"] = "not found" if e.status == 404 else f"error {e.status}"
    try:
        co.delete_namespaced_custom_object(CLAIM_GROUP, CLAIM_VERSION, ns, CLAIM_PLURAL, claim)
        deleted[f"SandboxClaim/{claim}"] = "deleted"
    except ApiException as e:
        deleted[f"SandboxClaim/{claim}"] = "not found" if e.status == 404 else f"error {e.status}"
    return {"claim_name": claim, "namespace": ns, "deleted": deleted,
            "note": "The Sandbox, its pod and its PVC are garbage-collected with the claim."}
