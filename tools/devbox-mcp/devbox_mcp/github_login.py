"""GitHub sign-in inside a devbox, per user, via `gh auth login` (device flow).

With GH_PROMPT_DISABLED=1 and no TTY, gh prints a one-time code and
https://github.com/login/device, then polls GitHub until the user completes the
login in a browser. It runs detached in the devbox (it waits up to ~15 min),
so the tool only has to report the code and URL, and a later status call
finalises git: `gh auth setup-git` makes git use gh's token, and user.name /
user.email are set from the GitHub profile. The token never leaves the devbox.
"""
from __future__ import annotations

import re
import shlex
import time

from .sandbox import ANSI_RE, SandboxTarget, execute

RUN_DIR = "/app/workspace/run"
LOG = f"{RUN_DIR}/gh-login.log"
PID = f"{RUN_DIR}/gh-login.pid"
DONE = f"{RUN_DIR}/gh-login.done"
SRC = "/app/workspace/bagofwords"

CODE_RE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
URL_RE = re.compile(r"https://github\.com/login/device\S*")


def _env(home: str) -> dict:
    return {"HOME": home, "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "GH_PROMPT_DISABLED": "1", "BROWSER": "/bin/false"}


def _sh(channel, script: str, home: str, timeout: float = 30) -> tuple[int, str]:
    code, out, err = execute(channel, ["bash", "-c", script], env=_env(home), timeout=timeout)
    return code, ANSI_RE.sub("", out + err).replace("\r", "")


def start(target: SandboxTarget, *, home: str = "/root", hostname: str = "github.com",
          wait_seconds: int = 30) -> dict:
    """Launch `gh auth login` detached and return the one-time code and URL."""
    cmd = f"gh auth login --hostname {shlex.quote(hostname)} --git-protocol https --web --skip-ssh-key"
    launcher = f"""
command -v gh >/dev/null || {{ echo "gh-missing"; exit 2; }}
mkdir -p {RUN_DIR}
if [ -f {PID} ] && kill -0 "$(cat {PID})" 2>/dev/null; then echo already-running; exit 0; fi
: > {LOG}; rm -f {DONE}
setsid bash -c {shlex.quote(f"{cmd}; echo EXIT:$? > {DONE}")} >> {LOG} 2>&1 </dev/null &
echo $! > {PID}
echo started
"""
    channel, cleanup = target.open()
    try:
        code, out = _sh(channel, launcher, home)
        if "gh-missing" in out:
            raise RuntimeError("the gh CLI is not installed in this devbox image; rebuild the sandbox runtime image")
        launched = out.strip().splitlines()[-1] if out.strip() else "started"
        deadline = time.monotonic() + wait_seconds
        log = ""
        while time.monotonic() < deadline:
            _, log = _sh(channel, f"cat {LOG} 2>/dev/null || true", home)
            if CODE_RE.search(log) and URL_RE.search(log):
                break
            if "EXIT:" in _sh(channel, f"cat {DONE} 2>/dev/null || true", home)[1]:
                break
            time.sleep(1.5)
    finally:
        cleanup()
    m_code, m_url = CODE_RE.search(log), URL_RE.search(log)
    if not (m_code and m_url):
        return {"claim_name": target.claim_name, "state": "failed", "launch": launched,
                "log_tail": [l for l in log.splitlines() if l.strip()][-8:]}
    return {"claim_name": target.claim_name, "state": "waiting_for_user", "launch": launched,
            "code": m_code.group(1), "url": m_url.group(0)}


def status(target: SandboxTarget, *, home: str = "/root", finalize: bool = True) -> dict:
    """Whether gh is signed in; on first success wire git to it and set the identity."""
    script = f"""
command -v gh >/dev/null || {{ echo "GH=missing"; exit 0; }}
if [ -f {PID} ] && kill -0 "$(cat {PID})" 2>/dev/null; then echo "LOGIN_PROC=running"; else echo "LOGIN_PROC=idle"; fi
cat {DONE} 2>/dev/null | sed 's/^/DONE=/'
if gh auth status --hostname github.com >/dev/null 2>&1; then
  echo "LOGGED_IN=1"
  gh api user --jq '"LOGIN=" + .login + "\nNAME=" + (.name // "") + "\nEMAIL=" + (.email // "") + "\nID=" + (.id|tostring)' 2>/dev/null || true
else
  echo "LOGGED_IN=0"
fi
"""
    channel, cleanup = target.open()
    try:
        _, out = _sh(channel, script, home, timeout=60)
        kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        result = {
            "claim_name": target.claim_name,
            "gh_installed": kv.get("GH") != "missing",
            "login_process": kv.get("LOGIN_PROC", "idle"),
            "logged_in": kv.get("LOGGED_IN") == "1",
            "github_login": kv.get("LOGIN"),
        }
        if result["logged_in"] and finalize:
            login, name, email, uid = kv.get("LOGIN", ""), kv.get("NAME", ""), kv.get("EMAIL", ""), kv.get("ID", "")
            name = name or login
            email = email or (f"{uid}+{login}@users.noreply.github.com" if uid and login else "")
            fin = f"""
gh auth setup-git --hostname github.com 2>&1 | tail -1
git config --global user.name {shlex.quote(name)}
[ -n {shlex.quote(email)} ] && git config --global user.email {shlex.quote(email)}
cd {SRC} && git config --global --add safe.directory {SRC} 2>/dev/null
echo "IDENTITY=$(git config --global user.name) <$(git config --global user.email)>"
rm -f {PID}
"""
            _, fout = _sh(channel, fin, home, timeout=60)
            ident = [l for l in fout.splitlines() if l.startswith("IDENTITY=")]
            result["git_identity"] = ident[-1][len("IDENTITY="):] if ident else None
            result["git_configured"] = True
    finally:
        cleanup()
    return result


def cancel(target: SandboxTarget, *, home: str = "/root") -> dict:
    script = f"""
if [ -f {PID} ]; then p=$(cat {PID}); pkill -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; rm -f {PID}; echo cancelled; else echo not-running; fi
"""
    channel, cleanup = target.open()
    try:
        _, out = _sh(channel, script, home)
    finally:
        cleanup()
    return {"claim_name": target.claim_name, "result": out.strip().splitlines()[-1] if out.strip() else "unknown"}
