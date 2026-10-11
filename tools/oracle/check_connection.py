#!/usr/bin/env python3
"""Check connectivity to an Oracle database.

Performs, in order, and reports each step:
  1. TCP reachability of host:port
  2. Oracle logon (thick mode when Oracle Instant Client is installed, like the
     app itself; thin mode otherwise or with --thin)
  3. A round-trip query (SELECT 1 FROM DUAL)
  4. Server banner + session info (version, instance, current schema)

Usage:
  python tools/oracle/check_connection.py --host db.example.com --port 1521 \
      --service-name ORCLPDB1 --user scott --password tiger

  # or via environment variables (ORACLE_HOST, ORACLE_PORT, ORACLE_SERVICE_NAME,
  # ORACLE_USER, ORACLE_PASSWORD, ORACLE_SCHEMA, ORACLE_TCPS, ORACLE_VERIFY_SSL,
  # ORACLE_THICK_MODE):
  ORACLE_HOST=... ORACLE_USER=... ORACLE_PASSWORD=... python tools/oracle/check_connection.py

Exit codes: 0 ok, 1 connection/query failed, 2 bad arguments / missing driver.

Requires: pip install oracledb   (already a backend dependency)
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time
from getpass import getpass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=os.getenv("ORACLE_HOST"), help="DB host (env ORACLE_HOST)")
    p.add_argument("--port", type=int, default=int(os.getenv("ORACLE_PORT", "1521")), help="Listener port (env ORACLE_PORT, default 1521)")
    p.add_argument("--service-name", default=os.getenv("ORACLE_SERVICE_NAME"), help="Service name (env ORACLE_SERVICE_NAME)")
    p.add_argument("--user", default=os.getenv("ORACLE_USER"), help="Username (env ORACLE_USER)")
    p.add_argument("--password", default=os.getenv("ORACLE_PASSWORD"), help="Password (env ORACLE_PASSWORD; prompted if omitted)")
    p.add_argument("--schema", default=os.getenv("ORACLE_SCHEMA"), help="Optional schema to ALTER SESSION SET CURRENT_SCHEMA to (env ORACLE_SCHEMA)")
    p.add_argument("--tcps", action="store_true", default=_env_bool("ORACLE_TCPS", False), help="Use TCPS (TLS) protocol (env ORACLE_TCPS)")
    p.add_argument("--no-verify-ssl", action="store_true", default=not _env_bool("ORACLE_VERIFY_SSL", True), help="Skip TLS cert verification; thin mode only (env ORACLE_VERIFY_SSL=0)")
    p.add_argument("--thin", action="store_true", default=not _env_bool("ORACLE_THICK_MODE", True), help="Stay in thin mode even if Oracle Instant Client is installed (env ORACLE_THICK_MODE=0). Default: try thick mode, same as the app")
    p.add_argument("--timeout", type=float, default=float(os.getenv("ORACLE_TIMEOUT", "10")), help="Connect timeout in seconds (default 10)")
    a = p.parse_args()

    missing = [n for n, v in (("--host", a.host), ("--service-name", a.service_name), ("--user", a.user)) if not v]
    if missing:
        p.error("missing required: " + ", ".join(missing))
    if a.password is None:
        a.password = getpass(f"Password for {a.user}@{a.host}: ")
    return a


def step(label: str) -> None:
    print(f"[..] {label}", end="", flush=True)


def ok(msg: str = "", started: float | None = None) -> None:
    dur = f" ({(time.perf_counter() - started) * 1000:.0f} ms)" if started is not None else ""
    print(f"\r[OK] {msg}{dur}")


def fail(msg: str) -> None:
    print(f"\r[!!] {msg}")


def check_tcp(host: str, port: int, timeout: float) -> bool:
    step(f"TCP connect {host}:{port}")
    t = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        fail(f"TCP connect {host}:{port} failed: {e}")
        return False
    ok(f"TCP connect {host}:{port}", t)
    return True


def main() -> int:
    a = parse_args()

    try:
        import oracledb
    except ImportError:
        print("python-oracledb is not installed. Run: pip install oracledb", file=sys.stderr)
        return 2

    if not check_tcp(a.host, a.port, a.timeout):
        return 1

    # Driver mode. Thick mode is process-wide and must be set before any connection.
    mode = "thin"
    if not a.thin:
        try:
            oracledb.init_oracle_client()
            mode = "thick"
        except Exception as e:
            print(f"[--] Oracle Instant Client not loaded, staying thin (pass --thin to silence): {e}")
    print(f"[--] python-oracledb {oracledb.__version__}, {mode} mode")

    protocol = "TCPS" if a.tcps else "TCP"
    dsn = (
        f"(DESCRIPTION=(CONNECT_TIMEOUT={int(a.timeout)})"
        f"(ADDRESS=(PROTOCOL={protocol})(HOST={a.host})(PORT={a.port}))"
        f"(CONNECT_DATA=(SERVICE_NAME={a.service_name})))"
    )
    connect_kwargs: dict = {"user": a.user, "password": a.password, "dsn": dsn}
    if a.tcps and a.no_verify_ssl:
        connect_kwargs["ssl_server_dn_match"] = False
        if oracledb.is_thin_mode():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_kwargs["ssl_context"] = ctx
        else:
            print("[--] --no-verify-ssl is only honoured in thin mode; thick mode trusts the Oracle wallet")

    step(f"Logon {a.user}@{a.service_name} over {protocol}")
    t = time.perf_counter()
    try:
        conn = oracledb.connect(**connect_kwargs)
    except Exception as e:
        fail(f"Logon failed: {e}")
        _hint(str(e), mode)
        return 1
    ok(f"Logon {a.user}@{a.service_name} over {protocol}", t)

    rc = 0
    try:
        with conn.cursor() as cur:
            step("SELECT 1 FROM DUAL")
            t = time.perf_counter()
            cur.execute("SELECT 1 FROM DUAL")
            row = cur.fetchone()
            if row and row[0] == 1:
                ok("SELECT 1 FROM DUAL", t)
            else:
                fail(f"SELECT 1 FROM DUAL returned unexpected row: {row}")
                rc = 1

            if a.schema:
                step(f"ALTER SESSION SET CURRENT_SCHEMA = {a.schema}")
                t = time.perf_counter()
                try:
                    cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {a.schema}")
                    ok(f"ALTER SESSION SET CURRENT_SCHEMA = {a.schema}", t)
                except Exception as e:
                    fail(f"Could not switch to schema {a.schema}: {e}")
                    rc = 1

            step("Session info")
            t = time.perf_counter()
            cur.execute(
                "SELECT SYS_CONTEXT('USERENV','DB_NAME'), SYS_CONTEXT('USERENV','INSTANCE_NAME'), "
                "SYS_CONTEXT('USERENV','CURRENT_SCHEMA'), SYS_CONTEXT('USERENV','SERVER_HOST') FROM DUAL"
            )
            db_name, instance, cur_schema, server_host = cur.fetchone()
            ok("Session info", t)
            print(f"     version        : {conn.version}")
            print(f"     db_name        : {db_name}")
            print(f"     instance       : {instance}")
            print(f"     server_host    : {server_host}")
            print(f"     current_schema : {cur_schema}")

            try:
                cur.execute("SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER = SYS_CONTEXT('USERENV','CURRENT_SCHEMA')")
                (n_tables,) = cur.fetchone()
                print(f"     tables visible : {n_tables} in {cur_schema}")
            except Exception as e:
                print(f"     tables visible : n/a ({e})")
    except Exception as e:
        fail(f"Query failed: {e}")
        rc = 1
    finally:
        conn.close()

    print("\nRESULT:", "CONNECTION OK" if rc == 0 else "CONNECTION FAILED")
    return rc


def _hint(err: str, mode: str) -> None:
    """Print a short remediation hint for the most common failures."""
    hints = {
        "ORA-01017": "invalid username/password",
        "ORA-12514": "listener does not know this SERVICE_NAME; check `lsnrctl services` or use the PDB service name",
        "ORA-12541": "no listener on host:port",
        "ORA-12170": "connect timeout; firewall or wrong host/port",
        "ORA-28000": "account is locked",
        "ORA-28001": "password has expired",
        "DPY-3010": "server is older than Oracle 12.1; thin mode cannot connect, thick mode (Oracle Instant Client) is required",
        "DPY-3015": "account uses a 10G password verifier; thin mode cannot log on, thick mode (Oracle Instant Client) is required",
        "DPY-4011": "connection reset by peer; usually Native Network Encryption on the server, thick mode (Oracle Instant Client) is required",
        "DPY-6000": "listener refused the connection; check SERVICE_NAME",
        "CERTIFICATE_VERIFY_FAILED": "TLS cert not trusted; use --no-verify-ssl (thin) or configure a wallet (thick)",
        # DPY-6005 is a generic wrapper around a more specific inner error, so it is matched last
        "DPY-6005": "cannot connect to database; check host/port/protocol (TCP vs TCPS)",
    }
    for code, hint in hints.items():
        if code in err:
            print(f"     hint: {hint} [{mode} mode]")
            return


if __name__ == "__main__":
    sys.exit(main())
