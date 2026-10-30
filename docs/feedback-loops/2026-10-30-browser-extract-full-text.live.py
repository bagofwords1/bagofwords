#!/usr/bin/env python3
"""Loop B for browser_extract full-text saving (see 2026-10-30-browser-extract-full-text.md).

Against a running stack (tools/agent/boot_stack.sh + seed_org.py):
  1. serves a long "ledger" page on 127.0.0.1:9123 (~180k chars, the asked-for
     row sits near the END, past browser_extract's 60k inline maximum);
  2. registers an Anthropic provider (key from ANTHROPIC_API_KEY, never printed)
     with claude-haiku-5-5 as default + small default;
  3. adds a browser connection allowlisting that page, opens a chat report on it
     and asks a question only the end of the page answers;
  4. prints every tool call the agent made and its final answer.

    cd backend && ANTHROPIC_API_KEY=... BOW_DATABASE_URL=sqlite:///db/agent.db \
      uv run python ../docs/feedback-loops/2026-10-30-browser-extract-full-text.live.py
"""
import http.server
import json
import os
import sqlite3
import threading
import uuid

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
DB = os.environ.get("BOW_DATABASE_URL", "sqlite:///db/agent.db").replace("sqlite:///", "")
PORT = 9123
ROWS = 4000
TARGET = 3871  # near the end of the page


def ledger_html():
    rows = "\n".join(
        f"<tr><td>INV-{i:05d}</td><td>Vendor {i % 97}</td><td>{(i * 7919) % 100000 / 100:.2f}</td></tr>"
        for i in range(ROWS))
    return f"<html><head><title>Invoice ledger</title></head><body><h1>Invoice ledger</h1><table>{rows}</table></body></html>"


EXPECTED = f"{(TARGET * 7919) % 100000 / 100:.2f}"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ledger_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

c = httpx.Client(base_url=BASE, timeout=None, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
c.headers.update({"Authorization": f"Bearer {tok}", "X-Organization-Id": org})

# 1. LLM provider
provs = c.get("/api/llm/providers").json()
prov = next((p for p in provs if p["name"] == "loop-anthropic"), None)
if prov is None:
    r = c.post("/api/llm/providers", json={
        "name": "loop-anthropic", "provider_type": "anthropic",
        "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY"]},
        "models": [{"name": "Claude Haiku 5.5", "model_id": "claude-haiku-5-5"}]})
    assert r.status_code == 200, r.status_code
    prov = r.json()
model = next(m for m in prov["models"] if m["model_id"] == "claude-haiku-5-5")
for small in ("false", "true"):
    assert c.post(f"/api/llm/models/{model['id']}/set_default?small={small}").status_code == 200

# 2. Browser connection + agent
conn = c.post("/api/connections", json={
    "name": f"Ledger site {uuid.uuid4().hex[:4]}", "type": "browser",
    "config": {"url_patterns": [f"http://127.0.0.1:{PORT}/**"], "allow_downloads": False},
    "credentials": {}})
assert conn.status_code in (200, 201), conn.text
ds = c.post("/api/data_sources", json={
    "name": f"Ledger {uuid.uuid4().hex[:4]}", "connection_id": conn.json()["id"], "is_public": True,
    "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False})
assert ds.status_code in (200, 201), ds.text
ds_id = ds.json()["id"]

# 3. Ask
prompt = (f"Open http://127.0.0.1:{PORT}/ledger and tell me the amount on invoice INV-{TARGET:05d}. "
          "Answer with just the amount.")
report = c.post("/api/reports", json={"title": "Ledger lookup", "data_sources": [ds_id]}).json()
with c.stream("POST", f"/api/reports/{report['id']}/completions",
              json={"prompt": {"content": prompt, "mode": "chat"}, "stream": True},
              headers={"Accept": "text/event-stream"}) as resp:
    for _ in resp.iter_lines():
        pass

# 4. Evidence
con = sqlite3.connect(DB)
calls = con.execute(
    "SELECT te.tool_name, te.status, te.arguments_json, te.result_summary FROM tool_executions te "
    "JOIN agent_executions ae ON ae.id = te.agent_execution_id WHERE ae.report_id = ? ORDER BY te.created_at",
    (report["id"],)).fetchall()
files = con.execute(
    "SELECT f.filename, f.source_kind, f.source_ref FROM files f JOIN report_file_association a ON a.file_id = f.id "
    "WHERE a.report_id = ?", (report["id"],)).fetchall()
con.close()
print("REPORT_ID", report["id"])
for name, status, args, summary in calls:
    a = json.loads(args) if isinstance(args, str) else args
    a = {k: v for k, v in (a or {}).items() if k in ("url", "session_id", "max_chars", "file_id", "offset", "length")}
    print(f"TOOL {name} [{status}] {json.dumps(a)}\n     -> {(summary or '')[:300]}")
print("REPORT FILES", files)
completions = c.get(f"/api/reports/{report['id']}/completions").json()
items = completions if isinstance(completions, list) else completions.get("completions", completions.get("items", []))
answer = next((x for x in reversed(items) if x.get("role") == "system"), {})
blocks = answer.get("completion_blocks") or []
text = (blocks[-1].get("content") if blocks else None) or json.dumps(answer)[:600]
print("ANSWER", text)
print("EXPECTED", EXPECTED, "FOUND_IN_ANSWER", EXPECTED in text)
server.shutdown()
