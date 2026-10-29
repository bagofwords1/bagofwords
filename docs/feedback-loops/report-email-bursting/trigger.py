"""Fire every report-email path against the live stack.

Run from backend/ with the stack's env (BOW_CONFIG_PATH, database):

    python trigger.py ids.json http://127.0.0.1:8063

For each seeded report (identity dashboard, plain dashboard, identity deck):

  1. share it (artifact_visibility=shared) with alice + bob — carol is a member
     who is NOT shared, partner@acme-partner.com has no account;
  2. "Share by email"   POST /api/reports/{id}/notify  (HTTP, owner token)
     to alice, bob, carol and the partner;
  3. scheduled-prompt result email (the path with a PDF + the run's written
     summary): notification_service.send_scheduled_prompt_results, in
     process, subscribers owner + alice + bob + carol + partner;
  4. scheduled report refresh: POST /api/reports/{id}/schedule with the same
     subscribers, then the job body the scheduler fires
     (ReportService.scheduled_rerun_report_steps), in process.

Mail lands in the SMTP sink; read it with read_mail.py.
"""
import asyncio
import json
import sys

import httpx

sys.path.insert(0, ".")
import main  # noqa: F401,E402
from sqlalchemy import select  # noqa: E402

from app.dependencies import async_session_maker  # noqa: E402
from app.models.user import User  # noqa: E402

PARTNER = "partner@acme-partner.com"
SUMMARY = "Top account this week: OWNER-ACCT-1 (written by the run, from the owner's rows)."


async def user_ids():
    async with async_session_maker() as db:
        rows = (await db.execute(select(User.email, User.id))).all()
        return {e: str(i) for e, i in rows}


async def main_(ids_path, base):
    ids = json.load(open(ids_path))
    uid = await user_ids()
    org = ids["org_id"]
    c = httpx.Client(base_url=base, timeout=300)
    tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com",
                                               "password": "Password123!"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    subs = [{"type": "user", "id": uid[e]} for e in
            ("admin@example.com", "alice@example.com", "bob@example.com", "carol@example.com")]
    subs.append({"type": "email", "address": PARTNER})

    from app.services.notification_service import notification_service
    from app.services.report_service import ReportService

    for key in ("identity", "plain", "deck"):
        rid = ids[key]["report_id"]
        r = c.put(f"/api/reports/{rid}/visibility/artifact", headers=h, json={
            "visibility": "shared",
            "shared_user_ids": [uid["alice@example.com"], uid["bob@example.com"]]})
        print(key, "share", r.status_code)

        r = c.post(f"/api/reports/{rid}/notify", headers=h, json={
            "type": "share_dashboard", "channels": ["email"],
            "recipients": ["alice@example.com", "bob@example.com", "carol@example.com", PARTNER],
            "share_url": f"{base}/r/{rid}"})
        body = r.json()
        print(key, "notify", r.status_code, json.dumps({
            "dispatched": [d["recipients"] for d in body.get("dispatched", [])],
            "skipped": [d["recipients"] for d in body.get("skipped", [])],
            "errors": [d["recipients"] for d in body.get("errors", [])]}))

        await notification_service.send_scheduled_prompt_results(
            report_id=rid, report_title=f"[scheduled-prompt] {key}", subscribers=subs,
            report_url=f"{base}/reports/{rid}",
            exec_summary={"iterations": 1, "queries": 1, "artifacts": 1, "last_content": SUMMARY},
            locale="en",
        )
        print(key, "scheduled-prompt results sent")

        r = c.post(f"/api/reports/{rid}/schedule", headers=h, json={
            "cron_expression": "0 6 * * 1", "notification_subscribers": subs})
        print(key, "schedule", r.status_code)
        await ReportService().scheduled_rerun_report_steps(rid, uid["admin@example.com"], org)
        # The refresh emails go out on a fire-and-forget task; let it finish.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=600)
        print(key, "scheduled refresh fired")


asyncio.run(main_(sys.argv[1], sys.argv[2]))
