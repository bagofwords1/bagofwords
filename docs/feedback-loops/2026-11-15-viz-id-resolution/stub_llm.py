#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub: a model that miscopies a visualization id.

Plays the planner of a weak model asked for a dashboard. It calls
``create_artifact`` with the id in STUB_VIZ_ID — written exactly as given, in
both ``visualization_ids`` and the code's ``vizById("…")`` — which is how a
real model carries a miscopied id. When the tool refuses and its error lists
``available_visualizations``, the stub retries ONCE with the first id from
that list, the way a model copies from an error. Then it answers in text.

The whole stack runs for real (planner loop, tool runner, create_artifact,
render validation, chat UI); only the model's words are scripted. Model
judgment is not tested here.

Env: STUB_VIZ_ID (required), STUB_PORT (default 9099).
Run (from backend/): STUB_VIZ_ID=... uv run python ../docs/feedback-loops/2026-11-15-viz-id-resolution/stub_llm.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools", "agent"))
import stub_llm as base  # noqa: E402  (SSE / JSON response helpers)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

app = FastAPI()


def _viz_id() -> str:
    # Read per request so one stub serves several scenarios (see run.sh).
    path = os.environ.get("STUB_VIZ_ID_FILE")
    if path and os.path.exists(path):
        return open(path).read().strip()
    return os.environ["STUB_VIZ_ID"]


def _page(viz_id: str) -> str:
    return (
        '<script type="text/babel">\n'
        "function App() {\n"
        f'  const sales = vizById("{viz_id}");\n'
        "  const rows = (sales && sales.rows) || [];\n"
        "  return (\n"
        '    <div className="p-8 max-w-3xl mx-auto">\n'
        '      <h1 className="text-2xl font-semibold mb-4">Revenue by genre</h1>\n'
        '      <table className="w-full text-sm">\n'
        "        <tbody>\n"
        "          {rows.map((r, i) => (\n"
        '            <tr key={i} className="border-b"><td className="py-2">{r.genre}</td>'
        '<td className="py-2 text-right">{r.revenue}</td></tr>\n'
        "          ))}\n"
        "        </tbody>\n"
        "      </table>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
        "</script>"
    )


def _tool_results(body: dict) -> list:
    return [m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            for m in body.get("messages", []) if m.get("role") == "tool"]


def _available_ids(observation: str) -> list:
    """The ids under ``error.available_visualizations`` in a tool result."""
    try:
        error = json.loads(observation[observation.index("{"):]).get("error") or {}
        return [v["id"] for v in error.get("available_visualizations") or []]
    except (ValueError, TypeError, KeyError, AttributeError):
        return []


def _create(viz_id: str) -> list:
    return [{"name": "create_artifact", "arguments": {
        "prompt": "User and task: revenue by genre. Primary surface: one table. Data: the revenue viz.",
        "title": "Revenue by genre", "mode": "page",
        "visualization_ids": [viz_id], "code": _page(viz_id),
    }}]


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))

    def say(text):
        return base._stream_response(text, None) if stream else base._json_response(text)

    if not body.get("tools"):
        return say("Revenue by genre")

    results = _tool_results(body)
    if not results:
        return base._stream_response("Building the dashboard.", _create(_viz_id()))
    last = results[-1]
    if len(results) == 1:
        listed = _available_ids(last)
        if listed:
            return base._stream_response("That id was not found; using one the error lists.", _create(listed[0]))
    if '"artifact_id"' in last and '"error"' not in last:
        return say("The dashboard is ready.")
    return say("I could not build the dashboard: " + last[:300])


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
