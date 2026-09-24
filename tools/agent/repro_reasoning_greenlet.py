"""Reproduce: coder reasoning persistence vs. a parallel sibling tool on the
agent's shared AsyncSession (Postgres/asyncpg) -> rollback -> MissingGreenlet.

Runs the REAL AgentV2._coder_reasoning_callback bound to a thin stub, while a
second coroutine does unlocked reads on the same session (as create_data's viz
instruction build / LoadablesResolver do for a parallel sibling tool).

    cd backend && BOW_DATABASE_URL=postgresql://bow:bow@localhost:5432/bow \
        uv run python ../tools/agent/repro_reasoning_greenlet.py
Exit 0 = healthy, 1 = bug reproduced.
"""
import asyncio, os, sys, uuid, traceback
sys.path.insert(0, os.getcwd())
import main  # noqa: F401  registers all mappers
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.ai.agent_v2 import AgentV2
from app.ai.llm.types import ReasoningDeltaEvent, ReasoningCompleteEvent
from app.models.agent_execution import AgentExecution
from app.models.completion_block import CompletionBlock
from app.project_manager import ProjectManager

POISON = "--poison" in sys.argv
FAIL_COMMIT = "--commit-fails-once" in sys.argv
EXT_ROLLBACK = "--external-rollback" in sys.argv
URL = os.environ["BOW_DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")


async def seed(engine):
    ids = {k: str(uuid.uuid4()) for k in ("completion", "exec", "block")}
    async with engine.begin() as c:
        await c.execute(text("SET session_replication_role = replica"))  # skip FKs for the fixture
        await c.execute(text(
            "INSERT INTO completions (id, prompt, completion, status, model, turn_index, feedback_score,"
            " sigkill, message_type, role, report_id, main_router, created_at, updated_at) VALUES (:id, '{}', '{}',"
            " 'in_progress', 'x', 0, 0, now(), 'ai_completion', 'system', :id, 'x', now(), now())"), {"id": ids["completion"]})
        await c.execute(text(
            "INSERT INTO agent_executions (id, completion_id, status, latest_seq, created_at, updated_at)"
            " VALUES (:id, :c, 'in_progress', 0, now(), now())"), {"id": ids["exec"], "c": ids["completion"]})
        await c.execute(text(
            "INSERT INTO completion_blocks (id, completion_id, agent_execution_id, source_type, block_index,"
            " title, status, created_at, updated_at) VALUES (:id, :c, :e, 'tool', 0, 'Create Data',"
            " 'in_progress', now(), now())"), {"id": ids["block"], "c": ids["completion"], "e": ids["exec"]})
    return ids


class Stub:
    _coder_reasoning_callback = AgentV2._coder_reasoning_callback

    def __init__(self, db, execution, completion_id):
        self.db = db
        self._tool_db_lock = asyncio.Lock()
        self._session_maker = None  # set by run()
        self.project_manager = ProjectManager()
        self.current_execution = execution
        self.system_completion_id = completion_id
        self.seqs = []

    def _use_single_write_session(self):
        return False  # Postgres default (single-writer is opt-in there)

    async def _emit_sse_event(self, ev):
        self.seqs.append(ev.seq)


async def run():
    engine = create_async_engine(URL)
    ids = await seed(engine)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    errors = []
    async with Session() as db:
        execution = await db.get(AgentExecution, ids["exec"])
        agent = Stub(db, execution, ids["completion"])
        agent._session_maker = Session
        if FAIL_COMMIT:
            # One reasoning-snapshot commit fails the way a colliding sibling
            # makes it fail in production (asyncpg: another operation is in
            # progress). Everything after this is the real code path.
            real_commit, calls = db.commit, {"n": 0}
            async def flaky_commit():
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("injected: cannot perform operation: another operation is in progress")
                return await real_commit()
            db.commit = flaky_commit
        cb = agent._coder_reasoning_callback(ids["block"])
        stop = asyncio.Event()

        async def sibling_tool():  # parallel create_data: unlocked shared-session reads
            poisoned = False
            while not stop.is_set():
                if FAIL_COMMIT:
                    return
                if EXT_ROLLBACK:
                    # Some other best-effort path rolls back the shared session
                    # mid-stream (expires every loaded instance).
                    while len(agent.seqs) <= 20:
                        await asyncio.sleep(0.01)
                    await db.rollback()
                    return
                if POISON and not poisoned and len(agent.seqs) > 20:
                    # A sibling statement fails and is swallowed (create_data's
                    # `except Exception: viz_instructions = ""`). On Postgres the
                    # transaction is now aborted; SQLite has no such state.
                    poisoned = True
                    try:
                        await db.execute(text("SELECT 1/0"))
                    except Exception:
                        pass
                    return  # poison-only mode: no further concurrent reads
                if POISON:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    await db.execute(select(CompletionBlock.id).where(text("pg_sleep(0.15) IS NOT NULL")))
                except Exception as e:
                    errors.append(f"sibling: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(0)

        async def reasoning():  # coder streams provider reasoning for ~4s
            for i in range(80):
                try:
                    await cb(ReasoningDeltaEvent(text=f"thought {i}. "))
                except Exception as e:
                    errors.append(f"reasoning: {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(0.05)
            try:
                await cb(ReasoningCompleteEvent(text=""))
            except Exception as e:
                errors.append(f"reasoning-complete: {type(e).__name__}: {e}"[:200])
            stop.set()

        await asyncio.gather(sibling_tool(), reasoning())

        # What the agent loop does next: plain attribute access / next_seq.
        after = None
        try:
            _ = agent.current_execution.id
            after = await agent.project_manager.next_seq(db, agent.current_execution)
        except Exception as e:
            errors.append(f"agent-after: {type(e).__name__}: {e}"[:200])

        seqs = agent.seqs
        dup = len(seqs) - len(set(seqs))
        regress = sum(1 for a, b in zip(seqs, seqs[1:]) if b <= a)
    async with Session() as s:
        blk = await s.get(CompletionBlock, ids["block"])
        persisted = len(blk.reasoning or "")
    await engine.dispose()

    kinds = {}
    for e in errors:
        k = e.split(":")[0] + ":" + e.split(":")[1]
        kinds[k] = kinds.get(k, 0) + 1
    print(f"events emitted={len(seqs)} duplicate_seqs={dup} non_monotonic={regress} next_seq_after={after}")
    print(f"persisted reasoning chars={persisted}")
    print(f"errors={len(errors)} by kind={kinds}")
    for e in errors[:5]:
        print("  ", e)
    # Any error is the bug: the two tools must never collide on one connection.
    bad = bool(errors) or dup or regress
    print("RESULT:", "BUG REPRODUCED" if bad else "OK")
    return 1 if bad else 0

sys.exit(asyncio.run(run()))
