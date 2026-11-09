"""Deterministic retail mock graph for the Neo4j connector demo.

(:Customer {id, name, city, segment, joined})-[:PLACED]->(:Order {id, date, status})
(:Order)-[:CONTAINS {qty, unit_price}]->(:Product {sku, name, price})
(:Product)-[:IN_CATEGORY]->(:Category {name})
(:Supplier {name, country})-[:SUPPLIES]->(:Product)
(:Customer)-[:REFERRED]->(:Customer)
Users: svc (reader, may impersonate ann), ann (analyst: cannot read Customer.credit_limit).
"""
import random
from datetime import date, timedelta

from neo4j import GraphDatabase

rng = random.Random(42)
CITIES = ["Tel Aviv", "Haifa", "Jerusalem", "Berlin", "London", "New York", "Paris", "Madrid"]
SEGMENTS = ["Consumer", "SMB", "Enterprise"]
CATEGORIES = ["Laptops", "Phones", "Audio", "Monitors", "Accessories", "Smart Home", "Gaming", "Storage"]
SUPPLIERS = [("Northwind", "US"), ("Contoso", "DE"), ("Fabrikam", "IL"), ("Tailspin", "UK"),
             ("Litware", "FR"), ("Adatum", "US"), ("Proseware", "ES"), ("Wingtip", "IL")]
FIRST = ["Noa", "Liam", "Maya", "Ethan", "Yael", "Omer", "Sara", "David", "Lea", "Adam", "Tamar", "Ben",
         "Mia", "Noah", "Shira", "Eli", "Emma", "Daniel", "Roni", "Lucas"]
LAST = ["Cohen", "Levi", "Smith", "Muller", "Garcia", "Martin", "Rossi", "Brown", "Mizrahi", "Klein"]

customers = []
for i in range(1, 201):
    customers.append({"id": f"C{i:03d}", "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                      "city": rng.choice(CITIES), "segment": rng.choices(SEGMENTS, [6, 3, 1])[0],
                      "joined": str(date(2023, 1, 1) + timedelta(days=rng.randint(0, 900))),
                      "credit_limit": rng.choice([1000, 2500, 5000, 10000, 25000])})
products = []
for i in range(1, 41):
    cat = CATEGORIES[(i - 1) % len(CATEGORIES)]
    products.append({"sku": f"P{i:03d}", "name": f"{cat[:-1] if cat.endswith('s') else cat} {100 + i}",
                     "price": round(rng.uniform(15, 2400), 2), "category": cat,
                     "supplier": SUPPLIERS[rng.randrange(len(SUPPLIERS))][0]})
orders, lines = [], []
for i in range(1, 1501):
    c = rng.choices(customers, weights=[1 + (k % 7 == 0) * 6 for k in range(len(customers))])[0]
    d = date(2025, 1, 1) + timedelta(days=rng.randint(0, 670))
    orders.append({"id": f"O{i:05d}", "customer": c["id"], "date": str(d),
                   "status": rng.choices(["delivered", "shipped", "returned", "cancelled"], [80, 10, 6, 4])[0]})
    for p in rng.sample(products, rng.randint(1, 4)):
        lines.append({"order": f"O{i:05d}", "sku": p["sku"], "qty": rng.randint(1, 3),
                      "unit_price": round(p["price"] * rng.uniform(0.85, 1.0), 2)})
referrals = []
for c in customers[20:]:
    if rng.random() < 0.6:
        referrer = rng.choice(customers[: max(20, int(c["id"][1:]) - 1)])
        if referrer["id"] != c["id"]:
            referrals.append({"src": referrer["id"], "dst": c["id"]})

d = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "test-password-123"))
d.execute_query("MATCH (n) DETACH DELETE n")
for q in ["CREATE CONSTRAINT IF NOT EXISTS FOR (c:Customer) REQUIRE c.id IS UNIQUE",
          "CREATE CONSTRAINT IF NOT EXISTS FOR (o:Order) REQUIRE o.id IS UNIQUE",
          "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Product) REQUIRE p.sku IS UNIQUE"]:
    d.execute_query(q)
d.execute_query("UNWIND $cats AS n CREATE (:Category {name: n})", cats=CATEGORIES)
d.execute_query("UNWIND $s AS s CREATE (:Supplier {name: s[0], country: s[1]})", s=SUPPLIERS)
d.execute_query("""UNWIND $rows AS r CREATE (c:Customer {id: r.id, name: r.name, city: r.city,
  segment: r.segment, joined: date(r.joined), credit_limit: r.credit_limit})""", rows=customers)
d.execute_query("""UNWIND $rows AS r MATCH (cat:Category {name: r.category}), (s:Supplier {name: r.supplier})
  CREATE (p:Product {sku: r.sku, name: r.name, price: r.price})-[:IN_CATEGORY]->(cat), (s)-[:SUPPLIES]->(p)""",
                rows=products)
d.execute_query("""UNWIND $rows AS r MATCH (c:Customer {id: r.customer})
  CREATE (c)-[:PLACED]->(:Order {id: r.id, date: date(r.date), status: r.status})""", rows=orders)
d.execute_query("""UNWIND $rows AS r MATCH (o:Order {id: r.order}), (p:Product {sku: r.sku})
  CREATE (o)-[:CONTAINS {qty: r.qty, unit_price: r.unit_price}]->(p)""", rows=lines)
d.execute_query("""UNWIND $rows AS r MATCH (a:Customer {id: r.src}), (b:Customer {id: r.dst})
  CREATE (a)-[:REFERRED]->(b)""", rows=referrals)
for q in ["CREATE OR REPLACE USER svc SET PASSWORD 'svc-password-123' CHANGE NOT REQUIRED",
          "CREATE OR REPLACE USER ann SET PASSWORD 'ann-password-123' CHANGE NOT REQUIRED",
          "CREATE OR REPLACE ROLE bow_service", "GRANT ROLE reader TO svc", "GRANT ROLE bow_service TO svc",
          "GRANT IMPERSONATE (ann) ON DBMS TO bow_service",
          "CREATE OR REPLACE ROLE analyst AS COPY OF reader",
          "DENY READ {credit_limit} ON GRAPH neo4j NODES Customer TO analyst",
          "GRANT ROLE analyst TO ann"]:
    d.execute_query(q, database_="system")
counts = d.execute_query("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label").records
rels = d.execute_query("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY type").records
print({r["label"]: r["n"] for r in counts}, {r["type"]: r["n"] for r in rels})
d.close()
