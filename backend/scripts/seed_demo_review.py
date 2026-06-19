"""Seed a demo instruction with 2 multi-hunk suggestions for screenshotting the
per-hunk tracked-changes review. Direct sqlite (app.db)."""
import sqlite3, uuid, hashlib, datetime, sys

DB = "db/app.db"
ORG = "5f7ff784-b9f6-47b9-967f-6eb70955cc4e"
ADMIN_ROLE = "4fb0a35a-544a-488f-9dad-45c788c59fc1"
REVIEWER = "28f6a22c-b427-45b2-8e5d-2a59d2be8a24"
NOW = datetime.datetime.utcnow().isoformat(sep=" ")


def h(t): return hashlib.sha256((t or "").encode()).hexdigest()
def uid(): return str(uuid.uuid4())


c = sqlite3.connect(DB)
cur = c.cursor()

# 1) membership + role assignment (admin) for reviewer
if not cur.execute("SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?", (REVIEWER, ORG)).fetchone():
    cur.execute("INSERT INTO memberships (id,user_id,organization_id,role,email,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (uid(), REVIEWER, ORG, "admin", "reviewer@example.com", NOW, NOW))
if not cur.execute("SELECT 1 FROM role_assignments WHERE principal_id=? AND role_id=?", (REVIEWER, ADMIN_ROLE)).fetchone():
    cur.execute("INSERT INTO role_assignments (id,organization_id,role_id,principal_type,principal_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (uid(), ORG, ADMIN_ROLE, "user", REVIEWER, NOW, NOW))

# 2) instruction + main v0
v0 = ("Use Sales for digital music store analytics: catalog structure, customer purchases, and invoice revenue.\n\n"
      "Key tables:\n"
      "- @Track: purchasable tracks.\n"
      "- @Album: albums linked to artists.\n"
      "- @Customer: customer contacts.\n\n"
      "Always exclude refunded invoices when computing revenue.")

instr = uid(); v0id = uid()
cur.execute("""INSERT INTO instructions (id,text,title,status,category,kind,organization_id,user_id,load_mode,
              current_version_id,content_hash,thumbs_up,is_seen,can_user_toggle,private_status,global_status,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (instr, v0, "SALES_OVERVIEW (demo)", "published", "general", "instruction", ORG, REVIEWER, "always",
             v0id, h(v0), 0, 1, 1, None, None, NOW, NOW))
cur.execute("""INSERT INTO instruction_versions (id,instruction_id,version_number,text,status,load_mode,content_hash,created_by_user_id,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)""", (v0id, instr, 1, v0, "published", "always", h(v0), REVIEWER, NOW, NOW))


def next_bn():
    return (cur.execute("SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds WHERE organization_id=?", (ORG,)).fetchone()[0])


def mk_build(source, is_main, base, status):
    bid = uid()
    cur.execute("""INSERT INTO instruction_builds (id,build_number,status,source,is_main,organization_id,base_build_id,
                  total_instructions,added_count,modified_count,removed_count,created_by_user_id,created_at,updated_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bid, next_bn(), status, source, 1 if is_main else 0, ORG, base, 1, 0, 0, 0, REVIEWER, NOW, NOW))
    return bid


def add_content(bid, vid):
    cur.execute("INSERT INTO build_contents (id,build_id,instruction_id,instruction_version_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (uid(), bid, instr, vid, NOW, NOW))


def mk_version(n, text):
    vid = uid()
    cur.execute("""INSERT INTO instruction_versions (id,instruction_id,version_number,text,status,load_mode,content_hash,created_by_user_id,created_at,updated_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""", (vid, instr, n, text, "published", "always", h(text), REVIEWER, NOW, NOW))
    return vid


# Attach to the org's EXISTING single main build (must stay exactly one main).
main_b = cur.execute(
    "SELECT id FROM instruction_builds WHERE organization_id=? AND is_main=1 AND deleted_at IS NULL LIMIT 1",
    (ORG,)).fetchone()[0]
add_content(main_b, v0id)

# Suggestion A (user): prepend a guidance line + change "customer purchases" -> "customer orders"
a = ("IMPORTANT: scope all metrics to the last 12 months.\n\n"
     + v0.replace("customer purchases", "customer orders"))
add_content(mk_build("user", False, main_b, "pending_approval"), mk_version(100, a))

# Suggestion B (ai): refine the @Album line + append a time-filtering note
b = (v0.replace("albums linked to artists", "albums, each linked to exactly one artist")
     + "\nUse @Invoice.InvoiceDate (billing month) for sales-time filtering.")
add_content(mk_build("ai", False, main_b, "pending_approval"), mk_version(101, b))

c.commit()
print("seeded instruction:", instr)
print("title: SALES_OVERVIEW (demo) — 2 suggestions (1 user, 1 ai)")
c.close()
