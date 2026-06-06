import os, sys
from playwright.sync_api import sync_playwright
FE = "http://localhost:3000"; REPORT = os.environ["DEMO_ID"]; OUT = "/tmp/shots"
with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1500, "height": 950}).new_page()
    pg.goto(f"{FE}/users/sign-in", wait_until="domcontentloaded")
    pg.wait_for_selector("#email", timeout=20000)
    pg.fill("#email", "sandbox@bow.dev"); pg.fill("#password", "Sandbox123!")
    pg.click("button[type=submit]"); pg.wait_for_timeout(4000)
    pg.goto(f"{FE}/reports/{REPORT}", wait_until="domcontentloaded")
    pg.wait_for_timeout(6000)
    pg.screenshot(path=f"{OUT}/06_chat.png", full_page=False)
    print("shot:", f"{OUT}/06_chat.png")
    b.close()
