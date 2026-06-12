"""Drive a REAL BOW completion (Anthropic Haiku) over a Spark Connect data source.

Prereqs (set up by the runner script):
  - a local Spark Connect server on sc://localhost:15002 with verify_db.sales seeded
  - ANTHROPIC_API_KEY_TEST in env
Run: pytest tests/e2e/test_spark_completion.py -s -m e2e
"""
import os
import json
import pytest


def _seed_spark():
    from pyspark.sql import SparkSession
    import datetime
    s = SparkSession.builder.remote("sc://localhost:15002").getOrCreate()
    s.sql("CREATE DATABASE IF NOT EXISTS verify_db")
    s.sql("DROP TABLE IF EXISTS verify_db.sales")
    s.sql("CREATE TABLE verify_db.sales (id BIGINT, amount DOUBLE, region STRING) "
          "USING parquet PARTITIONED BY (dt STRING) LOCATION 'file:///tmp/bow_sales'")
    base = datetime.date(2024, 1, 1)
    for d in range(10):
        day = (base + datetime.timedelta(days=d)).isoformat()
        vals = ",".join(f"({i},{float((i%97)+d)},'r{i%5}')" for i in range(200))
        s.sql(f"INSERT INTO verify_db.sales PARTITION(dt='{day}') VALUES {vals}")
    s.sql("ANALYZE TABLE verify_db.sales COMPUTE STATISTICS")
    n = s.sql("SHOW PARTITIONS verify_db.sales").count()
    s.stop()
    return n


@pytest.mark.e2e
def test_spark_connect_completion(
    create_completion, get_completions, create_report,
    create_user, login_user, whoami,
    create_anthropic_provider_and_models, create_data_source,
):
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST not set")
    import socket
    try:
        socket.create_connection(("localhost", 15002), timeout=3).close()
    except OSError:
        pytest.skip("no Spark Connect server on localhost:15002")

    print("\n[seed] partitions:", _seed_spark())

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_anthropic_provider_and_models(token, org_id)

    ds = create_data_source(
        name="spark_sales", type="spark_connect",
        config={"host": "localhost", "port": 15002, "database": "verify_db"},
        credentials={}, auth_policy="system_only",
        user_token=token, org_id=org_id,
    )
    print("[data_source] id:", ds.get("id"), "| status:", ds.get("status"))

    report = create_report(title="Spark sales", user_token=token, org_id=org_id,
                           data_sources=[ds["id"]])

    completions = create_completion(
        report_id=report["id"],
        prompt="From the sales table, what is the total amount for each region? "
               "Filter to dt = '2024-01-05'. Return a small table.",
        user_token=token, org_id=org_id, background=False,
    )

    system = [c for c in completions if c.get("role") == "system"][-1]
    print("\n================ AGENT COMPLETION ================")
    print("status:", system.get("status"), "| model:", system.get("model"))
    for b in system.get("completion_blocks", []):
        print(f"\n--- block: {b.get('title')} [{b.get('status')}] ---")
        if b.get("content"):
            print(b["content"][:1500])
        te = b.get("tool_execution") or {}
        if te:
            print("  tool:", te.get("tool_name") or te.get("name"))
            args = te.get("arguments") or te.get("tool_arguments")
            if args:
                print("  args:", json.dumps(args)[:800])
    for w in system.get("created_widgets", []) or []:
        print("\n--- widget:", w.get("title"), "---")
        print("  data sample:", json.dumps(w.get("data"))[:800] if w.get("data") else w)
    for st in system.get("created_steps", []) or []:
        print("\n--- step:", st.get("title"), "--- code/query:")
        print((st.get("code") or st.get("query") or "")[:800])
        if st.get("data"):
            print("  result:", json.dumps(st["data"])[:800])

    assert system.get("status") in ("completed", "success")
