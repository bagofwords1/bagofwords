"""A stand-in Mapepire server for simulating Db2 for IBM i in tests.

A real IBM i cannot run in a container. Mapepire's wire protocol is small
(JSON over a TLS WebSocket at ``/db/`` with HTTP Basic auth), so this server
speaks it and executes the SQL on any DB-API database: a Db2 LUW test server
(where `tests/integrations/db2_sim.py` created ``QSYS2`` catalog stand-ins) or
sqlite in unit tests. IBM's real `mapepire-python` client and `IbmiClient` run
unchanged against it.

Requests handled: connect, prepare_sql_execute, sql, sqlmore, sqlclose, exit.
Every connect's job properties are recorded in ``connect_props`` so tests can
assert what the client asked the IBM i job for (SQL naming, read-only access).

Usage::

    srv = FakeMapepireServer(lambda: ibm_db_dbi.connect(dsn, "", ""), user="bow", password="secret")
    srv.start()            # TLS with a fresh self-signed cert; srv.cert_pem
    ...                    # IbmiClient(host="127.0.0.1", port=srv.port, ...)
    srv.stop()
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import os
import ssl
import tempfile
import threading
from typing import Any


def _self_signed_cert(host: str = "127.0.0.1") -> tuple[str, str]:
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address(host)), x509.DNSName("localhost"),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


class FakeMapepireServer:
    def __init__(self, connect, user: str = "bow", password: str = "secret",
                 host: str = "127.0.0.1", port: int = 0, first_batch: int = 100):
        # `connect`: zero-arg callable returning a DB-API connection (qmark params).
        self._connect = connect
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.first_batch = first_batch
        self.connect_props: list[str] = []
        self.statements: list[str] = []
        self.cert_pem = ""
        self._server = None
        self._thread = None
        self._terse: dict[str, bool] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeMapepireServer":
        from websockets.sync.server import serve

        self.cert_pem, key_pem = _self_signed_cert(self.host)
        d = tempfile.mkdtemp(prefix="fake-mapepire-")
        cert_path, key_path = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        with open(cert_path, "w") as f:
            f.write(self.cert_pem)
        with open(key_path, "w") as f:
            f.write(key_pem)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)

        self._server = serve(self._handle, self.host, self.port, ssl=ctx,
                             process_request=self._auth)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # -- protocol ----------------------------------------------------------

    def _auth(self, connection, request):
        expected = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return connection.respond(401, "Unauthorized\n")
        if request.path.rstrip("/") != "/db":
            return connection.respond(404, "Not found\n")
        return None

    def _handle(self, ws) -> None:
        conn = None
        cursors: dict[str, Any] = {}
        try:
            for raw in ws:
                req = json.loads(raw)
                rid, kind = req.get("id"), req.get("type")
                if kind == "connect":
                    self.connect_props.append(req.get("props", ""))
                    conn = self._connect()
                    ws.send(json.dumps({"id": rid, "success": True, "job": "123456/QUSER/QZDASOINIT"}))
                elif kind in ("prepare_sql_execute", "sql"):
                    ws.send(json.dumps(self._execute(conn, cursors, req)))
                elif kind == "sqlmore":
                    cur = cursors.get(req.get("cont_id"))
                    if cur is None:
                        ws.send(json.dumps({"id": rid, "success": False, "error": "Query not found"}))
                        continue
                    data, done = self._rows(cur, int(req.get("rows") or 100), self._terse.get(req.get("cont_id"), False))
                    if done:
                        cursors.pop(req.get("cont_id"), None)
                    ws.send(json.dumps({"id": rid, "success": True, "data": data, "is_done": done}))
                elif kind == "sqlclose":
                    cursors.pop(req.get("cont_id"), None)
                    ws.send(json.dumps({"id": rid, "success": True}))
                elif kind == "exit":
                    ws.send(json.dumps({"id": rid, "success": True}))
                    break
                else:
                    ws.send(json.dumps({"id": rid, "success": False, "error": f"unsupported: {kind}"}))
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _rows(cur, n: int, terse: bool = False) -> tuple[list, bool]:
        names = [d[0] for d in cur.description]
        batch = cur.fetchmany(n)
        if terse:
            data = [[_jsonable(v) for v in row] for row in batch]
        else:
            # Like the real server: rows keyed by column name, so a repeated
            # name keeps only its last value.
            data = [{k: _jsonable(v) for k, v in zip(names, row)} for row in batch]
        return data, len(batch) < n

    def _execute(self, conn, cursors: dict, req: dict) -> dict:
        rid = req.get("id")
        sql = req.get("sql", "")
        self.statements.append(sql)
        cur = conn.cursor()
        try:
            params = req.get("parameters")
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
        except Exception as e:
            msg = str(e)
            state = ""
            if "SQLSTATE=" in msg:
                state = msg.split("SQLSTATE=", 1)[1][:5]
            return {"id": rid, "success": False, "error": msg.split("] ")[-1][:500],
                    "sql_state": state, "sql_rc": -1, "has_results": False, "is_done": True}
        if not cur.description:
            return {"id": rid, "success": True, "has_results": False, "is_done": True,
                    "update_count": cur.rowcount, "data": [],
                    "metadata": {"column_count": 0, "columns": [], "job": ""}}
        columns = [
            {"display_size": d[2] or 0, "label": d[0], "name": d[0],
             "type": str(d[1] or ""), "precision": d[4], "scale": d[5]}
            for d in cur.description
        ]
        terse = bool(req.get("terse"))
        data, done = self._rows(cur, self.first_batch, terse)
        if not done:
            cursors[rid] = cur
            self._terse[rid] = terse
        return {"id": rid, "success": True, "has_results": True, "is_done": done,
                "update_count": -1, "data": data,
                "metadata": {"column_count": len(columns), "columns": columns, "job": ""}}


if __name__ == "__main__":
    import sys
    import time

    import ibm_db_dbi

    dsn = sys.argv[1]
    srv = FakeMapepireServer(lambda: ibm_db_dbi.connect(dsn, "", ""),
                             port=int(sys.argv[2]) if len(sys.argv) > 2 else 8076,
                             user=os.environ.get("FAKE_MAPEPIRE_USER", "bow"),
                             password=os.environ.get("FAKE_MAPEPIRE_PASSWORD", "secret")).start()
    cert_out = os.environ.get("FAKE_MAPEPIRE_CERT_OUT")
    if cert_out:
        with open(cert_out, "w") as f:
            f.write(srv.cert_pem)
    print(f"fake mapepire on {srv.host}:{srv.port}", flush=True)
    while True:
        time.sleep(3600)
