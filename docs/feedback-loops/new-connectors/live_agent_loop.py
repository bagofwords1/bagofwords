"""Loop B: every new connector end to end through the real app + a real LLM.

For each connector: create the connection through the API (the same route the
connect form posts to), wait for indexing, activate its tables, then ask the
agent a question whose answer is known from the seed data and check the
answer's numbers. The model is Claude Haiku 5.5 (ANTHROPIC_API_KEY env).

Usage (stack + backing servers up, seeded):
  cd backend && uv run python <this file> [--only gcs,cassandra] [--aws]
"""
import argparse
import json
import os
import re
import sys
import time

import httpx

BASE = "http://localhost:8000"
MODEL = "claude-haiku-5-5"
AZ = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
      "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
      "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")
COSMOS_KEY = "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=="
SUF = os.environ.get("SUF", "")
AWS = {"access_key": os.environ.get("AWS_ACCESS_KEY_ID", ""), "secret_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "")}

# name, type, config (incl. auth_type), credentials, question, expected substrings (any-of groups)
CASES = [
    ("GCS reports", "gcs", {"auth_type": "adc", "bucket": "bow-test", "endpoint_url": "http://127.0.0.1:4443", "project": "test"}, {},
     "What revenue does the file sales/2025/q1.csv report for the AMER region?", [["340"]]),
    ("Azure reports", "azure_blob", {"auth_type": "connection_string", "container": "bow-test"}, {"connection_string": AZ},
     "Find the ERROR line in logs/app.log and quote it exactly.", [["café exploded", "cafe exploded"]]),
    ("Cassandra shop", "cassandra", {"auth_type": "userpass", "contact_points": "127.0.0.1", "port": 9042}, {},
     "What is the total order value for customer c-1 in shop.orders?", [["1015", "1,015"]]),
    ("Couchbase shop", "couchbase", {"auth_type": "userpass", "connection_string": "couchbase://127.0.0.1", "bucket": "shop"},
     {"username": "Administrator", "password": "password"},
     "What is the total revenue of OPEN orders in the sales.orders collection?", [["2062.5", "2,062.5", "2062.50", "2,062.50"]]),
    ("Cosmos shop", "cosmosdb", {"auth_type": "account_key", "endpoint": "https://127.0.0.1:8081/", "database": "shop", "verify_ssl": False},
     {"account_key": COSMOS_KEY}, "What is the total of all orders for customer c-3?", [["1080", "1,080"]]),
    ("DynamoDB local", "dynamodb", {"auth_type": "aws_keys", "region": "us-east-1", "endpoint_url": "http://127.0.0.1:8001", "tables": "bow-test-orders"},
     {"access_key": "x", "secret_key": "y"}, "How many orders in bow-test-orders have status SHIPPED?", [["20"]]),
    ("Iceberg lake", "iceberg", {"auth_type": "none", "catalog_type": "rest", "uri": "http://127.0.0.1:8181"}, {},
     "How many orders were placed in March 2025 in sales.orders?", [["150"]]),
    ("Dremio lake", "dremio", {"auth_type": "userpass", "url": "http://127.0.0.1:9047"}, {"username": "admin", "password": "Passw0rd!x"},
     "Which region has the highest total revenue in Sales.orders, and what is it?", [["APAC"], ["205725", "205,725"]]),
    ("Hive warehouse", "hive", {"auth_type": "none", "host": "127.0.0.1", "port": 10010, "engine": "hive"}, {},
     "How many orders are in the 2025-03 partition of sales.orders?", [["150"]]),
]
AWS_CASES = [
    ("DynamoDB AWS", "dynamodb", {"auth_type": "aws_keys", "region": "us-east-1", "tables": "bow-test-orders"}, AWS,
     "How many orders does customer c-2 have in bow-test-orders, and what is their total?", [["10"], ["1047.5", "1,047.5", "1047.50"]]),
    ("Iceberg S3 Tables", "iceberg", {"auth_type": "aws_keys", "catalog_type": "s3tables", "region": "us-east-1",
                                      "warehouse": f"{os.environ.get('ICEBERG_S3TABLES_ARN', '')}"}, AWS,
     f"What is the total amount for the AMER region in bow_test_{SUF}.orders?", [["205275", "205,275"]]),
    ("Iceberg Glue", "iceberg", {"auth_type": "aws_keys", "catalog_type": "glue", "region": "us-east-1", "namespaces": f"bow_test_{SUF}"}, AWS,
     f"How many gold-tier customers' orders are there? Join bow_test_{SUF}.orders with bow_test_{SUF}.customers on customer_id.", [["450"]]),
]

c = httpx.Client(base_url=BASE, timeout=120)


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        raise RuntimeError(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:600]}")
    return r.json() if r.content else None


tok = ok(c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}))["access_token"]
org = ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {tok}"}))["organizations"][0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


def ensure_llm():
    provs = ok(c.get("/api/llm/providers", headers=H))
    prov = next((p for p in provs if p.get("provider_type") == "anthropic"), None)
    if prov is None:
        prov = ok(c.post("/api/llm/providers", headers=H, json={
            "name": "anthropic-live", "provider_type": "anthropic",
            "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY"]},
            "models": [{"model_id": MODEL, "name": "Claude Haiku 5.5", "is_custom": False}]}))
        provs = ok(c.get("/api/llm/providers", headers=H))
        prov = next(p for p in provs if p.get("provider_type") == "anthropic")
    model = next(m for m in prov["models"] if m["model_id"] == MODEL)
    for small in ("false", "true"):
        ok(c.post(f"/api/llm/models/{model['id']}/set_default?small={small}", headers=H))
    return model["id"]


def create(name, type_, config, creds):
    existing = next((d for d in ok(c.get("/api/data_sources", headers=H)) if d["name"] == name), None)
    if existing:
        return existing["id"]
    t0 = time.time()
    ds = ok(c.post("/api/data_sources", headers=H, json={
        "name": name, "type": type_, "config": config, "credentials": creds, "auth_policy": "system_only",
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False}))
    print(f"  created in {time.time() - t0:.1f}s")
    return ds["id"]


def activate_all(ds_id):
    names = []
    for _ in range(90):
        fr = c.get(f"/api/data_sources/{ds_id}/full_schema", headers=H)
        body = fr.json() if fr.status_code == 200 else {}
        items = body.get("tables", body) if isinstance(body, dict) else body
        names = [t.get("name") for t in items] if isinstance(items, list) else []
        if names:
            break
        time.sleep(2)
    if names:
        ok(c.put(f"/api/data_sources/{ds_id}/update_tables_status", headers=H, json={"activate": names, "deactivate": []}))
    return names


def ask(ds_id, question):
    report = ok(c.post("/api/reports", headers=H, json={"title": question[:60], "files": [], "data_sources": [ds_id]}))
    text, tools, event = [], [], None
    t0 = time.time()
    with c.stream("POST", f"/api/reports/{report['id']}/completions", headers={**H, "Accept": "text/event-stream"},
                  timeout=600, json={"prompt": {"content": question, "mentions": []}, "stream": True}) as r:
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                payload = data.get("data", data) if isinstance(data, dict) else {}
                if event and "tool" in event and isinstance(payload, dict) and payload.get("tool_name"):
                    tools.append(payload.get("tool_name"))
    # The final answer as stored (what the UI renders).
    comps = ok(c.get(f"/api/reports/{report['id']}/completions", headers=H))
    items = comps.get("completions", comps) if isinstance(comps, dict) else comps
    answer = json.dumps(items[-1], default=str, ensure_ascii=False) if items else ""
    return report["id"], answer, sorted(set(tools)), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--aws", action="store_true")
    a = ap.parse_args()
    ensure_llm()
    cases = CASES + (AWS_CASES if a.aws else [])
    if a.only:
        keep = set(a.only.split(","))
        cases = [x for x in cases if x[1] in keep or x[0] in keep]
    results = []
    for name, type_, config, creds, question, expect in cases:
        print(f"== {name} ({type_})")
        try:
            test = ok(c.post("/api/data_sources/test_connection", headers=H, json={"name": name, "type": type_, "config": config, "credentials": creds}))
            print("  test:", test.get("success"), str(test.get("message"))[:100])
            ds_id = create(name, type_, config, creds)
            tables = activate_all(ds_id)
            print(f"  tables ({len(tables)}):", tables[:6])
            rid, answer, tools, secs = ask(ds_id, question)
            hit = all(any(e.lower() in answer.lower() for e in group) for group in expect)
            print(f"  agent ({secs:.0f}s, report {rid[:8]}) tools={tools} -> {'PASS' if hit else 'FAIL'}")
            m = re.search(r'"(?:content|text)"\s*:\s*"([^"]{0,400})', answer)
            print("  answer:", (m.group(1) if m else answer[:300]).replace("\\n", " ")[:300])
            results.append((name, hit, rid))
        except Exception as e:
            print("  ERROR:", str(e)[:500])
            results.append((name, False, None))
    print("\nSUMMARY:", json.dumps([{"connector": n, "pass": p, "report": r} for n, p, r in results], indent=1))
    sys.exit(0 if all(p for _, p, _ in results) else 1)


if __name__ == "__main__":
    main()
