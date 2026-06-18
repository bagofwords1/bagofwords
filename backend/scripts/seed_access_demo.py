"""Seed a realistic access-control + suggestions demo against a LIVE backend.

Creates, idempotently:
  1. Users at different access levels in the admin's org:
       - admin   (the existing sandbox admin)
       - editor  (system role 'member' + per-agent `manage_instructions` grants
                  so they can author suggestions)
       - viewer  (system role 'member', read-only: no private-agent access)
  2. Public vs private agents (chinook.sqlite):
       - 2 public  (is_public=true)  -> visible to everyone incl. the viewer
       - 2 private (is_public=false) -> member_user_ids grants only the editor
  3. Many instructions across scopes (global / per-agent / multi-agent /
     table-scoped). Table-scoped refs use a table NAME (exercises Task A's
     name-based datasource_table fallback). ~20+.
  4. Many pending suggestions: a non-admin (the editor) edits existing
     instructions via PUT /api/instructions/{id}. Because the editor is not an
     org admin, the auto-finalized build stays in `pending_approval`, producing
     a pending build with `pending_text` that the "Suggested changes / Approve"
     UI surfaces. 5-8 of these are created.

Re-runnable: every create is guarded by an existence check (by email / agent
name / instruction title), so running twice is a no-op for already-seeded data.

    python scripts/seed_access_demo.py
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error
import urllib.parse

BASE = os.environ.get("BOW_SEED_BASE", "http://localhost:8000")
ADMIN_EMAIL = "sandbox@bow.dev"
ADMIN_PASSWORD = "Sandbox123!"
CHINOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "demo-datasources", "chinook.sqlite")
)

# Demo users (besides the admin). Passwords are sandbox-only.
EDITOR = {"email": "editor@bow.dev", "password": "Editor123!", "name": "Edie Editor"}
VIEWER = {"email": "viewer@bow.dev", "password": "Viewer123!", "name": "Vic Viewer"}

ADMIN_TOKEN = None
ORG_ID = None


def req(method, path, body=None, form=False, token=None, org=True):
    """Generic JSON/form request helper. Returns (status, parsed_body)."""
    url = BASE + path
    headers = {}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org and ORG_ID:
        headers["X-Organization-Id"] = ORG_ID
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def wait_for_server():
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/settings", timeout=3):
                return True
        except Exception:
            time.sleep(1)
    return False


def login(email, password):
    st, data = req(
        "POST", "/api/auth/jwt/login",
        {"username": email, "password": password}, form=True, org=False,
    )
    if st == 200 and data and data.get("access_token"):
        return data["access_token"]
    return None


def register(email, password, name):
    """Register a user (idempotent). Returns the user id (existing or new)."""
    st, data = req(
        "POST", "/api/auth/register",
        {"email": email, "password": password, "name": name}, org=False,
    )
    if st in (200, 201) and isinstance(data, dict) and data.get("id"):
        return data["id"]
    # Already registered -> log in and pull the id from /users/me-ish via members later.
    tok = login(email, password)
    if tok:
        st2, me = req("GET", "/api/users/me", token=tok, org=False)
        if st2 == 200 and isinstance(me, dict) and me.get("id"):
            return me["id"]
    return None


def setup_admin():
    global ADMIN_TOKEN, ORG_ID
    ADMIN_TOKEN = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    assert ADMIN_TOKEN, "admin login failed"
    st, orgs = req("GET", "/api/organizations", token=ADMIN_TOKEN, org=False)
    assert orgs, f"no orgs: {st} {orgs}"
    ORG_ID = orgs[0]["id"]
    print(f"admin ok -- org {ORG_ID}")


def get_member_user_id_by_email(email):
    """Find an existing membership's user_id by email, via the members list."""
    st, members = req("GET", f"/api/organizations/{ORG_ID}/members", token=ADMIN_TOKEN)
    if st != 200 or not isinstance(members, list):
        return None
    for m in members:
        user = m.get("user") or {}
        if (user.get("email") or m.get("email")) == email:
            return user.get("id")
    return None


def ensure_member(user, role):
    """Register the user and add them to the org with `role` (idempotent).
    Returns the user's id."""
    uid = register(user["email"], user["password"], user["name"])
    # Add to org (assigns the system role via role_assignments).
    st, res = req(
        "POST", f"/api/organizations/{ORG_ID}/members",
        {"organization_id": ORG_ID, "email": user["email"], "role": role},
        token=ADMIN_TOKEN,
    )
    if st in (200, 201):
        print(f"  member '{user['email']}' added as {role}")
        if isinstance(res, dict):
            ruser = res.get("user") or {}
            uid = ruser.get("id") or uid
    elif st == 400 and isinstance(res, dict) and "Already a member" in str(res.get("detail", "")):
        print(f"  member '{user['email']}' already in org ({role})")
    else:
        print(f"  ! member '{user['email']}' add failed: {st} {str(res)[:160]}")
    if not uid:
        uid = get_member_user_id_by_email(user["email"])
    return uid


def list_agents():
    """Map of agent name -> id (admin view, includes private)."""
    st, data = req("GET", "/api/data_sources?show_all=true", token=ADMIN_TOKEN)
    out = {}
    if st == 200 and isinstance(data, list):
        for d in data:
            out[d["name"]] = d["id"]
    return out


def create_agent(name, is_public, member_user_ids=None):
    """Create a chinook sqlite agent (idempotent by name)."""
    existing = list_agents()
    if name in existing:
        print(f"  agent '{name}' exists -> {existing[name]} (public={is_public})")
        return existing[name]
    body = {
        "name": name,
        "type": "sqlite",
        "config": {"database": CHINOOK},
        "credentials": {},
        "is_public": is_public,
        "member_user_ids": member_user_ids or [],
    }
    st, data = req("POST", "/api/data_sources", body, token=ADMIN_TOKEN)
    if st in (200, 201) and isinstance(data, dict) and data.get("id"):
        print(f"  agent '{name}' -> {data['id']} (public={is_public}, members={member_user_ids or []})")
        return data["id"]
    print(f"  ! agent '{name}' failed: {st} {str(data)[:200]}")
    return None


def grant_manage_instructions(agent_id, user_id):
    """Grant `manage_instructions` on an agent to a user.

    Idempotent and self-healing: POST creates the grant; if one already exists
    (409, e.g. an empty-permission grant minted by `member_user_ids`), PUT the
    existing grant to add `manage_instructions`."""
    perms = ["view", "view_schema", "manage_instructions"]
    body = {
        "resource_type": "data_source",
        "resource_id": agent_id,
        "principal_type": "user",
        "principal_id": user_id,
        "permissions": perms,
    }
    st, res = req("POST", f"/api/organizations/{ORG_ID}/resource-grants", body, token=ADMIN_TOKEN)
    if st in (200, 201):
        return True
    # Grant already exists — find it and PUT the permissions.
    st_l, grants = req(
        "GET",
        f"/api/organizations/{ORG_ID}/resource-grants"
        f"?resource_type=data_source&resource_id={agent_id}&principal_type=user&principal_id={user_id}",
        token=ADMIN_TOKEN,
    )
    if st_l == 200 and isinstance(grants, list) and grants:
        gid = grants[0]["id"]
        st_u, _ = req(
            "PUT", f"/api/organizations/{ORG_ID}/resource-grants/{gid}",
            {"permissions": perms}, token=ADMIN_TOKEN,
        )
        if st_u == 200:
            return True
    print(f"  ! grant manage_instructions on {agent_id} to {user_id} failed: {st} {str(res)[:160]}")
    return False


def existing_instruction_titles():
    titles = {}
    st, data = req(
        "GET",
        "/api/instructions?limit=200&include_drafts=true&include_archived=true&include_hidden=true",
        token=ADMIN_TOKEN,
    )
    items = data.get("items", []) if isinstance(data, dict) else []
    for it in items:
        if it.get("title"):
            titles[it["title"]] = it["id"]
    return titles


def create_instruction(title, text, category, load_mode, status, source_type, ds_ids, references=None, existing=None):
    """Create a global instruction (idempotent by title). Returns id."""
    if existing and title in existing:
        print(f"  instr '{title}' exists -> {existing[title]}")
        return existing[title]
    body = {
        "text": text, "title": title, "category": category, "load_mode": load_mode,
        "status": status, "source_type": source_type, "data_source_ids": ds_ids or [],
    }
    if references:
        body["references"] = references
    st, data = req("POST", "/api/instructions/global", body, token=ADMIN_TOKEN)
    if st in (200, 201) and isinstance(data, dict) and data.get("id"):
        if existing is not None:
            existing[title] = data["id"]
        return data["id"]
    print(f"  ! instr '{title}' failed: {st} {str(data)[:200]}")
    return None


def has_pending_build(instruction_id, token):
    st, data = req("GET", f"/api/instructions/{instruction_id}/pending-builds", token=token)
    return st == 200 and isinstance(data, list) and len(data) > 0


def pending_suggestion_titles():
    """Titles of instructions that already sit in a pending build.

    Editor-authored suggestion instructions never reach the main build, so they
    don't appear in the instruction list. We instead scan every pending build's
    contents for the suggestion-prefixed titles. Used to make editor suggestion
    seeding idempotent across re-runs."""
    titles = set()
    st, data = req("GET", "/api/builds?status=pending_approval&limit=200", token=ADMIN_TOKEN)
    items = data.get("items", []) if isinstance(data, dict) else []
    for b in items:
        st_c, contents = req("GET", f"/api/builds/{b['id']}/contents", token=ADMIN_TOKEN)
        for c in (contents.get("items", []) if isinstance(contents, dict) else []):
            t = c.get("title") or ""
            if t.startswith("ACL: [suggestion]"):
                titles.add(t)
    return titles


def editor_owned_suggestion(title, text, agent_id, editor_token, existing, already_pending):
    """Create a pending suggestion authored by the editor (a non-admin).

    A non-admin creating an instruction routes through the build auto-finalizer,
    which leaves the build in `pending_approval` (admin must review). The result
    is an editor-authored pending build that the "Suggested changes" UI shows.
    Idempotent: skip if a pending build already exists with this title.
    """
    if title in already_pending:
        print(f"  editor suggestion '{title}' already pending -- skip")
        return True
    if title in existing:
        iid = existing[title]
        ok = has_pending_build(iid, ADMIN_TOKEN)
        print(f"  editor suggestion '{title}' exists -> {iid} (pending={ok})")
        return ok
    body = {
        "text": text, "title": title, "category": "general", "load_mode": "always",
        "status": "published", "source_type": "user", "data_source_ids": [agent_id],
    }
    st, data = req("POST", "/api/instructions", body, token=editor_token)
    if st not in (200, 201) or not isinstance(data, dict) or not data.get("id"):
        print(f"  ! editor suggestion '{title}' failed: {st} {str(data)[:160]}")
        return False
    iid = data["id"]
    existing[title] = iid
    ok = has_pending_build(iid, ADMIN_TOKEN)
    print(f"  editor suggestion '{title}' -> {iid} (pending={ok}, author=editor)")
    return ok


def admin_build_suggestion(instruction_id, new_text):
    """Create a pending suggestion authored by the admin via the build API
    (the flow the task describes): create draft build -> PUT a modified version
    into it (target_build_id, no auto-finalize) -> submit -> pending_approval.
    Idempotent: skip if the instruction already has a pending build."""
    if has_pending_build(instruction_id, ADMIN_TOKEN):
        print(f"  admin suggestion on {instruction_id} already pending")
        return True
    st, build = req("POST", "/api/builds", {"source": "user"}, token=ADMIN_TOKEN)
    if st not in (200, 201) or not isinstance(build, dict) or not build.get("id"):
        print(f"  ! admin suggestion: create build failed: {st} {str(build)[:160]}")
        return False
    bid = build["id"]
    st, res = req(
        "PUT", f"/api/instructions/{instruction_id}",
        {"text": new_text, "target_build_id": bid}, token=ADMIN_TOKEN,
    )
    if st not in (200, 201):
        print(f"  ! admin suggestion: PUT failed: {st} {str(res)[:160]}")
        return False
    st, res = req("POST", f"/api/builds/{bid}/submit", token=ADMIN_TOKEN)
    if st not in (200, 201):
        print(f"  ! admin suggestion: submit failed: {st} {str(res)[:160]}")
        return False
    ok = has_pending_build(instruction_id, ADMIN_TOKEN)
    print(f"  admin suggestion on {instruction_id} -> pending={ok} (build {bid[:8]})")
    return ok


def main():
    if not wait_for_server():
        print("server never came up"); sys.exit(1)
    setup_admin()

    # ---- 1. Users -------------------------------------------------------
    print("ensuring users...")
    admin_uid = get_member_user_id_by_email(ADMIN_EMAIL)
    editor_uid = ensure_member(EDITOR, "member")
    viewer_uid = ensure_member(VIEWER, "member")
    print(f"  admin uid={admin_uid} editor uid={editor_uid} viewer uid={viewer_uid}")

    editor_token = login(EDITOR["email"], EDITOR["password"])
    viewer_token = login(VIEWER["email"], VIEWER["password"])

    # ---- 2. Public vs private agents -----------------------------------
    print("ensuring agents...")
    agents = {}
    # Public agents — visible to everyone (incl. viewer).
    agents["Public Sales"] = create_agent("Public Sales", is_public=True)
    agents["Public Catalog"] = create_agent("Public Catalog", is_public=True)
    # Private agents — only the editor is granted access via member_user_ids.
    priv_members = [u for u in [editor_uid] if u]
    agents["Private Finance"] = create_agent("Private Finance", is_public=False, member_user_ids=priv_members)
    agents["Private HR"] = create_agent("Private HR", is_public=False, member_user_ids=priv_members)
    agents = {k: v for k, v in agents.items() if v}

    # Grant the editor manage_instructions on every demo agent so they can
    # author suggestions (member_user_ids only grants view/view_schema).
    if editor_uid:
        for aid in agents.values():
            grant_manage_instructions(aid, editor_uid)

    A = lambda *names: [agents[n] for n in names if n in agents]

    # ---- 3. Many instructions across scopes ----------------------------
    print("ensuring instructions...")
    existing = existing_instruction_titles()
    # Table-scoped refs use a NAME (object_id) — exercises Task A fallback.
    def tref(name):
        return [{"object_type": "datasource_table", "object_id": name, "relation_type": "scope"}]

    rows = [
        # (title, text, category, load_mode, status, source_type, ds_ids, references)
        # --- Global / org-wide ---
        ("ACL: Net revenue", "Revenue is net of refunds and excludes test accounts.", "data_modeling", "always", "published", "user", [], None),
        ("ACL: Currency format", "Format currency as USD, thousands separators, no decimals over $1,000.", "visualizations", "always", "published", "user", [], None),
        ("ACL: Read-only guardrail", "Never run UPDATE/DELETE/INSERT/DDL. Read-only analytics only.", "system", "always", "published", "user", [], None),
        ("ACL: Fiscal quarter", "'Last quarter' means the most recent completed fiscal quarter.", "general", "intelligent", "published", "ai", [], None),
        ("ACL: Active customer", "Active customer = >=1 purchase in the trailing 30 days.", "data_modeling", "always", "draft", "user", [], None),
        ("ACL: Charting conventions", "Bar charts for categories, line charts for time series.", "visualizations", "intelligent", "published", "ai", [], None),
        # --- Per-agent (public) ---
        ("ACL: Public Sales MRR", "MRR = sum of invoice totals grouped by billing month.", "data_modeling", "intelligent", "published", "user", A("Public Sales"), None),
        ("ACL: Public Sales pipeline", "Pipeline coverage = open pipeline / quota; flag <3x.", "general", "intelligent", "published", "user", A("Public Sales"), None),
        ("ACL: Public Catalog genres", "Group tracks by Genre; treat null genre as 'Unclassified'.", "data_modeling", "intelligent", "published", "user", A("Public Catalog"), None),
        ("ACL: Public Catalog adoption", "Catalog adoption = distinct purchased tracks / total tracks.", "general", "intelligent", "published", "ai", A("Public Catalog"), None),
        # --- Per-agent (private) ---
        ("ACL: Private Finance accrual", "Use accrual accounting: recognize revenue when earned.", "general", "always", "published", "git", A("Private Finance"), None),
        ("ACL: Private Finance margin", "Gross margin = (revenue - COGS) / revenue.", "data_modeling", "intelligent", "published", "user", A("Private Finance"), None),
        ("ACL: Private HR headcount", "Headcount excludes contractors and interns.", "data_modeling", "always", "published", "user", A("Private HR"), None),
        ("ACL: Private HR tenure", "Tenure is measured from the employee HireDate.", "general", "intelligent", "draft", "user", A("Private HR"), None),
        # --- Multi-agent reach ---
        ("ACL: Fiscal calendar", "Fiscal year starts Feb 1; align YoY to fiscal periods.", "data_modeling", "always", "published", "git", A("Public Sales", "Private Finance"), None),
        ("ACL: Region rollups", "EMEA includes the UK; APAC includes India.", "data_modeling", "intelligent", "published", "user", A("Public Sales", "Public Catalog"), None),
        ("ACL: Business-day averaging", "Exclude weekends from daily-average B2B metrics.", "general", "intelligent", "draft", "ai", A("Public Catalog", "Private HR"), None),
        # --- Table-scoped (NAME-based refs -> exercises Task A) ---
        ("ACL: Album table", "The Album table is the album catalog; AlbumId is the PK.", "data_modeling", "always", "published", "user", A("Public Catalog"), tref("Album")),
        ("ACL: Customer table", "The Customer table holds billing contacts; one row per customer.", "data_modeling", "always", "published", "user", A("Public Sales"), tref("Customer")),
        ("ACL: Invoice table", "The Invoice table is the grain for revenue; Total is gross.", "data_modeling", "intelligent", "published", "user", A("Public Sales"), tref("Invoice")),
        ("ACL: Employee table", "The Employee table is the HR roster; ReportsTo is the manager FK.", "data_modeling", "always", "published", "user", A("Private HR"), tref("Employee")),
        ("ACL: Track table", "The Track table lists songs; UnitPrice is the list price.", "data_modeling", "intelligent", "published", "user", A("Public Catalog"), tref("Track")),
    ]
    instr_ids = {}
    n_ok = 0
    for r in rows:
        iid = create_instruction(*r, existing=existing)
        if iid:
            instr_ids[r[0]] = iid
            n_ok += 1

    # ---- 4. Many pending suggestions -----------------------------------
    # Two authorship paths so the review UI shows different authors:
    #   (a) editor-authored: a non-admin creating an instruction auto-finalizes
    #       to `pending_approval` (the editor must hold manage_instructions on
    #       the target agent — granted above).
    #   (b) admin-authored: the build API flow the task describes
    #       (create draft build -> PUT modified version -> submit).
    print("ensuring pending suggestions...")
    n_pending = 0
    pending_instruction_ids = []

    # (a) Editor-authored suggestions on agents the editor manages.
    editor_suggestions = [
        ("ACL: [suggestion] Sales VP approval", "Deals over $25k require VP approval before close.", "Public Sales"),
        ("ACL: [suggestion] Sales lead SLA", "Inbound leads must get first touch within 24 business hours.", "Public Sales"),
        ("ACL: [suggestion] Catalog explicit flag", "Flag explicit tracks; exclude them from family playlists.", "Public Catalog"),
        ("ACL: [suggestion] Catalog price tiers", "Group tracks into price tiers: <$0.99, $0.99, >$0.99.", "Public Catalog"),
        ("ACL: [suggestion] Finance close calendar", "Books close on the 5th business day of the following month.", "Private Finance"),
    ]
    already_pending = pending_suggestion_titles()
    if editor_token:
        for title, text, agent_name in editor_suggestions:
            aid = agents.get(agent_name)
            if not aid:
                continue
            if editor_owned_suggestion(title, text, aid, editor_token, existing, already_pending):
                n_pending += 1
                pending_instruction_ids.append(existing.get(title))
    else:
        print("  (editor login unavailable — skipping editor-authored suggestions)")

    # (b) Admin-authored suggestions via the build API on existing admin-owned
    #     per-agent instructions.
    admin_suggestion_targets = [
        ("ACL: Public Sales MRR", "MRR = sum of invoice totals grouped by billing month, EXCLUDING refunded invoices."),
        ("ACL: Public Catalog genres", "Group tracks by Genre; treat null genre as 'Unclassified' and merge 'Misc' into it."),
        ("ACL: Album table", "The Album table is the album catalog; AlbumId is the PK and joins Track.AlbumId."),
    ]
    for title, new_text in admin_suggestion_targets:
        iid = instr_ids.get(title) or existing.get(title)
        if not iid:
            continue
        if admin_build_suggestion(iid, new_text):
            n_pending += 1
            pending_instruction_ids.append(iid)

    # ---- Verification snapshot -----------------------------------------
    admin_agents = list_agents()
    viewer_agent_count = None
    if viewer_token:
        st, vdata = req("GET", "/api/data_sources", token=viewer_token)
        if st == 200 and isinstance(vdata, list):
            viewer_agent_count = len(vdata)

    # ---- Summary --------------------------------------------------------
    print("\n================ SEED SUMMARY ================")
    print("Users (email / password / role):")
    print(f"  admin  : {ADMIN_EMAIL} / {ADMIN_PASSWORD} / admin")
    print(f"  editor : {EDITOR['email']} / {EDITOR['password']} / member (+manage_instructions on demo agents)")
    print(f"  viewer : {VIEWER['email']} / {VIEWER['password']} / member (read-only, public agents only)")
    print("Agents:")
    for name in ["Public Sales", "Public Catalog", "Private Finance", "Private HR"]:
        if name in agents:
            vis = "public" if name.startswith("Public") else "private"
            print(f"  {name:16s} {agents[name]}  ({vis})")
    print(f"Instructions created/ensured : {n_ok}/{len(rows)}")
    print(f"Pending suggestions          : {n_pending} (editor- and admin-authored)")
    distinct_pending = [p for p in dict.fromkeys(pending_instruction_ids) if p]
    print(f"  instructions with a pending build: {len(distinct_pending)}")
    print(f"Agents visible to ADMIN      : {len(admin_agents)} (incl. private)")
    if viewer_agent_count is not None:
        print(f"Agents visible to VIEWER     : {viewer_agent_count} (public only)")
    print("==============================================")


if __name__ == "__main__":
    main()
