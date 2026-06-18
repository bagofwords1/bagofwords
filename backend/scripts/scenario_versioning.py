"""Drive the complex instruction-versioning / suggestion-approval scenarios
against a LIVE backend and assert the logic behaves.

Builds one instruction through a deliberately messy timeline so we can verify
the system handles concurrent suggestions across a moving main version:

  1. admin creates an instruction                          -> v1 (live)
  2. admin updates it                                       -> v2 (live)
  3. member (editor) suggests a change      -> pending build A (S_A)
  4. admin updates the instruction again, NOT accepting S_A -> v3 (live), S_A still pending
  5. member suggests another change         -> pending build B (S_B); 2 pending now
  6. admin ACCEPTS S_B in full                              -> v4 (live), S_A still pending
  7. admin accepts ONE hunk of S_A (subset)                 -> v5 (live), S_A shrinks/stays
  8. admin rejects the remainder of S_A                     -> S_A fully resolved, 0 pending

Run AFTER seed_access_demo.py (reuses admin + editor + the Public Sales agent).

    python scripts/scenario_versioning.py
"""
import os, sys, json, time, urllib.request, urllib.error, urllib.parse

BASE = os.environ.get("BOW_SEED_BASE", "http://localhost:8000")
ADMIN = ("sandbox@bow.dev", "Sandbox123!")
EDITOR = ("editor@bow.dev", "Editor123!")
ORG_ID = None

def req(method, path, body=None, form=False, token=None):
    url = BASE + path
    headers, data = {}, None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode(); headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    if token: headers["Authorization"] = f"Bearer {token}"
    if ORG_ID: headers["X-Organization-Id"] = ORG_ID
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode(); return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, raw

def login(email, password):
    st, d = req("POST", "/api/auth/jwt/login", {"username": email, "password": password}, form=True)
    return d["access_token"] if st == 200 and d else None

PASS, FAIL = 0, 0
def check(label, cond, extra=""):
    global PASS, FAIL
    ok = bool(cond)
    PASS += ok; FAIL += (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    return ok

def live_text(iid, token):
    st, d = req("GET", f"/api/instructions/{iid}", token=token)
    return (d or {}).get("text") if st == 200 else None

def versions(iid, token):
    st, d = req("GET", f"/api/instructions/{iid}/versions?limit=200", token=token)
    items = (d or {}).get("items", []) if isinstance(d, dict) else []
    return items

def pending(iid, token):
    st, d = req("GET", f"/api/instructions/{iid}/pending-builds", token=token)
    return d if (st == 200 and isinstance(d, list)) else []

def review_items(token):
    st, d = req("GET", "/api/review?types=instruction_suggestion&limit=200", token=token)
    return (d or {}).get("items", []) if isinstance(d, dict) else []

def snapshot(label, iid, token):
    vs, pb = versions(iid, token), pending(iid, token)
    lt = live_text(iid, token)
    print(f"\n--- {label} ---")
    print(f"    live text : {lt!r}")
    print(f"    versions  : {len(vs)} (v#: {[v.get('version_number') for v in vs]})")
    print(f"    pending   : {len(pb)} suggestion(s)")
    for p in pb:
        by = (p.get('created_by') or {}).get('name') or p.get('source')
        print(f"        - build {p['build_id'][:8]} v{p.get('pending_version_number')} by {by}: {p.get('pending_text')!r}")
    return lt, vs, pb

def member_suggest(iid, new_text, etoken):
    """Member edit -> creates a fresh pending build (build stays pending_approval)."""
    st, d = req("PUT", f"/api/instructions/{iid}", {"text": new_text}, token=etoken)
    return st in (200, 201)

def admin_direct_update(iid, new_text, atoken):
    """Admin edit -> auto-approved + promoted to main (goes live immediately)."""
    st, d = req("PUT", f"/api/instructions/{iid}", {"text": new_text}, token=atoken)
    return st in (200, 201)

def admin_resolve(iid, build_id, promote_text, remaining_text, atoken):
    st, d = req("POST", f"/api/instructions/{iid}/resolve",
                {"build_id": build_id, "promote_text": promote_text, "remaining_text": remaining_text}, token=atoken)
    return st in (200, 201), d

def main():
    global ORG_ID
    atoken = login(*ADMIN); etoken = login(*EDITOR)
    assert atoken and etoken, "login failed"
    st, orgs = req("GET", "/api/organizations", token=atoken)
    ORG_ID = orgs[0]["id"]
    # Public Sales agent id
    st, dss = req("GET", "/api/data_sources?show_all=true", token=atoken)
    agent = next((d["id"] for d in dss if d["name"] == "Public Sales"), None)
    assert agent, "Public Sales agent missing — run seed_access_demo.py first"
    print(f"org={ORG_ID} agent(Public Sales)={agent}")

    TITLE = "SCN: Churn definition"
    # Clean any prior run: find + delete existing instruction with this title.
    st, lst = req("GET", "/api/instructions?limit=200&include_drafts=true&include_archived=true&include_hidden=true", token=atoken)
    for it in (lst or {}).get("items", []):
        if it.get("title") == TITLE:
            req("DELETE", f"/api/instructions/{it['id']}", token=atoken)
            print(f"  (cleaned prior instruction {it['id'][:8]})")

    # ---- Scenario 1: admin creates -> v1 ----
    print("\n=== 1. admin creates instruction ===")
    V1 = "Churn = customers with no purchase in the trailing 90 days."
    st, d = req("POST", "/api/instructions/global",
                {"text": V1, "title": TITLE, "category": "data_modeling", "load_mode": "always",
                 "status": "published", "source_type": "user", "data_source_ids": [agent]}, token=atoken)
    check("instruction created", st in (200, 201) and isinstance(d, dict) and d.get("id"), f"status={st}")
    iid = d["id"]
    lt, vs, pb = snapshot("after create", iid, atoken)
    check("v1 live text correct", lt == V1)
    check("exactly 1 version", len(vs) == 1)
    check("no pending suggestions", len(pb) == 0)

    # ---- Scenario 2: admin updates -> v2 ----
    print("\n=== 2. admin updates the instruction ===")
    V2 = "Churn = customers with no purchase in the trailing 90 days (exclude trial accounts)."
    check("admin update ok", admin_direct_update(iid, V2, atoken))
    lt, vs, pb = snapshot("after admin update", iid, atoken)
    check("v2 live text correct", lt == V2)
    check("2 versions", len(vs) == 2)
    check("still no pending (admin edits go live)", len(pb) == 0)

    # ---- Scenario 3: member suggests change S_A ----
    print("\n=== 3. member (editor) suggests a change (S_A) ===")
    S_A = "Churn = customers with no purchase in the trailing 60 days (exclude trial accounts)."
    check("member suggest ok", member_suggest(iid, S_A, etoken))
    lt, vs, pb = snapshot("after member suggestion A", iid, atoken)
    check("live text UNCHANGED (suggestion not applied)", lt == V2)
    check("1 pending suggestion (S_A)", len(pb) == 1)
    pb_A = pb[0]["build_id"] if pb else None
    check("S_A pending_text is the member's text", pb and pb[0]["pending_text"] == S_A)
    ri = review_items(atoken)
    check("review feed has a suggestion item", any(i for i in ri), f"{len(ri)} items")

    # ---- Scenario 4: admin updates again, NOT accepting S_A -> v3 ----
    print("\n=== 4. admin updates again (does NOT accept S_A) -> v3 ===")
    V3 = "Churn = customers with no purchase in the trailing 90 days (exclude trial and internal accounts)."
    check("admin update ok", admin_direct_update(iid, V3, atoken))
    lt, vs, pb = snapshot("after admin v3", iid, atoken)
    check("v3 is live", lt == V3)
    check("version grew after admin v3 (member A + admin both versioned)", len(vs) >= 4, f"got {len(vs)}")
    check("S_A STILL pending against moved main", any(p["build_id"] == pb_A for p in pb), f"pending builds={[p['build_id'][:8] for p in pb]}")

    # ---- Scenario 5: member suggests another change S_B ----
    print("\n=== 5. member suggests another change (S_B) -> 2 concurrent pending ===")
    S_B = "Churn = customers with no purchase in the trailing 90 days (exclude trial and internal accounts). Report monthly."
    check("member suggest B ok", member_suggest(iid, S_B, etoken))
    lt, vs, pb = snapshot("after member suggestion B", iid, atoken)
    check("live text still v3", lt == V3)
    check("2 concurrent pending suggestions", len(pb) == 2, f"got {len(pb)}")
    pb_B = next((p["build_id"] for p in pb if p["pending_text"] == S_B), None)
    check("S_B present as distinct pending build", pb_B is not None and pb_B != pb_A)
    check("S_A also still present", any(p["build_id"] == pb_A for p in pb))

    # ---- Scenario 6: admin accepts S_B in full ----
    print("\n=== 6. admin ACCEPTS S_B in full ===")
    ok, _ = admin_resolve(iid, pb_B, promote_text=S_B, remaining_text=S_B, atoken=atoken)
    check("resolve(accept S_B) ok", ok)
    lt, vs, pb = snapshot("after accepting S_B", iid, atoken)
    check("live text now == S_B", lt == S_B)
    check("version grew after accepting S_B (build-of-one promoted)", len(vs) >= 5, f"got {len(vs)}")
    check("S_B no longer pending", not any(p["build_id"] == pb_B for p in pb))
    check("S_A STILL pending (untouched by accepting B)", any(p["build_id"] == pb_A for p in pb))

    # ---- Scenario 7: admin accepts a SUBSET (one hunk) of S_A ----
    # S_A proposed "60 days" + dropped "internal accounts" vs its base; live has moved.
    # We accept only the "60 days" change: promote a text that takes 60 from S_A but
    # keeps the rest of live. remaining = live (nothing else pending after).
    print("\n=== 7. admin accepts a SUBSET of S_A (the '60 days' hunk only) ===")
    # Build a promote_text = live S_B text but with 90 -> 60 (the one hunk we accept).
    promote_partial = lt.replace("trailing 90 days", "trailing 60 days")
    # remaining_text: what's still proposed after — here nothing further, so == promote.
    ok, _ = admin_resolve(iid, pb_A, promote_text=promote_partial, remaining_text=promote_partial, atoken=atoken)
    check("resolve(accept subset of S_A) ok", ok)
    lt2, vs2, pb2 = snapshot("after accepting subset of S_A", iid, atoken)
    check("live text took the '60 days' hunk", "trailing 60 days" in (lt2 or ""))
    check("live text kept S_B's 'Report monthly.'", "Report monthly." in (lt2 or ""))
    check("version grew after accepting subset of S_A", len(vs2) > len(vs), f"got {len(vs2)}")
    check("S_A resolved (0 pending remain)", len(pb2) == 0, f"pending={len(pb2)}")

    # ---- Final: review feed should be clear for this instruction ----
    print("\n=== Final: review feed reconciliation ===")
    ri = review_items(atoken)
    related = [i for i in ri if (i.get("subject") or {}).get("instruction_id") == iid]
    check("no lingering review items for this instruction", len(related) == 0, f"{len(related)} items")

    print(f"\n================ SCENARIO RESULT: {PASS} passed / {FAIL} failed ================")
    print(f"instruction_id = {iid}")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
