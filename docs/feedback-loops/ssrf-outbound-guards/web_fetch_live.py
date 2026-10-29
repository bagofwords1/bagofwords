import asyncio, importlib.util, sys
import main  # noqa
spec = importlib.util.spec_from_file_location("t", "tests/unit/test_web_fetch_tool.py"); t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)
from app.ai.tools.implementations.web_fetch import WebFetchTool
async def go(url):
    ev = await t._collect(WebFetchTool(), {"url": url}, t._runtime_ctx())
    o = t._end_payload(ev)["output"]
    print(f"{url:45s} success={o['success']} error={(o.get('error_message') or '')[:70]!r}")
for u in ["http://100.64.0.1/latest/meta-data/", "http://100.100.100.200/latest/meta-data/", "http://[::ffff:169.254.169.254]/latest/", "http://127.0.0.1:9999/agent-internal"]:
    asyncio.run(go(u))
