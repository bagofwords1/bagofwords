"""Seed bulk agent-execution data for the /monitoring/diagnosis perf loop.

Inserts N agent executions for the (single) existing organization, spread
uniformly over the last 30 days, with the surrounding rows the diagnosis
endpoints join against:

- 1 report per 8 executions (title, org, owner = existing admin)
- per execution: a user completion (prompt) + a system completion (parent_id
  -> user completion); AgentExecution.completion_id -> system completion
- 3 tool executions per agent execution (create_data / create_artifact /
  inspect_data), ~5% failed; a failed create_data marks the run "failed query"
- ~7% of executions have status='error'
- ~10% of system completions get a CompletionFeedback (1/3 negative)

Usage:  uv run python scripts/seed_diagnosis_perf.py 25000
Re-run to add more rows on top (cumulative), mirroring real growth.
Raw sqlite3 bulk inserts — seeding 100k executions takes seconds.
"""
import json
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
DB = sys.argv[2] if len(sys.argv) > 2 else "db/app.db"
DAYS = 30

random.seed(42 + N)  # deterministic per tier

conn = sqlite3.connect(DB)
cur = conn.cursor()

org_id = cur.execute("SELECT id FROM organizations LIMIT 1").fetchone()[0]
admin_id = cur.execute("SELECT id FROM users LIMIT 1").fetchone()[0]

now = datetime.utcnow()


def ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


# ---- users (20 fake members, created once) ----
existing = cur.execute(
    "SELECT id FROM users WHERE email LIKE 'perfuser%@bow.dev'"
).fetchall()
if existing:
    user_ids = [r[0] for r in existing]
else:
    user_ids = []
    rows = []
    for i in range(20):
        uid = str(uuid.uuid4())
        user_ids.append(uid)
        rows.append((uid, f"Perf User {i}",
                     f"perfuser{i}@bow.dev", "x", 1, 0, 1, 0))
    cur.executemany(
        "INSERT INTO users (id, name, email, hashed_password,"
        " is_active, is_superuser, is_verified, is_service_account)"
        " VALUES (?,?,?,?,?,?,?,?)", rows)

# ---- reports ----
n_reports = max(1, N // 8)
report_rows, report_ids = [], []
seed_tag = uuid.uuid4().hex[:8]
for i in range(n_reports):
    rid = str(uuid.uuid4())
    report_ids.append(rid)
    created = now - timedelta(seconds=random.randint(0, DAYS * 86400))
    report_rows.append((rid, ts(created), ts(created), f"Perf report {i}",
                        f"perf-{seed_tag}-{i}", "published", "regular", "chat",
                        "none", "none", 0, random.choice(user_ids), org_id))
cur.executemany(
    "INSERT INTO reports (id, created_at, updated_at, title, slug, status, report_type,"
    " mode, artifact_visibility, conversation_visibility, conversation_share_enabled,"
    " user_id, organization_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", report_rows)

# ---- completions / agent_executions / tool_executions / feedback ----
TOOLS = ["create_data", "create_artifact", "inspect_data"]
PROMPTS = [
    "Show me revenue by hotel for last month",
    "Compare occupancy rates across regions",
    "Pickup RN drop alert for the coming holiday",
    "Daily bookings trend by channel",
    "Top 10 customers by total spend",
]

completion_rows, ae_rows, te_rows, fb_rows = [], [], [], []
for i in range(N):
    created = now - timedelta(seconds=random.randint(0, DAYS * 86400))
    report_id = random.choice(report_ids)
    user_id = random.choice(user_ids)

    user_cid = str(uuid.uuid4())
    sys_cid = str(uuid.uuid4())
    prompt = json.dumps({"content": random.choice(PROMPTS)})
    completion_rows.append((user_cid, ts(created), ts(created), prompt, json.dumps(""),
                            "success", "gpt4o", 0, 0, "user_message", "user",
                            report_id, user_id, "table", None,
                            random.randint(1, 5), random.randint(1, 5), random.randint(1, 5)))
    completion_rows.append((sys_cid, ts(created), ts(created), json.dumps(""), json.dumps("done"),
                            "success", "gpt4o", 0, 0, "ai_completion", "system",
                            report_id, None, "table", user_cid, None, None, None))

    ae_id = str(uuid.uuid4())
    is_error = random.random() < 0.07
    error_json = json.dumps({"message": "boom: tool crashed"}) if is_error else None
    ae_rows.append((ae_id, ts(created), ts(created), sys_cid, org_id, user_id, report_id,
                    "error" if is_error else "completed", 0, 0, error_json))

    for t in range(3):
        tool = TOOLS[t]
        failed = random.random() < 0.05
        te_rows.append((str(uuid.uuid4()), ts(created), ts(created), ae_id, tool,
                        json.dumps({}), "error" if failed else "success",
                        0 if failed else 1, 1, 0))

    r = random.random()
    if r < 0.10:
        direction = -1 if r < 0.033 else 1
        fb_rows.append((str(uuid.uuid4()), ts(created), ts(created), user_id, sys_cid,
                        org_id, direction, "not what I asked" if direction < 0 else None))

cur.executemany(
    "INSERT INTO completions (id, created_at, updated_at, prompt, completion, status,"
    " model, turn_index, feedback_score, message_type, role, report_id, user_id,"
    " main_router, parent_id, instructions_effectiveness, context_effectiveness,"
    " response_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", completion_rows)
cur.executemany(
    "INSERT INTO agent_executions (id, created_at, updated_at, completion_id,"
    " organization_id, user_id, report_id, status, latest_seq, is_eval_run, error_json)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?)", ae_rows)
cur.executemany(
    "INSERT INTO tool_executions (id, created_at, updated_at, agent_execution_id,"
    " tool_name, arguments_json, status, success, attempt_number, max_retries)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)", te_rows)
cur.executemany(
    "INSERT INTO completion_feedbacks (id, created_at, updated_at, user_id,"
    " completion_id, organization_id, direction, message) VALUES (?,?,?,?,?,?,?,?)",
    fb_rows)

conn.commit()
total = cur.execute("SELECT count(*) FROM agent_executions").fetchone()[0]
print(f"seeded +{N} agent executions (org total now {total}); "
      f"+{len(completion_rows)} completions, +{len(te_rows)} tool executions, "
      f"+{len(fb_rows)} feedbacks")
