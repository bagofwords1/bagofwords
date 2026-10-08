"""`claude auth login` inside a sandbox, split into two phases so an MCP client can
relay the browser step to a human.

`claude auth login` prints a sign-in URL, waits on "Paste code here if prompted >",
and needs the code the browser hands back after sign-in. An MCP tool call cannot
block on a person mid-call, so:

  1. start()        -> runs the command, returns the URL and a session id
  2. submit_code()  -> types the code into that session, returns the outcome

Sessions are kept in memory in this server process; the sandbox process stays
alive (and the warm-pool sandbox stays claimed) until the code arrives, the
session is cancelled, or it times out.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .sandbox import InteractiveProcess, SandboxTarget

URL_RE = re.compile(r"https://\S+")
CODE_PROMPT_RE = re.compile(r"Paste code here if prompted")
SUCCESS_RE = re.compile(r"Login successful")
# One pattern: a leading inline flag is only legal at the very start of a regex.
OUTCOME_RE = re.compile(r"(?i)login successful|login failed|invalid code|error")

SESSION_TTL_SECONDS = 15 * 60


@dataclass
class LoginSession:
    id: str
    url: str
    proc: InteractiveProcess
    cleanup: Callable[[], None]
    target: SandboxTarget
    created_at: float = field(default_factory=time.monotonic)

    def close(self) -> None:
        try:
            self.proc.kill()
        finally:
            self.cleanup()


_sessions: dict[str, LoginSession] = {}
_lock = threading.Lock()


def _expire_stale() -> None:
    now = time.monotonic()
    for sid, s in list(_sessions.items()):
        if now - s.created_at > SESSION_TTL_SECONDS:
            _sessions.pop(sid, None)
            s.close()


def start(
    target: SandboxTarget,
    *,
    home: str = "/root",
    config_dir: str | None = None,
    email: str | None = None,
    console: bool = False,
    prompt_timeout: int = 60,
) -> LoginSession:
    """Run `claude auth login` in the sandbox and return once it prints the sign-in URL."""
    command = ["claude", "auth", "login"]
    if email:
        command += ["--email", email]
    if console:
        command.append("--console")

    env = {
        "HOME": home,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
    }
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir

    channel, cleanup = target.open()
    try:
        # Wide PTY so Ink doesn't wrap the (long) OAuth URL across lines. No cwd: sandboxd
        # confines it to --root-dir, and the login doesn't care where it runs.
        proc = InteractiveProcess(channel, command, cols=1000, rows=40, env=env)
        proc.expect(CODE_PROMPT_RE, timeout=prompt_timeout)  # URL is printed before the code prompt
        url = URL_RE.search(proc.text)
        if not url:
            proc.kill()
            raise RuntimeError(f"no sign-in URL found in output:\n{proc.text}")
    except Exception:
        cleanup()
        raise

    session = LoginSession(id=uuid.uuid4().hex[:12], url=url.group(0), proc=proc, cleanup=cleanup, target=target)
    with _lock:
        _expire_stale()
        _sessions[session.id] = session
    return session


def submit_code(session_id: str, code: str, *, timeout: int = 120) -> dict:
    """Type the code the browser showed into the waiting login and report the outcome."""
    with _lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        raise KeyError(f"unknown or expired login session {session_id!r}; call claude_login again")

    code = code.strip()
    if not code:
        session.close()
        raise ValueError("empty code; login aborted")

    try:
        proc = session.proc
        proc.send_line(code)
        m = proc.expect(OUTCOME_RE, timeout=timeout)
        exit_code = proc.wait(timeout=30)
        ok = bool(SUCCESS_RE.search(m.group(0)))
        return {"success": ok, "exit_code": exit_code, "output": proc.text}
    finally:
        session.close()


def cancel(session_id: str) -> bool:
    with _lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        return False
    session.close()
    return True


def pending() -> list[dict]:
    with _lock:
        _expire_stale()
        return [
            {"session_id": s.id, "url": s.url, "age_seconds": int(time.monotonic() - s.created_at),
             "target": s.target.warmpool or s.target.grpc}
            for s in _sessions.values()
        ]
