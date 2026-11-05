"""An authentication failure recorded the way the OAuth/OIDC handlers record it (organization_id=None),
through the very same function (app.services.auth_providers._audit_auth_event), against the live DB.
Then: is it stamped, exported, or visible in the org's audit list?"""
import sys, os, json, time, sqlite3, asyncio; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import main  # registers mappers (uvicorn only runs under __main__)
from starlette.requests import Request
from app.services.auth_providers import _audit_auth_event
scope = {"type": "http", "method": "GET", "path": "/api/auth/google/callback", "headers": [(b"user-agent", b"audit-loop/oauth-sim"), (b"x-forwarded-for", b"198.51.100.9")], "client": ("127.0.0.1", 5555), "query_string": b"", "server": ("localhost", 8000), "scheme": "http"}
req = Request(scope)
asyncio.run(_audit_auth_event(action="auth.login_failed", request=req, details={"provider": "google", "reason": "provider_error", "error": "access_denied", "attempted_email": "intruder@example.com"}))
asyncio.run(_audit_auth_event(action="auth.login", request=req, user_id=None, details={"provider": "google", "email": "someone@example.com"}))
base = stats()["deliveries"]
time.sleep(13)
con = sqlite3.connect(os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db")); con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute("select id, organization_id, user_id, action, ip_address, user_agent, export_seq, details from audit_logs where action like 'auth.%'")]
print("DB rows:"); [print("  ", r) for r in rows]
H = hdr(ADMIN["token"])
lst = api.get("/api/enterprise/audit", headers=H, params={"action": "auth.login_failed,auth.login", "page_size": 50}).json()
print("org audit list total for auth.* :", lst.get("total"))
st = stats(); print("collector auth.* received:", {k: v for k, v in st["actions"].items() if k.startswith("auth.")}, "deliveries since:", st["deliveries"] - base)
print("stream status pending:", [(s["name"], s["status"]) for s in api.get("/api/enterprise/audit/streams", headers=H).json()])
