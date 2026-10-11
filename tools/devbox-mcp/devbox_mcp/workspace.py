"""Small git helpers inside a devbox: report the current branch, set the commit
identity. Branch switching is not done by the MCP server: devboxes always start
on main and the agent switches branches inside the devbox itself.

Runs over sandboxd's Execute RPC; scripts source /app/workspace/run/stack.env
(written by entrypoint.sh) so they see the same environment as the boot did.
"""
from __future__ import annotations

import shlex

from .sandbox import SandboxTarget, execute

SRC = "/app/workspace/bagofwords"
ENV_FILE = "/app/workspace/run/stack.env"
BRANCH_RE = r"^[A-Za-z0-9._/-]+$"


def _env(home: str) -> dict:
    return {"HOME": home, "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"}


def _sh(channel, script: str, home: str = "/root", timeout: float = 600) -> tuple[int, str]:
    wrapped = f"set -a; . {ENV_FILE} 2>/dev/null; set +a; cd {SRC} || exit 97\n{script}"
    code, out, err = execute(channel, ["bash", "-c", wrapped], env=_env(home), timeout=timeout)
    return code, (out + err)


def _tail(text: str, n: int = 12) -> list[str]:
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    return lines[-n:]


def current_branch(target: SandboxTarget) -> dict:
    channel, cleanup = target.open()
    try:
        code, out = _sh(channel, "git rev-parse --abbrev-ref HEAD; git log -1 --format=%h; git status --porcelain | wc -l", timeout=30)
    finally:
        cleanup()
    parts = [l.strip() for l in out.strip().splitlines()]
    if code != 0 or len(parts) < 3:
        return {"error": out.strip()[-300:]}
    return {"branch": parts[0], "commit": parts[1], "dirty_files": int(parts[2])}


def configure_git(target: SandboxTarget, *, user_name: str | None = None,
                  user_email: str | None = None, home: str = "/root") -> dict:
    """Set the commit identity inside the devbox. Push credentials are per user and
    come from devbox_github_login, never from here."""
    parts = []
    if user_name:
        parts.append(f"git config --global user.name {shlex.quote(user_name)}")
    if user_email:
        parts.append(f"git config --global user.email {shlex.quote(user_email)}")
    if not parts:
        return {"claim_name": target.claim_name, "changed": False}
    parts.append("echo \"identity: $(git config --global user.name) <$(git config --global user.email)>\"")
    channel, cleanup = target.open()
    try:
        code, out = _sh(channel, "\n".join(parts), home=home, timeout=60)
    finally:
        cleanup()
    return {"claim_name": target.claim_name, "changed": code == 0, "output": _tail(out, 4)}
