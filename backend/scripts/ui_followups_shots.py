"""Playwright walkthrough for the follow-up suggestions feature.

Drives the real app: logs in, opens the report (which already has a completion
with persisted follow_ups), screenshots the chips below the thumbs, then toggles
the org `enable_follow_ups` setting OFF and screenshots that they disappear.

Run after backend+frontend are up, with sandbox_state.json populated.
"""
import json
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

STATE = json.load(open(Path(__file__).resolve().parent.parent / "sandbox_state.json"))
FE = STATE["endpoints"]["frontend"]
BE = STATE["endpoints"]["backend"]
SESS = STATE["session"]
CRED = STATE["credentials"]
RID = SESS["report_id"]
OUT = "/tmp/followup_shots"
os.makedirs(OUT, exist_ok=True)


def H():
    return {"Authorization": f"Bearer {SESS['token']}", "X-Organization-Id": SESS["org_id"],
            "Content-Type": "application/json"}


def set_follow_ups(enabled: bool):
    cur = requests.get(f"{BE}/api/organization/settings", headers=H()).json()
    fc = cur["config"].get("enable_follow_ups", {"name": "Follow-up suggestions", "description": ""})
    fc = {**fc, "value": enabled, "state": "enabled" if enabled else "disabled"}
    r = requests.put(f"{BE}/api/organization/settings", headers=H(), json={"config": {"enable_follow_ups": fc}})
    print("set enable_follow_ups =", enabled, "->", r.status_code)


def shot(page, name):
    path = f"{OUT}/{name}.png"
    page.screenshot(path=path, full_page=False)
    print("shot:", path)


def login(page):
    page.goto(f"{FE}/users/sign-in", wait_until="networkidle")
    page.wait_for_selector("#email", timeout=30000)
    page.wait_for_timeout(1500)  # let Vue hydrate so the submit handler is bound
    page.fill("#email", CRED["email"])
    page.fill("input[type=password]", CRED["password"])
    page.click("button[type=submit]")
    # wait until we navigate away from the sign-in page
    try:
        page.wait_for_url(lambda u: "sign-in" not in u, timeout=20000)
    except Exception:
        # retry once (first-compile races can drop the first click)
        page.click("button[type=submit]")
        page.wait_for_url(lambda u: "sign-in" not in u, timeout=20000)
    page.wait_for_timeout(3000)
    print("after login:", page.url)


def open_report(page):
    page.goto(f"{FE}/reports/{RID}", wait_until="networkidle")
    # wait for the chat content (the user's question) to render, then settle
    try:
        page.wait_for_selector("text=top 5 artists", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(4000)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1300, "height": 1000})
        page = ctx.new_page()

        login(page)

        # 1) Setting ON -> chips visible
        set_follow_ups(True)
        open_report(page)
        shot(page, "01_followups_on")

        # Try to scroll the suggestions into view and shoot a tight crop
        try:
            el = page.get_by_text("Follow up", exact=True).first
            if el and el.is_visible():
                el.scroll_into_view_if_needed()
                page.wait_for_timeout(800)
                shot(page, "02_followups_closeup")
        except Exception as e:
            print("closeup:", e)

        # 2) Setting OFF -> chips gone
        set_follow_ups(False)
        open_report(page)
        shot(page, "03_followups_off")

        # restore ON for any later manual inspection
        set_follow_ups(True)

        ctx.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
