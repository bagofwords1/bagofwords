"""Provision an edge agent's mTLS identity from the Data Tunnels wizard.

Talks to the cluster that hosts the NATS broker (design A11): creates a
cert-manager Certificate for the agent (CN=<org>-<edge_agent_id>), reads back the
issued Secret plus the broker CA, and idempotently adds the agent's user to the
nats-accounts ConfigMap so verify_and_map authorizes it. Also generates the
agent's config.yaml and packages everything as a downloadable bundle.

All cluster access goes through kubernetes_asyncio; in-cluster ServiceAccount is
used when running in the cluster, otherwise the default kubeconfig. Every helper
raises ProvisioningError on failure so routes can return a clean 4xx/5xx.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import zipfile

from kubernetes_asyncio import client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.rest import ApiException

from app.settings.config import settings

logger = logging.getLogger(__name__)

# The kubernetes_asyncio REST client logs full HTTP response bodies at DEBUG,
# which for a Secret read includes the base64 private key. Never let cert
# material reach the logs — pin its client loggers above DEBUG.
for _noisy in ("kubernetes_asyncio.client.rest", "kubernetes_asyncio"):
    logging.getLogger(_noisy).setLevel(logging.INFO)

_CERT_GROUP = "cert-manager.io"
_CERT_VERSION = "v1"
_CERT_PLURAL = "certificates"

# Fail fast when the cluster is unreachable so a request returns a clean error
# instead of hanging: (connect, read) seconds passed as `_request_timeout`.
_K8S_TIMEOUT = (5, 15)
_PROBE_TIMEOUT = 5

# A DNS-1123 label that also keeps the cert CN (<org-uuid>-<id>) within the 64
# char X.509 common-name limit: a 36-char org UUID + '-' leaves 27.
_AGENT_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,25}[a-z0-9])?$")

_config_loaded = False


class ProvisioningError(Exception):
    """A cluster/cert-manager operation failed."""


class ProvisioningUnavailable(ProvisioningError):
    """Provisioning is not configured/reachable (no agent_url or no cluster)."""


def _cfg():
    return settings.bow_config.data_tunnel


def validate_edge_agent_id(edge_agent_id: str) -> None:
    """Reject ids that are unsafe as a NATS subject token or a k8s resource name."""
    if not edge_agent_id or not _AGENT_ID_RE.match(edge_agent_id):
        raise ProvisioningError(
            "edge_agent_id must be 1-27 chars, lowercase letters/digits/'-', "
            "start and end alphanumeric (no dots, spaces, '*' or '>')"
        )


def agent_cn(org_id: str, edge_agent_id: str) -> str:
    """The cert CN / k8s resource name for an agent — must match the NATS user."""
    return f"{org_id}-{edge_agent_id}"


async def _ensure_config() -> None:
    global _config_loaded
    if _config_loaded:
        return
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        await k8s_config.load_kube_config()
    _config_loaded = True


async def is_available() -> bool:
    """Whether the wizard can provision: an agent_url is set, the kube config
    loads, and the API server answers a lightweight probe (so a down cluster
    reports unavailable fast instead of failing later mid-wizard)."""
    if not _cfg().agent_url:
        return False
    try:
        await _ensure_config()
        async with client.ApiClient() as api:
            await client.VersionApi(api).get_code(_request_timeout=_PROBE_TIMEOUT)
        return True
    except Exception as e:  # pragma: no cover - environment dependent
        logger.info("tunnel.provisioning.unavailable", extra={"error": str(e)})
        return False


async def _require_config() -> None:
    if not _cfg().agent_url:
        raise ProvisioningUnavailable("data_tunnel.agent_url is not configured")
    try:
        await _ensure_config()
    except Exception as e:
        raise ProvisioningUnavailable(f"cluster is not reachable: {e}") from e


# -- cert-manager Certificate ------------------------------------------------

async def create_certificate(org_id: str, edge_agent_id: str) -> None:
    """Apply the agent's cert-manager Certificate (idempotent)."""
    await _require_config()
    p = _cfg().provisioning
    name = agent_cn(org_id, edge_agent_id)
    body = {
        "apiVersion": f"{_CERT_GROUP}/{_CERT_VERSION}",
        "kind": "Certificate",
        "metadata": {"name": name, "namespace": p.namespace},
        "spec": {
            "secretName": name,
            "issuerRef": {"name": p.issuer, "kind": "Issuer"},
            "commonName": name,
            "usages": ["client auth"],
            "duration": p.cert_duration,
            "renewBefore": "720h",
            "privateKey": {"algorithm": "ECDSA", "size": 256},
        },
    }
    async with client.ApiClient() as api:
        co = client.CustomObjectsApi(api)
        try:
            await co.create_namespaced_custom_object(
                group=_CERT_GROUP, version=_CERT_VERSION, namespace=p.namespace,
                plural=_CERT_PLURAL, body=body, _request_timeout=_K8S_TIMEOUT,
            )
            logger.info("tunnel.provisioning.cert_created", extra={"cert": name})
        except ApiException as e:
            if e.status == 409:
                return  # already exists — reuse
            raise ProvisioningError(f"failed to create Certificate {name}: {e.reason}") from e


async def certificate_ready(org_id: str, edge_agent_id: str) -> tuple[bool, str]:
    """(ready, reason) from the Certificate's Ready condition."""
    await _require_config()
    p = _cfg().provisioning
    name = agent_cn(org_id, edge_agent_id)
    async with client.ApiClient() as api:
        co = client.CustomObjectsApi(api)
        try:
            obj = await co.get_namespaced_custom_object(
                group=_CERT_GROUP, version=_CERT_VERSION, namespace=p.namespace,
                plural=_CERT_PLURAL, name=name, _request_timeout=_K8S_TIMEOUT,
            )
        except ApiException as e:
            if e.status == 404:
                return False, "certificate not found"
            raise ProvisioningError(f"failed to read Certificate {name}: {e.reason}") from e
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True", cond.get("message") or ""
    return False, "issuing"


async def read_bundle_material(org_id: str, edge_agent_id: str) -> dict:
    """Read the issued client cert/key and the broker CA (raw PEM bytes)."""
    await _require_config()
    p = _cfg().provisioning
    name = agent_cn(org_id, edge_agent_id)
    async with client.ApiClient() as api:
        core = client.CoreV1Api(api)
        try:
            sec = await core.read_namespaced_secret(name, p.namespace, _request_timeout=_K8S_TIMEOUT)
        except ApiException as e:
            if e.status == 404:
                raise ProvisioningError(
                    f"certificate secret {name} not ready yet"
                ) from e
            raise ProvisioningError(f"failed to read secret {name}: {e.reason}") from e
        try:
            ca_sec = await core.read_namespaced_secret(p.ca_secret, p.namespace, _request_timeout=_K8S_TIMEOUT)
        except ApiException as e:
            raise ProvisioningError(f"failed to read CA secret {p.ca_secret}: {e.reason}") from e

    def _dec(data: dict, key: str) -> bytes:
        if key not in data:
            raise ProvisioningError(f"secret missing key {key!r}")
        return base64.b64decode(data[key])

    return {
        "cert": _dec(sec.data, "tls.crt"),
        "key": _dec(sec.data, "tls.key"),
        "ca": _dec(ca_sec.data, "tls.crt"),
    }


async def delete_certificate(org_id: str, edge_agent_id: str) -> None:
    """Delete the Certificate CR and its Secret (idempotent)."""
    await _require_config()
    p = _cfg().provisioning
    name = agent_cn(org_id, edge_agent_id)
    async with client.ApiClient() as api:
        co = client.CustomObjectsApi(api)
        core = client.CoreV1Api(api)
        for call in (
            lambda: co.delete_namespaced_custom_object(
                group=_CERT_GROUP, version=_CERT_VERSION, namespace=p.namespace,
                plural=_CERT_PLURAL, name=name, _request_timeout=_K8S_TIMEOUT),
            lambda: core.delete_namespaced_secret(name, p.namespace, _request_timeout=_K8S_TIMEOUT),
        ):
            try:
                await call()
            except ApiException as e:
                if e.status != 404:
                    raise ProvisioningError(f"failed to delete {name}: {e.reason}") from e


# -- nats-accounts ConfigMap -------------------------------------------------

def _agent_user_block(org_id: str, edge_agent_id: str) -> str:
    cn = agent_cn(org_id, edge_agent_id)
    return (
        "      {\n"
        f'        user: "CN={cn}"\n'
        "        permissions {\n"
        f'          subscribe = {{ allow: ["tunnel.{org_id}.{edge_agent_id}.>"] }}\n'
        f'          publish   = {{ allow: ["tunnel.{org_id}.advertisements", "tunnel.{org_id}.{edge_agent_id}.>"] }}\n'
        '          allow_responses = { max: 1, expires: "20m" }\n'
        "        }\n"
        "      }\n"
    )


def _insert_user(content: str, block: str) -> str:
    # Insert before the TUNNEL users array close (…] } }).
    close = "    ]\n  }\n}"
    if content.count(close) != 1:
        raise ProvisioningError("unexpected nats-accounts structure; refusing to edit")
    return content.replace(close, block + close)


def _remove_user(content: str, cn: str) -> str:
    marker = f'user: "CN={cn}"'
    i = content.find(marker)
    if i == -1:
        return content
    open_i = content.rfind("{", 0, i)
    line_start = content.rfind("\n", 0, open_i) + 1
    depth, j = 0, open_i
    while j < len(content):
        if content[j] == "{":
            depth += 1
        elif content[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    line_end = content.find("\n", j)
    line_end = len(content) if line_end == -1 else line_end + 1
    return content[:line_start] + content[line_end:]


async def _rewrite_accounts(mutate) -> None:
    p = _cfg().provisioning
    async with client.ApiClient() as api:
        core = client.CoreV1Api(api)
        try:
            cm = await core.read_namespaced_config_map(
                p.accounts_configmap, p.namespace, _request_timeout=_K8S_TIMEOUT
            )
        except ApiException as e:
            raise ProvisioningError(
                f"failed to read ConfigMap {p.accounts_configmap}: {e.reason}"
            ) from e
        content = (cm.data or {}).get(p.accounts_key)
        if content is None:
            raise ProvisioningError(
                f"ConfigMap {p.accounts_configmap} has no key {p.accounts_key!r}"
            )
        new = mutate(content)
        if new == content:
            return  # idempotent no-op
        try:
            await core.patch_namespaced_config_map(
                p.accounts_configmap, p.namespace, {"data": {p.accounts_key: new}},
                _request_timeout=_K8S_TIMEOUT,
            )
        except ApiException as e:
            raise ProvisioningError(
                f"failed to patch ConfigMap {p.accounts_configmap}: {e.reason}"
            ) from e


async def ensure_account_user(org_id: str, edge_agent_id: str) -> None:
    """Add the agent's NATS user to nats-accounts (idempotent)."""
    await _require_config()
    cn = agent_cn(org_id, edge_agent_id)
    block = _agent_user_block(org_id, edge_agent_id)

    def mutate(content: str) -> str:
        if f'user: "CN={cn}"' in content:
            return content
        return _insert_user(content, block)

    await _rewrite_accounts(mutate)
    logger.info("tunnel.provisioning.account_user_ensured", extra={"cn": cn})


async def remove_account_user(org_id: str, edge_agent_id: str) -> None:
    """Remove the agent's NATS user from nats-accounts (idempotent)."""
    await _require_config()
    cn = agent_cn(org_id, edge_agent_id)
    await _rewrite_accounts(lambda content: _remove_user(content, cn))
    logger.info("tunnel.provisioning.account_user_removed", extra={"cn": cn})


# -- generated config + bundle -----------------------------------------------

def generate_config_yaml(org_id: str, edge_agent_id: str, label: str | None) -> str:
    """The config.yaml shipped in the bundle. Cert paths are relative to it so
    the extracted directory works as-is."""
    name = json.dumps(label or edge_agent_id)  # safely quoted YAML scalar
    agent_url = _cfg().agent_url or "wss://<your-bow-host>:443"
    return (
        f"# Bow Data Edge Agent — generated for '{edge_agent_id}'.\n"
        "# Keep this file and the certs/ directory together; run the agent from here.\n"
        f"org_id: {org_id}\n"
        f"edge_agent_id: {edge_agent_id}\n"
        f"edge_agent_name: {name}\n"
        f"tunnel_endpoint_url: {agent_url}\n"
        "\n"
        "# mTLS to the broker (paths relative to this file).\n"
        "tls_ca: certs/ca.pem\n"
        "tls_cert: certs/client.pem\n"
        "tls_key: certs/client-key.pem\n"
        "tls_verify: true\n"
        "\n"
        "# Add the on-prem data sources this agent serves. Credentials stay on\n"
        "# this host and never reach Bow.\n"
        "connections: []\n"
        "#  - name: prod-pg\n"
        "#    type: postgresql\n"
        "#    config: { host: localhost, port: 5432, database: analytics }\n"
        "#    credentials: { user: bow_reader, password: \"\" }\n"
    )


def _readme(edge_agent_id: str) -> str:
    return (
        f"Bow Data Edge Agent bundle for '{edge_agent_id}'.\n\n"
        "Contents:\n"
        "  config.yaml           agent configuration\n"
        "  certs/ca.pem          broker CA (verifies the server)\n"
        "  certs/client.pem      this agent's mTLS client certificate\n"
        "  certs/client-key.pem  its private key — keep it secret (chmod 600)\n\n"
        "Install:\n"
        "  1. Extract this archive on the host that will run the agent.\n"
        "  2. Edit config.yaml to add your data source connections.\n"
        "  3. Run the agent from the extracted directory so the relative cert\n"
        "     paths resolve.\n"
    )


async def build_bundle(org_id: str, edge_agent_id: str, label: str | None) -> bytes:
    """A zip with config.yaml, the three PEMs, and a README."""
    mat = await read_bundle_material(org_id, edge_agent_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("config.yaml", generate_config_yaml(org_id, edge_agent_id, label))
        z.writestr("certs/ca.pem", mat["ca"])
        z.writestr("certs/client.pem", mat["cert"])
        z.writestr("certs/client-key.pem", mat["key"])
        z.writestr("README.txt", _readme(edge_agent_id))
    return buf.getvalue()
