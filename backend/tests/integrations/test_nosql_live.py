"""DynamoDB, Cassandra, Couchbase and Cosmos DB clients against real servers
(DynamoDB Local, Apache Cassandra 5, Couchbase Server 7.6, the Cosmos DB
Linux emulator). Needs Docker.

Every backend is seeded with the same 60 orders over 6 customers, then held
to one contract: the catalog names the table and its key, a key-scoped query
returns exactly that customer's orders, an aggregate (server-side where the
dialect has one) matches the seed, a full read is capped and flagged
`truncated`, and writes are refused before reaching the server.

Run:
    cd backend
    uv run pytest tests/integrations/test_nosql_live.py -v
    uv run pytest tests/integrations/test_nosql_live.py -v -k cassandra
"""
from __future__ import annotations

import time
import uuid

import pytest

tc_core = pytest.importorskip("testcontainers.core.container")

ORDERS = [
    {"id": f"o{i:02d}", "customer_id": f"c-{i % 6}", "status": ["OPEN", "SHIPPED", "CANCELLED"][i % 3],
     "total": round(10.5 + i * 3.25, 2), "city": ["Paris", "Austin", "Tel Aviv"][i % 3]}
    for i in range(60)
]
CUSTOMER = "c-1"
CUSTOMER_TOTAL = round(sum(o["total"] for o in ORDERS if o["customer_id"] == CUSTOMER), 2)
STATUS_COUNTS = {s: sum(1 for o in ORDERS if o["status"] == s) for s in ("OPEN", "SHIPPED", "CANCELLED")}
CAP = 25


# Containers started for the current backend; stopped on teardown even when
# seeding fails half-way (otherwise a failed setup leaks a running server).
_STARTED: list = []


def _stop_started() -> None:
    while _STARTED:
        try:
            _STARTED.pop().stop()
        except Exception:
            pass


def _start(image, ports, env=None, command=None):
    c = tc_core.DockerContainer(image)
    for p in ports:
        c = c.with_exposed_ports(p)
    for k, v in (env or {}).items():
        c = c.with_env(k, v)
    if command:
        c = c.with_command(command)
    try:
        c.start()
        _STARTED.append(c)
    except Exception as e:  # no Docker here
        pytest.skip(f"Docker / {image} unavailable: {e}")
    return c


def _retry(fn, timeout=180, every=2):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            return fn()
        except Exception as e:  # server still starting
            last = e
            time.sleep(every)
    raise TimeoutError(f"not ready after {timeout}s: {last}")


class Backend:
    """A seeded server + how to phrase the contract's queries in its dialect."""

    def __init__(self, name, client, table, by_customer, read_all, writes, status_counts):
        self.name, self.client, self.table = name, client, table
        self.by_customer, self.read_all, self.writes = by_customer, read_all, writes
        self.status_counts = status_counts  # df -> {status: count}


# ----------------------------------------------------------------- backends


def _dynamodb():
    import boto3

    from app.data_sources.clients.dynamodb_client import DynamodbClient

    c = _start("amazon/dynamodb-local:2.5.4", [8000])
    ep = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(8000)}"
    kw = dict(region_name="us-east-1", endpoint_url=ep, aws_access_key_id="x", aws_secret_access_key="y")
    ddb = _retry(lambda: (boto3.client("dynamodb", **kw).list_tables(), boto3.client("dynamodb", **kw))[1])
    ddb.create_table(
        TableName="orders", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "customer_id", "AttributeType": "S"},
                              {"AttributeName": "id", "AttributeType": "S"},
                              {"AttributeName": "status", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "customer_id", "KeyType": "HASH"}, {"AttributeName": "id", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": "by_status", "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}]}],
    )
    import decimal
    table = boto3.resource("dynamodb", **kw).Table("orders")
    with table.batch_writer() as w:
        for o in ORDERS:
            w.put_item(Item={**o, "total": decimal.Decimal(str(o["total"]))})

    client = DynamodbClient(region="us-east-1", endpoint_url=ep, access_key="x", secret_key="y", max_items=CAP)

    def status_counts(_client):
        out = {}
        for s in STATUS_COUNTS:
            out[s] = len(_client.execute_query('SELECT "id" FROM "orders"."by_status" WHERE "status" = ?', parameters=[s]))
        return out

    return c, Backend(
        "dynamodb", client, "orders",
        by_customer=lambda cl: cl.execute_query('SELECT * FROM "orders" WHERE "customer_id" = ?', parameters=[CUSTOMER]),
        read_all=lambda cl: cl.execute_query('SELECT * FROM "orders"'),
        writes=['DELETE FROM "orders" WHERE "customer_id" = \'c-1\' AND "id" = \'o01\'',
                'UPDATE "orders" SET "total" = 0 WHERE "customer_id" = \'c-1\' AND "id" = \'o01\''],
        status_counts=status_counts,
    )


def _cassandra():
    from cassandra.cluster import Cluster

    from app.data_sources.clients.cassandra_client import CassandraClient

    c = _start("cassandra:5.0", [9042], env={"MAX_HEAP_SIZE": "512M", "HEAP_NEWSIZE": "128M"})
    host, port = c.get_container_host_ip(), int(c.get_exposed_port(9042))
    def connect():
        # A Cluster whose first control connection fails is shut down for
        # good, so every attempt needs a fresh one.
        cl = Cluster([host], port=port)
        try:
            return cl, cl.connect()
        except Exception:
            cl.shutdown()
            raise

    cluster, s = _retry(connect, timeout=240, every=5)
    s.execute("CREATE KEYSPACE shop WITH replication = {'class':'SimpleStrategy','replication_factor':1}")
    s.execute("CREATE TABLE shop.orders (customer_id text, id text, status text, total double, city text, "
              "created timeuuid, PRIMARY KEY ((customer_id), id))")
    for o in ORDERS:
        s.execute("INSERT INTO shop.orders (customer_id, id, status, total, city, created) VALUES (%s,%s,%s,%s,%s,%s)",
                  (o["customer_id"], o["id"], o["status"], o["total"], o["city"], uuid.uuid1()))
    cluster.shutdown()
    client = CassandraClient(contact_points=host, port=port, max_rows=CAP)

    def status_counts(cl):
        # CQL can't GROUP BY a regular column: read each partition, count client-side.
        rows = []
        for i in range(6):
            rows += list(cl.execute_query("SELECT status FROM shop.orders WHERE customer_id = %s", parameters=[f"c-{i}"])["status"])
        return {s: rows.count(s) for s in STATUS_COUNTS}

    return c, Backend(
        "cassandra", client, "shop.orders",
        by_customer=lambda cl: cl.execute_query("SELECT * FROM shop.orders WHERE customer_id = %s", parameters=[CUSTOMER]),
        read_all=lambda cl: cl.execute_query("SELECT * FROM shop.orders"),
        writes=["DELETE FROM shop.orders WHERE customer_id = 'c-1'", "TRUNCATE shop.orders",
                "SELECT * FROM shop.orders; DROP TABLE shop.orders"],
        status_counts=status_counts,
    )


def _couchbase():
    from datetime import timedelta

    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions

    from app.data_sources.clients.couchbase_client import CouchbaseClient

    ports = [8091, 8092, 8093, 8094, 8095, 8096, 11210]
    c = tc_core.DockerContainer("couchbase/server:community-7.6.2")
    # The SDK learns node ports from the cluster map, so they must be the
    # same on the host: bind them 1:1.
    for p in ports:
        c = c.with_bind_ports(p, p)
    try:
        c.start()
        _STARTED.append(c)
    except Exception as e:
        pytest.skip(f"Docker / couchbase unavailable (ports busy?): {e}")

    def cli(*args):
        code, out = c.exec(["couchbase-cli", *args, "-c", "127.0.0.1", "-u", "Administrator", "-p", "password"])
        assert code == 0, out
    def init():
        code, out = c.exec(["couchbase-cli", "cluster-init", "-c", "127.0.0.1", "--cluster-username", "Administrator",
                            "--cluster-password", "password", "--services", "data,index,query",
                            "--cluster-ramsize", "512", "--cluster-index-ramsize", "256",
                            "--index-storage-setting", "default"])
        if code != 0:
            raise RuntimeError(out)

    _retry(init, timeout=180, every=3)
    cli("bucket-create", "--bucket", "shop", "--bucket-type", "couchbase", "--bucket-ramsize", "128", "--wait")
    cli("collection-manage", "--bucket", "shop", "--create-scope", "sales")
    cli("collection-manage", "--bucket", "shop", "--create-collection", "sales.orders")
    cluster = Cluster("couchbase://127.0.0.1", ClusterOptions(PasswordAuthenticator("Administrator", "password")))
    cluster.wait_until_ready(timedelta(seconds=60))
    coll = cluster.bucket("shop").scope("sales").collection("orders")
    for o in ORDERS:
        _retry(lambda o=o: coll.upsert(o["id"], o), timeout=60)
    _retry(lambda: cluster.query("CREATE PRIMARY INDEX ON `shop`.`sales`.`orders`").execute(), timeout=120, every=3)

    def index_online():
        if len(cluster.query("SELECT RAW 1 FROM `shop`.`sales`.`orders` LIMIT 1").execute()) != 1:
            raise RuntimeError("index still building")

    _retry(index_online, timeout=60)
    cluster.close()
    client = CouchbaseClient(connection_string="couchbase://127.0.0.1", bucket="shop",
                             username="Administrator", password="password", max_rows=CAP)
    ks = "`shop`.`sales`.`orders`"

    def status_counts(cl):
        df = cl.execute_query(f"SELECT o.status, COUNT(*) AS n FROM {ks} AS o GROUP BY o.status")
        return dict(zip(df["status"], df["n"], strict=True))

    return c, Backend(
        "couchbase", client, "sales.orders",
        by_customer=lambda cl: cl.execute_query(f"SELECT o.* FROM {ks} AS o WHERE o.customer_id = $c",
                                                parameters={"c": CUSTOMER}),
        read_all=lambda cl: cl.execute_query(f"SELECT o.* FROM {ks} AS o"),
        writes=[f"DELETE FROM {ks} AS o WHERE o.customer_id = 'c-1'",
                f"UPSERT INTO {ks} (KEY, VALUE) VALUES ('x', {{}})", "SELECT 1; DELETE FROM x"],
        status_counts=status_counts,
    )


COSMOS_KEY = "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=="


def _cosmosdb():
    from azure.cosmos import CosmosClient, PartitionKey

    from app.data_sources.clients.cosmosdb_client import CosmosdbClient

    c = _start("mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-preview", [8081],
               command="--protocol https")
    ep = f"https://{c.get_container_host_ip()}:{c.get_exposed_port(8081)}/"
    # The client constructor already calls the account endpoint, so it is
    # part of the readiness wait.
    db = _retry(lambda: CosmosClient(ep, credential=COSMOS_KEY, connection_verify=False,
                                     enable_endpoint_discovery=False).create_database_if_not_exists("shop"),
                timeout=180, every=3)
    ct = db.create_container_if_not_exists("orders", partition_key=PartitionKey(path="/customer_id"))
    for o in ORDERS:
        ct.upsert_item(o)
    client = CosmosdbClient(endpoint=ep, database="shop", account_key=COSMOS_KEY, verify_ssl=False, max_items=CAP)

    def status_counts(cl):
        df = cl.execute_query("SELECT o.status, COUNT(1) AS n FROM orders o GROUP BY o.status")
        return dict(zip(df["status"], df["n"], strict=True))

    return c, Backend(
        "cosmosdb", client, "orders",
        by_customer=lambda cl: cl.execute_query("SELECT * FROM orders o WHERE o.customer_id = @c",
                                                parameters={"@c": CUSTOMER}),
        read_all=lambda cl: cl.execute_query("SELECT * FROM orders o"),
        writes=["DELETE FROM orders o", "SELECT * FROM orders o; SELECT 1"],
        status_counts=status_counts,
    )


FACTORIES = {"dynamodb": _dynamodb, "cassandra": _cassandra, "couchbase": _couchbase, "cosmosdb": _cosmosdb}


@pytest.fixture(scope="module", params=list(FACTORIES))
def backend(request):
    try:
        _container, b = FACTORIES[request.param]()
        yield b
        close = getattr(b.client, "close", None)
        if close:
            close()
    finally:
        _stop_started()


# ----------------------------------------------------------------- contract


def test_connection_succeeds(backend):
    res = backend.client.test_connection()
    assert res["success"], res["message"]


def test_catalog_lists_the_table_with_its_key(backend):
    tables = {t.name: t for t in backend.client.get_schemas()}
    assert backend.table in tables, sorted(tables)
    cols = {c.name for c in tables[backend.table].columns}
    assert {"customer_id", "status", "total"} <= cols
    desc = tables[backend.table].description.lower()
    if backend.name in ("dynamodb", "cassandra", "cosmosdb"):
        assert "customer_id" in desc  # the partition key is spelled out for the agent


def test_key_scoped_query_returns_exactly_that_customer(backend):
    df = backend.by_customer(backend.client)
    assert set(df["customer_id"]) == {CUSTOMER}
    assert len(df) == 10
    assert round(float(df["total"].sum()), 2) == CUSTOMER_TOTAL


def test_status_breakdown_matches_the_seed(backend):
    counts = {k: int(v) for k, v in backend.status_counts(backend.client).items()}
    assert counts == STATUS_COUNTS


def test_full_read_is_capped_and_flagged(backend):
    df = backend.read_all(backend.client)
    assert len(df) == CAP
    assert df.attrs["truncated"] is True


def test_writes_are_refused_and_data_is_intact(backend):
    for stmt in backend.writes:
        with pytest.raises(ValueError):
            backend.client.execute_query(stmt)
    assert len(backend.by_customer(backend.client)) == 10
