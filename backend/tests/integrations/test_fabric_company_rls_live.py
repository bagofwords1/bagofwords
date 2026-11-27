"""Opt-in. See docs/feedback-loops/fabric-company-rls.md for the sandbox fixture."""
import os

import httpx
import pytest


@pytest.mark.skipif(os.getenv("BOW_COMPANY_RLS_LIVE") != "1", reason="requires isolated Fabric demo and BOW sessions")
def test_signed_in_company_reads():
    origin = os.environ["BOW_RLS_BASE_URL"]
    org = os.environ["BOW_RLS_ORG"]
    connection = os.environ["BOW_RLS_CONNECTION"]
    query = os.environ["BOW_RLS_QUERY"]
    dashboard = os.environ["BOW_RLS_DASHBOARD"]
    owner_sales = [] if os.getenv("BOW_RLS_REVOKED") == "1" else [[101, 10]]
    viewers = [
        ("BOW_RLS_DEMO1_TOKEN", owner_sales, [[202, 90]]),
        ("BOW_RLS_DEMO2_TOKEN", [[202, 90]], []),
    ]
    with httpx.Client(base_url=origin, timeout=120) as client:
        for token, sales, costs in viewers:
            headers = {"Authorization": "Bearer " + os.environ[token], "X-Organization-Id": org}
            for table_var, expected in (("BOW_RLS_SALES_TABLE", sales), ("BOW_RLS_COSTS_TABLE", costs)):
                table_id = os.environ[table_var]
                response = client.get(f"/api/connections/{connection}/tables/{table_id}/preview", headers=headers)
                response.raise_for_status()
                assert response.json()["rows"] == expected
                assert response.json()["total_rows"] is None

            expected_rows = [dict(company_id=c, amount=a) for c, a in sales]
            response = client.post(f"/api/queries/{query}/run", headers=headers, json={"mode": "viewer"})
            response.raise_for_status()
            assert response.json()["data"]["rows"] == expected_rows
            assert response.json()["cached"] is False
            response = client.get(f"/api/d/{dashboard}", headers=headers)
            response.raise_for_status()
            assert "no-store" in response.headers["Cache-Control"]
            assert response.json()["tables"][0]["rows"] == expected_rows
            assert "OLD_PRIVATE_CAPTION" not in response.text
            assert client.get(f"/api/d/{dashboard}/content", headers=headers).status_code == 404
        assert client.get(f"/api/d/{dashboard}").status_code in (401, 403, 404)


@pytest.mark.skipif(os.getenv("BOW_COMPANY_RLS_CLAUDE") != "1", reason="requires live Claude and two prepared BOW queries")
def test_claude_requested_company_is_scoped_to_each_reader():
    """Use exactly the same model-generated SQL in both signed-in preview requests."""
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={
            "model": os.environ["BOW_RLS_CLAUDE_MODEL"],
            "max_tokens": 256,
            "messages": [{"role": "user", "content": (
                "Return one Fabric SELECT for company_id and amount from "
                "bow_shortcut_source.sales for company 202. Return only SQL."
            )}],
        },
        timeout=60,
    )
    response.raise_for_status()
    sql = next(block["text"] for block in response.json()["content"] if block["type"] == "text")
    sql = sql.strip().removeprefix("```sql").removesuffix("```").strip()
    source = os.environ["BOW_RLS_DATA_SOURCE_NAME"]
    code = f"def generate_df(ds_clients, excel_files):\n    return ds_clients[{source!r}].execute_query({sql!r})"
    with httpx.Client(base_url=os.environ["BOW_RLS_BASE_URL"], timeout=120) as client:
        for viewer, expected in (("DEMO1", []), ("DEMO2", [{"company_id": 202, "amount": 90}])):
            query = os.environ[f"BOW_RLS_{viewer}_OWN_QUERY"]
            headers = {
                "Authorization": "Bearer " + os.environ[f"BOW_RLS_{viewer}_TOKEN"],
                "X-Organization-Id": os.environ["BOW_RLS_ORG"],
            }
            result = client.post(f"/api/queries/{query}/preview", headers=headers, json={"code": code})
            result.raise_for_status()
            assert result.json()["preview"]["rows"] == expected
