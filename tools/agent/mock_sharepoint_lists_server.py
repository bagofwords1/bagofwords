#!/usr/bin/env python3
"""A local SharePoint Server REST endpoint for lists, with real Windows (NTLM) sign-in.

For sandbox runs of the SharePoint Server lists connector (`sharepoint_onprem_lists`)
when no SharePoint farm is available. It serves the in-memory provider the unit
tests use (``backend/tests/fixtures/fake_sharepoint_lists.py``) over HTTP(S),
behind a genuine NTLM handshake — so the connector's real session,
authentication and REST code run end to end. The provider enforces the List
View Threshold (5,000) and the lookup column threshold (12) the way SharePoint
Server does.

Run from ``backend/`` with its venv:

    uv run python ../tools/agent/mock_sharepoint_lists_server.py --port 8444 --tls

    --tls                       serve HTTPS with a throwaway certificate; its PEM
                                is written to --cert-out so the backend can trust
                                it through REQUESTS_CA_BUNDLE
    --require-channel-binding   refuse sign-ins not bound to the TLS channel
                                (what Extended Protection enforces)
    --log FILE                  one JSON line per request: who, which path, which
                                query options (so a run can prove no $filter was sent)

The site is ``<scheme>://<name>:<port>/sites/ops``. Lab identities (domain CORP;
throwaway lab passwords):

    svc-sharepoint   service account; sees every list
    dana             sees every list, including Salaries
    noa              sees every list except Salaries
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import ssl
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

from tests.fixtures.fake_sharepoint_lists import SITE_PATH, build_world  # noqa: E402

DOMAIN = "CORP"
USERS = {"svc-sharepoint": "Lab-Svc-1!", "dana": "Lab-Dana-1!", "noa": "Lab-Noa-1!"}


def make_handler(world, *, bindings, log_path, lock):
    import spnego
    from spnego.exceptions import SpnegoError

    def record(**fields):
        if not log_path:
            return
        with lock, open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}) + "\n")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            # NTLM authenticates the connection, not the request.
            self.context = None
            self.principal = None

        def log_message(self, *args):
            pass

        def _reply(self, status, body=b"", headers=None, content_type="application/json;odata=verbose;charset=utf-8"):
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _challenge(self, token=None, reason=None):
            if reason:
                record(event="sign-in refused", reason=reason)
            value = "NTLM" + (" " + base64.b64encode(token).decode() if token else "")
            self._reply(401, b"", {"WWW-Authenticate": value}, "text/plain")

        def _authenticate(self) -> bool:
            if self.principal:
                return True
            header = self.headers.get("Authorization") or ""
            if not header.startswith("NTLM "):
                self.context = None
                self._challenge()
                return False
            try:
                token = base64.b64decode(header[5:])
                if self.context is None:
                    self.context = spnego.server(protocol="ntlm", channel_bindings=bindings)
                reply = self.context.step(token)
            except (SpnegoError, ValueError) as exc:
                self.context = None
                self._challenge(reason=type(exc).__name__)
                return False
            if not self.context.complete:
                self._challenge(reply)
                return False
            self.principal = (self.context.client_principal or "").split("\\")[-1].split("@")[0].lower()
            record(event="signed in", principal=self.principal, channel_bound=bool(bindings))
            return True

        def do_GET(self):
            if not self.path.startswith(SITE_PATH + "/_api/"):
                self._reply(404, b"", content_type="text/plain")
                return
            if not self._authenticate():
                return
            with lock:
                status, body = world.handle("GET", self.path, accept=self.headers.get("Accept") or "",
                                            principal=self.principal)
                request = world.requests[-1]
            record(event="request", principal=self.principal, path=request["path"],
                   options=sorted(k for k in request["params"] if k.startswith("$")),
                   top=request["params"].get("$top"), skiptoken=request["params"].get("$skiptoken"),
                   status=status)
            self._reply(status, body)

        def do_POST(self):
            self._reply(405, b"", content_type="text/plain")

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--name", default="localhost", help="host name clients use (certificate subject)")
    parser.add_argument("--port", type=int, default=8444)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--require-channel-binding", action="store_true")
    parser.add_argument("--cert-out", default="mock-sharepoint-ca.pem")
    parser.add_argument("--log", default=None)
    args = parser.parse_args()
    if args.require_channel_binding and not args.tls:
        parser.error("--require-channel-binding needs --tls")

    users = tempfile.NamedTemporaryFile("w", suffix=".ntlm", delete=False)
    users.write("".join(f"{DOMAIN}:{user}:{password}\n" for user, password in USERS.items()))
    users.close()
    os.environ["NTLM_USER_FILE"] = users.name

    scheme = "https" if args.tls else "http"
    world = build_world()
    world.origin = f"{scheme}://{args.name}:{args.port}"

    bindings = None
    server = ThreadingHTTPServer((args.host, args.port), BaseHTTPRequestHandler)
    if args.tls:
        from mock_exchange_server import _self_signed
        pem_cert, pem_key, der = _self_signed(args.name)
        Path(args.cert_out).write_bytes(pem_cert)
        with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False) as bundle:
            bundle.write(pem_cert + pem_key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(bundle.name)
        os.unlink(bundle.name)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        if args.require_channel_binding:
            from spnego.channel_bindings import GssChannelBindings
            bindings = GssChannelBindings(application_data=b"tls-server-end-point:" + hashlib.sha256(der).digest())
    server.RequestHandlerClass = make_handler(world, bindings=bindings, log_path=args.log, lock=threading.Lock())
    print(f"mock SharePoint Server at {world.origin}{SITE_PATH}"
          + (f" (certificate: {args.cert_out})" if args.tls else "")
          + (" — channel binding required" if bindings else ""), flush=True)
    try:
        server.serve_forever()
    finally:
        os.unlink(users.name)


if __name__ == "__main__":
    main()
