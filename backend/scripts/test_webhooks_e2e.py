#!/usr/bin/env python3
"""Comprehensive end-to-end checks for the inbound webhook feature.

Runs against a live sandbox (backend on :8000). Exercises every auth mode,
dedup, signature rejection, the AI classifier (act + decline), rate limiting,
the org master switch, and the per-org cap. Prints a PASS/FAIL summary.

Usage:  python scripts/test_webhooks_e2e.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

import requests

BASE = "http://localhost:8000"
DB = os.path.join(os.path.dirname(__file__), "..", "db", "app.db")
HERE = os.path.dirname(__file__)

results = []


def ok(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def login():
    r = requests.post(f"{BASE}/api/auth/jwt/login",
                      data={"username": "sandbox@bow.dev", "password": "Sandbox123!"})
    tok = r.json()["access_token"]
    org = requests.get(f"{BASE}/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    state = json.load(open(os.path.join(HERE, "..", "sandbox_state.json")))["session"]
    return tok, org, state["report_id"], state["ds_id"]


def H(tok, org):
    return {"Authorization": f"Bearer {tok}", "X-Organization-Id": org, "Content-Type": "application/json"}


def create_webhook(tok, org, report, **body):
    r = requests.post(f"{BASE}/api/reports/{report}/webhooks", headers=H(tok, org), json=body)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


def fire(url, secret, **kw):
    cmd = ["python", os.path.join(HERE, "mock_webhook.py"), "--url", url, "--secret", secret]
    for k, v in kw.items():
        if v is True:
            cmd.append(f"--{k.replace('_','-')}")
        elif v is not None:
            cmd += [f"--{k.replace('_','-')}", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    status = out.split()[0] if out else "0"
    return int(status), out


def db():
    return sqlite3.connect(os.path.abspath(DB))


def count(report, role=None, webhook_id=None):
    q = "select count(*) from completions where report_id=?"
    args = [report]
    if role:
        q += " and role=?"; args.append(role)
    if webhook_id:
        q += " and webhook_id=?"; args.append(webhook_id)
    c = db(); n = c.execute(q, args).fetchone()[0]; c.close(); return n


def latest_decision(webhook_id):
    c = db()
    row = c.execute(
        "select json_extract(completion,'$.decision') from completions "
        "where webhook_id=? and role='external' order by turn_index desc limit 1", (webhook_id,)
    ).fetchone()
    c.close()
    return json.loads(row[0]) if row and row[0] else None


def wait_for(fn, timeout=15, interval=1):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return False


def main():
    tok, org, report, ds = login()
    print(f"report={report}\n")

    # ---------- A. Generic HMAC, alert-only (mechanical checks) ----------
    sc, wh = create_webhook(tok, org, report, name="alert", source="generic",
                            auth_mode="hmac", classify_enabled=False)
    ok("A0 create alert-only webhook", sc == 200 and wh.get("secret"), f"status={sc}")
    url, sec, wid = wh["delivery_url"], wh["secret"], wh["id"]

    base = count(report, webhook_id=wid)
    s, _ = fire(url, sec, source="generic", auth_mode="hmac", action="ping.test", delivery="A-001")
    ok("A1 valid HMAC accepted (200)", s == 200, f"status={s}")
    ok("A1 event entry created", wait_for(lambda: count(report, role="external", webhook_id=wid) == base + 1),
       f"count={count(report, role='external', webhook_id=wid)}")

    n_before = count(report, webhook_id=wid)
    s, _ = fire(url, sec, source="generic", auth_mode="hmac", action="ping.test", delivery="A-001")
    time.sleep(3)
    ok("A2 duplicate delivery is a no-op", count(report, webhook_id=wid) == n_before,
       f"before={n_before} after={count(report, webhook_id=wid)}")

    n_before = count(report, webhook_id=wid)
    s, _ = fire(url, sec, source="generic", auth_mode="hmac", action="x", delivery="A-002", tamper=True)
    ok("A3 tampered body rejected (401)", s == 401, f"status={s}")
    time.sleep(2)
    ok("A3 no entry on bad signature", count(report, webhook_id=wid) == n_before)

    s, _ = fire(url, "whsec_WRONGSECRET", source="generic", auth_mode="hmac", delivery="A-003")
    ok("A4 wrong secret rejected (401)", s == 401, f"status={s}")

    # ---------- B. Token mode (Jira Cloud / legacy) ----------
    sc, wh = create_webhook(tok, org, report, name="token", source="jira",
                            auth_mode="token", classify_enabled=False)
    url, sec, wid = wh["delivery_url"], wh["secret"], wh["id"]
    s, _ = fire(url, sec, source="jira", auth_mode="token", delivery="B-001")
    ok("B1 token-mode accepted (200)", s == 200, f"status={s}")
    ok("B1 event entry created", wait_for(lambda: count(report, role="external", webhook_id=wid) >= 1))
    s, _ = fire(url, "whsec_WRONG", source="jira", auth_mode="token", delivery="B-002")
    ok("B2 wrong token rejected (401)", s == 401, f"status={s}")
    # hmac headers but token mode -> still needs the bearer token -> 401
    s, _ = fire(url, sec, source="jira", auth_mode="hmac", delivery="B-003")
    ok("B3 missing token header rejected (401)", s == 401, f"status={s}")

    # ---------- C. url_token mode (Jira Server / dumb POST) ----------
    sc, wh = create_webhook(tok, org, report, name="urltok", source="generic",
                            auth_mode="url_token", classify_enabled=False)
    url, sec, wid = wh["delivery_url"], wh["secret"], wh["id"]
    s, _ = fire(url, sec, source="generic", auth_mode="url_token", delivery="C-001")
    ok("C1 url_token accepted (200)", s == 200, f"status={s}")
    ok("C1 event entry created", wait_for(lambda: count(report, role="external", webhook_id=wid) >= 1))

    # ---------- D. GitHub source, HMAC, handshake ----------
    sc, wh = create_webhook(tok, org, report, name="gh", source="github",
                            auth_mode="hmac", classify_enabled=False)
    url, sec, wid = wh["delivery_url"], wh["secret"], wh["id"]
    s, _ = fire(url, sec, source="github", auth_mode="hmac", action="opened", delivery="D-001")
    ok("D1 github PR event accepted (200)", s == 200, f"status={s}")
    ok("D1 github event entry created",
       wait_for(lambda: count(report, role="external", webhook_id=wid) >= 1))
    c = db()
    summ = c.execute("select json_extract(prompt,'$.summary') from completions where webhook_id=? and role='external' order by turn_index desc limit 1", (wid,)).fetchone()[0]
    c.close()
    ok("D1 github summary normalized", summ and summ.startswith("PR opened:"), f"summary={summ!r}")

    # ---------- E. Classifier ACT (relevant event) ----------
    sc, wh = create_webhook(tok, org, report, name="classify", source="generic", auth_mode="hmac",
                            classify_enabled=True,
                            classifier_prompt="Only respond to events about the music store data (tracks, albums, artists, sales). Ignore anything else.")
    url, sec, wid = wh["delivery_url"], wh["secret"], wh["id"]
    sys_before = count(report, role="system", webhook_id=wid)
    fire(url, sec, source="generic", auth_mode="hmac", action="data.updated",
         title="New sales rows for albums — summarize top 5 artists by revenue", delivery="E-001")
    acted = wait_for(lambda: (latest_decision(wid) or {}).get("act") is True, timeout=45)
    dec = latest_decision(wid) or {}
    ok("E1 classifier decided ACT on relevant event", acted, f"decision={dec.get('act')} conf={dec.get('conf') or dec.get('confidence')}")
    ok("E1 classifier authored a task", bool(dec.get("task")), f"task={str(dec.get('task'))[:60]!r}")
    ok("E1 agent run produced a reply",
       wait_for(lambda: count(report, role="system", webhook_id=wid) > sys_before, timeout=60),
       f"system completions for wh = {count(report, role='system', webhook_id=wid)}")
    ok("E1 hidden trigger exists (role=user, webhook_id)",
       count(report, role="user", webhook_id=wid) >= 1)

    # ---------- F. Classifier DECLINE (irrelevant event) ----------
    fire(url, sec, source="generic", auth_mode="hmac", action="infra.alert",
         title="Kubernetes node disk pressure on prod-3", delivery="F-001")
    declined = wait_for(lambda: (latest_decision(wid) or {}).get("act") is False, timeout=45)
    dec = latest_decision(wid) or {}
    ok("F1 classifier DECLINED irrelevant event", declined, f"decision={dec.get('act')} reason={str(dec.get('reason'))[:50]!r}")
    sys_after = count(report, role="system", webhook_id=wid)
    time.sleep(3)
    ok("F1 no agent run on decline", count(report, role="system", webhook_id=wid) == sys_after)

    # ---------- G. Rate limiting ----------
    # Drain the rolling 60s window first (limiter is shared across the run).
    print("...draining rate-limit window (61s)")
    time.sleep(61)
    set_setting(tok, org, "webhook_rate_limit_per_min", 3)
    sc, wh = create_webhook(tok, org, report, name="rl", source="generic", auth_mode="hmac", classify_enabled=False)
    url, sec, wid = wh["delivery_url"], wh["secret"], wh["id"]
    codes = [fire(url, sec, source="generic", auth_mode="hmac", delivery=f"G-{i}")[0] for i in range(6)]
    ok("G1 rate limit returns 429 when exceeded", 429 in codes, f"codes={codes}")
    ok("G1 some deliveries still accepted", 200 in codes, f"codes={codes}")
    set_setting(tok, org, "webhook_rate_limit_per_min", 60)

    # ---------- H. Org master switch ----------
    set_setting(tok, org, "allow_report_webhooks", False)
    s, _ = fire(url, sec, source="generic", auth_mode="hmac", delivery="H-001")
    ok("H1 receiver blocked when flag off (403)", s == 403, f"status={s}")
    sc2, _ = create_webhook(tok, org, report, name="blocked", source="generic", auth_mode="hmac")
    ok("H2 CRUD create blocked when flag off (403)", sc2 == 403, f"status={sc2}")
    set_setting(tok, org, "allow_report_webhooks", True)
    s, _ = fire(url, sec, source="generic", auth_mode="hmac", delivery="H-002")
    ok("H3 receiver works again when flag on (200)", s == 200, f"status={s}")

    # ---------- I. Per-org cap ----------
    set_setting(tok, org, "max_webhooks", 1)
    sc3, _ = create_webhook(tok, org, report, name="overcap", source="generic", auth_mode="hmac")
    ok("I1 create blocked over max_webhooks (409)", sc3 == 409, f"status={sc3}")
    set_setting(tok, org, "max_webhooks", 20)

    # ---------- summary ----------
    print("\n" + "=" * 60)
    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"RESULT: {passed}/{total} passed")
    fails = [n for n, c, _ in results if not c]
    if fails:
        print("FAILED:", ", ".join(fails))
        return 1
    print("ALL CHECKS PASSED ✅")
    return 0


def set_setting(tok, org, key, value):
    """Merge a single FeatureConfig value into org settings."""
    cur = requests.get(f"{BASE}/api/organization/settings", headers=H(tok, org)).json()
    cfg = cur.get("config", cur)
    fc = cfg.get(key)
    if isinstance(fc, dict):
        fc = {**fc, "value": value, "state": ("enabled" if value else "disabled") if isinstance(value, bool) else fc.get("state")}
    else:
        fc = {"value": value, "name": key, "description": key}
    requests.put(f"{BASE}/api/organization/settings", headers=H(tok, org), json={"config": {key: fc}})


if __name__ == "__main__":
    sys.exit(main())
