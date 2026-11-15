"""Seed an Iceberg catalog: sales.orders partitioned by month(order_ts), 2 snapshots; sales.customers."""
import os, sys, datetime as dt
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, StringType, DoubleType, TimestampType, LongType, DateType
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.transforms import MonthTransform

def seed(cat, ns="sales"):
    try: cat.create_namespace(ns)
    except Exception: pass
    for t in ("orders", "customers"):
        try: cat.drop_table((ns, t))
        except Exception: pass
    schema = Schema(NestedField(1, "order_id", LongType(), required=False), NestedField(2, "customer_id", StringType(), required=False),
                    NestedField(3, "region", StringType(), required=False, doc="Sales region"), NestedField(4, "amount", DoubleType(), required=False),
                    NestedField(5, "order_ts", TimestampType(), required=False), NestedField(6, "order_date", DateType(), required=False))
    spec = PartitionSpec(PartitionField(source_id=5, field_id=1000, transform=MonthTransform(), name="order_month"))
    orders = cat.create_table((ns, "orders"), schema=schema, partition_spec=spec, properties={"comment": "Customer orders"})
    def batch(start, n):
        rows = []
        for i in range(start, start + n):
            ts = dt.datetime(2025, 1 + i % 6, 1 + i % 27, 12, 0)
            rows.append({"order_id": i, "customer_id": f"c-{i%6}", "region": ["EMEA","AMER","APAC"][i%3], "amount": round(10 + i*1.5, 2), "order_ts": ts, "order_date": ts.date()})
        return pa.Table.from_pylist(rows, schema=orders.schema().as_arrow())
    orders.append(batch(0, 600))      # snapshot 1: 600 rows
    orders.append(batch(600, 300))    # snapshot 2: +300 rows
    cs = Schema(NestedField(1, "customer_id", StringType(), required=False), NestedField(2, "name", StringType(), required=False), NestedField(3, "tier", StringType(), required=False))
    cust = cat.create_table((ns, "customers"), schema=cs)
    cust.append(pa.Table.from_pylist([{"customer_id": f"c-{i}", "name": f"Customer {i}", "tier": ["gold","silver"][i%2]} for i in range(6)], schema=cust.schema().as_arrow()))
    return orders

if __name__ == "__main__":
    cat = load_catalog("seed", type="rest", uri=os.environ.get("ICEBERG_URI", "http://127.0.0.1:8181"))
    o = seed(cat); print("seeded; snapshots:", [s.snapshot_id for s in o.snapshots()])
