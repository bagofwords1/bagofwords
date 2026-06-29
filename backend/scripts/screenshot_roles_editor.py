"""Screenshot the Roles editor showing the manage_service_accounts permission.
Requires enterprise license (custom_roles) so the Roles tab is visible.
"""
import os
from playwright.sync_api import sync_playwright

FRONTEND = "http://localhost:3000"
EMAIL, PASSWORD = "admin@example.com", "supersecret123"
OUT = "/tmp/sa_shots"
EXE = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
os.makedirs(OUT, exist_ok=True)


def run():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        pg.goto(f"{FRONTEND}/users/sign-in", wait_until="networkidle", timeout=60000)
        pg.fill("#email", EMAIL); pg.fill("#password", PASSWORD)
        pg.click("button[type=submit]"); pg.wait_for_timeout(4000)
        try:
            pg.get_by_text("Skip onboarding", exact=False).first.click(timeout=4000); pg.wait_for_timeout(1500)
        except Exception: pass
        pg.goto(f"{FRONTEND}/settings/members", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500)
        # Roles tab (visible now that custom_roles is licensed)
        pg.get_by_role("button", name="Roles").first.click(timeout=10000)
        pg.wait_for_timeout(2000)
        pg.screenshot(path=f"{OUT}/07_roles_tab.png", full_page=True)
        # System roles aren't editable — open the New Role editor to show the
        # permission catalog (which includes manage_service_accounts).
        try:
            pg.get_by_role("button", name="New Role").first.click(timeout=6000)
            pg.wait_for_timeout(1500)
        except Exception as e:
            print("new role failed:", e)
        try:
            pg.fill("input[placeholder*='name' i]", "Automation Manager")
        except Exception:
            pass
        # Reveal the permission and screenshot a focused region around it
        try:
            loc = pg.get_by_text("Manage service accounts", exact=False).first
            loc.scroll_into_view_if_needed(timeout=6000)
            pg.wait_for_timeout(600)
            box = loc.bounding_box()
            if box:
                pg.screenshot(path=f"{OUT}/08b_perm_closeup.png",
                              clip={"x": max(0, box["x"]-360), "y": max(0, box["y"]-120),
                                    "width": 760, "height": 320})
        except Exception as e:
            print("scroll to perm failed:", e)
        pg.wait_for_timeout(600)
        pg.screenshot(path=f"{OUT}/08_role_editor_permission.png")
        b.close()
        print("done")


if __name__ == "__main__":
    run()
