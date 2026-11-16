"""Loop B — the Qlik on-prem client against a fake site over real TLS sockets.

Serves `FakeQlikSite.demo()` (backend/tests/fixtures/fake_qlik_engine.py) as a
QSEoW site on localhost — QRS on https://localhost:<port>/qrs and the Engine on
wss://localhost:<port>/app/<id>, both TLS with a throwaway self-signed
certificate — then drives the unmodified `QlikSenseOnPremClient` through the
real `requests` + `websockets` transports:

  1. index the site (`get_schemas`) and render what the agent would see;
  2. read an existing chart (`object_id`), with and without a selection;
  3. look a value up (`search`);
  4. send a misspelled measure (expression check).

Run from backend/:

    TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
        uv run python ../docs/feedback-loops/2026-11-16-qlik-onprem-sheet-objects.py

Exits non-zero if any expectation fails. No credentials, no network beyond
loopback, synthetic data only.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import socket
import ssl
import sys
import tempfile
import threading
import logging
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))
# Loopback must not be sent through an outbound proxy.
for var in ("NO_PROXY", "no_proxy"):
    os.environ[var] = ",".join(filter(None, [os.environ.get(var), "localhost", "127.0.0.1"]))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from app.ai.context.sections.tables_schema_section import TablesSchemaContext  # noqa: E402
from app.data_sources.clients.qlik_sense_onprem_client import QlikSenseOnPremClient  # noqa: E402
from app.schemas.data_source_schema import DataSourceSummarySchema  # noqa: E402
from tests.fixtures.fake_qlik_engine import FakeQlikSite  # noqa: E402

APP = "Sales/Consumer Sales"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def self_signed(tmp: Path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    (tmp / "cert.pem").write_text(cert_pem)
    (tmp / "key.pem").write_text(key_pem)
    return cert_pem, key_pem


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fake_qlik_"))
    cert_pem, key_pem = self_signed(tmp)
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(tmp / "cert.pem", tmp / "key.pem")

    site = FakeQlikSite.demo()
    qrs_port, engine_port = free_port(), free_port()
    site.serve_qrs("127.0.0.1", qrs_port, server_ctx)

    ready = threading.Event()

    def run_engine() -> None:
        async def runner():
            async with site.serve_engine("127.0.0.1", engine_port, server_ctx):
                ready.set()
                await asyncio.Future()

        asyncio.run(runner())

    threading.Thread(target=run_engine, daemon=True).start()
    ready.wait(10)
    print(f"fake QRS on https://localhost:{qrs_port}/qrs, Engine on wss://localhost:{engine_port}/app/*")

    client = QlikSenseOnPremClient(
        server_url="https://localhost",
        client_cert=cert_pem,
        client_key=key_pem,
        qrs_port=qrs_port,
        engine_port=engine_port,
        verify_ssl=False,
    )
    try:
        print("\n1. test_connection + index")
        probe = client.test_connection()
        check("QRS reachable over TLS", probe.get("success") is True, probe.get("message", ""))
        tables = client.get_schemas()
        names = sorted(t.name for t in tables)
        for n in names:
            print(f"     catalog: {n} ({len(next(t for t in tables if t.name == n).columns)} columns)")
        check("approved sheet catalogued", f"{APP}/Sheets/Revenue Overview" in names)
        check("community sheet catalogued", f"{APP}/Sheets/Regional Deep Dive" in names)
        check("private sheet absent", not any("Private" in n for n in names))

        ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Qlik", type="qlik_sense_onprem"), tables=tables)])
        rendered = ctx.render("full")
        sheet_xml = rendered[rendered.find("Sheets/Revenue Overview") - 20:]
        sheet_xml = sheet_xml[: sheet_xml.find("</table>") + 8]
        print("\n   what the agent sees for the overview sheet:\n")
        print("\n".join("     " + line for line in sheet_xml.splitlines()))
        check("variable rendered", 'name="vCurrentYear" dtype="variable"' in rendered)
        check("object ids rendered", "id=obj-rev-region" in rendered)
        check("private object never rendered", "Unreviewed margin" not in rendered)

        print("\n2. read an existing chart")
        df = client.execute_query(app=APP, object_id="obj-rev-region")
        print(df.to_string(index=False).replace("\n", "\n     ").join(["     ", ""]))
        check("chart columns", list(df.columns) == ["Region Name", "Revenue"])
        check("chart sort (desc by Revenue)", list(df["Revenue"]) == sorted(df["Revenue"], reverse=True))
        df23 = client.execute_query(app=APP, object_id="obj-rev-region", filters={"Year": [2023]})
        check("selection narrows the chart", df23["Revenue"].sum() < df["Revenue"].sum(),
              f"{df23['Revenue'].sum():.1f} < {df['Revenue'].sum():.1f}")
        pivot = client.execute_query(app=APP, object_id="obj-mix")
        check("pivot read flat", len(pivot) == 12, f"{len(pivot)} rows, columns {list(pivot.columns)}")

        print("\n3. look a value up")
        found = client.execute_query(app=APP, search="north")
        print(found.to_string(index=False).replace("\n", "\n     ").join(["     ", ""]))
        check("search finds spelling", ("Region Name", "Northeast") in set(zip(found["field"], found["value"], strict=True)))

        print("\n4. misspelled measure")
        try:
            out = client.execute_query(app=APP, dimensions=["Region Name"],
                                       measures=[{"expr": "Sum([Sales Qty])", "alias": "Qty"}])
            check("typo rejected", False, f"returned {out['Qty'].tolist()}")
        except ValueError as e:
            print(f"     ValueError: {e}")
            check("typo rejected with the field name", "Sales Qty" in str(e))
    finally:
        client.close()

    print(f"\nEngine calls observed: {len(site.calls)}; methods: {sorted(set(site.methods()))}")
    print("\nRESULT:", "PASS" if not FAILURES else f"FAIL ({len(FAILURES)}): {FAILURES}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
