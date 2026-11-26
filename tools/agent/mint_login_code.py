#!/usr/bin/env python3
"""Mint a real single-use SSO login code for a sandbox user, without an IdP.

Prints what the provider callback would put in ``/users/sign-in?login_code=``
after a successful SSO round trip, so a browser flow that ends at the provider
can be driven to completion in a sandbox that has no provider credentials.
Used by ``framed_sso_popup_flow.mjs``.

Sandbox only: it logs in with a password and writes the code row straight into
the app's SQLite database.

    source tools/agent/devbox_env.sh        # BOW_EMAIL / BOW_PASSWORD / BOW_SQLITE_DB
    python3 tools/agent/mint_login_code.py
"""
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

api = os.environ.get("BOW_API_URL", "http://localhost:8000")
email, password, db_path = (os.environ.get(k) for k in ("BOW_EMAIL", "BOW_PASSWORD", "BOW_SQLITE_DB"))
if not (email and password and db_path):
    sys.exit("set BOW_EMAIL, BOW_PASSWORD and BOW_SQLITE_DB (source tools/agent/devbox_env.sh)")

body = urllib.parse.urlencode({"username": email, "password": password}).encode()
with urllib.request.urlopen(urllib.request.Request(f"{api}/api/auth/jwt/login", data=body)) as resp:
    token = json.load(resp)["access_token"]

db = sqlite3.connect(db_path)
user_id = db.execute("select id from users where email = ?", (email,)).fetchone()[0]
code = secrets.token_urlsafe(32)
now = datetime.now(timezone.utc).replace(tzinfo=None)
fmt = "%Y-%m-%d %H:%M:%S.%f"
db.execute(
    "insert into login_exchange_codes (id, created_at, updated_at, code_hash, user_id, access_token, expires_at)"
    " values (?, ?, ?, ?, ?, ?, ?)",
    (str(uuid.uuid4()), now.strftime(fmt), now.strftime(fmt), hashlib.sha256(code.encode()).hexdigest(),
     user_id, token, (now + timedelta(seconds=60)).strftime(fmt)),
)
db.commit()
print(code)
