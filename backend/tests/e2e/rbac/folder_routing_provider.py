"""Optional live planner boundary for the synthetic 300-agent completion fixture."""

import json
import os
from urllib.request import Request, urlopen


def select_focus_tool(step, system, messages, tools):
    from app.ai.llm.clients.openai_client import OpenAi

    if step >= 3:
        return None
    names = {"search_agents", "set_report_agents"}
    translated = OpenAi._translate_tools(tools)
    body = {
        "model": os.environ.get("FOLDER_ROUTING_MODEL", "gpt-4.1-mini"),
        "messages": [{"role": "system", "content": system}, *OpenAi._translate_messages(messages)],
        "temperature": 0,
        "tools": [t for t in translated if t["function"]["name"] in names],
        "tool_choice": "auto", "parallel_tool_calls": False,
    }
    req = Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + os.environ["FOLDER_ROUTING_LIVE_KEY"],
                           "Content-Type": "application/json"})
    with urlopen(req, timeout=60) as response:
        message = json.load(response)["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    if calls:
        call = calls[0]["function"]
        print(f"Provider step {step + 1}: {call['name']} {call['arguments']}", flush=True)
        if call["name"] in names:
            return call["name"], json.loads(call["arguments"])
    return None
