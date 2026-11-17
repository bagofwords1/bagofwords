"""A mock MCP client: a GPT model driving BOW's MCP server like Claude Desktop / Cursor would.

    cd backend && OPENAI_API_KEY=... uv run python ../docs/feedback-loops/mcp-agent-folders/mcp_client.py \
        <persona> "<message>" ["<follow-up>" ...]

It speaks plain JSON-RPC to /api/mcp with the persona's API key, hands the
server's tools/list to the model as functions, and runs the tool loop. The
system prompt is deliberately generic — it says nothing about folders, scopes
or how BOW works — so the tool descriptions have to carry the model.
Each run appends to transcripts.jsonl.
"""
import json
import os
import pathlib
import sys
import time

import httpx
from openai import OpenAI

HERE = pathlib.Path(__file__).resolve().parent
SEED = json.loads((HERE / "seed.json").read_text())  # written by seed.py
KEY = os.environ["OPENAI_API_KEY"]  # never commit a key
MODEL = os.environ.get("MOCK_CLIENT_MODEL", "gpt-6-luna")
BASE = "http://localhost:8000/api/mcp"

SYSTEM = (
    "You are a helpful assistant. You are connected to the user's company data platform "
    "through an MCP server and can use its tools. Do what the user asks; ask only if you "
    "truly cannot proceed. Keep your final answer short."
)


class BowMCP:
    def __init__(self, api_key: str):
        self.http = httpx.Client(timeout=300, headers={"X-API-Key": api_key})
        self.n = 0
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "mock-gpt", "version": "0"},
                                 "capabilities": {}})

    def _rpc(self, method, params):
        self.n += 1
        r = self.http.post(BASE, json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    def tools(self):
        return self._rpc("tools/list", {})["tools"]

    def call(self, name, args):
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return text, bool(res.get("isError"))


def to_openai(tool):
    params = tool.get("inputSchema") or {"type": "object", "properties": {}}
    return {"type": "function", "name": tool["name"], "description": tool.get("description") or "",
            "parameters": params, "strict": False}


def run(persona: str, messages: list[str], max_steps: int = 25, quiet: bool = False):
    user = SEED["users"][persona]
    mcp = BowMCP(user["key"])
    tools = [to_openai(t) for t in mcp.tools()]
    llm = OpenAI(api_key=KEY)
    prev = None
    log = {"persona": persona, "ts": time.time(), "turns": []}
    for msg in messages:
        turn = {"user": msg, "calls": [], "answer": None}
        if not quiet:
            print(f"\n👤 {persona}: {msg}")
        pending = [{"role": "user", "content": msg}]
        for _ in range(max_steps):
            resp = llm.responses.create(model=MODEL, instructions=SYSTEM, input=pending, tools=tools,
                                        previous_response_id=prev)
            prev = resp.id
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                turn["answer"] = resp.output_text
                if not quiet:
                    print(f"🤖 {resp.output_text}")
                break
            pending = []
            for tc in calls:
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                text, is_err = mcp.call(tc.name, args)
                turn["calls"].append({"tool": tc.name, "args": args, "error": is_err, "result": text[:4000]})
                if not quiet:
                    short = text if len(text) < 700 else text[:700] + "…"
                    print(f"  🔧 {tc.name}({json.dumps(args)[:400]}) {'❌' if is_err else '→'} {short}")
                pending.append({"type": "function_call_output", "call_id": tc.call_id, "output": text[:30000]})
        log["turns"].append(turn)
    with open(HERE / "transcripts.jsonl", "a") as f:
        f.write(json.dumps(log) + "\n")
    return log


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2:])
