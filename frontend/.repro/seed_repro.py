#!/usr/bin/env python3
"""Seed reproduction data for the two reported UI issues.

Issue 1: instruction scoped to a data source that is no longer visible in
         /data_sources/active (deactivated) -> edit-mode agent chip shows UUID.
Issue 2: instruction with a pending AI suggestion build (Hebrew, multi-line
         hunks) -> hover popover far from change / disappears.

Run:  cd backend && uv run python <this file>
"""
import datetime
import json
import sqlite3
import sys
import uuid

import httpx

BASE = "http://localhost:8000"
DB = "db/agent.db"
EMAIL, PASSWORD = "admin@example.com", "Password123!"

client = httpx.Client(base_url=BASE, timeout=30)

r = client.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
r.raise_for_status()
token = r.json()["access_token"]
orgs = client.get("/api/organizations", headers={"Authorization": f"Bearer {token}"}).json()
org_id = orgs[0]["id"]
H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

now = datetime.datetime.utcnow().isoformat(sep=" ")

# ── 1. data sources (agents), straight into the DB ──────────────────────────
conn = sqlite3.connect(DB)
conn.execute("PRAGMA journal_mode=WAL")

def ensure_ds(name: str) -> str:
    row = conn.execute(
        "SELECT id FROM data_sources WHERE organization_id=? AND name=?", (org_id, name)
    ).fetchone()
    if row:
        return row[0]
    dsid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO data_sources
           (id, created_at, updated_at, name, organization_id, is_active, is_public,
            publish_status, reliability_status, use_llm_sync)
           VALUES (?,?,?,?,?,1,1,'published','ok',0)""",
        (dsid, now, now, name, org_id),
    )
    return dsid

ds_sales = ensure_ds("מכירות")  # מכירות
ds_po = ensure_ds("PO")
ds_orders = ensure_ds("Orders")
ds_legacy = ensure_ds("Legacy DWH")  # will be deactivated after linking
conn.commit()

# ── 2. issue-1 instruction: scoped to [legacy, מכירות] ──────────────────────
MAIN_TEXT_A = (
    "## כללי עבודה עם טבל֪ המכירות\n\n"
    "בעת כתיבת שאיל֪ות על `fact_table_lm_bow` יש להש֪מש ב-`SUM(\"מכירות\")`."
)
r = client.post(
    "/api/instructions",
    headers=H,
    json={
        "title": "כללי מכירות (issue 1)",
        "text": MAIN_TEXT_A,
        "status": "published",
        "load_mode": "always",
        "category": "code_gen",
        "data_source_ids": [ds_legacy, ds_sales],
    },
)
if r.status_code not in (200, 201):
    sys.exit(f"create instruction A failed: {r.status_code} {r.text}")
ins_a = r.json()["id"]

# Deactivate the legacy DS — same thing the connectivity check does on failure
# (data_source_service.py:1745). It vanishes from /data_sources/active while
# the instruction keeps the association.
conn.execute("UPDATE data_sources SET is_active=0 WHERE id=?", (ds_legacy,))
conn.commit()

# ── 3. issue-2 instruction: Hebrew multi-line text + AI suggestion build ────
MAIN_TEXT_B = """## כללי כתיבת code_gen

בעת כתיבת שאילתות על `fact_table_lm_bow`, אם המשתמש מבקש ערך נטו בלי לציין מדד אחר — ברירת המחדל היא לחשב מכירות באמצעות SUM. יש להבחין בין שני סוגי שימושי זמן.

### כלל שנלמד מ-flow:
- יש לסנן את העמודה sales בסכמה כמו שהיא מופיעה.
1. ישירות ליום הנוכחי באמצעות סינון תאריך.
- לא לשנות את שם הטבלה.
- לא להמיר את השם ל-Fact_Table_LM_BOW.
- לא להוסיף סיומת קובץ כמו qvd.

### גרנולריות ואגרגציה
- זו טבלת פקט ברמת שורת תעודה/פריט, ולכן ברוב השאלות האנליטיות יש לבצע אגרגציה.
- למדדי מכירה, עלות, מרווח וכמויות יש להשתמש בדרך כלל ב-SUM.
- לספירת מסמכים יש להעדיף COUNT DISTINCT של מספר תעודה ולא COUNT רגיל.
- לספירת לקוחות יש להעדיף COUNT DISTINCT של מספר לקוח.
"""

# Proposed text — several scattered edits, incl. a long multi-line rewrite in
# the middle (the kind of hunk that wraps across lines).
SUGG_TEXT_B = MAIN_TEXT_B.replace(
    "ברירת המחדל היא לחשב מכירות באמצעות SUM. יש להבחין בין שני סוגי שימושי זמן.",
    "ברירת המחדל היא תמיד לחשב ערך נטו ללא מע״מ באמצעות SUM על עמודת המכירות המוסכמ֪, ולצרף הערה קצרה על הנח֪ המחדל. יש להבחין בין שני סוגי שימושי זמן ולתעד את הבחירה בתגובה.",
).replace(
    "- יש לסנן את העמודה sales בסכמה כמו שהיא מופיעה.",
    "- יש לסנן את העמודה בדיוק כפי שהיא מופיעה בסכמה, כולל רווחים ואותיות מיוחדות.",
).replace(
    "- לספירת לקוחות יש להעדיף COUNT DISTINCT של מספר לקוח.",
    "- לספירת לקוחות יש להעדיף COUNT DISTINCT של מספר לקוח LM לפי ההקשר העסקי.",
)

r = client.post(
    "/api/instructions",
    headers=H,
    json={
        "title": "כללי כתיבת code_gen (issue 2)",
        "text": MAIN_TEXT_B,
        "status": "published",
        "load_mode": "always",
        "category": "code_gen",
        "data_source_ids": [ds_sales],
    },
)
if r.status_code not in (200, 201):
    sys.exit(f"create instruction B failed: {r.status_code} {r.text}")
ins_b = r.json()["id"]

# Inject the AI suggestion build (same shape as backend/tests/e2e/test_instruction.py
# _inject_suggestion_build): fork from main, pending_approval, source=ai.
main = conn.execute(
    """SELECT bc.build_id FROM build_contents bc
       JOIN instruction_builds ib ON ib.id = bc.build_id
       WHERE bc.instruction_id=? AND ib.is_main=1 AND ib.deleted_at IS NULL""",
    (ins_b,),
).fetchone()
assert main, "instruction B not in main build"
vid, bid, bcid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
vnum = conn.execute(
    "SELECT COALESCE(MAX(version_number),0)+1 FROM instruction_versions WHERE instruction_id=?",
    (ins_b,),
).fetchone()[0]
bnum = conn.execute(
    "SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds WHERE organization_id=?",
    (org_id,),
).fetchone()[0]
conn.execute(
    """INSERT INTO instruction_versions
       (id, created_at, updated_at, instruction_id, version_number, text, status, load_mode, content_hash)
       VALUES (?,?,?,?,?,?,'published','always',?)""",
    (vid, now, now, ins_b, vnum, SUGG_TEXT_B, "h" + uuid.uuid4().hex[:12]),
)
conn.execute(
    """INSERT INTO instruction_builds
       (id, created_at, updated_at, build_number, status, source, is_main, organization_id, base_build_id, title)
       VALUES (?,?,?,?,'pending_approval','ai',0,?,?,'suggestion build')""",
    (bid, now, now, bnum, org_id, main[0]),
)
conn.execute(
    """INSERT INTO build_contents
       (id, created_at, updated_at, build_id, instruction_id, instruction_version_id)
       VALUES (?,?,?,?,?,?)""",
    (bcid, now, now, bid, ins_b, vid),
)
conn.commit()
conn.close()

# sanity: review-hunks must show live hunks
rh = client.get(f"/api/instructions/{ins_b}/review-hunks", headers=H).json()
hunks = sum(len(s["hunks"]) for s in rh.get("suggestions", []))

print(json.dumps({
    "org_id": org_id,
    "ds": {"sales": ds_sales, "po": ds_po, "orders": ds_orders, "legacy_hidden": ds_legacy},
    "instruction_issue1": ins_a,
    "instruction_issue2": ins_b,
    "issue2_live_hunks": hunks,
}, indent=2))
