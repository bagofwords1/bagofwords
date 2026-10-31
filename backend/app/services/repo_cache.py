"""Cached, read-only local clones of remote git repositories.

Backs the GitHub / GitLab / Bitbucket "code" connections: the agent's file
tools (list / read / search / grep) run against a local checkout instead of
one provider API call per file. The model follows OpenCode's repository cache
(one tracked checkout per remote + branch, locked, refreshed when stale,
self-healing) with server-side additions: the key includes a hash of the
credential, every git call is hardened against hostile repo content, and idle
clones are evicted under a disk budget.

Lifecycle of one entry (``ensure``):
  * no clone yet → shallow clone (``--depth 1``) into a temp dir, size-check,
    strip exec bits, atomically move into place;
  * clone younger than the TTL → used as is;
  * older → ``fetch`` + hard reset to the tracked branch; a failed refresh
    keeps serving the previous checkout and is retried only after a back-off;
  * broken / foreign directory → deleted and re-cloned.

Nothing here executes repository content: see app.services.git_hardening.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from app.services.git_hardening import basic_auth_header, hardened_args, hardened_env, with_https_auth

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


CACHE_ROOT = Path(os.environ.get("BOW_REPO_CACHE_DIR", "uploads/repo_cache"))
TTL_SECONDS = _env_int("BOW_REPO_CACHE_TTL_SECONDS", 600)             # refresh when older
RETRY_AFTER_FAILURE_SECONDS = _env_int("BOW_REPO_CACHE_RETRY_SECONDS", 120)
IDLE_EVICT_SECONDS = _env_int("BOW_REPO_CACHE_IDLE_SECONDS", 7 * 24 * 3600)
MAX_TOTAL_BYTES = _env_int("BOW_REPO_CACHE_MAX_MB", 5120) * 1024 * 1024
MAX_REPO_BYTES = _env_int("BOW_REPO_CACHE_MAX_REPO_MB", 1024) * 1024 * 1024
CLONE_TIMEOUT = _env_int("BOW_REPO_CACHE_CLONE_TIMEOUT", 300)
FETCH_TIMEOUT = _env_int("BOW_REPO_CACHE_FETCH_TIMEOUT", 120)
LOCK_TIMEOUT = _env_int("BOW_REPO_CACHE_LOCK_TIMEOUT", CLONE_TIMEOUT + 30)
_EVICT_EVERY_SECONDS = 600

_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/\-]{1,200}(?<!\.lock)$")


class RepoCacheError(Exception):
    """A clone/refresh/check failed; the message is safe to show (redacted)."""


@dataclass(frozen=True)
class RepoSpec:
    url: str                       # clean https URL, no credentials
    branch: Optional[str] = None   # None → the remote's default branch
    username: Optional[str] = None
    secret: Optional[str] = None   # token / API token / password

    def key(self) -> str:
        cred = hashlib.sha256(f"{self.username or ''}:{self.secret or ''}".encode()).hexdigest()
        raw = f"{self.url}\n{self.branch or ''}\n{cred}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


# --- git runner ---------------------------------------------------------------

def _redact(text: str, spec: Optional[RepoSpec]) -> str:
    out = text or ""
    if spec and spec.secret:
        out = out.replace(spec.secret, "***")
        if spec.username:
            out = out.replace(basic_auth_header(spec.username, spec.secret), "Authorization: ***")
    out = re.sub(r"(Authorization:\s*\w+\s+)\S+", r"\1***", out)
    out = re.sub(r"https://[^/\s:@]+:[^@\s]+@", "https://***@", out)
    return out.strip()[-800:]


def _git_env(spec: RepoSpec) -> Dict[str, str]:
    from fastapi import HTTPException
    from app.services.git_service import GitService

    try:
        # Re-validates the host (no metadata / private ranges unless allowed)
        # and pins libcurl to the address that was just checked.
        base = GitService._git_env(spec.url)
    except HTTPException as e:
        raise RepoCacheError(f"Repository host is not allowed: {e.detail}") from None
    env = hardened_env(base)
    if spec.secret:
        env = with_https_auth(env, spec.username or "x-access-token", spec.secret)
    return env


def _run(args: List[str], *, spec: RepoSpec, cwd: Optional[Path] = None, timeout: int) -> str:
    cmd = ["git", *hardened_args(), *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, env=_git_env(spec),
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise RepoCacheError(f"git {args[0]} timed out after {timeout}s") from None
    if proc.returncode != 0:
        raise RepoCacheError(f"git {args[0]} failed: {_redact(proc.stderr or proc.stdout, spec)}")
    return proc.stdout


def _validate(spec: RepoSpec) -> None:
    if not spec.url.startswith("https://") or "@" in spec.url.split("/", 3)[2]:
        raise RepoCacheError("Repository URL must be https:// without credentials")
    if spec.branch and not _BRANCH_RE.match(spec.branch):
        raise RepoCacheError(f"Invalid branch name: {spec.branch!r}")


def list_remote_branches(spec: RepoSpec) -> List[str]:
    """Branch names on the remote (``git ls-remote --heads``) — no clone.

    Used by "Test connection": proves the URL, the credential and read access.
    """
    _validate(spec)
    out = _run(["ls-remote", "--heads", spec.url], spec=spec, timeout=FETCH_TIMEOUT)
    return [line.split("\t", 1)[1].removeprefix("refs/heads/")
            for line in out.splitlines() if "\trefs/heads/" in line]


# --- cache entries --------------------------------------------------------------

def _entry_dir(spec: RepoSpec) -> Path:
    return CACHE_ROOT / spec.key()


def _ensure_root() -> None:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CACHE_ROOT, 0o700)
    except OSError:
        pass


@contextmanager
def _locked(entry: Path, timeout: int = LOCK_TIMEOUT) -> Iterator[None]:
    """Exclusive lock on one entry, across threads and worker processes."""
    entry.mkdir(parents=True, exist_ok=True)
    fd = os.open(entry / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RepoCacheError("Repository is busy (clone in progress); try again shortly") from None
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_meta(entry: Path) -> Dict:
    try:
        return json.loads((entry / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(entry: Path, meta: Dict) -> None:
    tmp = entry / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, entry / "meta.json")


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _make_read_only_data(path: Path) -> None:
    """Owner-only, never executable: files 0600, directories 0700.

    Belt and braces next to ``core.fileMode=false`` — a checked-out script
    can't be run by anything that stumbles on the cache directory.
    """
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o700)
            except OSError:
                pass
        for f in files:
            p = os.path.join(root, f)
            try:
                if not stat.S_ISLNK(os.lstat(p).st_mode):
                    os.chmod(p, 0o600)
            except OSError:
                pass


def _valid_checkout(worktree: Path, meta: Dict, spec: RepoSpec) -> bool:
    return (
        (worktree / ".git").is_dir()
        and meta.get("url") == spec.url
        and meta.get("branch_requested") == (spec.branch or "")
        and bool(meta.get("branch"))
    )


def _clone(entry: Path, spec: RepoSpec) -> Dict:
    worktree = entry / "repo"
    tmp = Path(tempfile.mkdtemp(prefix=".clone-", dir=entry))
    try:
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules"]
        if spec.branch:
            args += ["--branch", spec.branch]
        args += ["--", spec.url, str(tmp / "repo")]
        _run(args, spec=spec, timeout=CLONE_TIMEOUT)
        size = _tree_size(tmp / "repo")
        if size > MAX_REPO_BYTES:
            raise RepoCacheError(
                f"Repository is {size // (1024 * 1024)} MB, over the "
                f"{MAX_REPO_BYTES // (1024 * 1024)} MB limit for code connections"
            )
        branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], spec=spec, cwd=tmp / "repo",
                      timeout=30).strip()
        head = _run(["rev-parse", "HEAD"], spec=spec, cwd=tmp / "repo", timeout=30).strip()
        _make_read_only_data(tmp / "repo")
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        os.replace(tmp / "repo", worktree)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    now = time.time()
    return {"url": spec.url, "branch_requested": spec.branch or "", "branch": branch,
            "head": head, "fetched_at": now, "attempted_at": now, "last_error": None,
            "used_at": now}


def _refresh(entry: Path, spec: RepoSpec, meta: Dict) -> Dict:
    worktree = entry / "repo"
    branch = meta["branch"]
    _run(["fetch", "--depth", "1", "--no-tags", "--no-recurse-submodules", "origin",
          f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
         spec=spec, cwd=worktree, timeout=FETCH_TIMEOUT)
    _run(["reset", "--hard", f"origin/{branch}"], spec=spec, cwd=worktree, timeout=60)
    _run(["clean", "-ffdx"], spec=spec, cwd=worktree, timeout=60)
    head = _run(["rev-parse", "HEAD"], spec=spec, cwd=worktree, timeout=30).strip()
    _make_read_only_data(worktree)
    now = time.time()
    return {**meta, "head": head, "fetched_at": now, "attempted_at": now, "last_error": None}


@dataclass
class Checkout:
    path: Path
    branch: str
    head: str
    status: str            # "cloned" | "refreshed" | "cached" | "stale"
    error: Optional[str] = None


def ensure(spec: RepoSpec, *, force_refresh: bool = False) -> Checkout:
    """Return an up-to-date (or best-available) local checkout for ``spec``."""
    _validate(spec)
    _ensure_root()
    entry = _entry_dir(spec)
    with _locked(entry):
        meta = _read_meta(entry)
        worktree = entry / "repo"
        now = time.time()
        if not _valid_checkout(worktree, meta, spec):
            meta = _clone(entry, spec)
            _write_meta(entry, meta)
            status, error = "cloned", None
        elif not force_refresh and now - meta.get("fetched_at", 0) < TTL_SECONDS:
            status, error = "cached", None
        elif (not force_refresh and meta.get("last_error")
              and now - meta.get("attempted_at", 0) < RETRY_AFTER_FAILURE_SECONDS):
            status, error = "stale", meta["last_error"]
        else:
            try:
                meta = _refresh(entry, spec, meta)
                status, error = "refreshed", None
            except RepoCacheError as e:
                # Serve the previous checkout; say it may be out of date.
                logger.warning("repo_cache: refresh failed for %s: %s", spec.url, e)
                meta = {**meta, "attempted_at": now, "last_error": str(e)}
                status, error = "stale", str(e)
        meta["used_at"] = now
        _write_meta(entry, meta)
        result = Checkout(path=worktree.resolve(), branch=meta["branch"], head=meta.get("head", ""),
                          status=status, error=error)
    _maybe_evict()
    return result


# --- eviction -----------------------------------------------------------------------

_last_evict = 0.0


def _maybe_evict() -> None:
    global _last_evict
    if time.time() - _last_evict < _EVICT_EVERY_SECONDS:
        return
    _last_evict = time.time()
    try:
        evict()
    except Exception as e:  # never fail a read because cleanup did
        logger.warning("repo_cache: eviction failed: %s", e)


def evict(now: Optional[float] = None) -> List[str]:
    """Delete idle clones, then least-recently-used ones over the disk budget.

    An entry that is locked (in use) is skipped. Returns the removed keys.
    """
    if not CACHE_ROOT.is_dir():
        return []
    now = now or time.time()
    entries = []
    for entry in CACHE_ROOT.iterdir():
        if entry.is_dir():
            meta = _read_meta(entry)
            entries.append((meta.get("used_at", 0), entry))
    removed: List[str] = []

    def _remove(entry: Path) -> bool:
        try:
            with _locked(entry, timeout=0):
                shutil.rmtree(entry / "repo", ignore_errors=True)
                (entry / "meta.json").unlink(missing_ok=True)
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
            return True
        except RepoCacheError:
            return False

    keep = []
    for used_at, entry in entries:
        if now - used_at > IDLE_EVICT_SECONDS:
            _remove(entry)
        else:
            keep.append((used_at, entry))
    sizes = {entry: _tree_size(entry) for _, entry in keep}
    total = sum(sizes.values())
    for used_at, entry in sorted(keep, key=lambda x: x[0]):
        if total <= MAX_TOTAL_BYTES:
            break
        if _remove(entry):
            total -= sizes[entry]
    return removed
