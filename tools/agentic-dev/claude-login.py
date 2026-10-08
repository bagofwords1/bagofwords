"""Log Claude Code in inside an Agent Sandbox by driving `claude auth login` over a PTY.

`claude auth login` is interactive: it prints a sign-in URL, waits on
"Paste code here if prompted >", and needs the code the browser hands back after
sign-in. The Python SDK's `commands.run()` is one-shot (Execute RPC), so this uses
the streaming `Start` RPC with a PTY plus `WriteStdin`, through the gRPC stubs
bundled in the SDK. Requires: pip install 'k8s-agent-sandbox[grpc]'

Flow:
  1. start `claude auth login` in the sandbox and print the URL it emits
  2. you open the URL, sign in, and copy the code the page shows
  3. paste the code here -> it is typed into the sandbox -> "Login successful"

The session then lives in the sandbox's $CLAUDE_CONFIG_DIR (or ~/.claude), so later
`claude` starts there need no login.

Usage:
  # local sandboxd (docker ... -p 9090:9090)
  python claude-login.py

  # sandbox in a cluster, reached through the SDK's pod tunnel
  python claude-login.py --warmpool bow-sandbox-pool --namespace default
"""
import argparse
import os
import queue
import re
import threading
import time

import grpc
from google.protobuf import empty_pb2
from k8s_agent_sandbox.commands._process_stubs import process_pb2, process_pb2_grpc

# CSI (colors, cursor moves) and OSC (hyperlinks, titles) escape sequences Ink emits.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
URL_RE = re.compile(r"https://\S+")
CODE_PROMPT_RE = re.compile(r"Paste code here if prompted")
SUCCESS_RE = re.compile(r"Login successful")
# One pattern: a leading inline flag is only legal at the very start of a regex.
OUTCOME_RE = re.compile(r"(?i)login successful|login failed|invalid code|error")


class InteractiveProcess:
    """Start a command under a PTY, read its output as it arrives, and answer prompts."""

    def __init__(self, channel, command, cols=80, rows=24, env=None, cwd=None):
        self.stub = process_pb2_grpc.ProcessServiceStub(channel)
        self._chunks = queue.Queue()
        self.output = b""
        self.exit_code = None
        self.process_id = None
        started = threading.Event()

        request = process_pb2.StartRequest(
            config=process_pb2.ProcessConfig(command=command, env_vars=env or {}, cwd=cwd),
            pty=process_pb2.PTY(cols=cols, rows=rows),  # a PTY is what makes claude prompt
        )
        self._stream = self.stub.Start(request)

        def pump():
            for ev in self._stream:
                kind = ev.WhichOneof("event")
                if kind == "init":
                    self.process_id = ev.init.process_id
                    started.set()
                elif kind in ("stdout", "stderr"):  # with a PTY everything arrives on stdout
                    self._chunks.put(getattr(ev, kind))
                elif kind == "exit":
                    self.exit_code = ev.exit.exit_code
            self._chunks.put(None)  # sentinel: stream closed

        threading.Thread(target=pump, daemon=True).start()
        if not started.wait(10):
            raise TimeoutError("process did not start")

    @property
    def text(self):
        """Accumulated output with terminal escape sequences removed."""
        return ANSI_RE.sub("", self.output.decode(errors="replace"))

    def expect(self, pattern, timeout=30):
        """Block until the cleaned output matches `pattern` (regex) or the process exits."""
        deadline = time.monotonic() + timeout
        while True:
            m = re.search(pattern, self.text)
            if m:
                return m
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no match for {pattern!r}; output so far:\n{self.text}")
            try:
                chunk = self._chunks.get(timeout=remaining)
            except queue.Empty:
                continue
            if chunk is None:
                raise EOFError(
                    f"process exited (code {self.exit_code}) before {pattern!r}; output:\n{self.text}"
                )
            self.output += chunk

    def send_line(self, text):
        """Type a line and press Enter."""
        self.stub.WriteStdin(
            process_pb2.WriteStdinRequest(process_id=self.process_id, input=(text + "\n").encode())
        )

    def close_stdin(self):
        self.stub.WriteStdin(
            process_pb2.WriteStdinRequest(process_id=self.process_id, eof=empty_pb2.Empty())
        )

    def wait(self, timeout=60):
        """Drain output until the process exits; return its exit code."""
        deadline = time.monotonic() + timeout
        while True:
            chunk = self._chunks.get(timeout=max(0.1, deadline - time.monotonic()))
            if chunk is None:
                return self.exit_code
            self.output += chunk

    def kill(self):
        self.stub.SendSignal(
            process_pb2.SendSignalRequest(process_id=self.process_id, signal=process_pb2.SIGNAL_SIGKILL)
        )


def open_channel(args):
    """Return (grpc channel, cleanup callable) for a local sandboxd or an in-cluster sandbox."""
    if not args.warmpool:
        return grpc.insecure_channel(args.grpc), lambda: None

    from k8s_agent_sandbox import SandboxClient
    from k8s_agent_sandbox.models import SandboxdPodTunnelConnectionConfig

    client = SandboxClient(connection_config=SandboxdPodTunnelConnectionConfig())
    sandbox = client.create_sandbox(warmpool=args.warmpool, namespace=args.namespace)
    sandbox.connector.connect()
    return sandbox.connector.grpc_channel(), sandbox.terminate


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--grpc", default="localhost:9090", help="sandboxd gRPC address for local mode")
    parser.add_argument("--warmpool", help="create a sandbox from this SandboxWarmPool instead of local sandboxd")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--home", default="/root", help="HOME inside the sandbox (where ~/.claude lives)")
    parser.add_argument("--config-dir", help="set CLAUDE_CONFIG_DIR inside the sandbox")
    parser.add_argument("--email", help="pre-populate the email on the login page")
    parser.add_argument("--console", action="store_true", help="Anthropic Console (API billing) instead of a subscription")
    parser.add_argument("--code", default=os.environ.get("CLAUDE_LOGIN_CODE"),
                        help="code to paste (default: $CLAUDE_LOGIN_CODE, else asked interactively)")
    parser.add_argument("--code-timeout", type=int, default=600, help="seconds to wait for the pasted code")
    args = parser.parse_args()

    command = ["claude", "auth", "login"]
    if args.email:
        command += ["--email", args.email]
    if args.console:
        command.append("--console")

    env = {
        "HOME": args.home,
        "PATH": f"{args.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
    }
    if args.config_dir:
        env["CLAUDE_CONFIG_DIR"] = args.config_dir

    channel, cleanup = open_channel(args)
    try:
        # Wide PTY so Ink doesn't wrap the (long) OAuth URL across lines. No cwd: sandboxd
        # confines it to --root-dir, and the login doesn't care where it runs.
        proc = InteractiveProcess(channel, command, cols=1000, rows=40, env=env)

        proc.expect(CODE_PROMPT_RE, timeout=60)  # URL is printed before the code prompt
        url = URL_RE.search(proc.text)
        if not url:
            raise RuntimeError(f"no sign-in URL found in output:\n{proc.text}")

        print("Open this URL in your browser and sign in:\n")
        print(f"  {url.group(0)}\n")
        code = args.code or input("Paste the code the page shows > ").strip()
        if not code:
            proc.kill()
            raise SystemExit("no code given; aborted")

        proc.send_line(code)
        m = proc.expect(OUTCOME_RE, timeout=args.code_timeout)
        exit_code = proc.wait(timeout=30)

        if not SUCCESS_RE.search(m.group(0)):
            print(f"--- login failed (exit {exit_code}) ---\n{proc.text}")
            return exit_code or 1

        print(f"Login successful (exit {exit_code}). Claude is now authenticated in the sandbox.")
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
