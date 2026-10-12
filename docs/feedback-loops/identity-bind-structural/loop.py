"""Live loop for pentest finding 828508752 (identity-param filter bypass).

Run from backend/ against a backend started by launch.sh (same DB env):

    TEST_DATABASE_URL=sqlite:///<db> .venv/bin/python \
        ../docs/feedback-loops/identity-bind-structural/loop.py <base_url> <seed.json>

<seed.json> is tools/agent/seed_org.py's output (admin + one invited member).
Creates a SQLite source with orders for three owners, a report with an
identity-param query (viewer.email), then:
  * viewer runs (mode=viewer) as each user  -> rows must be only their own
  * builder runs (mode=builder) of edited code as the owner: the legit filter,
    a tautology, an OR-widened filter, a `<>` filter, and an SQL shape the
    parser can't read (fallback path).
Prints one line per case: rows served / owners seen / error.
"""
import asyncio
import json
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime

import httpx

sys.path.insert(0, ".")
import main  # noqa: F401,E402
from app.dependencies import async_session_maker  # noqa: E402
from app.models.query import Query  # noqa: E402
from app.models.report import Report  # noqa: E402
from app.models.step import Step  # noqa: E402
from app.models.widget import Widget  # noqa: E402

BASE, SEED = sys.argv[1], json.load(open(sys.argv[2]))
ADMIN = SEED["admin"]["email"]

SPEC = {"name": "email", "type": "string", "label": "email", "source": "identity",
        "identity_binding": "viewer.email", "default": None, "required": False,
        "options": None, "options_source": None, "strict_options": False}


def code(where: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files, params):\n"
        "    c = list(ds_clients.values())[0]\n"
        f"    return c.execute_query(\"SELECT owner, item FROM orders {where}\", params={{'email': params['email']}})\n"
    )


LEGIT = code("WHERE owner = :email")
CASES = [
    ("legit  owner = :email", LEGIT),
    ("taut   :email = :email", code("WHERE :email = :email")),
    ("OR     region='EU' OR owner=:email", code("WHERE region = 'EU' OR owner = :email")),
    ("neg    owner <> :email", code("WHERE owner <> :email")),
    # sqlglot can't parse SQLite's NOT INDEXED -> legacy heuristic decides
    ("fallbk NOT INDEXED, owner = :email", code("NOT INDEXED WHERE owner = :email")),
    ("fallbk NOT INDEXED, :email = :email", code("NOT INDEXED WHERE :email = :email")),
]


def h(tok, org):
    return {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}


async def seed_query(report_id):
    s = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        report.status = "published"
        w = Widget(title=f"W {s}", slug=f"w-{s}", report_id=report_id)
        db.add(w); await db.flush()
        q = Query(title="My orders", report_id=report_id, widget_id=w.id,
                  organization_id=report.organization_id, user_id=report.user_id, parameters=[SPEC])
        db.add(q); await db.flush()
        st = Step(title="Step", slug=f"s-{s}", status="success", widget_id=w.id, query_id=q.id,
                  code=LEGIT, data={"rows": [], "columns": []}, created_at=datetime.utcnow())
        db.add(st); await db.flush()
        q.default_step_id = st.id
        await db.commit()
        return str(q.id)


def summarize(r):
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
    if r.status_code != 200:
        return f"HTTP {r.status_code} {str(body.get('detail', body))[:150]}"
    if body.get("error"):
        return f"REFUSED: {str(body['error'])[:150]}"
    step = body.get("step") or body
    rows = ((step.get("data") or {}).get("rows")) or []
    return f"{len(rows)} rows, owners={sorted({r.get('owner') for r in rows})}"


def main_():
    org = SEED["organization"]["id"]
    tokens = {ADMIN: SEED["admin"]["token"]}
    for m in SEED.get("members", []):
        tokens[m["email"]] = m["token"]
    db_path = tempfile.mktemp(suffix=".sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, owner TEXT, region TEXT, item TEXT)")
    owners = list(tokens) + ["carol@example.com"]
    for i, o in enumerate(owners * 2):
        conn.execute("INSERT INTO orders (owner, region, item) VALUES (?, ?, ?)", (o, "EU" if i % 2 else "US", f"item-{i}"))
    conn.commit(); conn.close()
    with httpx.Client(base_url=BASE, timeout=120) as c:
        admin_h = h(tokens[ADMIN], org)
        ds = c.post("/api/data_sources", headers=admin_h, json={
            "name": f"orders-{uuid.uuid4().hex[:4]}", "type": "sqlite", "config": {"database": db_path},
            "credentials": {}, "auth_policy": "system_only", "is_public": True, "generate_summary": False,
            "generate_conversation_starters": False, "generate_ai_rules": False}).json()
        rep = c.post("/api/reports", headers=admin_h, json={"title": "Identity loop", "files": [],
                                                            "data_sources": [ds["id"]]}).json()
        qid = asyncio.run(seed_query(rep["id"]))
        print(f"source rows: {len(owners) * 2} across owners {owners}")
        for email, tok in tokens.items():
            r = c.post(f"/api/queries/{qid}/run", headers=h(tok, org), json={"mode": "viewer", "params": {}})
            print(f"viewer {email:<22} legit query           -> {summarize(r)}")
        for label, src in CASES:
            r = c.post(f"/api/queries/{qid}/run", headers=admin_h, json={"code": src, "params": {}})
            print(f"builder {label:<38} -> {summarize(r)}")
        # reset to the scoped step, then the builder saves the OR-widened edit
        # and a different viewer opens the query
        c.post(f"/api/queries/{qid}/run", headers=admin_h, json={"code": LEGIT, "params": {}})
        c.post(f"/api/queries/{qid}/run", headers=admin_h, json={"code": CASES[2][1], "params": {}})
        for email, tok in list(tokens.items())[1:]:
            r = c.post(f"/api/queries/{qid}/run", headers=h(tok, org), json={"mode": "viewer", "params": {}})
            print(f"viewer {email:<22} after OR edit         -> {summarize(r)}")


main_()
