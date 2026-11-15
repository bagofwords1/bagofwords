import sys
from pyhive import hive
port = int(sys.argv[1]) if len(sys.argv) > 1 else 10010
c = hive.connect(host="127.0.0.1", port=port, username="hive", auth="NONE").cursor()
for s in ["CREATE DATABASE IF NOT EXISTS sales", "DROP TABLE IF EXISTS sales.orders",
          "CREATE TABLE sales.orders (order_id INT, customer_id STRING, region STRING COMMENT 'Sales region', amount DOUBLE) COMMENT 'Customer orders' PARTITIONED BY (order_month STRING)",
          "SET hive.exec.dynamic.partition.mode=nonstrict"]:
    c.execute(s)
vals = ", ".join(f"({i}, 'c-{i%6}', '{['EMEA','AMER','APAC'][i%3]}', {10+i*1.5}, '2025-0{1+i%6}')" for i in range(900))
c.execute(f"INSERT INTO sales.orders PARTITION (order_month) VALUES {vals}")
c.execute("SELECT COUNT(*) FROM sales.orders"); print("seeded", c.fetchall())
