"""Names, paths and addresses on a sandbox host. One place, so the commands,
the bootstrap steps and reconcile cannot disagree."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import List

ROOT = "/var/lib/bow-sandbox"
OWNER_FILE = f"{ROOT}/owner.json"
SANDBOXD_PATH = f"{ROOT}/bin/sandboxd"
WORKSPACES = f"{ROOT}/ws"
ENV_DIR = f"{ROOT}/env"
ETC_DIR = f"{ROOT}/etc"
RESOLV_NONE = f"{ETC_DIR}/resolv-none.conf"
RESOLV_FULL = f"{ETC_DIR}/resolv-full.conf"
CADDY_DIR = f"{ROOT}/caddy"
EGRESS_DIR = f"{ROOT}/egress"
FIREWALL_DIR = f"{ROOT}/firewall"

NETWORK = "bow-sbx"
BRIDGE = "bow-sbx0"
SUBNET = "10.213.77.0/24"
CADDY_CONTAINER = "bow-sbx-caddy"
EGRESS_CONTAINER = "bow-sbx-egress"
FIREWALL_CONTAINER = "bow-sbx-firewall"
SANDBOX_PREFIX = "bow-sbx-"
VERIFY_PREFIX = "bow-sbx-verify-"

LABEL_INSTANCE = "bow.instance"
LABEL_SANDBOX = "bow.sandbox"
LABEL_ROLE = "bow.role"

CADDY_INTERNAL_PORT = 8443
EGRESS_PORT = 3128
SANDBOXD_GRPC_PORT = 9090
SANDBOXD_REST_PORT = 8080
RUN_UID = 1000
TMPFS_MB = 256
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")

RUNTIME_STANDARD = "runc"
RUNTIME_STRONG = "runsc"


@dataclass(frozen=True)
class Addresses:
    subnet: str
    gateway: str
    caddy: str
    egress: str
    first_sandbox: int  # host number of the first sandbox address

    def sandbox_candidates(self) -> List[str]:
        network = ipaddress.ip_network(self.subnet)
        hosts = list(network.hosts())
        return [str(h) for h in hosts[self.first_sandbox - 1:]]


def addresses(subnet: str = SUBNET) -> Addresses:
    hosts = list(ipaddress.ip_network(subnet).hosts())
    return Addresses(subnet=subnet, gateway=str(hosts[0]), caddy=str(hosts[1]), egress=str(hosts[2]), first_sandbox=10)


def sandbox_container(sandbox_id: str) -> str:
    return f"{SANDBOX_PREFIX}{sandbox_id}"


def workspace_image(sandbox_id: str) -> str:
    return f"{WORKSPACES}/{sandbox_id}.img"


def workspace_mount(sandbox_id: str) -> str:
    return f"{WORKSPACES}/{sandbox_id}"


def _version() -> str:
    from app.settings.config import settings

    return str(getattr(settings, "PROJECT_VERSION", "") or "latest").lstrip("v")


def caddy_image() -> str:
    """The BoW Caddy image (router and egress proxy). Published with each release."""
    return os.getenv("BOW_SANDBOX_CADDY_IMAGE", "").strip() or f"bagofwords/sandbox-caddy:{_version()}"


def firewall_image() -> str:
    return os.getenv("BOW_SANDBOX_FIREWALL_IMAGE", "").strip() or f"bagofwords/sandbox-firewall:{_version()}"


def tools_image() -> str:
    """Carries the sandboxd binary at the pinned upstream commit."""
    return os.getenv("BOW_SANDBOX_TOOLS_IMAGE", "").strip() or f"bagofwords/sandbox-tools:{_version()}"
