"""Act as an external MCP client (official `mcp` SDK, streamable HTTP) against
/api/mcp and report exactly what it receives for each instruction."""
import asyncio, json, pathlib
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

S = json.load(open(pathlib.Path(__file__).with_name("state.json")))
URL = "http://localhost:8000/api/mcp"

async def main():
    async with streamablehttp_client(URL, headers={"X-API-Key": S["key"]}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            async def call(name, args):
                res = await s.call_tool(name, args)
                raw = res.content[0].text
                return json.loads(raw), len(raw)

            tools = {t.name: t for t in (await s.list_tools()).tools}
            print("list_instructions description mentions read_instruction:",
                  "read_instruction" in (tools["list_instructions"].description or ""))
            print("read_instruction accepts:", sorted(tools["read_instruction"].inputSchema["properties"]))

            out, size = await call("list_instructions", {"search": S["tag"], "limit": 50})
            print("\n== list_instructions ==")
            for it in out["instructions"]:
                name = next(k for k, v in S["ids"].items() if v["id"] == it["id"])
                full = S["ids"][name]["chars"]
                print(f"  {name:6} full={full:>7} shown={len(it['text']):>4} "
                      f"text_truncated={it.get('text_truncated', '<missing>')} text_length={it.get('text_length', '<missing>')}")

            for name in ("medium", "long", "giant"):
                meta = S["ids"][name]
                out, size = await call("read_instruction", {"instruction_id": meta["id"]})
                inst = out["instruction"]
                print(f"\n== read_instruction {name} ({meta['chars']:,} chars, {meta['lines']:,} lines) ==")
                print(f"  response size: {size:,} chars   whole text in one call: {inst['text'] == meta['text']}")
                print(f"  read_mode={inst.get('read_mode')} truncated={inst.get('truncated')} next_offset={inst.get('next_offset')} "
                      f"clipped_line={inst.get('clipped_line')} outline={'yes' if inst.get('outline') else 'no'}")
                if out.get("message"):
                    print("  message:", out["message"][:220])
                if inst.get("truncated"):
                    text, sizes, calls = inst["text"], [size], 1
                    while inst.get("truncated"):
                        args = {"instruction_id": meta["id"], "offset": inst["next_offset"]}
                        if inst.get("next_col_offset") is not None:
                            args["col_offset"] = inst["next_col_offset"]
                        o, sz = await call("read_instruction", args)
                        inst = o["instruction"]; sizes.append(sz); calls += 1
                        text += inst["text"] if inst.get("start_col") else "\n" + inst["text"]
                    print(f"  paged in {calls} calls (largest {max(sizes):,} chars): rebuilt == original: {text == meta['text']}")
                if name != "medium":
                    pat = "LAST LINE" if name == "long" else "TAIL use"
                    o, sz = await call("read_instruction", {"instruction_id": meta["id"], "grep_pattern": pat})
                    m = (o.get("instruction") or {}).get("matches")
                    print(f"  grep {pat!r}: {'not supported' if m is None else [x['line'][-40:] for x in m]}")

asyncio.run(main())
