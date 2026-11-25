"""Loop B — dynamic Qlik charts, end to end over real TLS sockets.

Serves `FakeQlikSite.demo()` (backend/tests/fixtures/fake_qlik_engine.py) on
localhost exactly like `2026-11-16-qlik-onprem-sheet-objects.py` (whose
certificate and port helpers it reuses), then drives the unmodified
`QlikSenseOnPremClient` through the "Dynamic Metrics" sheet:

  1. index the site and print what the agent reads for each dynamic chart;
  2. run every choice the catalog lists, with the exact call it shows, and
     compare it with the choice's explicit formula run as a plain query;
  3. show that a choice is never written back to the app.

Run from backend/:

    TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
        uv run python ../docs/feedback-loops/2026-11-17-qlik-onprem-dynamic-measures.py

Exits non-zero if any expectation fails. Loopback only, synthetic data.
"""
from __future__ import annotations

import asyncio
import importlib.util
import ssl
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sheet_loop", HERE / "2026-11-16-qlik-onprem-sheet-objects.py")
sheet_loop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sheet_loop)  # sets sys.path, NO_PROXY and quiet logging

from app.ai.context.sections.tables_schema_section import TablesSchemaContext  # noqa: E402
from app.data_sources.clients.qlik_sense_onprem_client import QlikSenseOnPremClient  # noqa: E402
from app.schemas.data_source_schema import DataSourceSummarySchema  # noqa: E402
from tests.fixtures.fake_qlik_engine import FakeQlikSite  # noqa: E402

APP = "Sales/Consumer Sales"
SHEET = f"{APP}/Sheets/Dynamic Metrics"
check = sheet_loop.check


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fake_qlik_"))
    cert_pem, key_pem = sheet_loop.self_signed(tmp)
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(tmp / "cert.pem", tmp / "key.pem")

    site = FakeQlikSite.demo()
    qrs_port, engine_port = sheet_loop.free_port(), sheet_loop.free_port()
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

    client = QlikSenseOnPremClient(
        server_url="https://localhost", client_cert=cert_pem, client_key=key_pem,
        qrs_port=qrs_port, engine_port=engine_port, verify_ssl=False,
    )
    try:
        print("1. what the agent reads for the dynamic sheet\n")
        tables = client.get_schemas()
        sheet = next(t for t in tables if t.name == SHEET)
        ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Qlik", type="qlik_sense_onprem"), tables=[sheet])])
        rendered = ctx.render("full")
        for line in rendered.splitlines():
            if line.startswith("<column"):
                print("   ", line, "\n")
        dynamic = [c for c in sheet.columns if (c.metadata or {}).get("options")]
        switches = [c for c in sheet.columns if (c.metadata or {}).get("role") == "switch"]
        check("switches catalogued", len(switches) == 3, ", ".join(c.name for c in switches))
        check("dynamic charts catalogued", len(dynamic) == 3, ", ".join(c.name for c in dynamic))

        print("\n2. every listed choice, run with the call the catalog shows\n")
        for column in dynamic:
            object_id = column.metadata["objectId"]
            for option in column.metadata["options"]:
                run = option["run"]
                got = client.execute_query(app=APP, object_id=object_id, **run)
                dims = [d["field"] for d in option["dimensions"]]
                expected = client.execute_query(
                    app=APP, dimensions=dims, filters=run.get("filters"),
                    measures=[{"expr": m["expression"], "alias": m.get("label") or m["expression"]}
                              for m in option["measures"]],
                )
                same = (list(got.columns) == list(expected.columns)
                        and got.set_index(dims[0]).sort_index().equals(expected.set_index(dims[0]).sort_index()))
                total = ", ".join(f"{c}={got[c].sum():g}" for c in got.columns[1:])
                check(f"{column.name} when {option['when']}", same, f"{run} -> {total}")

        print("\n3. a choice is local to the query")
        before = client.execute_query(app=APP, object_id="obj-dyn-bar")
        client.execute_query(app=APP, object_id="obj-dyn-bar", variables={"vMetric": "Quantity"})
        after = client.execute_query(app=APP, object_id="obj-dyn-bar")
        check("default unchanged after a choice", before.equals(after))
        writes = {"SetStringValue", "SetNumValue", "SetProperties", "ApplyPatches", "DoSave"}
        check("no write calls reached the Engine", not writes.intersection(site.methods()))
    finally:
        client.close()

    print("\nRESULT:", "PASS" if not sheet_loop.FAILURES else f"FAIL ({len(sheet_loop.FAILURES)}): {sheet_loop.FAILURES}")
    return 0 if not sheet_loop.FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
