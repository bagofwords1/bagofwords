#!/usr/bin/env python3
"""Drive the run-control regression through a live localhost stack.

Prerequisites:
  tools/agent/boot_stack.sh --dev
  cd backend && uv run python ../tools/agent/seed_org.py
  cd backend && uv run python ../tools/agent/run_control_stub_llm.py

Then run from backend/:
  uv run python ../tools/agent/run_control_probe.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

import httpx
from run_concurrency_probe import auth, ensure_llm_provider

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "backend" / "db" / "agent.db"


def _latest_run(db_path: str, report_id: str) -> dict:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        completion = connection.execute(
            """
            SELECT id, status, completion
            FROM completions
            WHERE report_id = ? AND role = 'system'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (report_id,),
        ).fetchone()
        if completion is None:
            raise AssertionError("no system completion was persisted")
        execution = connection.execute(
            """
            SELECT id, status
            FROM agent_executions
            WHERE completion_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (completion["id"],),
        ).fetchone()
        if execution is None:
            raise AssertionError("no agent execution was persisted")
        tools = connection.execute(
            """
            SELECT tool_name, success, status, result_summary
            FROM tool_executions
            WHERE agent_execution_id = ?
            ORDER BY created_at
            """,
            (execution["id"],),
        ).fetchall()
        notes = connection.execute(
            """
            SELECT title, content, source, agent_execution_id
            FROM notes
            WHERE agent_execution_id = ? AND deleted_at IS NULL
            ORDER BY created_at
            """,
            (execution["id"],),
        ).fetchall()
    finally:
        connection.close()
    return {
        "completion": dict(completion),
        "execution": dict(execution),
        "tools": [dict(row) for row in tools],
        "notes": [dict(row) for row in notes],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--stub-url", default="http://127.0.0.1:9099")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default="Password123!")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    args = parser.parse_args()

    stub = httpx.Client(base_url=args.stub_url, timeout=20)
    response = stub.post("/reset")
    if response.status_code != 200:
        sys.exit(f"stub reset failed: {response.status_code} {response.text}")

    client = httpx.Client(base_url=args.base_url, timeout=600)
    response = client.post(
        "/api/auth/jwt/login",
        data={"username": args.email, "password": args.password},
    )
    if response.status_code != 200:
        sys.exit(f"login failed: {response.status_code} {response.text}")
    token = response.json()["access_token"]
    orgs = client.get("/api/organizations", headers=auth(token)).json()
    org_id = orgs[0]["id"]

    # Local development may use an ephemeral encryption key. Refresh an
    # existing probe provider's credentials so the probe remains repeatable
    # after the backend is restarted with the same SQLite database.
    providers_response = client.get(
        "/api/llm/providers", headers=auth(token, org_id)
    )
    providers = (
        providers_response.json() if providers_response.status_code == 200 else []
    )
    existing_stub = next(
        (provider for provider in providers if provider.get("name") == "stub probe provider"),
        None,
    )
    if existing_stub is not None:
        response = client.put(
            f"/api/llm/providers/{existing_stub['id']}",
            headers=auth(token, org_id),
            json={
                "credentials": {
                    "api_key": "stub-key",
                    "base_url": f"{args.stub_url}/v1",
                },
                "models": [],
            },
        )
        if response.status_code != 200:
            sys.exit(
                "stub provider refresh failed: "
                f"{response.status_code} {response.text}"
            )

    ensure_llm_provider(
        client,
        token,
        org_id,
        "stub",
        stub_base_url=f"{args.stub_url}/v1",
    )
    response = client.put(
        "/api/organization/settings",
        headers=auth(token, org_id),
        json={
            "config": {
                "enable_agent_notes": {"value": True},
                "suggest_instructions": {"value": False},
                "enable_follow_ups": {"value": False},
                "enable_llm_judgement": {"value": False},
                "ai_tool_concurrency": {"value": 1},
                "agent_max_steps": {"value": 20},
            }
        },
    )
    if response.status_code != 200:
        sys.exit(f"settings update failed: {response.status_code} {response.text}")

    response = client.post(
        "/api/reports",
        headers=auth(token, org_id),
        json={
            "title": "Run control localhost probe",
            "widget": None,
            "files": [],
            "data_sources": [],
        },
    )
    if response.status_code != 200:
        sys.exit(f"report create failed: {response.status_code} {response.text}")
    report_id = response.json()["id"]

    response = client.post(
        f"/api/reports/{report_id}/completions",
        headers=auth(token, org_id),
        params={"background": False},
        json={
            "prompt": {
                "content": "Run the deterministic multi-step completion-control probe.",
                "widget_id": None,
                "step_id": None,
                "mentions": [{}],
            }
        },
    )
    if response.status_code != 200:
        sys.exit(f"completion failed: {response.status_code} {response.text[:500]}")

    run = _latest_run(args.db_path, report_id)
    stats = stub.get("/stats").json()
    plan = next((note for note in run["notes"] if note["title"] == "Plan"), None)
    tool_names = [tool["tool_name"] for tool in run["tools"]]
    create_note_results = [
        bool(tool["success"])
        for tool in run["tools"]
        if tool["tool_name"] == "create_note"
    ]
    completion_payload = json.loads(run["completion"]["completion"] or "{}")
    final_content = str(completion_payload.get("content") or "")

    assert run["completion"]["status"] == "success", run
    assert run["execution"]["status"] == "success", run
    assert stats["planner_calls"] >= 8, stats
    assert create_note_results[:2] == [False, True], create_note_results
    assert tool_names.count("search_agents") == 3, tool_names
    assert tool_names.count("edit_note") == 1, tool_names
    assert plan is not None, run["notes"]
    assert "- [ ]" not in plan["content"], plan
    assert plan["content"].count("- [x]") == 3, plan
    assert "Verified completion" in final_content, completion_payload
    assert "Premature answer" not in final_content, completion_payload

    print(json.dumps({
        "report_id": report_id,
        "completion_status": run["completion"]["status"],
        "execution_status": run["execution"]["status"],
        "planner_calls": stats["planner_calls"],
        "tool_names": tool_names,
        "create_note_successes": create_note_results,
        "plan": plan["content"],
        "final_content": final_content,
    }, indent=2))
    print("PASS: localhost run recovered from a tool error and rejected premature completion")


if __name__ == "__main__":
    main()
