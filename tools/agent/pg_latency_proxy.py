"""TCP proxy 127.0.0.1:6543 -> 127.0.0.1:5432 adding DELAY_MS one-way latency (order-preserving).

Mimics a networked Postgres for race reproductions: point BOW_DATABASE_URL at
postgresql://USER:PASS@127.0.0.1:6543/DB.  Run: DELAY_MS=15 python3 tools/agent/pg_latency_proxy.py
"""
import asyncio, os
DELAY = float(os.environ.get("DELAY_MS", "15")) / 1000
async def pipe(r, w):
    q = asyncio.Queue()
    async def reader():
        while True:
            d = await r.read(65536)
            await q.put((asyncio.get_running_loop().time() + DELAY, d))
            if not d: return
    async def writer():
        while True:
            t, d = await q.get()
            await asyncio.sleep(max(0, t - asyncio.get_running_loop().time()))
            if not d:
                w.close(); return
            w.write(d); await w.drain()
    await asyncio.gather(reader(), writer(), return_exceptions=True)
async def handle(cr, cw):
    try:
        sr, sw = await asyncio.open_connection("127.0.0.1", 5432)
    except Exception:
        cw.close(); return
    await asyncio.gather(pipe(cr, sw), pipe(sr, cw), return_exceptions=True)
async def main():
    s = await asyncio.start_server(handle, "127.0.0.1", 6543)
    async with s: await s.serve_forever()
asyncio.run(main())
