import os, sys, time, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "backend"))
import urllib3; urllib3.disable_warnings()
ORDERS = [{"id": f"o{i}", "customer_id": f"c-{i%6}", "status": ["OPEN","SHIPPED","CANCELLED"][i%3], "total": round(10.5+i*3.25, 2),
           "address": {"city": ["Paris","Austin","Tel Aviv"][i%3]}, "items": [{"sku": f"s{i}", "qty": 1+i%4}]} for i in range(60)]
which = sys.argv[1:] or ["cassandra", "couchbase", "cosmos"]

def check_guard(c, bad):
    for b in bad:
        try: c.execute_query(b); print("  NOT BLOCKED:", b); sys.exit(1)
        except ValueError as e: print("  blocked:", str(e)[:60])

if "cassandra" in which:
    from cassandra.cluster import Cluster
    s = Cluster(["127.0.0.1"]).connect()
    s.execute("CREATE KEYSPACE IF NOT EXISTS shop WITH replication = {'class':'SimpleStrategy','replication_factor':1}")
    s.execute("CREATE TABLE IF NOT EXISTS shop.orders (customer_id text, order_id text, status text, total decimal, city text, tags set<text>, created timeuuid, PRIMARY KEY ((customer_id), order_id)) WITH CLUSTERING ORDER BY (order_id DESC) AND comment='Customer orders'")
    s.execute("TRUNCATE shop.orders")
    import uuid, decimal
    for o in ORDERS:
        s.execute("INSERT INTO shop.orders (customer_id, order_id, status, total, city, tags, created) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (o["customer_id"], o["id"], o["status"], decimal.Decimal(str(o["total"])), o["address"]["city"], {"vip"} if int(o["id"][1:])%5==0 else set(), uuid.uuid1()))
    from app.data_sources.clients.cassandra_client import CassandraClient
    c = CassandraClient(contact_points="127.0.0.1", max_rows=25)
    print("cassandra:", c.test_connection())
    t = [t for t in c.get_schemas() if t.name == "shop.orders"][0]; print(" ", t.description, [(x.name, x.dtype) for x in t.columns])
    df = c.execute_query("SELECT * FROM shop.orders WHERE customer_id = %s", parameters=["c-1"]); print("  by pk:", len(df), round(sum(df.total), 2), type(df.created[0]).__name__)
    df = c.execute_query("SELECT * FROM shop.orders"); print("  capped:", len(df), df.attrs["truncated"])
    df = c.execute_query("SELECT customer_id, count(*) AS n FROM shop.orders GROUP BY customer_id"); print("  group:", len(df), int(df.n.sum()))
    check_guard(c, ["DELETE FROM shop.orders WHERE customer_id='c-1'", "SELECT * FROM shop.orders; DROP TABLE shop.orders", "BEGIN BATCH INSERT INTO shop.orders (customer_id) VALUES ('x') APPLY BATCH"])

if "couchbase" in which:
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster as CB
    from couchbase.options import ClusterOptions
    from datetime import timedelta
    cl = CB("couchbase://127.0.0.1", ClusterOptions(PasswordAuthenticator("Administrator", "password"))); cl.wait_until_ready(timedelta(seconds=30))
    col = cl.bucket("shop").scope("sales").collection("orders")
    for o in ORDERS: col.upsert(o["id"], {k: v for k, v in o.items() if k != "id"})
    cl.bucket("shop").scope("sales").collection("customers").upsert("c-1", {"name": "Ada", "tier": "gold"})
    try: cl.query("CREATE PRIMARY INDEX IF NOT EXISTS ON `shop`.`sales`.`orders`").execute()
    except Exception as e: print("idx", e)
    time.sleep(2)
    from app.data_sources.clients.couchbase_client import CouchbaseClient
    c = CouchbaseClient(connection_string="couchbase://127.0.0.1", bucket="shop", username="Administrator", password="password", max_rows=25)
    print("couchbase:", c.test_connection())
    tabs = {t.name: t for t in c.get_schemas()}; print(" ", sorted(tabs)); t = tabs["sales.orders"]; print(" ", t.description, [(x.name, x.dtype) for x in t.columns][:8])
    df = c.execute_query("SELECT o.status, COUNT(*) AS n, SUM(o.total) AS revenue FROM `shop`.`sales`.`orders` AS o GROUP BY o.status ORDER BY o.status"); print("  group:\n", df)
    df = c.execute_query("SELECT META(o).id, o.* FROM `shop`.`sales`.`orders` o WHERE o.customer_id = $c", parameters={"c": "c-2"}); print("  param:", len(df), list(df.columns)[:5])
    df = c.execute_query("SELECT o.* FROM `shop`.`sales`.`orders` o"); print("  capped:", len(df), df.attrs["truncated"])
    check_guard(c, ["DELETE FROM `shop`.`sales`.`orders` o WHERE o.status='OPEN'", "UPSERT INTO `shop`.`sales`.`orders` (KEY, VALUE) VALUES ('x', {})", "SELECT 1; DELETE FROM x"])

if "cosmos" in which:
    from azure.cosmos import CosmosClient, PartitionKey
    KEY = "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=="
    EP = "https://127.0.0.1:8081/"
    cc = CosmosClient(EP, credential=KEY, connection_verify=False, enable_endpoint_discovery=False)
    db = cc.create_database_if_not_exists("shop")
    ct = db.create_container_if_not_exists("orders", partition_key=PartitionKey(path="/customer_id"))
    for o in ORDERS: ct.upsert_item(o)
    from app.data_sources.clients.cosmosdb_client import CosmosdbClient
    c = CosmosdbClient(endpoint=EP, database="shop", account_key=KEY, verify_ssl=False, max_items=25)
    print("cosmos:", c.test_connection())
    t = c.get_schemas()[0]; print(" ", t.name, t.description, [(x.name, x.dtype) for x in t.columns][:8])
    df = c.execute_query("SELECT o.id, o.total FROM orders o WHERE o.customer_id = @c", parameters={"@c": "c-3"}); print("  by pk:", len(df), round(df.total.sum(), 2))
    df = c.execute_query("SELECT VALUE COUNT(1) FROM orders o"); print("  count:", df.iloc[0].to_dict())
    df = c.execute_query("SELECT o.status, COUNT(1) AS n FROM orders o GROUP BY o.status"); print("  group:", df.to_dict("records"))
    df = c.execute_query("SELECT * FROM orders o"); print("  capped:", len(df), df.attrs["truncated"], list(df.columns)[:6])
    df = c.execute_query("SELECT o.id, i.sku FROM orders o JOIN i IN o.items WHERE i.qty > 3"); print("  join-in:", len(df))
    check_guard(c, ["DELETE FROM orders o", "SELECT * FROM orders o; SELECT 1"])
print("ALL OK")
