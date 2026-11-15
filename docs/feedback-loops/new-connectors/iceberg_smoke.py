import os, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "backend"))
from app.data_sources.clients.iceberg_client import IcebergClient
kw = dict(catalog_type=os.environ.get("ICE_TYPE", "rest"), uri=os.environ.get("ICE_URI", "http://127.0.0.1:8181"),
          warehouse=os.environ.get("ICE_WH"), region=os.environ.get("ICE_REGION"),
          access_key=os.environ.get("AWS_ACCESS_KEY_ID") if os.environ.get("ICE_TYPE") in ("glue","s3tables") else None,
          secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY") if os.environ.get("ICE_TYPE") in ("glue","s3tables") else None,
          namespaces=os.environ.get("ICE_NS"), max_scan_rows=int(os.environ.get("ICE_MAX", "100000")))
NS = os.environ.get("ICE_NS", "sales")
c = IcebergClient(**kw)
print(c.test_connection())
for t in c.get_schemas(): print(" ", t.name, "|", t.description, [(x.name, x.dtype) for x in t.columns][:6])
q = lambda s, **k: (lambda t0: (c.execute_query(s, **k), time.time()-t0))(time.time())
df, dt_ = q(f"SELECT region, COUNT(*) AS n, ROUND(SUM(amount),2) AS revenue FROM {NS}.orders GROUP BY region ORDER BY region"); print("group:", df.to_dict("records"), f"{dt_:.2f}s")
df, _ = q(f"SELECT COUNT(*) AS n FROM {NS}.orders WHERE order_ts >= '2025-03-01' AND order_ts < '2025-04-01'"); print("march:", df.iloc[0].n)
df, _ = q(f"SELECT c.tier, COUNT(*) n FROM {NS}.orders o JOIN {NS}.customers c ON o.customer_id = c.customer_id GROUP BY 1 ORDER BY 1"); print("join:", df.to_dict("records"))
df, _ = q(f"WITH big AS (SELECT * FROM {NS}.orders WHERE amount > 1000) SELECT COUNT(*) n FROM big"); print("cte:", df.iloc[0].n)
from pyiceberg.catalog import load_catalog
tbl = c._cat().load_table((NS, "orders")); first = tbl.snapshots()[0]
df, _ = q(f"SELECT COUNT(*) n FROM {NS}.orders", snapshot_id=first.snapshot_id); print("time travel (snapshot 1):", df.iloc[0].n)
df, _ = q(f"SELECT COUNT(*) n FROM {NS}.orders", as_of=str(__import__('pandas').Timestamp(first.timestamp_ms, unit='ms', tz='UTC'))); print("as_of snapshot1:", df.iloc[0].n)
# pushdown actually prunes: count planned files with/without filter
from app.data_sources.clients.iceberg_client import _pushdown_filter
import sqlglot
f = _pushdown_filter(sqlglot.parse_one(f"SELECT * FROM {NS}.orders WHERE order_ts >= '2025-03-01' AND order_ts < '2025-04-01' AND region IN ('EMEA','APAC')", read="duckdb"), tbl.schema().fields); print("filter:", f)
print("files all/pruned:", len(list(tbl.scan().plan_files())), len(list(tbl.scan(row_filter=f).plan_files())))
small = IcebergClient(**{**kw, "max_scan_rows": 200})
try: small.execute_query(f"SELECT * FROM {NS}.orders"); print("NOT REFUSED"); sys.exit(1)
except ValueError as e: print("too large:", str(e)[:110])
df = small.execute_query(f"SELECT COUNT(*) n FROM {NS}.orders WHERE order_ts >= '2025-03-01' AND order_ts < '2025-03-10'"); print("narrowed under cap:", df.iloc[0].n)
for bad in [f"DELETE FROM {NS}.orders", f"DROP TABLE {NS}.orders", f"SELECT 1; DROP TABLE {NS}.orders", "SELECT * FROM nope.nothing"]:
    try: c.execute_query(bad); print("NOT BLOCKED", bad); sys.exit(1)
    except ValueError as e: print("refused:", type(e).__name__, str(e)[:70])
print("ALL OK")
