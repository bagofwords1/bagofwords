"""Read-only file access to one GitHub / GitLab / Bitbucket repository.

The agent's file tools (list_files / read_file / search_files / grep_files)
run against a cached local clone (app.services.repo_cache) — the same way
Hermes, OpenCode and OpenClaw read code: a local copy plus plain file reads,
not one provider API call per file.

Everything is inherited from NetworkDirClient (root confinement, listing,
search, grep, document extraction) with these differences:

* the root is a clone made lazily on first use and refreshed when stale;
* read-only: no WRITE_FILE capability;
* `.git/` internals and vendored directories are never listed or read;
* dotfiles are visible (code config lives there) EXCEPT secret-looking files
  (.env, keys, certificates), which are hidden unless the admin allows them;
* invisible Unicode TAG characters are stripped from everything returned.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.data_sources.clients.base import Capability
from app.data_sources.clients.network_dir_client import NetworkDirClient
from app.services import repo_cache

logger = logging.getLogger(__name__)

# Directories never surfaced: git internals and dependency/vendor trees.
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
                 ".pytest_cache", "vendor", "bower_components", ".gradle", ".idea"}

# Secret-looking files, hidden unless allow_secret_files is on.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519", "*.ppk", ".npmrc", ".pypirc",
    ".netrc", ".git-credentials", "credentials.json", "*.kdbx", "secrets.y*ml", "*.tfstate",
    "*.tfvars",
)

_OS_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_PATTERNS)


class GitRepoClient(NetworkDirClient):
    """Base for the provider clients; subclasses set the provider defaults."""

    provider = "git"
    default_host = ""
    default_username = "x-access-token"

    # The root is an app-owned cache directory, never user input, so the
    # network_dir root policy (which refuses the app's own uploads/) doesn't
    # apply; `_resolve` still confines every path to the clone.
    _root_policy_exempt = True

    capabilities = {
        Capability.LIST_FILES,
        Capability.READ_FILE,
        Capability.SEARCH_FILES,
        Capability.GREP_FILES,
    }
    cheap_live_listing = True

    def __init__(
        self,
        repo: str,
        branch: Optional[str] = None,
        host: Optional[str] = None,
        token: Optional[str] = None,
        username: Optional[str] = None,
        include_globs: Optional[str] = None,
        allow_secret_files: bool = False,
        max_file_mb: int = 5,
        index_mode: Optional[str] = "metadata",
        **_ignored,
    ):
        # No root yet: the clone happens on first use (see _root).
        super().__init__(
            root_path=None, include_globs=include_globs, recursive=True, writable=False,
            max_file_mb=max_file_mb, index_mode=index_mode or "metadata",
        )
        self.capabilities = set(type(self).capabilities)
        self.allow_secret_files = bool(allow_secret_files)
        self.branch = (branch or "").strip() or None
        self.repo_url = self._repo_url(repo, host)
        self.spec = repo_cache.RepoSpec(
            url=self.repo_url,
            branch=self.branch,
            username=(username or "").strip() or (self.default_username if token else None),
            secret=token or None,
        )
        self.last_checkout: Optional[repo_cache.Checkout] = None

    # ------------------------------------------------------------- repo url

    def _repo_url(self, repo: str, host: Optional[str]) -> str:
        raw = (repo or "").strip()
        if not raw:
            raise ValueError("Repository is required")
        if raw.startswith("https://"):
            parsed = urlparse(raw)
            if parsed.username or parsed.password:
                raise ValueError("Put the token in the credentials, not in the repository URL")
            path = parsed.path.strip("/")
            host_part = parsed.netloc
        else:
            if raw.startswith(("http://", "git@", "ssh://", "file:", "-")):
                raise ValueError("Use owner/name or an https:// URL")
            path = raw.strip("/")
            host_part = (host or "").strip().removeprefix("https://").strip("/") or self.default_host
        if path.endswith(".git"):
            path = path[:-4]
        if not path or ".." in path.split("/") or path.count("/") < 1:
            raise ValueError(f"Repository must look like owner/name, got {repo!r}")
        if not host_part or "/" in host_part or "@" in host_part:
            raise ValueError(f"Invalid server: {host!r}")
        return f"https://{host_part}/{path}.git"

    # ------------------------------------------------------------- the clone

    def _checkout(self, *, force_refresh: bool = False) -> repo_cache.Checkout:
        try:
            co = repo_cache.ensure(self.spec, force_refresh=force_refresh)
        except repo_cache.RepoCacheError as e:
            raise ValueError(str(e)) from None
        self.last_checkout = co
        self.root_path = str(co.path)
        return co

    def _root(self) -> Path:
        if not self.root_path:
            self._checkout()
        return super()._root()

    # ------------------------------------------------------------- visibility

    def _excluded(self, rel_parts) -> bool:
        if any(part in EXCLUDED_DIRS for part in rel_parts[:-1]):
            return True
        name = rel_parts[-1] if rel_parts else ""
        if not self.allow_secret_files and is_secret_name(name):
            return True
        return False

    @staticmethod
    def _is_junk(name: str) -> bool:
        # Unlike a document share, dotfiles are real code (.eslintrc, .github).
        return not name or name.startswith("~$") or name.lower() in _OS_JUNK

    def _iter_files(self, base: Path, recursive: bool, limit: Optional[int] = None) -> List[Path]:
        root = self._root()
        files = super()._iter_files(base, recursive, limit)
        out = []
        for p in files:
            try:
                parts = p.relative_to(root).parts
            except ValueError:
                continue
            if not self._excluded(parts):
                out.append(p)
        return out

    def _resolve(self, rel_or_id: str, *, must_exist: bool = True, enforce_scope: bool = True) -> Path:
        path = super()._resolve(rel_or_id, must_exist=must_exist, enforce_scope=enforce_scope)
        root = self._root()
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            raise ValueError(f"Path escapes the repository: {rel_or_id}") from None
        if parts and (parts[0] == ".git" or (path.is_file() and self._excluded(parts))):
            raise ValueError(f"Not available: {rel_or_id} (hidden for safety)")
        return path

    # ------------------------------------------------------------- public API

    def read_file(self, file_id: str, *args, **kwargs) -> Any:
        from app.data_sources.clients.mcp_client import scrub_tags
        result = super().read_file(file_id, *args, **kwargs)
        return result if isinstance(result, (bytes, bytearray, tuple)) else scrub_tags(result)

    def grep_files(self, pattern: str, **kwargs) -> Dict[str, Any]:
        from app.data_sources.clients.mcp_client import scrub_tags
        return scrub_tags(super().grep_files(pattern, **kwargs))

    def test_connection(self) -> Dict[str, Any]:
        """Prove URL, credential and read access with ``git ls-remote`` (no clone)."""
        try:
            branches = repo_cache.list_remote_branches(self.spec)
        except repo_cache.RepoCacheError as e:
            return {"success": False, "message": str(e)}
        if not branches:
            return {"success": False, "message": "The repository is empty or the token cannot read it"}
        if self.branch and self.branch not in branches:
            shown = ", ".join(sorted(branches)[:10])
            return {"success": False, "message": f"Branch '{self.branch}' not found. Available: {shown}"}
        return {"success": True,
                "message": f"Connected to {self.repo_url} ({len(branches)} branch(es) visible)"}

    def refresh(self) -> Dict[str, Any]:
        co = self._checkout(force_refresh=True)
        return {"branch": co.branch, "head": co.head, "status": co.status, "error": co.error}


class GitHubRepoClient(GitRepoClient):
    provider = "github"
    default_host = "github.com"
    default_username = "x-access-token"


class GitLabRepoClient(GitRepoClient):
    provider = "gitlab"
    default_host = "gitlab.com"
    default_username = "oauth2"


class BitbucketRepoClient(GitRepoClient):
    provider = "bitbucket"
    default_host = "bitbucket.org"
    # Atlassian API tokens; repository access tokens use x-token-auth.
    default_username = "x-bitbucket-api-token-auth"
