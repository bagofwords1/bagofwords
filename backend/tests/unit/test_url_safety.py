"""Outbound-URL SSRF guards (app.core.url_safety)."""

from unittest.mock import patch

import pytest

from app.core import url_safety
from app.core.url_safety import (
    UnsafeURLError,
    check_admin_url,
    check_git_remote,
    host_is_public,
)


def _resolves_to(*addrs):
    return patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", (a, 0)) for a in addrs])


INTERNAL = [
    "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.0.1", "0.0.0.0",
    "100.64.12.1",  # CGNAT
    "169.254.169.254", "100.100.100.200", "fd00:ec2::254", "::ffff:169.254.169.254",
    "::1", "fc00::1", "fe80::1",
]
METADATA = ["169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254",
            "::ffff:169.254.169.254", "fe80::1", "0.0.0.0"]


@pytest.mark.parametrize("addr", INTERNAL)
def test_strict_tier_rejects_any_internal_address(addr):
    with _resolves_to(addr):
        assert host_is_public("anything.example.com") is False


def test_strict_tier_rejects_if_any_resolved_address_is_internal():
    with _resolves_to("93.184.216.34", "10.0.0.5"):
        assert host_is_public("mixed.example.com") is False


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:4700::1111"])
def test_strict_tier_accepts_public_addresses(addr):
    with _resolves_to(addr):
        assert host_is_public("example.com") is True


@pytest.mark.parametrize("addr", INTERNAL)
def test_strict_tier_rejects_ip_literals_without_dns(addr):
    assert host_is_public(addr) is False


@pytest.mark.parametrize("addr", METADATA)
def test_admin_tier_always_blocks_metadata(addr):
    with _resolves_to(addr), pytest.raises(UnsafeURLError):
        check_admin_url("https://llm.internal.example.com/v1")


@pytest.mark.parametrize("host", ["metadata.google.internal", "METADATA.GOOGLE.INTERNAL."])
def test_admin_tier_blocks_metadata_hostnames(host):
    with pytest.raises(UnsafeURLError):
        check_admin_url(f"http://{host}/computeMetadata/v1/")


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.7", "192.168.1.20", "100.64.0.3", "93.184.216.34"])
def test_admin_tier_allows_private_hosts_by_default(addr):
    # Self-hosted installs point LLMs / git / SIEMs at LAN hosts.
    with _resolves_to(addr):
        check_admin_url("http://ollama.lan:11434/v1")


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.7", "100.64.0.3"])
def test_admin_tier_blocks_private_hosts_when_configured(addr):
    with patch.object(url_safety, "_block_private", return_value=True), _resolves_to(addr):
        with pytest.raises(UnsafeURLError):
            check_admin_url("http://ollama.lan:11434/v1")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://example.com/", "", "//example.com"])
def test_admin_tier_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeURLError):
        check_admin_url(url)


@pytest.mark.parametrize("url", [
    "https://github.com/org/repo.git",
    "http://gitlab.lan/org/repo.git",
    "ssh://git@github.com/org/repo.git",
    "git@github.com:org/repo.git",
    "git://example.com/repo.git",
    "deploy@git.example.com:team/repo.git",
])
def test_git_remote_accepts_network_transports(url):
    with _resolves_to("93.184.216.34"):
        check_git_remote(url)


@pytest.mark.parametrize("url", [
    "file:///srv/repos/secret.git",
    "/srv/repos/secret.git",
    "../other.git",
    "C:/repos/x.git",
    "ext::sh -c touch% /tmp/pwned",
    "fd::17",
])
def test_git_remote_rejects_local_and_helper_transports(url):
    with pytest.raises(UnsafeURLError):
        check_git_remote(url)


@pytest.mark.parametrize("url", [
    "https://169.254.169.254/latest/meta-data",
    "git@169.254.169.254:org/repo.git",
    "ssh://git@[fd00:ec2::254]/repo.git",
])
def test_git_remote_rejects_metadata_hosts(url):
    with pytest.raises(UnsafeURLError):
        check_git_remote(url)
