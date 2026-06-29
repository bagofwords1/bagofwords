"""Drive the dev UI and screenshot the Service Accounts feature.

Requires: backend on :8000 (app.db with admin@example.com), frontend dev on :3000.
Run: uv run python scripts/screenshot_service_accounts.py
"""
import os
from playwright.sync_api import sync_playwright

FRONTEND = "http://localhost:3000"
EMAIL = "admin@example.com"
PASSWORD = "supersecret123"
OUT = "/tmp/sa_shots"
CHROMIUM = "/opt/pw-browsers/chromium"
os.makedirs(OUT, exist_ok=True)


def run():
    with sync_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        exe = None
        # Prefer the pre-installed chromium if the bundled one is absent.
        for cand in (CHROMIUM, "/opt/pw-browsers/chromium/chrome-linux/chrome"):
            if os.path.exists(cand):
                exe = cand
                break
        if exe:
            launch["executable_path"] = exe
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        # 1. Login
        page.goto(f"{FRONTEND}/users/sign-in", wait_until="networkidle", timeout=60000)
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.screenshot(path=f"{OUT}/01_login.png")
        page.click("button[type=submit]")
        page.wait_for_timeout(5000)
        page.screenshot(path=f"{OUT}/02_after_login.png")

        # 2. Service Accounts tab
        page.goto(f"{FRONTEND}/settings/members", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        try:
            page.get_by_text("Service Accounts", exact=True).first.click(timeout=10000)
        except Exception as e:
            print("tab click failed:", e)
        page.wait_for_timeout(2500)
        page.screenshot(path=f"{OUT}/03_service_accounts_tab.png", full_page=True)

        # 3. Open the detail/keys modal for the existing CI Pipeline account
        try:
            page.get_by_role("button", name="").first  # noop
            # open the row action menu
            menus = page.locator("button:has(.i-heroicons-ellipsis-horizontal)")
            if menus.count():
                menus.first.click()
                page.wait_for_timeout(800)
                page.get_by_text("Manage keys").first.click(timeout=5000)
                page.wait_for_timeout(1500)
                page.screenshot(path=f"{OUT}/04_keys_modal.png")
        except Exception as e:
            print("keys modal step skipped:", e)

        # 4. Roles editor showing the permission
        try:
            page.goto(f"{FRONTEND}/settings/members", wait_until="networkidle", timeout=60000)
            page.get_by_text("Roles", exact=True).first.click(timeout=8000)
            page.wait_for_timeout(2000)
            page.screenshot(path=f"{OUT}/05_roles_tab.png", full_page=True)
        except Exception as e:
            print("roles step skipped:", e)

        browser.close()
        print("screenshots written to", OUT)


if __name__ == "__main__":
    run()
