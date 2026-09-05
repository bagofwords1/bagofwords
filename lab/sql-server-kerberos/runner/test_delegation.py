"""End-to-end Kerberos constrained-delegation test for the BoW SQL Server path.

Runs inside the lab runner container against the Samba AD DC and SQL Server
2022. Two tiers:

  Tier A — delegation core (no SQL Server needed): using the *production*
    KerberosTicketManager, acquire a delegated (S4U2Self) credential for an AD
    user with only the service keytab, then drive S4U2Proxy by initiating a
    GSSAPI context to each MSSQLSvc SPN. Proves the exact code in
    app/data_sources/kerberos.py performs protocol transition + constrained
    delegation against a real KDC.

  Tier B — SQL last mile: use the production MSSQLClient with
    use_kerberos + kerberos_impersonate to connect to SQL Server 2022
    and confirm service-account and delegated queries run under the expected identity
    (SUSER_SNAME() == BOWLAB\\alice, auth_scheme == KERBEROS).

An unreachable SQL instance fails Tier B, so a green suite always proves the
whole delegation path rather than only the KDC half.
"""
from __future__ import annotations

import os
import socket
import stat
from concurrent.futures import ThreadPoolExecutor

import gssapi
import pytest

# The production module under test (mounted from the repo at /app). Tier A
# depends only on this — it imports only stdlib + lazy gssapi. MSSQLClient
# (Tier B) is imported lazily inside those tests so Tier A runs regardless.
from app.data_sources.kerberos import KerberosTicketManager

REALM = "BOWLAB.LOCAL"
SVC_KEYTAB = "/keytabs/svc-bow.keytab"
ALICE = f"alice@{REALM}"
BOB = f"bob@{REALM}"

SQL_TARGETS = [
    ("2022", "sql2022.bowlab.local", 1433),
]


def _manager() -> KerberosTicketManager:
    return KerberosTicketManager(ccache_dir="/tmp/bow_krb5_lab", client_keytab=SVC_KEYTAB)


def _reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Tier A: delegation core ─────────────────────────────────────────────────


def test_service_keytab_present():
    assert os.path.exists(SVC_KEYTAB), "svc-bow keytab not exported by the DC"
    assert stat.S_IMODE(os.stat(SVC_KEYTAB).st_mode) == 0o600


def test_s4u2self_acquires_delegated_ccache():
    """S4U2Self: impersonate alice with only the service keytab (no password)."""
    mgr = _manager()
    ccache = mgr.delegated_ccache(ALICE)
    assert os.path.exists(ccache)
    # Cached on second call (no re-acquisition).
    assert mgr.delegated_ccache(ALICE) == ccache


@pytest.mark.parametrize("version,host,port", SQL_TARGETS)
def test_s4u2proxy_to_mssql_spn(version, host, port):
    """S4U2Proxy: the delegated cred yields a service ticket to MSSQLSvc/<sql>.

    Initiating a GSSAPI context to the SPN triggers the S4U2Proxy request to the
    KDC. Success (a token is produced) proves constrained delegation works — no
    SQL Server handshake required.
    """
    mgr = _manager()
    ccache = mgr.delegated_ccache(ALICE)
    spn = gssapi.Name(f"MSSQLSvc/{host}:{port}", gssapi.NameType.kerberos_principal)
    with mgr.activate(ccache):
        store = {"ccache": f"FILE:{ccache}"}
        cred = gssapi.Credentials(usage="initiate", store=store)
        ctx = gssapi.SecurityContext(name=spn, creds=cred, usage="initiate")
        token = ctx.step()
    assert token, f"no S4U2Proxy token produced for SQL {version} SPN"


# ── Tier B: SQL Server last mile ─────────────────────────────────────────────


@pytest.mark.parametrize("version,host,port", SQL_TARGETS)
def test_mssql_client_service_query(version, host, port):
    """MSSQLClient authenticates directly as the BOW service account."""
    if not _reachable(host, port):
        pytest.fail(f"SQL Server {version} ({host}:{port}) not reachable")
    from app.data_sources.clients.mssql_client import MSSQLClient

    client = MSSQLClient(
        host=host, port=port, database="bowlab",
        use_kerberos=True,
        encrypt=False,
    )
    df = client.execute_query(
        "SELECT SUSER_SNAME() AS who, "
        "(SELECT auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID) AS scheme"
    )
    assert str(df.iloc[0]["who"]).upper().endswith("SVC-BOW")
    assert str(df.iloc[0]["scheme"]).upper() == "KERBEROS"
    assert any(table.name.lower().endswith(".sales") for table in client.get_schemas())


@pytest.mark.parametrize("version,host,port", SQL_TARGETS)
def test_concurrent_service_and_delegated_handshakes_keep_identity(version, host, port):
    """Default service-cache and delegated handshakes cannot swap identities."""
    if not _reachable(host, port):
        pytest.fail(f"SQL Server {version} ({host}:{port}) not reachable")
    from app.data_sources.clients.mssql_client import MSSQLClient

    def query(expected: str) -> str:
        client = MSSQLClient(
            host=host, port=port, database="bowlab",
            use_kerberos=True,
            kerberos_impersonate=ALICE if expected == "alice" else None,
            encrypt=False,
        )
        return str(client.execute_query("SELECT SUSER_SNAME() AS who").iloc[0]["who"])

    expected = ["service", "alice"] * 8
    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = list(pool.map(query, expected))
    for identity, who in zip(expected, actual):
        suffix = "SVC-BOW" if identity == "service" else "ALICE"
        assert who.upper().endswith(suffix), (identity, who)


@pytest.mark.parametrize("version,host,port", SQL_TARGETS)
def test_mssql_client_delegated_query(version, host, port):
    """MSSQLClient impersonating alice runs a query under her AD identity."""
    if not _reachable(host, port):
        pytest.fail(f"SQL Server {version} ({host}:{port}) not reachable")
    from app.data_sources.clients.mssql_client import MSSQLClient

    client = MSSQLClient(
        host=host, port=port, database="bowlab",
        use_kerberos=True, kerberos_impersonate=ALICE,
        encrypt=False,
    )
    df = client.execute_query(
        "SELECT SUSER_SNAME() AS who, "
        "(SELECT auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID) AS scheme"
    )
    who = str(df.iloc[0]["who"])
    scheme = str(df.iloc[0]["scheme"])
    assert who.upper().endswith("ALICE"), f"expected BOWLAB\\alice, got {who!r}"
    assert scheme.upper() == "KERBEROS", f"expected KERBEROS, got {scheme!r}"

    # And the impersonated user can read the data she was granted.
    rows = client.execute_query("SELECT COUNT(*) AS n FROM dbo.sales")
    assert int(rows.iloc[0]["n"]) == 3


@pytest.mark.parametrize("version,host,port", SQL_TARGETS)
def test_delegation_respects_sql_permissions(version, host, port):
    """bob has a login but no read grant → per-user identity really reaches SQL."""
    if not _reachable(host, port):
        pytest.fail(f"SQL Server {version} ({host}:{port}) not reachable")
    from app.data_sources.clients.mssql_client import MSSQLClient

    client = MSSQLClient(
        host=host, port=port, database="bowlab",
        use_kerberos=True, kerberos_impersonate=BOB,
        encrypt=False,
    )
    with pytest.raises(Exception) as exc:
        client.execute_query("SELECT * FROM dbo.sales")
    error = str(exc.value).lower()
    assert "permission" in error and "sales" in error
