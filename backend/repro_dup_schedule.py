"""
Reproduction: scheduled jobs (and thus scheduled emails) fire once PER uvicorn
worker because every worker calls scheduler.start() against the SAME shared
job store, while the leader lock only gates job *registration*, not *execution*.

This harness uses the project's real modules:
  - app.core.scheduler.scheduler            (AsyncIOScheduler + SQLAlchemyJobStore on Postgres)
  - app.core.scheduler.try_acquire_scheduler_leader  (the /tmp flock leader lock)

It spawns N separate processes (one per simulated uvicorn worker) that each run
the exact startup sequence main.py uses:
   1. try_acquire_scheduler_leader()
   2. ONLY the leader registers the scheduled job  (like register_all_jobs / report schedule)
   3. ALL workers call scheduler.start()           (main.py line 457)

The job callback simulates notification_service sending an email by inserting a
row into repro_email_sends. If the bug is present, each scheduled fire produces
one row PER worker instead of exactly one.
"""
import os
import sys
import time
import multiprocessing as mp

# Configure the app to use Postgres BEFORE importing any app module.
os.environ.setdefault("ENVIRONMENT", "production")
os.environ.setdefault("BOW_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/bow")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")

PG = "postgresql://postgres:postgres@127.0.0.1:5432/bow"
JOB_ID = "scheduled_report_email_repro"
# Keep interval > claim window, mirroring production (cron >=60s fires vs 30s
# claim window). _USE_CLAIM=1 turns on the fix (app.core.scheduler.claim_scheduled_run).
INTERVAL_SECONDS = int(os.environ.get("_INTERVAL", "8"))
CLAIM_WINDOW = int(os.environ.get("_CLAIM_WINDOW", "4"))
USE_CLAIM = os.environ.get("_USE_CLAIM") == "1"
RUN_SECONDS = 30


def record_email_send():
    """Stand-in for notification_service.send_scheduled_report_results().

    A real worker would, at this point, render the report and SMTP the email to
    every notification subscriber. We just log the send to Postgres so we can
    count how many times the single scheduled job actually executed.
    """
    import psycopg2, time as _t
    pid = os.getpid()
    role = os.environ.get("_WORKER_ROLE", "?")
    # THE FIX: claim this scheduled fire; only the winning worker sends.
    if USE_CLAIM:
        from app.core.scheduler import claim_scheduled_run
        if not claim_scheduled_run(JOB_ID, window_seconds=CLAIM_WINDOW):
            print(f"[pid {pid} role={role}] claim lost — skipping send (fix working)", flush=True)
            return
    # Bucket the send into its scheduled interval window. Both workers compute
    # the SAME next_run_time from the shared job store, so same-tick sends land
    # in the same bucket even if they execute a fraction of a second apart.
    tick = int(_t.time()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    conn = psycopg2.connect(PG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repro_email_sends (job_id, scheduled_fire_time, worker_pid, worker_role) "
            "VALUES (%s, to_timestamp(%s), %s, %s)",
            (JOB_ID, tick, pid, role),
        )
    conn.close()
    print(f"[pid {pid} role={role}] >>> SENT scheduled email (tick={tick})", flush=True)


def worker_main(worker_index: int):
    import asyncio
    from datetime import datetime

    # Fresh import of the project's real scheduler in this process.
    from app.core.scheduler import scheduler, try_acquire_scheduler_leader

    is_leader = try_acquire_scheduler_leader()
    os.environ["_WORKER_ROLE"] = "LEADER" if is_leader else "follower"
    role = os.environ["_WORKER_ROLE"]
    pid = os.getpid()
    print(f"[pid {pid}] worker #{worker_index} startup — scheduler_leader={is_leader}", flush=True)

    # Two production paths register this job:
    #   - leader registers ALL jobs at startup (register_all_jobs)
    #   - whichever worker SERVES the create/update API request registers it
    #     too (set_report_schedule -> scheduler.add_job). That request can land
    #     on ANY worker, leader or not. We model it landing on worker #1.
    mode = os.environ.get("_REPRO_MODE", "realistic")
    serves_api_request = (worker_index == 1)
    if mode == "realistic":
        should_register = is_leader or serves_api_request
    else:  # "leader-only": only the leader ever registers
        should_register = is_leader

    # --- register the scheduled job ---
    if should_register:
        why = "register_all_jobs@startup" if is_leader else "served set_report_schedule API request"
        print(f"[pid {pid} role={role}] registering job ({why})", flush=True)
        scheduler.add_job(
            record_email_send,
            trigger="interval",
            seconds=INTERVAL_SECONDS,
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        print(f"[pid {pid}] registered job '{JOB_ID}' (every {INTERVAL_SECONDS}s)", flush=True)
    else:
        print(f"[pid {pid}] not registering job in this worker", flush=True)

    # --- mirrors main.py line 457: ALL workers start their scheduler ---
    async def run():
        scheduler.start()
        print(f"[pid {pid} role={role}] scheduler.start() called — entering run loop", flush=True)
        await asyncio.sleep(RUN_SECONDS)
        scheduler.shutdown(wait=False)

    asyncio.run(run())
    print(f"[pid {pid}] worker #{worker_index} exiting", flush=True)


def main():
    n_workers = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    # clean slate
    import psycopg2
    conn = psycopg2.connect(PG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE repro_email_sends")
        # Pre-create the job store table so we model STEADY STATE (the table
        # persists across restarts in prod). This avoids a cold-start race where
        # two workers both run CREATE TABLE ... checkfirst at once.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS apscheduler_jobs (
                id VARCHAR(191) NOT NULL PRIMARY KEY,
                next_run_time FLOAT(25),
                job_state BYTEA NOT NULL
            )
        """)
        cur.execute("DELETE FROM apscheduler_jobs WHERE id = %s", (JOB_ID,))
    conn.close()

    # spawn => each worker re-imports app.core.scheduler fresh, like a real
    # uvicorn --workers N fork (separate process, separate scheduler object,
    # SAME Postgres job store).
    ctx = mp.get_context("spawn")
    print(f"\n=== Simulating uvicorn --workers {n_workers} against shared Postgres job store ===\n", flush=True)
    procs = [ctx.Process(target=worker_main, args=(i,)) for i in range(n_workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    # tally
    conn = psycopg2.connect(PG)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM repro_email_sends")
        total = cur.fetchone()[0]
        cur.execute("""
            SELECT date_trunc('second', scheduled_fire_time) AS tick,
                   count(*) AS sends,
                   count(distinct worker_pid) AS distinct_workers,
                   string_agg(worker_role, ',' ORDER BY worker_role) AS roles
            FROM repro_email_sends
            GROUP BY 1 ORDER BY 1
        """)
        rows = cur.fetchall()
    conn.close()

    print("\n================= RESULT =================")
    print(f"workers simulated:        {n_workers}")
    print(f"total email sends logged: {total}")
    print(f"distinct scheduled ticks: {len(rows)}")
    print("\nPer scheduled fire-time:")
    print(f"  {'tick':<22} {'sends':>5} {'workers':>8}  roles")
    dup = False
    for tick, sends, dworkers, roles in rows:
        flag = "  <-- DUPLICATE" if sends > 1 else ""
        if sends > 1:
            dup = True
        print(f"  {str(tick):<22} {sends:>5} {dworkers:>8}  {roles}{flag}")
    print("==========================================")
    if dup:
        print("BUG REPRODUCED: a single scheduled job fired in multiple workers -> duplicate emails.")
    else:
        print("No duplication observed.")


if __name__ == "__main__":
    main()
