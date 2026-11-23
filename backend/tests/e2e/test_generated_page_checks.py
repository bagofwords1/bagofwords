"""A page the tool writes itself must pass the page checks, in a real agent turn.

When the planner sends no code, create_artifact asks its own LLM for the page
and repairs it in-tool. It used to save the page even if a page check still
failed (only a note rode along), so every check could be skipped by not
sending code. Now the call fails with the check errors and no completed
version is saved.

The ask goes through the completions route. Only the external boundaries are
faked: the LLM provider (the planner calls create_artifact without code; the
tool's own call writes a raw line chart, and its "repair" returns the same
page) and the headless browser (the page renders without a thrown error).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_generated_page_checks.py -m e2e --db=sqlite
"""
import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

RAW_LINE_PAGE = (
    '```jsx\n<script type="text/babel">\n'
    "function App() {\n"
    "  const option = { xAxis: { type: 'category', data: ['a', 'b'] }, yAxis: { type: 'value' },\n"
    "    series: [{ type: 'line', data: [1, 2] }] };\n"
    "  return <EChart option={option} />;\n"
    "}\n"
    "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
    "</script>\n```"
)
FINAL_ANSWER = "I could not save the page."


class _Provider:
    """The planner asks create_artifact to write the page, then answers; the
    tool's own calls (generation and repair) get the raw line chart."""

    def __init__(self):
        self.planner_messages = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent,
            TextDeltaEvent,
            ToolUseCompleteEvent,
            ToolUseStartEvent,
            UsageEvent,
        )
        provider = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            if not tools:
                yield TextDeltaEvent(text=RAW_LINE_PAGE)
                yield MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")
                return
            provider.planner_messages.append(list(messages or []))
            if len(provider.planner_messages) == 1:
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                args = {"prompt": "A line chart of revenue by month", "title": "Revenue", "mode": "page",
                        "visualization_ids": []}
                events = [ToolUseStartEvent(id=tid, name="create_artifact"),
                          ToolUseCompleteEvent(id=tid, name="create_artifact", input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text=FINAL_ANSWER),
                          MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for event in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield event

        return scripted

    def planner_saw(self, text):
        return any(text in json.dumps(m, default=str) for m in self.planner_messages[1:])


@pytest.fixture
def provider(monkeypatch):
    from app.ai.llm.llm import LLM

    p = _Provider()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    return p


@pytest.fixture
def browser(monkeypatch):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async def renders(self, html):
        return None, []          # the page renders, no thrown error

    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", renders)
    # No vendored browser libs in CI: give the validator a page to "render".
    monkeypatch.setattr(CreateArtifactTool, "_build_thumbnail_html", lambda self, data, code, mode="page": "<html></html>")


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def test_a_generated_page_that_fails_a_check_is_not_saved_as_completed(test_client, env, provider, browser):
    r = test_client.post("/api/reports", headers=env, json={"title": "Generated page checks", "files": [],
                                                            "data_sources": []})
    assert r.status_code == 200, r.text
    report_id = r.json()["id"]

    r = test_client.post(f"/api/reports/{report_id}/completions", headers=env, params={"background": False},
                         json={"prompt": {"content": "Build a page with a line chart of revenue by month."}})
    assert r.status_code == 200, r.text

    r = test_client.get(f"/api/artifacts/report/{report_id}", headers=env)
    assert r.status_code == 200, r.text
    assert [a for a in r.json() if a["status"] == "completed"] == [], "no completed version is saved"

    r = test_client.get(f"/api/reports/{report_id}/completions", headers=env)
    assert r.status_code == 200, r.text
    turn = [c for c in r.json()["completions"] if c["role"] == "system"][-1]
    builds = [b["tool_execution"] for b in turn.get("completion_blocks") or []
              if (b.get("tool_execution") or {}).get("tool_name") == "create_artifact"]
    assert builds, "the planner's create_artifact call ran"
    assert all(not b["success"] for b in builds), "the build is shown as failed"
    assert provider.planner_saw("[charts]"), "the planner is told which check failed"
