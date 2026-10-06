"""Repro: Entra login side effects against a pooled Postgres engine (prod config).
Usage (from backend/): BOW_DATABASE_URL=postgresql://... uv run python ../tools/agent/repro_entra_login_pg.py"""
import asyncio, logging, uuid, sys
from sqlalchemy import text, select

records = []
class H(logging.Handler):
    def emit(self, r): records.append((r.levelname, r.name, r.getMessage()))
logging.getLogger().addHandler(H()); logging.getLogger().setLevel(logging.INFO)

async def main():
    import main  # noqa: F401  (registers all models)
    from app.dependencies import async_session_maker
    from app.models.user import User
    from app.services import connection_oauth_service as cos
    from app.services.auth_providers import _record_login
    uid = str(uuid.uuid4())
    # The request loop owns the pool: warm several connections here, like a live server.
    async with async_session_maker() as db:
        db.add(User(id=uid, name="demo1", email=f"{uid}@bow14.onmicrosoft.com", hashed_password="x"))
        await db.commit()
    for _ in range(3):
        async with async_session_maker() as db:
            await db.execute(text("select 1"))
    done = asyncio.Event()
    orig = cos.auto_provision_connection_credentials
    async def spy(db, user, tok):
        try: return await orig(db, user, tok)
        finally: pass
    cos.auto_provision_connection_credentials = spy
    cos.schedule_auto_provision(uid, "fake-login-token")
    await asyncio.sleep(3)
    await _record_login(type("U", (), {"id": uid})())
    async with async_session_maker() as db:
        ll = (await db.execute(select(User.last_login).where(User.id == uid))).scalar()
    bad = [r for r in records if r[0] in ("WARNING", "ERROR") and ("auto-provision" in r[2] or "last_login" in r[2] or "terminating" in r[2])]
    for r in bad: print("LOG", r[0], r[1], r[2][:160])
    print("last_login =", ll)
    ok = not bad and ll is not None
    print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
asyncio.run(main())
