#!/usr/bin/env python3
"""Boot an isolated HTTPS BOW + local SAML IdP, optionally with Entra.

Usage: backend/.venv/bin/python tools/agent/saml_sandbox.py [--entra]
Requires installed backend/frontend dependencies, node, and trusted mkcert CA.
No existing database is reused; stop with Ctrl-C. No cloud credentials needed.
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from tests.mocks.saml_idp import key_pair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--entra",
        action="store_true",
        help="Include the public Entra demo application configuration",
    )
    args = parser.parse_args()
    for port in (3000, 8010, 9443):
        with socket.socket() as sock:
            if sock.connect_ex(("localhost", port)) == 0:
                raise SystemExit(
                    f"Port {port} is occupied. Stop the existing dev stack before starting this isolated sandbox."
                )
    mkcert = shutil.which("mkcert")
    node = shutil.which("node")
    if not mkcert or not node:
        raise SystemExit(
            "Install node and mkcert; run mkcert -install once to trust its local CA."
        )
    state = Path(tempfile.mkdtemp(prefix="bow-saml-"))
    key, cert = key_pair()
    (state / "idp-key.pem").write_text(key)
    (state / "idp-key.pem").chmod(0o600)
    (state / "idp-cert.pem").write_text(cert)
    subprocess.run(
        [
            mkcert,
            "-cert-file",
            str(state / "localhost.pem"),
            "-key-file",
            str(state / "localhost-key.pem"),
            "localhost",
            "127.0.0.1",
            "::1",
        ],
        check=True,
        capture_output=True,
    )
    config = yaml.safe_load((ROOT / "configs/bow-config.saml-demo.yaml").read_text())
    entra = config.pop("saml_providers")
    config["saml_providers"] = []
    config["database"]["url"] = "sqlite:///" + str(state / "app.db")
    config["encryption_key"] = "${BOW_ENCRYPTION_KEY}"
    config_path = state / "bow-config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    env = {
        **os.environ,
        "BOW_CONFIG_PATH": str(config_path),
        "BOW_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "TESTING": "true",
        "TEST_DATABASE_URL": config["database"]["url"],
        "PYTHONPATH": str(ROOT / "backend"),
        "BOW_SAML_TEST_DIR": str(state),
    }
    processes = []
    logs = []

    def start(name, command, cwd, extra=None):
        log = (state / f"{name}.log").open("w")
        logs.append(log)
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env={**env, **(extra or {})},
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(proc)
        return proc

    def ready(url, proc):
        for _ in range(120):
            if proc.poll() is not None:
                raise RuntimeError(f"Service exited; inspect logs in {state}")
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"Timed out starting {url}; inspect {state}")

    backend = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8010",
        "--no-access-log",
    ]
    try:
        with (state / "migrate.log").open("w") as log:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=ROOT / "backend",
                env=env,
                check=True,
                stdout=log,
                stderr=log,
            )
        proc = start("backend", backend, ROOT / "backend")
        ready("http://127.0.0.1:8010/health", proc)
        import secrets

        password = secrets.token_urlsafe(32)
        with httpx.Client(base_url="http://127.0.0.1:8010/api") as client:
            r = client.post(
                "/auth/register",
                json={
                    "name": "SAML Sandbox Admin",
                    "email": "saml-admin@example.com",
                    "password": password,
                },
            )
            r.raise_for_status()
            r = client.post(
                "/auth/jwt/login",
                data={"username": "saml-admin@example.com", "password": password},
            )
            r.raise_for_status()
            r = client.get(
                "/users/whoami",
                headers={"Authorization": "Bearer " + r.json()["access_token"]},
            )
            r.raise_for_status()
            org = r.json()["organizations"][0]["id"]
        if args.entra:
            entra[0]["organization_id"] = org
            config["saml_providers"] = entra
        config["saml_providers"].append(
            {
                "name": "local-saml",
                "enabled": True,
                "label": "Local SAML",
                "organization_id": org,
                "auto_provision_users": True,
                "idp": {
                    "entity_id": "https://localhost:9443/idp",
                    "sso_url": "https://localhost:9443/sso",
                    "certificates": [cert],
                },
                "attributes": {
                    "email": "urn:example:mail",
                    "name": "urn:example:display",
                },
            }
        )
        config_path.write_text(yaml.safe_dump(config))
        proc.terminate()
        proc.wait(timeout=20)
        proc = start("backend", backend, ROOT / "backend")
        ready("http://127.0.0.1:8010/health", proc)
        start(
            "idp",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "saml_test_idp:app",
                "--app-dir",
                str(ROOT / "tools/agent"),
                "--host",
                "127.0.0.1",
                "--port",
                "9443",
                "--ssl-certfile",
                str(state / "localhost.pem"),
                "--ssl-keyfile",
                str(state / "localhost-key.pem"),
                "--no-access-log",
            ],
            ROOT,
        )
        start(
            "frontend",
            [
                node,
                "node_modules/nuxt/bin/nuxt.mjs",
                "dev",
                "--host",
                "localhost",
                "--port",
                "3000",
                "--https",
                "--https.cert",
                str(state / "localhost.pem"),
                "--https.key",
                str(state / "localhost-key.pem"),
            ],
            ROOT / "frontend",
            {"BOW_API_TARGET": "http://127.0.0.1:8010"},
        )
        print(
            f"Sandbox starting at https://localhost:3000. Logs/config: {state}",
            flush=True,
        )
        print(
            "Use Sign in with Local SAML. Press Ctrl-C to stop all sandbox services.",
            flush=True,
        )
        while all(p.poll() is None for p in processes if p is not processes[0]):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
