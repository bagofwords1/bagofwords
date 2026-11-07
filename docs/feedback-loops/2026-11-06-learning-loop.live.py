#!/usr/bin/env python3
"""Loop B for 2026-11-06-learning-loop.md.

Against a running backend (see the doc) with a seeded admin and
$ANTHROPIC_KEY set:

1. adds an Anthropic provider (key from env only) with one model,
2. builds a mock card-transactions database: `transactions` (300 rows) with a
   ground-truth `fraud_labels` table, and an unlabelled `new_transactions`
   holdout whose labels only this script knows. The labels follow two hidden
   rules plus a few noisy rows,
3. BASELINE: asks a chat turn to flag fraud in `new_transactions` with no
   instructions, and scores it against the hidden labels,
4. TRAINING: opens a training report and asks the agent to learn, from every
   labelled transaction, the rules that decide fraud — starting from zero
   instructions,
5. publishes the build the run produced and asks the same chat question again,
6. prints instructions written, holdout precision/recall before and after, and
   whether the planner saw a <run_efficiency> line (from BOW_LLM_TRACE_FILE).

    cd backend && uv run python ../docs/feedback-loops/2026-11-06-learning-loop.live.py
"""
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL, PASSWORD = os.environ.get("BOW_EMAIL", "admin@example.com"), os.environ.get("BOW_PASSWORD", "Password123!")
MODEL = os.environ.get("BOW_LIVE_MODEL", "claude-haiku-5-5")
TRACE = os.environ.get("BOW_LLM_TRACE_FILE")
OUT = Path(os.environ.get("LOOP_OUT", Path(__file__).with_suffix(".out.json")))
DB_FILE = Path(__file__).resolve().parents[2] / "backend" / "db" / "seed_sources" / "fraud_mock.db"
TRAIN_TIMEOUT_S = int(os.environ.get("TRAIN_TIMEOUT_S", "1500"))

c = httpx.Client(base_url=BASE, timeout=120)


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:600]}")
    return r.json() if r.content else None


# --- mock data -----------------------------------------------------------------
CATS = ["groceries", "electronics", "travel", "gift_cards", "restaurants", "fuel"]
COUNTRIES = ["US", "GB", "DE", "FR", "BR", "NG", "IN"]


def hidden_rule(amount, card_country, merchant_country, category, hour):
    if amount >= 800 and card_country != merchant_country:
        return 1
    if category == "gift_cards" and hour < 5:
        return 1
    return 0


def make_rows(rng, start_id, n):
    rows = []
    for i in range(n):
        tid = start_id + i
        card = rng.choice(COUNTRIES)
        merchant = card if rng.random() < 0.65 else rng.choice(COUNTRIES)
        cat = rng.choice(CATS)
        amount = round(rng.choice([rng.uniform(5, 300), rng.uniform(300, 799), rng.uniform(800, 3000)]), 2)
        hour = rng.randrange(24)
        day = 1 + rng.randrange(28)
        channel = rng.choice(["web", "pos", "app"])  # decoy: unrelated to fraud
        rows.append((tid, f"cust_{rng.randrange(1, 80)}", amount, card, merchant, cat, channel,
                     f"2026-10-{day:02d} {hour:02d}:{rng.randrange(60):02d}:00"))
    return rows


def label(row):
    _, _, amount, card, merchant, cat, _, ts = row
    return hidden_rule(amount, card, merchant, cat, int(ts[11:13]))


def build_db():
    rng = random.Random(1106)
    train = make_rows(rng, 1000, 300)
    hold = make_rows(rng, 5000, 120)
    labels = {r[0]: label(r) for r in train}
    for tid in rng.sample(sorted(labels), 4):  # a little label noise: exceptions, not rules
        labels[tid] = 1 - labels[tid]
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DB_FILE.exists():
        DB_FILE.unlink()
    schema = ("txn_id INTEGER PRIMARY KEY, customer_id TEXT, amount REAL, card_country TEXT, "
              "merchant_country TEXT, merchant_category TEXT, channel TEXT, created_at TEXT")
    with sqlite3.connect(DB_FILE) as db:
        db.execute(f"CREATE TABLE transactions ({schema})")
        db.execute(f"CREATE TABLE new_transactions ({schema})")
        db.execute("CREATE TABLE fraud_labels (txn_id INTEGER PRIMARY KEY, is_fraud INTEGER)")
        db.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", train)
        db.executemany("INSERT INTO new_transactions VALUES (?,?,?,?,?,?,?,?)", hold)
        db.executemany("INSERT INTO fraud_labels VALUES (?,?)", sorted(labels.items()))
    truth = {r[0] for r in hold if label(r)}
    return truth, sum(labels.values())


# --- stack setup ------------------------------------------------------------------
token = ok(c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}))["access_token"]
org = ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}))["organizations"][0]["id"]
H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}

providers = ok(c.get("/api/llm/providers", headers=H))
if not any(p.get("provider_type") == "anthropic" for p in providers):
    key = os.environ.get("ANTHROPIC_KEY") or sys.exit("ANTHROPIC_KEY not set")
    ok(c.post("/api/llm/providers", headers=H, json={
        "name": "anthropic-live", "provider_type": "anthropic", "credentials": {"api_key": key},
        "models": [{"model_id": MODEL, "name": MODEL, "is_custom": False, "is_default": True}],
    }))

holdout_truth, train_fraud = build_db()
ds_name = f"card_payments_{int(time.time()) % 100000}"
ds = ok(c.post("/api/data_sources", headers=H, json={
    "name": ds_name, "type": "sqlite", "config": {"database": str(DB_FILE)}, "credentials": {},
    "auth_policy": "system_only", "generate_summary": False,
    "generate_conversation_starters": False, "generate_ai_rules": False,
}))
TABLES = ["transactions", "fraud_labels", "new_transactions"]
for _ in range(60):
    body = ok(c.get(f"/api/data_sources/{ds['id']}/full_schema", headers=H))
    items = body.get("tables", body) if isinstance(body, dict) else body
    if set(TABLES) <= {t.get("name") for t in items}:
        break
    time.sleep(1)
ok(c.put(f"/api/data_sources/{ds['id']}/update_tables_status", headers=H,
         json={"activate": TABLES, "deactivate": []}))

skills = {s["key"]: s for s in ok(c.get("/api/instructions/skill-catalog", headers=H))}
assert skills.get("learning-loop", {}).get("installed"), "learning-loop skill is not installed in this org"
# The org holds a copy of the skill; re-sync it so edits to the library file are what runs.
ok(c.post("/api/instructions/skill-catalog/learning-loop/update", headers=H))


APP_DB = Path(__file__).resolve().parents[2] / "backend" / "db" / "agent.db"


def all_instructions():
    """Every non-skill instruction row, drafts included (read from the app DB:
    the training run's rows are attributed to the user who ran it, so the
    API's source filters cannot tell them apart)."""
    with sqlite3.connect(APP_DB) as db:
        return {r[0]: {"id": r[0], "status": r[1], "title": r[2], "text": r[3]} for r in db.execute(
            "select id, status, title, text from instructions where kind != 'skill' and deleted_at is null")}


before_ids = set(all_instructions())
start_count = len(before_ids)
print(f"[setup] data source {ds_name}; instructions in the org at start: {start_count}; "
      f"train fraud rows {train_fraud}/300; holdout fraud {len(holdout_truth)}/120")


# --- conversation helpers -------------------------------------------------------------
def completions(report_id):
    rows = ok(c.get(f"/api/reports/{report_id}/completions", headers=H, params={"limit": 200}))
    return rows.get("completions", rows) if isinstance(rows, dict) else rows


def busy(report_id):
    """A turn is running, or a sub-agent the run launched has not finished
    (the parent wakes with a follow-up turn when its wave completes)."""
    if any((x.get("status") or "") in ("in_progress", "pending", "queued", "running")
           for x in completions(report_id)):
        return True
    with sqlite3.connect(APP_DB) as db:
        open_children = db.execute(
            "select count(*) from sub_agent_runs where report_id=? and status not in "
            "('finished','failed','timed_out','interrupted')", (report_id,)).fetchone()[0]
    return open_children > 0


def send(report_id, text, mode="chat"):
    # Non-streaming: the call can hold until the turn's first phase is done.
    ok(c.post(f"/api/reports/{report_id}/completions", headers=H, timeout=900, json={
        "prompt": {"content": text, "mentions": [], "mode": mode}, "stream": False}), 200, 201, 202)


def settle(report_id, timeout_s, quiet_s=60):
    """Wait until the report is idle for quiet_s (sub-agent waves wake the
    parent with follow-up turns, so one idle moment is not the end)."""
    deadline, idle_since = time.time() + timeout_s, None
    while time.time() < deadline:
        if busy(report_id):
            idle_since = None
        else:
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= quiet_s:
                return True
        time.sleep(5)
    return False


def last_reply(report_id):
    texts = []
    for comp in completions(report_id):
        if comp.get("role") != "system":
            continue
        for b in comp.get("completion_blocks") or comp.get("blocks") or []:
            for k in ("content", "assistant", "text"):
                v = b.get(k) if isinstance(b, dict) else None
                if isinstance(v, str) and v.strip():
                    texts.append(v)
        content = comp.get("completion", {}) or {}
        if isinstance(content, dict) and content.get("content"):
            texts.append(content["content"])
    return "\n".join(texts[-6:])


ASK_HOLDOUT = (
    "Which transactions in new_transactions are fraud? Use whatever rules you know. "
    "Query the table, decide for every row, and end your reply with one line exactly like "
    "FRAUD_IDS: 5001, 5007 (all fraud txn_ids, comma separated, or FRAUD_IDS: none)."
)


def score_holdout(label):
    report = ok(c.post("/api/reports", headers=H, json={
        "title": f"holdout {label}", "files": [], "data_sources": [ds["id"]]}))
    send(report["id"], ASK_HOLDOUT)
    settle(report["id"], 600, quiet_s=10)
    reply = last_reply(report["id"])
    m = re.findall(r"FRAUD_IDS:\s*([^\n]*)", reply)
    picked = {int(x) for x in re.findall(r"\d{4}", m[-1])} if m else set()
    tp = len(picked & holdout_truth)
    res = {"label": label, "parsed": bool(m), "flagged": len(picked), "true_positives": tp,
           "precision": round(tp / len(picked), 2) if picked else 0.0,
           "recall": round(tp / len(holdout_truth), 2) if holdout_truth else 0.0}
    print("[holdout]", json.dumps(res))
    return res


# --- 1. baseline ----------------------------------------------------------------------
# Two samples per side: one chat answer over 120 rows is noisy.
baseline = [score_holdout("before learning") for _ in range(2)]

# --- 2. training run --------------------------------------------------------------------
TRAIN_PROMPT = (
    "fraud_labels.is_fraud is the correct answer for every transaction in transactions "
    "(300 rows, joined on txn_id). Learn the rules that decide whether a transaction is "
    "fraud, so this agent can flag fraud on new transactions by itself. Learn from all of "
    "them, not a sample, and write what you learn as instructions."
)
train_report = ok(c.post("/api/reports", headers=H, json={
    "title": "learn fraud rules", "files": [], "data_sources": [ds["id"]], "mode": "training"}))
t0 = time.time()
send(train_report["id"], TRAIN_PROMPT, mode="training")
finished = settle(train_report["id"], TRAIN_TIMEOUT_S)
print(f"[train] settled={finished} after {int(time.time() - t0)}s")
print("[train] reply tail:\n" + last_reply(train_report["id"])[-1500:])

learned = [i for k, i in all_instructions().items() if k not in before_ids]
for i in learned:
    print(f"[instruction] {i.get('status')} | {i.get('title')}\n    {(i.get('text') or '')[:400]}")

# --- 3. publish the run's build and re-score -------------------------------------------------
builds = []
for status in ("pending_approval", "draft"):
    page = ok(c.get("/api/builds", headers=H, params={"status": status, "data_source_id": ds["id"]}))
    builds += page.get("items", []) if isinstance(page, dict) else page
published = None
for b in sorted(builds, key=lambda b: b.get("created_at") or ""):
    published = ok(c.post(f"/api/builds/{b['id']}/publish", headers=H, json={}))
print(f"[publish] builds published: {len(builds)}")

after = [score_holdout("after learning") for _ in range(2)]

# --- 4. efficiency line in the planner's actual requests --------------------------------------
eff_lines = []
if TRACE and Path(TRACE).exists():
    for line in Path(TRACE).read_text().splitlines():
        found = re.findall(r"<run_efficiency>.*?</run_efficiency>", line)
        eff_lines += found
print(f"[efficiency] planner requests carrying <run_efficiency>: {len(eff_lines)}")
if eff_lines:
    print("  first:", eff_lines[0][:300])
    print("  last: ", eff_lines[-1][:300])

text_blob = " ".join((i.get("text") or "") + " " + (i.get("title") or "") for i in learned).lower()
summary = {
    "model": MODEL,
    "instructions_at_start": start_count,
    "instructions_learned": len(learned),
    "mentions_amount_threshold": bool(re.search(r"\b(8\d\d|800)\b", text_blob)),
    "mentions_cross_border": any(w in text_blob for w in ("card_country", "cross-border", "cross border", "differs", "!=", "different")),
    "mentions_gift_cards_night": "gift" in text_blob and bool(re.search(r"\b(0?[0-5]|night|early)\b", text_blob)),
    "mentions_record_ids": bool(re.search(r"\b1[0-2]\d\d\b", text_blob)),
    "baseline": baseline,
    "after": after,
    "efficiency_lines": len(eff_lines),
    "train_seconds": int(time.time() - t0),
}
OUT.write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
