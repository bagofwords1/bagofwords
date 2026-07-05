#!/usr/bin/env python
"""Live LLM end-to-end for the network_dir tools.

Drives a real Anthropic model (Haiku) through an agentic tool-use loop using the
ACTUAL tool schemas/descriptions from the network_dir agent tools
(list_files / search_files / read_file / write_file), dispatching each tool call
to a real NetworkDirClient over a generated fixture directory.

Proves the LLM-facing contract works end to end: given only the tool metadata,
the model can search the directory, read a file, and 'put' a summary file back.

Requires ANTHROPIC_API_KEY in the environment. Never hard-code the key.

Usage:
    ANTHROPIC_API_KEY=... python scripts/network_dir_live_agent.py /tmp/netdir_demo
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anthropic
import pandas as pd

from app.ai.tools.implementations.list_files import ListFilesTool
from app.ai.tools.implementations.read_file import ReadFileTool
from app.ai.tools.implementations.search_files import SearchFilesTool
from app.ai.tools.implementations.write_file import WriteFileTool
from app.data_sources.clients.network_dir_client import NetworkDirClient

MODEL = "claude-haiku-4-5-20251001"
CONNECTION_ID = "netdir"

TOOL_CLASSES = [ListFilesTool, SearchFilesTool, ReadFileTool, WriteFileTool]


def build_anthropic_tools():
    """Translate the real ToolMetadata into the Anthropic tools schema."""
    tools = []
    for cls in TOOL_CLASSES:
        m = cls().metadata
        tools.append({
            "name": m.name,
            "description": m.description,
            "input_schema": m.input_schema,
        })
    return tools


def dispatch(client: NetworkDirClient, name: str, args: dict) -> dict:
    """Execute a tool call against the real client; return a compact JSON-able
    result the model can reason over."""
    try:
        if name == "list_files":
            files = client.list_files(recursive=args.get("recursive", True))
            pat = args.get("name_pattern")
            if pat:
                import fnmatch
                files = [f for f in files if fnmatch.fnmatch(f["name"].lower(), pat.lower())]
            return {"success": True, "file_count": len(files),
                    "files": [{"id": f["id"], "name": f["name"]} for f in files[:40]]}
        if name == "search_files":
            files = client.search_files(args["query"])[: args.get("max_results", 50)]
            return {"success": True, "file_count": len(files),
                    "files": [{"id": f["id"], "name": f["name"]} for f in files]}
        if name == "read_file":
            payload = client.read_file(args["file_id"])
            if isinstance(payload, pd.DataFrame):
                return {"success": True, "content_type": "tabular",
                        "csv": payload.head(20).to_csv(index=False)}
            if isinstance(payload, (bytes, bytearray)):
                return {"success": True, "content_type": "binary", "byte_count": len(payload)}
            return {"success": True, "content_type": "text", "text": str(payload)[:2000]}
        if name == "write_file":
            entry = client.write_file(
                args["filename"], args.get("content", ""),
                folder_id=args.get("folder"), overwrite=args.get("overwrite", False),
            )
            return {"success": True, "file": {"id": entry["id"], "size": entry["size"]}}
        return {"success": False, "error": f"unknown tool {name}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "/tmp/netdir_demo"
    client = NetworkDirClient(root_path=root, writable=True, recursive=True)
    anth = anthropic.Anthropic()
    tools = build_anthropic_tools()

    system = (
        "You operate a 'network directory' file connection through tools. "
        f"The connection_id is '{CONNECTION_ID}' — pass it to every tool call. "
        "Complete the user's task using the tools, then give a short final summary."
    )
    task = (
        "In this directory, find contract files (search for 'contract'). Read ONE "
        "contract CSV to confirm its shape. Then write a markdown file at "
        "'_related/index.md' (use folder='_related', filename='index.md', "
        "overwrite=true) that lists the ids of up to 5 contract files you found. "
        "Then stop."
    )
    messages = [{"role": "user", "content": task}]

    tool_calls = 0
    for turn in range(10):
        resp = anth.messages.create(
            model=MODEL, max_tokens=1024, system=system, tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                print(f"[assistant] {b.text.strip()[:200]}")
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            tool_calls += 1
            out = dispatch(client, tu.name, tu.input)
            print(f"[tool] {tu.name}({json.dumps(tu.input)[:100]}) -> "
                  f"{json.dumps(out)[:120]}")
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})

    index = Path(root) / "_related" / "index.md"
    print("\n=== RESULT ===")
    print(f"tool calls: {tool_calls}")
    print(f"_related/index.md exists: {index.exists()}")
    if index.exists():
        print("--- index.md ---")
        print(index.read_text()[:600])
    ok = index.exists()
    print("\nLIVE E2E:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
