"""Claude Code Remote Control inside a sandbox.

`claude remote-control --name <name>` runs as a long-lived daemon that registers the
sandbox as an environment in the user's claude.ai account; sessions are then started
from claude.ai/code or the mobile app. It needs a PTY and must outlive the MCP tool
call (and the MCP server), so it is launched detached inside the sandbox:

    setsid nohup script -qfc "claude remote-control --name <name>" <log> &

`script` provides the PTY, `setsid` makes it a process-group leader we can stop
later, and the log file is what the tools read to report readiness and the URL.
State lives in the sandbox under /app/workspace/run (survives MCP restarts).
"""
from __future__ import annotations

import re
import shlex
import time

from .sandbox import ANSI_RE, SandboxTarget, execute

RUN_DIR = "/app/workspace/run"
LOG = f"{RUN_DIR}/remote-control.log"
PID = f"{RUN_DIR}/remote-control.pid"
DEFAULT_CWD = "/app/workspace/bagofwords"

# "Ready" with no sessions yet, "Connected" once one is attached; the
# environment URL is printed in both states.
READY_RE = re.compile(r"\bReady\b|\bConnected\b")
URL_RE = re.compile(r"https://claude\.ai/code\?environment=\S+")
ERROR_RE = re.compile(r"(?i)not logged in|please run /login|login required|error:")


def _env(home: str) -> dict:
    return {
        "HOME": home,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": "sandbox",
    }


def _sh(channel, script: str, home: str, timeout: float = 30) -> tuple[int, str]:
    code, out, err = execute(channel, ["sh", "-c", script], env=_env(home), timeout=timeout)
    return code, (out + err)


def _parse_log(raw: str) -> dict:
    text = ANSI_RE.sub("", raw).replace("\r", "")
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    url = URL_RE.search(text)
    ready = bool(READY_RE.search(text)) or url is not None
    return {
        "ready": ready,
        "url": url.group(0) if url else None,
        "error": bool(ERROR_RE.search(text)) and not ready,
        "log_tail": lines[-8:],
    }


def _pid_alive_script(home: str) -> str:
    return f'[ -f {PID} ] && kill -0 "$(cat {PID})" 2>/dev/null && echo alive || echo dead'


def status(target: SandboxTarget, *, home: str = "/root") -> dict:
    channel, cleanup = target.open()
    try:
        _, alive = _sh(channel, _pid_alive_script(home), home)
        _, log = _sh(channel, f"cat {LOG} 2>/dev/null || true", home)
    finally:
        cleanup()
    running = alive.strip().endswith("alive")
    parsed = _parse_log(log)
    return {"claim_name": target.claim_name, "running": running, **parsed}


def start(target: SandboxTarget, *, name: str | None = None, cwd: str = DEFAULT_CWD,
          home: str = "/root", mode: str = "daemon", spawn: str = "same-dir",
          wait_seconds: int = 60) -> dict:
    """Launch remote control detached in the sandbox and wait for it to report Ready."""
    name = name or target.claim_name
    if mode == "daemon":
        # --spawn answers the first-run "spawn mode" prompt non-interactively.
        cmd = ["claude", "remote-control", "--name", name, "--spawn", spawn]
    elif mode == "interactive":
        cmd = ["claude", "--remote-control", name]
    else:
        raise ValueError("mode must be 'daemon' or 'interactive'")
    cmd_str = " ".join(shlex.quote(c) for c in cmd)

    # Inside the PTY: give it a size (script's pty has none, and Claude Code
    # refuses to start without one), then run the command.
    inner = f"stty cols 200 rows 50 2>/dev/null; exec {cmd_str}"
    # Claude Code only works in a trusted directory; the image pre-trusts /app
    # but the checkout lives under the workspace. Record trust for `cwd`.
    trust = (
        "import json,os,sys; p=os.path.expanduser('~/.claude.json'); "
        "d=json.load(open(p)) if os.path.exists(p) else {}; "
        "d.setdefault('projects',{}).setdefault(sys.argv[1],{})['hasTrustDialogAccepted']=True; "
        "json.dump(d,open(p,'w'))"
    )
    launcher = f"""
set -e
mkdir -p {RUN_DIR}
if [ -f {PID} ] && kill -0 "$(cat {PID})" 2>/dev/null; then echo already-running; exit 0; fi
python3 -c {shlex.quote(trust)} {shlex.quote(cwd)}
cd {shlex.quote(cwd)}
: > {LOG}
# stdin: answer the first-run "Enable Remote Control? (y/n)" prompt with y (the
# user already agreed), then keep stdin open: script forwards EOF to the pty and
# Claude Code exits on it.
# The inner shell records its own pid: after setsid it is the session and
# process-group leader, so `kill -- -<pid>` later stops the whole tree.
# ($! would be the setsid wrapper, which forks and exits at once.)
setsid sh -c {shlex.quote(f"echo $$ > {PID}; (printf 'y\\n'; tail -f /dev/null) | script -qfc {shlex.quote(inner)} {LOG}")} >/dev/null 2>&1 &
i=0; while [ ! -s {PID} ] && [ $i -lt 20 ]; do sleep 0.1; i=$((i+1)); done
echo started
"""
    channel, cleanup = target.open()
    try:
        code, out = _sh(channel, launcher, home)
        if code != 0:
            raise RuntimeError(f"failed to launch remote control: {out.strip()}")
        launched = out.strip().splitlines()[-1] if out.strip() else "started"

        deadline = time.monotonic() + wait_seconds
        parsed = _parse_log("")
        while time.monotonic() < deadline:
            _, log = _sh(channel, f"cat {LOG} 2>/dev/null || true", home)
            parsed = _parse_log(log)
            if parsed["ready"] or parsed["error"]:
                break
            _, alive = _sh(channel, _pid_alive_script(home), home)
            if alive.strip().endswith("dead"):
                parsed["error"] = True
                break
            time.sleep(2)
    finally:
        cleanup()

    state = "ready" if parsed["ready"] else ("failed" if parsed["error"] else "starting")
    return {
        "claim_name": target.claim_name,
        "session_name": name,
        "mode": mode,
        "launch": launched,
        "state": state,
        "url": parsed["url"],
        "log_tail": parsed["log_tail"],
    }


def stop(target: SandboxTarget, *, home: str = "/root") -> dict:
    """Stop the daemon and everything launched with it.

    `script` puts Claude Code in its own PTY session, so a process-group kill
    on the launcher does not reach it. Kill by command line instead: the
    launcher shell (pid file), the `script` wrapper (references LOG), the
    claude process, and the stdin keeper.
    """
    script = f"""
killed=0
victims() {{
  for d in /proc/[0-9]*; do
    p=${{d#/proc/}}; [ "$p" = "$$" ] && continue
    c=$(tr "\\0" " " < "$d/cmdline" 2>/dev/null) || continue
    case "$c" in
      *"{LOG}"*|"claude remote-control "*|"claude --remote-control "*|"tail -f /dev/null ") echo "$p";;
    esac
  done
  [ -f {PID} ] && cat {PID}
}}
for sig in TERM KILL; do
  for p in $(victims | sort -u); do kill -$sig "$p" 2>/dev/null && killed=$((killed+1)); done
  [ "$sig" = TERM ] && sleep 1
done
rm -f {PID}
if [ "$killed" -gt 0 ]; then echo stopped; else echo not-running; fi
"""
    channel, cleanup = target.open()
    try:
        _, out = _sh(channel, script, home, timeout=30)
    finally:
        cleanup()
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return {"claim_name": target.claim_name, "result": lines[-1] if lines else "unknown"}
