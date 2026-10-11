"""Outbound-URL safety checks (SSRF guards).

Two tiers, because the callers differ in who controls the URL:

* ``host_is_public`` — strict. Every resolved address must be globally
  routable. For URLs the *agent* picks (web_fetch, the browser tool, assets an
  artifact pulls in while it renders to PDF): anyone who can chat can steer
  those, so nothing internal is reachable.

* ``check_admin_url`` — for URLs an admin configures (git remotes, LLM
  ``base_url``, audit-stream endpoints). Self-hosted deployments legitimately
  point these at internal hosts (an Ollama on the LAN, an internal GitLab, a
  private Splunk), so private networks stay allowed by default. What is never
  allowed is the cloud instance-metadata service and the rest of link-local
  space: no legitimate integration lives there, and it hands out credentials.
  Operators who want the strict tier for these too set
  ``security.block_private_outbound: true`` in bow-config.

Known limit: the address is checked at validation time and the HTTP client
resolves again when it connects, so a DNS server that answers differently the
second time (rebinding) is not stopped here.
"""

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Metadata endpoints outside 169.254.0.0/16 / fe80::/10.
_METADATA_ADDRESSES = {
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS over IPv6
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
}
_METADATA_HOSTNAMES = {"metadata.google.internal", "metadata.goog", "metadata"}


class UnsafeURLError(ValueError):
    """Raised when an outbound URL targets a disallowed destination."""


def _normalize(ip: IPAddress) -> IPAddress:
    # ::ffff:169.254.169.254 must be judged as the IPv4 address it carries.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def ip_is_public(ip: IPAddress) -> bool:
    ip = _normalize(ip)
    # is_global excludes private, loopback, link-local, CGNAT (100.64/10),
    # reserved and documentation ranges; multicast is "global" for some
    # ranges, so exclude it explicitly.
    return ip.is_global and not ip.is_multicast and ip not in _METADATA_ADDRESSES


def ip_is_metadata(ip: IPAddress) -> bool:
    ip = _normalize(ip)
    return ip.is_link_local or ip.is_unspecified or ip in _METADATA_ADDRESSES


def resolve(hostname: str) -> list[IPAddress]:
    """Every address ``hostname`` resolves to; [] if it does not resolve."""
    try:
        return [ipaddress.ip_address(hostname.strip("[]"))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return []
    out: list[IPAddress] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    return out


def _is_localhost_name(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    return lowered == "localhost" or lowered.endswith(".localhost")


def host_is_public(hostname: str | None) -> bool:
    """Strict tier: True only if every resolved address is globally routable."""
    if not hostname or _is_localhost_name(hostname):
        return False
    addrs = resolve(hostname)
    return bool(addrs) and all(ip_is_public(a) for a in addrs)


def _block_private() -> bool:
    try:
        from app.settings.config import settings

        return bool(settings.bow_config.security.block_private_outbound)
    except Exception:
        return False


def check_admin_host(hostname: str | None) -> None:
    """Admin tier for a bare hostname. Raises ``UnsafeURLError``."""
    if not hostname:
        raise UnsafeURLError("URL must include a hostname.")
    if hostname.lower().rstrip(".") in _METADATA_HOSTNAMES:
        raise UnsafeURLError("URL points at a cloud metadata service.")
    if _block_private():
        if not host_is_public(hostname):
            raise UnsafeURLError("URL must point at a public address.")
        return
    # Unresolvable hosts are left to fail at connect time with the client's
    # own error — they cannot reach anything, so there is nothing to block.
    if any(ip_is_metadata(a) for a in resolve(hostname)):
        raise UnsafeURLError("URL points at a link-local or cloud metadata address.")


def check_admin_url(url: str, schemes: Iterable[str] = ("http", "https")) -> None:
    """Admin tier for a full URL: scheme allowlist + ``check_admin_host``."""
    parsed = urlparse(url or "")
    if parsed.scheme.lower() not in set(schemes):
        raise UnsafeURLError(f"URL scheme must be one of: {', '.join(schemes)}.")
    check_admin_host(parsed.hostname)


def git_remote_host(repo_url: str) -> tuple[str, str | None]:
    """Return (transport, hostname) for the git remote forms we support.

    Supported: ``https://``, ``http://``, ``ssh://``, ``git://`` and scp-style
    ``user@host:path``. Everything else — ``file://``, local paths, and
    ``ext::`` / ``fd::`` remote helpers (which run commands) — is rejected.
    """
    url = (repo_url or "").strip()
    if "::" in url.split("/", 1)[0]:
        raise UnsafeURLError("Git remote helpers are not supported.")
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https", "ssh", "git"):
        return parsed.scheme, parsed.hostname
    if not parsed.scheme or len(parsed.scheme) == 1:
        # scp-like: [user@]host:path  (a one-letter "scheme" is a Windows drive)
        head, sep, _ = url.partition(":")
        if sep and "/" not in head and "@" in head:
            return "ssh", head.rsplit("@", 1)[1] or None
    raise UnsafeURLError("Repository URL must use https:// or ssh (git@host:org/repo).")


def check_git_remote(repo_url: str) -> None:
    _, host = git_remote_host(repo_url)
    check_admin_host(host)
