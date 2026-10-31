"""The existing Git connection (dbt/LookML indexing, push, PRs) keeps tokens off
disk and verifies SSH host keys.

Contract (app.services.git_service + app.services.git_hardening):
  * a PAT clone authenticates with a per-command header; the clone's
    .git/config holds a clean remote URL and no token;
  * a saved URL with credentials pasted into it is cleaned before use;
  * SSH never disables host-key checking: GitHub/GitLab/Bitbucket keys are
    pinned, other hosts are learned on first use and then enforced.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.mocks.git_https_server import GitHttpsServer, make_bare_repo

TOKEN = "ghp_secret_value_123"


class _Repo:
    """Stand-in for a GitRepository row (only what clone_repository reads)."""

    def __init__(self, url):
        self.repo_url = url
        self.branch = "main"
        self.has_access_token = True
        self.has_ssh_key = False
        self.access_token_username = None

    def decrypt_access_token(self):
        return TOKEN


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from app.services import git_hardening
    root = tmp_path / "srv"
    root.mkdir()
    make_bare_repo(root, "acme/models", {"models/orders.sql": "select 1\n"})
    monkeypatch.setattr(git_hardening, "_STATE_DIR", tmp_path / "state")
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "127.0.0.1,localhost")
    s = GitHttpsServer(root, password=TOKEN, cert_dir=tmp_path).start()
    monkeypatch.setenv("GIT_SSL_CAINFO", str(s.cert_path))
    yield s
    s.stop()


def test_pat_clone_keeps_the_token_out_of_the_clone(srv, tmp_path):
    from app.services.git_service import GitService
    dest = tmp_path / "clone"
    asyncio.run(GitService().clone_repository(_Repo(srv.url("acme/models")), str(dest)))
    assert (dest / "models" / "orders.sql").exists()
    config = (dest / ".git" / "config").read_text()
    assert TOKEN not in config and "@" not in config
    for p in (dest / ".git").rglob("*"):
        if p.is_file():
            assert TOKEN.encode() not in p.read_bytes(), p
    assert all(h and h.startswith("Basic ") for h in srv.auth_headers)


def test_pasted_credentials_are_stripped_from_the_url():
    from app.services.git_service import GitService
    assert GitService._https_url("https://user:tok@github.com/a/b.git") == "https://github.com/a/b.git"
    assert GitService._https_url("git@gitlab.com:g/p.git") == "https://gitlab.com/g/p.git"


def test_ssh_verifies_host_keys(tmp_path, monkeypatch):
    from app.services import git_hardening
    monkeypatch.setattr(git_hardening, "_STATE_DIR", tmp_path / "state")
    cmd = git_hardening.ssh_command("/tmp/key")
    assert "StrictHostKeyChecking=no" not in cmd
    assert "StrictHostKeyChecking=accept-new" in cmd
    pinned = (tmp_path / "state" / "known_hosts.pinned").read_text()
    for host in ("github.com ssh-ed25519", "gitlab.com ssh-ed25519", "bitbucket.org ssh-ed25519"):
        assert host in pinned


def test_no_code_path_disables_host_key_checking():
    src = (Path(__file__).resolve().parents[2] / "app/services/git_service.py").read_text()
    assert "StrictHostKeyChecking=no" not in src
    assert "_convert_to_https_url" not in src
