import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "backend"))
from app.data_sources.clients.hive_client import HiveClient
port = int(sys.argv[1]) if len(sys.argv) > 1 else 10010
c = HiveClient(host="127.0.0.1", port=port, max_rows=700)
print(c.test_connection())
for t in c.get_schemas(): print(" ", t.name, "|", t.description, [(x.name, x.dtype, x.description) for x in t.columns])
df = c.execute_query("SELECT region, COUNT(*) AS n, ROUND(SUM(amount), 2) AS revenue FROM sales.orders GROUP BY region ORDER BY region"); print(df.to_dict("records"))
df = c.execute_query("SELECT COUNT(*) AS n FROM sales.orders WHERE order_month = '2025-03'"); print("partition:", df.iloc[0].to_dict())
df = c.execute_query("SELECT * FROM sales.orders"); print("capped:", len(df), df.attrs["truncated"], list(df.columns))
for bad in ["DROP TABLE sales.orders", "INSERT INTO sales.orders PARTITION (order_month='x') VALUES (1,'a','b',1.0)", "SELECT 1; DROP TABLE sales.orders", "TRUNCATE TABLE sales.orders"]:
    try: c.execute_query(bad); print("NOT BLOCKED", bad); sys.exit(1)
    except ValueError as e: print("refused:", str(e)[:70])
print(HiveClient(host="127.0.0.1", port=1).test_connection()["success"])
print(HiveClient(host="127.0.0.1", port=port, databases="nope").test_connection())
print("ALL OK")
