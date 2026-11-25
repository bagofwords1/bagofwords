#!/usr/bin/env python3
"""Scripted LLM for the BigQuery keyless-auth loop: one `create_data` call whose
generated code runs a real BigQuery-dialect query through the agent's client.

Reuses stub_llm.py (planner rounds, streaming) and replaces only the generated
code, so the full runtime path is real: planner -> create_data -> codegen ->
sandbox -> BigqueryClient -> whatever identity the connection authenticates as.

    cd backend
    STUB_SOURCES_FILE=/abs/sources.json uv run python ../tools/agent/stub_llm_bigquery.py

sources.json: [{"id": "<agent id>", "name": "<agent name>", "table": "sales.orders"}]
Register it with tools/agent/setup_stub_llm.py. See
docs/feedback-loops/bigquery-keyless-auth.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("STUB_PHASES", "create")

import stub_llm  # noqa: E402

SQL = (
    "SELECT c.country, COUNT(*) AS orders, ROUND(SUM(o.amount), 2) AS shipped_amount "
    "FROM `acme-analytics.sales.orders` o "
    "JOIN `acme-analytics.sales.customers` c ON c.customer_id = o.customer_id "
    "WHERE o.status = 'shipped' GROUP BY c.country ORDER BY shipped_amount DESC"
)


def _codegen(_text: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        "    client = next(iter(ds_clients.values()))\n"
        f"    df = client.execute_query({SQL!r})\n"
        "    print('rows:', len(df))\n"
        "    return df\n"
    )


_plan = stub_llm._planner_tool_calls


def _planner(text, body=None):
    calls, content = _plan(text, body)
    return calls, ("Querying the warehouse." if calls else "Shipped order amount per customer country is in the table above.")


stub_llm._codegen_response = _codegen
stub_llm._planner_tool_calls = _planner

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(stub_llm.app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
