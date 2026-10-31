"""A real smart-HTTPS git server for tests (git http-backend behind uvicorn).

The repo cache only speaks https (``protocol.allow=never`` + https), so tests
need a genuine HTTPS remote: a self-signed certificate (trusted through
GIT_SSL_CAINFO) and ``git http-backend`` serving bare repos from a directory.

Optional Basic auth: when ``password`` is set, every request must carry
``Authorization: Basic base64(username:password)`` or gets 401 — that is how
the tests prove the token travels as a header and never in the URL.

    srv = GitHttpsServer(project_root, password="tok").start()
    srv.url("owner/name")   # https://127.0.0.1:<port>/owner/name.git
    srv.stats               # Counter of (method, service) requests
    srv.fail = True         # every request answers 500 until reset
    srv.stop()
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import socket
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class GitHttpsServer:
    def __init__(self, project_root: Path, *, username: str = "x-access-token",
                 password: Optional[str] = None, cert_dir: Optional[Path] = None):
        self.project_root = Path(project_root)
        self.username, self.password = username, password
        self.cert_dir = Path(cert_dir or self.project_root.parent)
        self.cert_path, self.key_path = make_cert(self.cert_dir)
        self.port = _free_port()
        self.stats: Counter = Counter()
        self.auth_headers: list = []
        self.request_urls: list = []
        self.fail = False
        self._server = None
        self._thread = None

    def url(self, path: str) -> str:
        return f"https://127.0.0.1:{self.port}/{path}.git"

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def _app(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        body = b""
        more = True
        while more:
            m = await receive()
            body += m.get("body", b"")
            more = m.get("more_body", False)
        query = scope.get("query_string", b"").decode()
        service = "upload-pack" if "upload-pack" in scope["path"] + query else "other"
        self.stats[(scope["method"], service)] += 1
        self.request_urls.append(scope["path"] + ("?" + query if query else ""))
        self.auth_headers.append(headers.get("authorization"))

        async def respond(status, hdrs, payload=b""):
            await send({"type": "http.response.start", "status": status,
                        "headers": [(k.encode(), v.encode()) for k, v in hdrs]})
            await send({"type": "http.response.body", "body": payload})

        if self.fail:
            return await respond(500, [("content-type", "text/plain")], b"boom")
        if self.password is not None:
            expected = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            if headers.get("authorization") != expected:
                return await respond(401, [("www-authenticate", 'Basic realm="git"')], b"auth required")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(self.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": scope["method"],
            "PATH_INFO": scope["path"],
            "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": self.username if self.password is not None else "",
            "REMOTE_ADDR": "127.0.0.1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        if headers.get("content-encoding"):
            env["HTTP_CONTENT_ENCODING"] = headers["content-encoding"]
        if headers.get("git-protocol"):
            env["GIT_PROTOCOL"] = headers["git-protocol"]
        proc = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
        raw = proc.stdout
        head, _, payload = raw.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = raw.partition(b"\n\n")
        status, hdrs = 200, []
        for line in head.decode(errors="replace").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            else:
                hdrs.append((k.strip(), v.strip()))
        await respond(status, hdrs, payload)

    def start(self) -> "GitHttpsServer":
        import uvicorn

        self._server = uvicorn.Server(uvicorn.Config(
            self._app, host="127.0.0.1", port=self.port, log_level="error",
            ssl_certfile=str(self.cert_path), ssl_keyfile=str(self.key_path), lifespan="on", interface="asgi3",
        ))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            assert time.monotonic() < deadline, "git https server did not start"
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=10)


def git(*args, cwd=None, env=None) -> str:
    """Run git for fixture setup (NOT hardened — this is the test's own repo)."""
    e = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", **(env or {})}
    return subprocess.run(["git", *args], cwd=cwd, env=e, check=True, capture_output=True, text=True).stdout


def make_bare_repo(project_root: Path, name: str, files: dict, *, symlinks: Optional[dict] = None,
                   executables: tuple = (), branch: str = "main") -> Path:
    """Create ``project_root/<name>.git`` from a dict of path → bytes|str.

    Returns the working directory (for pushing follow-up commits).
    """
    work = project_root.parent / f"work-{name.replace('/', '-')}"
    work.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", branch, str(work))
    for rel, content in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)
    for rel in executables:
        os.chmod(work / rel, 0o755)
    for rel, target in (symlinks or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, work / rel)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "init", cwd=work)
    bare = project_root / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--bare", str(work), str(bare))
    git("remote", "add", "origin", str(bare), cwd=work)
    return work


def push_commit(work: Path, files: dict, message: str = "update") -> None:
    for rel, content in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", message, cwd=work)
    git("push", "-q", "origin", "HEAD", cwd=work)
