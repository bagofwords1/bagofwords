#!/usr/bin/env python3
"""Loop B2 for 2026-11-06-learning-loop.md — items are files, Y is a judgment.

Against a running backend with a seeded admin and $ANTHROPIC_KEY set:

1. creates an agent ("support_desk", a tiny sqlite source so it has a home for
   instructions) and uploads 40 support tickets as .txt files to its file
   library, plus labels.csv saying which tickets are urgent. A hidden rule
   decides urgency from the ticket's meaning, so no query can answer it — each
   ticket has to be read and judged:
       urgent  <=>  data loss reported (any plan)
                    OR (enterprise plan AND an outage blocking several users)
   Decoys: angry tone with no impact, single-user problems, enterprise feature
   requests, outages on non-enterprise plans.
2. BASELINE: a chat turn triages 15 unseen tickets (uploaded to the report),
   scored against the hidden rule.
3. TRAINING: a training turn asks the agent to learn, from every labelled
   ticket, how to triage urgency — starting from zero instructions.
4. publishes the run's build and re-scores the 15 unseen tickets.

    cd backend && uv run python ../docs/feedback-loops/2026-11-06-learning-loop-files.live.py
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
OUT = Path(os.environ.get("LOOP_OUT", Path(__file__).with_suffix(".out.json")))
ROOT = Path(__file__).resolve().parents[2]
APP_DB = ROOT / "backend" / "db" / "agent.db"
SRC_DB = ROOT / "backend" / "db" / "seed_sources" / "support_desk.db"
TRAIN_TIMEOUT_S = int(os.environ.get("TRAIN_TIMEOUT_S", "1800"))

c = httpx.Client(base_url=BASE, timeout=120)


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:600]}")
    return r.json() if r.content else None


# --- mock tickets -------------------------------------------------------------------
PLANS = ["Enterprise", "Pro", "Starter"]
OUTAGE_MANY = [
    "Nobody on our team can log in since this morning — all {n} of us get a 503.",
    "The dashboard is down for our whole finance department ({n} people), nothing loads.",
    "Since the update none of our {n} analysts can open any report; it just spins.",
]
OUTAGE_ONE = [
    "I can't log in on my laptop, my colleagues are fine.",
    "My own account shows a blank page, everyone else on the team can work.",
]
DATA_LOSS = [
    "Last night's import wiped the customer table — the rows from 2024 are gone.",
    "After I renamed the project, all saved queries disappeared and are not in the trash.",
    "The sync deleted our historical orders; the totals dropped to zero for March.",
]
FEATURE = [
    "Could you add a dark mode? Our team would love it.",
    "It would be great to export charts to PowerPoint directly.",
]
ANGRY_NO_IMPACT = [
    "THIS IS UNACCEPTABLE!!! The font on the settings page is way too small.",
    "Honestly furious — the logo in the header looks blurry on my screen!!!",
]
MINOR = [
    "The tooltip on the revenue chart has a typo ('reveune').",
    "Exporting a CSV takes about ten seconds, a bit slow but it works.",
]


def make_ticket(rng, kind):
    plan = rng.choice(PLANS)
    if kind == "outage_many_ent":
        plan, body = "Enterprise", rng.choice(OUTAGE_MANY).format(n=rng.randrange(5, 60))
    elif kind == "outage_many_other":
        plan = rng.choice(["Pro", "Starter"])
        body = rng.choice(OUTAGE_MANY).format(n=rng.randrange(3, 12))
    elif kind == "outage_one":
        body = rng.choice(OUTAGE_ONE)
    elif kind == "data_loss":
        body = rng.choice(DATA_LOSS)
    elif kind == "feature":
        plan, body = rng.choice(["Enterprise", "Pro"]), rng.choice(FEATURE)
    elif kind == "angry":
        body = rng.choice(ANGRY_NO_IMPACT)
    else:
        body = rng.choice(MINOR)
    urgent = kind in ("data_loss", "outage_many_ent")
    company = rng.choice(["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne", "Soylent"])
    text = (f"From: support-request@{company.lower()}.example\nCompany: {company}\nPlan: {plan}\n"
            f"Subject: {body.split('.')[0][:60]}\n\n{body}\n\nThanks,\n{company} IT\n")
    return text, urgent


KINDS = (["outage_many_ent"] * 3 + ["outage_many_other"] * 2 + ["outage_one"] * 2 + ["data_loss"] * 3
         + ["feature"] * 2 + ["angry"] * 2 + ["minor"] * 2)


def tickets(rng, n, prefix):
    out = []
    for i in range(n):
        text, urgent = make_ticket(rng, KINDS[i % len(KINDS)] if i < len(KINDS) else rng.choice(KINDS))
        out.append((f"{prefix}{i + 1:03d}.txt", text, urgent))
    rng.shuffle(out)
    return out


rng = random.Random(611)
train = tickets(rng, 40, "ticket_")
hold = tickets(rng, 15, "new_ticket_")
holdout_truth = {name for name, _, u in hold if u}

# --- stack setup ---------------------------------------------------------------------
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
ok(c.post("/api/instructions/skill-catalog/learning-loop/update", headers=H))

SRC_DB.parent.mkdir(parents=True, exist_ok=True)
if SRC_DB.exists():
    SRC_DB.unlink()
with sqlite3.connect(SRC_DB) as db:
    db.execute("CREATE TABLE queues (queue TEXT PRIMARY KEY, owner TEXT)")
    db.executemany("INSERT INTO queues VALUES (?,?)", [("urgent", "on-call"), ("normal", "support")])
ds = ok(c.post("/api/data_sources", headers=H, json={
    "name": f"support_desk_{int(time.time()) % 100000}", "type": "sqlite", "config": {"database": str(SRC_DB)},
    "credentials": {}, "auth_policy": "system_only", "generate_summary": False,
    "generate_conversation_starters": False, "generate_ai_rules": False,
}))
for name, text, _ in train:
    ok(c.post(f"/api/data_sources/{ds['id']}/files", headers=H, files={"file": (name, text.encode(), "text/plain")}))
labels_csv = "ticket,urgent\n" + "".join(f"{n},{'yes' if u else 'no'}\n" for n, _, u in sorted(train))
ok(c.post(f"/api/data_sources/{ds['id']}/files", headers=H, files={"file": ("labels.csv", labels_csv.encode(), "text/csv")}))


def all_instructions():
    with sqlite3.connect(APP_DB) as db:
        return {r[0]: {"id": r[0], "status": r[1], "title": r[2], "text": r[3]} for r in db.execute(
            "select id, status, title, text from instructions where kind != 'skill' and deleted_at is null")}


before_ids = set(all_instructions())
print(f"[setup] agent {ds['name']}; 40 tickets + labels.csv uploaded ({sum(u for *_, u in train)} urgent); "
      f"holdout urgent {len(holdout_truth)}/15; instructions at start: {len(before_ids)}")


def completions(report_id):
    rows = ok(c.get(f"/api/reports/{report_id}/completions", headers=H, params={"limit": 200}))
    return rows.get("completions", rows) if isinstance(rows, dict) else rows


def busy(report_id):
    if any((x.get("status") or "") in ("in_progress", "pending", "queued", "running") for x in completions(report_id)):
        return True
    with sqlite3.connect(APP_DB) as db:
        return db.execute("select count(*) from sub_agent_runs where report_id=? and status not in "
                          "('finished','failed','timed_out','interrupted')", (report_id,)).fetchone()[0] > 0


def settle(report_id, timeout_s, quiet_s=60):
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


def last_text(report_id):
    blocks = [b.get("content") or "" for comp in completions(report_id) if comp.get("role") == "system"
              for b in comp.get("completion_blocks") or []]
    return "\n".join(b for b in blocks if b.strip())


ASK = ("Triage the attached support tickets: decide for each one whether it is urgent. End your reply "
       "with one line exactly like URGENT: new_ticket_003.txt, new_ticket_009.txt (or URGENT: none).")


def score(label):
    report = ok(c.post("/api/reports", headers=H, json={"title": f"triage {label}", "files": [],
                                                         "data_sources": [ds["id"]]}))
    file_ids = []
    for name, text, _ in hold:
        f = ok(c.post("/api/files", headers=H, data={"report_id": report["id"]},
                      files={"file": (name, text.encode(), "text/plain")}))
        file_ids.append(f["id"])
    ok(c.post(f"/api/reports/{report['id']}/completions", headers=H, timeout=900, json={
        "prompt": {"content": ASK, "mentions": [], "mode": "chat"}, "stream": False}), 200, 201, 202)
    settle(report["id"], 600, quiet_s=10)
    m = re.findall(r"URGENT:\s*([^\n]*)", last_text(report["id"]))
    picked = set(re.findall(r"new_ticket_\d{3}\.txt", m[-1])) if m else set()
    tp = len(picked & holdout_truth)
    res = {"label": label, "parsed": bool(m), "flagged": len(picked), "true_positives": tp,
           "precision": round(tp / len(picked), 2) if picked else 0.0,
           "recall": round(tp / len(holdout_truth), 2) if holdout_truth else 0.0}
    print("[holdout]", json.dumps(res))
    return res


baseline = [score("before learning") for _ in range(2)]

TRAIN_PROMPT = (
    "The agent's file library has 40 support tickets and labels.csv, which says for every ticket "
    "whether it is urgent. Learn how to decide urgency, so this agent can triage new tickets by "
    "itself. Learn from all 40 tickets, not a sample, and write what you learn as instructions."
)
train_report = ok(c.post("/api/reports", headers=H, json={
    "title": "learn ticket triage", "files": [], "data_sources": [ds["id"]], "mode": "training"}))
t0 = time.time()
ok(c.post(f"/api/reports/{train_report['id']}/completions", headers=H, timeout=900, json={
    "prompt": {"content": TRAIN_PROMPT, "mentions": [], "mode": "training"}, "stream": False}), 200, 201, 202)
finished = settle(train_report["id"], TRAIN_TIMEOUT_S)
print(f"[train] settled={finished} after {int(time.time() - t0)}s")
print("[train] final:\n" + last_text(train_report["id"])[-2500:])

learned = [i for k, i in all_instructions().items() if k not in before_ids]
for i in learned:
    print(f"[instruction] {i['status']} | {i['title']}\n    {(i['text'] or '')[:500]}")

builds = []
for status in ("pending_approval", "draft"):
    page = ok(c.get("/api/builds", headers=H, params={"status": status, "data_source_id": ds["id"]}))
    builds += page.get("items", []) if isinstance(page, dict) else page
for b in sorted(builds, key=lambda b: b.get("created_at") or ""):
    ok(c.post(f"/api/builds/{b['id']}/publish", headers=H, json={}))
print(f"[publish] builds published: {len(builds)}")

after = [score("after learning") for _ in range(2)]
blob = " ".join((i["text"] or "") + " " + (i["title"] or "") for i in learned).lower()
summary = {
    "model": MODEL, "instructions_learned": len(learned),
    "mentions_data_loss": any(w in blob for w in ("data loss", "deleted", "lost", "wiped", "disappear")),
    "mentions_enterprise_outage": "enterprise" in blob and any(w in blob for w in ("outage", "down", "can't log", "cannot log", "several", "multiple")),
    "mentions_ticket_names": bool(re.search(r"ticket_\d{3}", blob)),
    "baseline": baseline, "after": after, "train_seconds": int(time.time() - t0),
}
OUT.write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
