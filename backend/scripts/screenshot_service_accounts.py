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

        # Skip onboarding if it intercepts (new org has no model/data source yet).
        try:
            page.get_by_text("Skip onboarding", exact=False).first.click(timeout=5000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print("no onboarding skip:", e)

        # 2. Service Accounts tab
        page.goto(f"{FRONTEND}/settings/members", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        # if redirected back to onboarding, skip again then retry
        if "members" not in page.url:
            try:
                page.get_by_text("Skip onboarding", exact=False).first.click(timeout=5000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            page.goto(f"{FRONTEND}/settings/members", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
        page.screenshot(path=f"{OUT}/02b_members_page.png", full_page=True)
        try:
            page.get_by_role("button", name="Service Accounts").first.click(timeout=10000)
        except Exception as e:
            print("tab click failed:", e)
            try:
                page.get_by_text("Service Accounts").first.click(timeout=5000)
            except Exception as e2:
                print("tab text click failed:", e2)
        page.wait_for_timeout(2500)
        page.screenshot(path=f"{OUT}/03_service_accounts_tab.png", full_page=True)

        # 3. Create a NEW service account via the UI
        try:
            page.get_by_role("button", name="New service account").first.click(timeout=8000)
            page.wait_for_timeout(1000)
            page.fill("input[placeholder='CI Pipeline']", "Reporting Bot")
            page.fill("input[placeholder='Automated report generation']", "Nightly dashboard refresh")
            page.wait_for_timeout(300)
            page.screenshot(path=f"{OUT}/04_create_modal.png")
            # Leave role unset (backend defaults to member) for a reliable submit.
            page.get_by_role("button", name="Create", exact=True).first.click(timeout=5000)
            page.wait_for_timeout(2500)
            # detail modal opens — mint a key
            try:
                page.get_by_role("button", name="Create key").first.click(timeout=5000)
                page.wait_for_timeout(2000)
            except Exception as e:
                print("create key skipped:", e)
            page.screenshot(path=f"{OUT}/05_key_minted.png")
        except Exception as e:
            print("create flow failed:", e)

        # 6. Final list state
        page.goto(f"{FRONTEND}/settings/members", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        try:
            page.get_by_role("button", name="Service Accounts").first.click(timeout=8000)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        page.screenshot(path=f"{OUT}/06_final_list.png", full_page=True)

        browser.close()
        print("screenshots written to", OUT)


if __name__ == "__main__":
    run()
