#!/usr/bin/env python3
"""Ask the agent questions as different members and check each answer against
what that member may see — the end of the per-user-copies loop.

Each case signs in as one member, opens a new report on the agent, asks one
question of a real LLM, and reads back the reply and the queries its tools
ran. A case passes when every expected number is in the reply, no forbidden
number is, and (unless `cached` is false) at least one query went to the
cached `::fast` client rather than the live source.

    cd backend && UC_CHAT_CASES='[...]' uv run python ../tools/agent/user_copies/chat.py <data_source_id>

UC_CHAT_CASES: JSON list of
    {"email", "prompt", "expect": [numbers], "forbid": [numbers], "cached": true}
Env: BOW_BASE_URL, BOW_MEMBER_PASSWORD (default Password123!).

Exit code 0 only if every case passed.
"""
import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
PASSWORD = os.environ.get("BOW_MEMBER_PASSWORD", "Password123!")
CASES = json.loads(os.environ["UC_CHAT_CASES"])
DS_ID = sys.argv[1]

c = httpx.Client(base_url=BASE, timeout=1200)


def numbers(text_: str) -> set:
    """Whole numbers in a reply, read loosely: 666,667 / 666667 / 333,667,333.00."""
    found = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text_ or ""):
        whole = raw.split(".")[0].replace(",", "")
        if whole:
            found.add(int(whole))
    return found


def ask(email: str, prompt: str) -> dict:
    tok = c.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    h = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    report = c.post("/api/reports", headers=h, json={
        "title": prompt[:60], "widget": None, "files": [], "data_sources": [DS_ID]}).json()
    started = time.monotonic()
    r = c.post(f"/api/reports/{report['id']}/completions", headers=h, params={"background": False},
               json={"prompt": {"content": prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
    listing = c.get(f"/api/reports/{report['id']}/completions", headers=h).json()
    items = listing if isinstance(listing, list) else listing.get("completions") or listing.get("items") or []
    texts, clients, tools = [], set(), []
    for comp in items:
        if comp.get("role") == "user":
            continue
        body = comp.get("completion")
        if isinstance(body, dict) and body.get("content"):
            texts.append(str(body["content"]))
        for block in comp.get("completion_blocks") or []:
            if block.get("content"):
                texts.append(str(block["content"]))
            execution = block.get("tool_execution") or {}
            if execution.get("tool_name"):
                tools.append(execution["tool_name"])
            dump = json.dumps(execution, default=str)
            clients.update(re.findall(r"ds_clients\[\\?[\"']([^\"'\\]+)", dump))
    return {"http": r.status_code, "seconds": round(time.monotonic() - started, 1),
            "reply": "\n".join(texts), "clients": sorted(clients), "tools": tools,
            "report_id": report["id"]}


failed = False
for case in CASES:
    out = ask(case["email"], case["prompt"])
    seen = numbers(out["reply"])
    missing = [n for n in case.get("expect", []) if n not in seen]
    leaked = [n for n in case.get("forbid", []) if n in seen]
    used_cache = any(k.endswith("::fast") for k in out["clients"])
    passed = out["http"] == 200 and not missing and not leaked and (used_cache or case.get("cached") is False)
    failed = failed or not passed
    print(json.dumps({
        "pass": passed, "email": case["email"], "prompt": case["prompt"],
        "seconds": out["seconds"], "clients": out["clients"], "tools": out["tools"],
        "missing": missing, "leaked": leaked, "used_cache": used_cache,
        "reply": out["reply"][-900:], "report_id": out["report_id"],
    }, indent=1))
sys.exit(1 if failed else 0)
