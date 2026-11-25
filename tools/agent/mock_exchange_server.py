#!/usr/bin/env python3
"""A local Exchange Web Services endpoint with real Windows (NTLM) sign-in.

For sandbox runs of the Exchange Server (on-prem) mail connector when no
Exchange server is available. It serves the same in-memory provider the unit
tests use (``backend/tests/fixtures/fake_exchange.py``) over HTTP(S), behind a
genuine NTLM handshake — so the connector's real session, authentication and
SOAP code run end to end.

Run from ``backend/`` with its venv:

    uv run python ../tools/agent/mock_exchange_server.py --port 8443 --tls --require-channel-binding

    --tls                       serve HTTPS with a throwaway certificate; its PEM
                                is written to --cert-out so the backend can trust
                                it through REQUESTS_CA_BUNDLE
    --require-channel-binding   refuse sign-ins that are not bound to the TLS
                                channel (what Extended Protection enforces)
    --log FILE                  one JSON line per request: who, which operation

Lab identities (domain CORP; the passwords are throwaway lab values):

    dana       own mailbox; may open support@corp.example
    noa        own mailbox; no shared mailboxes
    svc-mail   empty own mailbox; may open support@ and sales@corp.example

The data is rebuilt on every start, with dates relative to now.
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

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from tests.fixtures.fake_exchange import FakeExchange, Mailbox, Message  # noqa: E402

DOMAIN = "CORP"
USERS = {"dana": "Lab-Dana-1!", "noa": "Lab-Noa-1!", "svc-mail": "Lab-Svc-1!"}
SUPPORT, SALES = "support@corp.example", "sales@corp.example"
EWS_PATH = "/EWS/Exchange.asmx"


def _at(hours_ago: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mail(subject, hours_ago, sender, body, *, read=True, to=None, cc=None, attachments=None):
    return Message(subject=subject, sender_name=sender[0], sender_email=sender[1], received=_at(hours_ago),
                   body=body, is_read=read, to=to or [], cc=cc or [], attachments=attachments or [])


def build_world() -> dict:
    """One Exchange view per lab identity; shared mailboxes are shared objects."""
    support = Mailbox(SUPPORT)
    support.add_folder("Escalations", parent="Inbox")
    support.add_folder("Resolved")
    acme, globex, initech = ("Priya Shah", "priya@acme.example"), ("Tom Becker", "tom@globex.example"), ("Lena Ortiz", "lena@initech.example")
    support.add("Inbox", _mail("Cannot log in after password reset", 2, acme, read=False, to=[("Support", SUPPORT)],
                               body="Hi team,\n\nSince this morning three of our users cannot log in after resetting their "
                                    "passwords. They get 'session expired' right after signing in. This blocks our month-end "
                                    "close. Can you look at it today?\n\nPriya"))
    support.add("Inbox", _mail("Invoice export shows wrong currency", 5, globex, read=False, to=[("Support", SUPPORT)],
                               body="The CSV export of invoices shows USD for our EUR entities since the last update. "
                                    "Totals are right, only the currency column is wrong.\n\nTom",
                               attachments=["invoice-export.csv"]))
    support.add("Inbox", _mail("Feature request: dark mode for dashboards", 26, initech, to=[("Support", SUPPORT)],
                               body="Our night-shift operators would love a dark theme on the wall dashboards."))
    support.add("Inbox", _mail("Re: Cannot log in after password reset", 1, acme, read=False, to=[("Support", SUPPORT)],
                               body="Update: it now affects eleven users. We tried another browser, same result."))
    support.add("Inbox/Escalations", _mail("URGENT: billing API returning 500", 3, globex, read=False,
                                           to=[("Support", SUPPORT)], cc=[("On-call", "oncall@corp.example")],
                                           body="All calls to the billing API fail with HTTP 500 since 09:40 UTC. "
                                                "We cannot issue invoices. Please escalate."))
    support.add("Resolved", _mail("Report schedule ran twice", 70, initech, to=[("Support", SUPPORT)],
                                  body="Resolved: the duplicate schedule was removed. Thanks!"))

    sales = Mailbox(SALES)
    sales.add("Inbox", _mail("Renewal quote for Globex", 8, ("Tom Becker", "tom@globex.example"), to=[("Sales", SALES)],
                             body="Could you send the renewal quote for 250 seats? Our contract ends on the 30th.",
                             attachments=["globex-order-form.pdf"]))
    sales.add("Inbox", _mail("Acme expansion: 40 more seats", 20, acme, read=False, to=[("Sales", SALES)],
                             body="Finance approved 40 additional seats starting next quarter."))

    def view(key: str, *, opens: tuple) -> FakeExchange:
        exchange = FakeExchange()
        exchange.own = Mailbox(key)
        exchange.shared = {SUPPORT: support, SALES: sales}
        exchange.denied = {address for address in (SUPPORT, SALES) if address not in opens}
        exchange.web_link_base = "https://mail.corp.example/owa/"
        return exchange

    dana = view("dana", opens=(SUPPORT,))
    dana.own.add_folder("Projects", parent="Inbox")
    dana.own.add_folder("Apollo", parent="Inbox/Projects")
    cfo, noa_p = ("Maya Rosen", "maya@corp.example"), ("Noa Cohen", "noa@corp.example")
    dana.own.add("Inbox", _mail("Q3 revenue numbers — final", 4, cfo, read=False, to=[("Dana Levi", "dana@corp.example")],
                                body="Dana,\n\nFinal Q3 numbers: revenue 4.82M (up 12% on Q2), gross margin 71%, "
                                     "churn 2.1%. The board deck needs the regional split by Thursday.\n\nMaya",
                                attachments=["q3-final.xlsx"]))
    dana.own.add("Inbox", _mail("Lunch on Thursday?", 6, noa_p, to=[("Dana Levi", "dana@corp.example")],
                                body="Are you free for lunch on Thursday? The new place on Herzl opened."))
    dana.own.add("Inbox", _mail("Vendor contract renewal", 50, ("Legal", "legal@corp.example"),
                                to=[("Dana Levi", "dana@corp.example")],
                                body="The analytics vendor contract renews in March. Notice period is 60 days."))
    dana.own.add("Inbox/Projects/Apollo", _mail("Apollo launch checklist", 9, noa_p, read=False,
                                                to=[("Dana Levi", "dana@corp.example")],
                                                body="Launch readiness: security review done, load test pending, "
                                                     "docs 80% complete. Go/no-go meeting is Monday 10:00."))
    dana.own.add("Sent Items", _mail("Re: Q3 revenue numbers — final", 3, ("Dana Levi", "dana@corp.example"),
                                     to=[cfo], body="Thanks Maya, regional split will be ready Wednesday."))
    dana.own.add("Deleted Items", _mail("Win a free revenue audit", 12, ("Promo", "promo@spam.example"), body="Click here."))

    noa = view("noa", opens=())
    noa.own.add("Inbox", _mail("Welcome to the team", 100, ("HR", "hr@corp.example"), to=[noa_p],
                               body="Welcome aboard, Noa. Your onboarding plan is attached.", attachments=["onboarding.pdf"]))
    noa.own.add("Inbox", _mail("Apollo load test results", 7, ("Perf Bot", "perf@corp.example"), read=False, to=[noa_p],
                               body="Load test passed at 1,200 rps with p95 latency of 180 ms."))

    return {"dana": dana, "noa": noa, "svc-mail": view("svc-mail", opens=(SUPPORT, SALES))}


def _self_signed(host: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    pem_cert = cert.public_bytes(serialization.Encoding.PEM)
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    return pem_cert, pem_key, cert.public_bytes(serialization.Encoding.DER)


def make_handler(world: dict, *, bindings, log_path, lock):
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

        def _reply(self, status, body=b"", headers=None, content_type="text/xml; charset=utf-8"):
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

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.split("?")[0].rstrip("/").lower() != EWS_PATH.lower():
                self._reply(404, b"Not Found", content_type="text/plain")
                return
            if not self._authenticate():
                return
            view = world.get(self.principal)
            if view is None:
                self._reply(403, b"", content_type="text/plain")
                return
            with lock:
                status, answer = view.handle(body)
                operation = view.operations[-1]
            record(event="request", principal=self.principal, operation=operation, status=status)
            self._reply(status, answer)

        def do_GET(self):
            self._reply(404 if self.path.split("?")[0].rstrip("/").lower() != EWS_PATH.lower() else 405,
                        b"", content_type="text/plain")

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--name", default="localhost", help="host name clients use (certificate subject)")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--require-channel-binding", action="store_true")
    parser.add_argument("--cert-out", default="mock-exchange-ca.pem")
    parser.add_argument("--log", default=None)
    args = parser.parse_args()
    if args.require_channel_binding and not args.tls:
        parser.error("--require-channel-binding needs --tls")

    users = tempfile.NamedTemporaryFile("w", suffix=".ntlm", delete=False)
    users.write("".join(f"{DOMAIN}:{user}:{password}\n" for user, password in USERS.items()))
    users.close()
    os.environ["NTLM_USER_FILE"] = users.name

    bindings = None
    server = ThreadingHTTPServer((args.host, args.port), BaseHTTPRequestHandler)
    if args.tls:
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
    server.RequestHandlerClass = make_handler(build_world(), bindings=bindings, log_path=args.log, lock=threading.Lock())
    scheme = "https" if args.tls else "http"
    print(f"mock Exchange at {scheme}://{args.name}:{args.port}{EWS_PATH}"
          + (f" (certificate: {args.cert_out})" if args.tls else "")
          + (" — channel binding required" if bindings else ""), flush=True)
    try:
        server.serve_forever()
    finally:
        os.unlink(users.name)


if __name__ == "__main__":
    main()
