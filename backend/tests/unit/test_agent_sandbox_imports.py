"""Starting the app must not load the sandbox drivers' libraries.

The SSH library and the docker-ssh driver are only needed when a sandbox is
actually used, and the feature ships off. They are imported lazily by the
driver registry; this guards against an import creeping to module level
somewhere on the app's import path.

(``grpc`` is not checked: it is already loaded by existing dependencies.)
"""
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

_PROBE = """
import sys
import main  # noqa: F401  (the FastAPI app, as uvicorn imports it)
loaded = [m for m in ("asyncssh", "app.services.sandbox.drivers.docker_ssh", "app.services.sandbox.runtime_client") if m in sys.modules]
loaded += [m for m in sys.modules if m.startswith("app.services.sandbox.drivers.docker_ssh.")]
print("LOADED=" + ",".join(sorted(set(loaded))))
"""


def test_importing_the_app_does_not_load_ssh_or_the_docker_ssh_driver():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=BACKEND, capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "TESTING": "true", "BOW_AGENT_SANDBOXES_ENABLED": ""},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("LOADED="))
    assert line == "LOADED=", f"loaded at app import: {line}"
