"""Playwright UI walkthrough for the webhook feature — drives the real app and
captures screenshots. Run after backend+frontend are up."""
import os
import sys
import time
from playwright.sync_api import sync_playwright

FE = "http://localhost:3000"
REPORT = os.environ["DEMO_ID"]
OUT = "/tmp/shots"
os.makedirs(OUT, exist_ok=True)


def shot(page, name):
    path = f"{OUT}/{name}.png"
    page.screenshot(path=path, full_page=False)
    print("shot:", path)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()

        # Login
        page.goto(f"{FE}/users/sign-in", wait_until="domcontentloaded")
        page.wait_for_selector("#email", timeout=20000)
        page.fill("#email", "sandbox@bow.dev")
        page.fill("#password", "Sandbox123!")
        page.click("button[type=submit]")
        page.wait_for_timeout(4000)
        print("after login url:", page.url)

        # Open the demo report
        page.goto(f"{FE}/reports/{REPORT}", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        shot(page, "01_report")

        # Open the side panel (Sidebar toggle), then the Summary tab
        for label in ["Sidebar", "Summary"]:
            try:
                el = page.get_by_text(label, exact=True).first
                if el and el.is_visible():
                    el.click()
                    page.wait_for_timeout(1500)
                    print("clicked:", label)
            except Exception as e:
                print(f"{label} click:", e)
        page.wait_for_timeout(1000)
        shot(page, "02_summary")

        # Open the Configure webhook modal
        try:
            page.get_by_text("Configure webhook", exact=True).first.click()
            page.wait_for_timeout(1500)
            shot(page, "03_modal")

            # Fill the form (generic, AI on, guidance)
            page.fill("input[placeholder='PR triage']", "Sales triage")
            # source select -> generic
            selects = page.locator("select")
            if selects.count() >= 1:
                selects.nth(0).select_option("generic")
                page.wait_for_timeout(300)
            page.fill("textarea", "Only respond to events about music store sales, tracks, albums or artists.")
            page.wait_for_timeout(400)
            shot(page, "04_modal_filled")

            page.get_by_text("Create webhook", exact=True).first.click()
            page.wait_for_timeout(2500)
            shot(page, "05_secret_reveal")
        except Exception as e:
            print("modal flow error:", e)
            shot(page, "03_modal_error")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
