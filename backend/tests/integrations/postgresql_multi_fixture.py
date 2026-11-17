"""One Postgres server holding several tenant databases, for postgresql_multi tests.

The tenants are chosen for the cases a naive unifier gets wrong:

* `tenant_a` and `Tenant_A` differ only by case, which DuckDB catalog aliases
  cannot tell apart
* `public` is named like the schema every tenant uses
* `tenant_old` predates `orders.note`, so the unified view must read NULL there
* `tenant_locked` exists and holds the same tables, but the reader role has no
  CONNECT privilege on it

Row counts differ per tenant so a test that mixes two tenants up fails. User
ids overlap between tenants, as separate id spaces do in production.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg2
import pytest

READER_USER = "bow_reader"
READER_PASSWORD = "reader-pw'\\x"

TENANT_ROWS: dict[str, dict[str, list[tuple]]] = {
    "tenant_a": {
        "users": [(1, "ada"), (2, "bob")],
        "orders": [(1, 1, Decimal("10.50"), "first"), (2, 2, Decimal("20.00"), None), (3, 1, Decimal("7.25"), "again")],
    },
    "Tenant_A": {
        "users": [(1, "cy"), (2, "di"), (3, "ed"), (4, "flo")],
        "orders": [
            (1, 1, Decimal("100.00"), "x"),
            (2, 2, Decimal("1.00"), "y"),
            (3, 3, Decimal("2.50"), None),
            (4, 4, Decimal("3.75"), "z"),
        ],
    },
    "public": {
        "users": [(5, "gus"), (6, "hal")],
        "orders": [(1, 5, Decimal("42.00"), "p"), (2, 6, Decimal("8.00"), "q")],
    },
    "tenant_old": {
        "users": [(1, "ivy"), (7, "jo")],
        "orders": [
            (1, 1, Decimal("5.00")),
            (2, 7, Decimal("6.00")),
            (3, 7, Decimal("7.00")),
            (4, 1, Decimal("8.00")),
            (5, 7, Decimal("9.00")),
        ],
    },
    "tenant_locked": {
        "users": [(9, "kim")],
        "orders": [(1, 9, Decimal("99.00"), "secret")],
    },
}

READABLE_TENANTS = ["tenant_a", "Tenant_A", "public", "tenant_old"]
LOCKED_TENANT = "tenant_locked"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(not _docker_available(), reason="Docker daemon not reachable")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class MultiTenantServer:
    host: str
    port: int
    admin_user: str
    admin_password: str
    reader_user: str = READER_USER
    reader_password: str = READER_PASSWORD
    tenants: dict[str, dict[str, list[tuple]]] = field(default_factory=lambda: TENANT_ROWS)

    def connect(self, database: str, *, admin: bool = True):
        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=database,
            user=self.admin_user if admin else self.reader_user,
            password=self.admin_password if admin else self.reader_password,
        )

    def query(self, database: str, sql: str) -> list[tuple]:
        conn = self.connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        finally:
            conn.close()

    def create_database(self, name: str) -> None:
        conn = self.connect("postgres")
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE {_quote_ident(name)}")
        finally:
            conn.close()


def _seed_tenant(server: MultiTenantServer, name: str, rows: dict[str, list[tuple]]) -> None:
    has_note = all(len(row) == 4 for row in rows["orders"])
    conn = server.connect(name)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE public.users (id int PRIMARY KEY, name text)")
            cur.execute(
                "CREATE TABLE public.orders (id int PRIMARY KEY, user_id int REFERENCES public.users(id),"
                " total numeric" + (", note text" if has_note else "") + ")"
            )
            cur.executemany("INSERT INTO public.users VALUES (%s, %s)", rows["users"])
            placeholders = ", ".join(["%s"] * len(rows["orders"][0]))
            cur.executemany(f"INSERT INTO public.orders VALUES ({placeholders})", rows["orders"])
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {READER_USER}")
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER_USER}")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def running_multi_tenant_server() -> Iterator[MultiTenantServer]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15") as container:
        server = MultiTenantServer(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            admin_user=container.username,
            admin_password=container.password,
        )
        admin = server.connect(container.dbname)
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                escaped = READER_PASSWORD.replace("'", "''")
                cur.execute(f"CREATE ROLE {READER_USER} LOGIN PASSWORD '{escaped}'")
                for name in TENANT_ROWS:
                    cur.execute(f"CREATE DATABASE {_quote_ident(name)}")
                cur.execute(f"REVOKE CONNECT ON DATABASE {_quote_ident(LOCKED_TENANT)} FROM PUBLIC")
        finally:
            admin.close()
        for name, rows in TENANT_ROWS.items():
            _seed_tenant(server, name, rows)
        yield server
