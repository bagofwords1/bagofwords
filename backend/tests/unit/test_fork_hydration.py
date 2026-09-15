"""A delegated-source fork must only ever gain what the FORKER could run.

`fork_report` deliberately creates these forks empty — no rows (they are the
creator's slice) and no SQL (the share withholds it from a reader who may have
no access). `hydrate_fork` is what fills them back in, so it is the single
place where the creator's queries can become the forker's. Everything here
pins that boundary: code reaches a step only via a successful run under the
forker's own credentials, a failed run leaves the step with neither code nor
rows, and a forker who could run nothing is left with no fork at all.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.fork_service import ForkService


class _Session:
    """Enough AsyncSession for hydrate_fork, recording what it was asked to do."""

    def __init__(self, report, user, org, steps=None):
        # hydrate_fork opens a session for setup, one per step, and one to
        # settle — each reloads the fork, forker and org. Answer by table, not
        # by call order, so the stub fits any number of sessions.
        self._by_table = {"reports": report, "users": user, "organizations": org}
        self.sessions_opened = 0
        self.steps = steps or {}
        self.deleted = []
        self.rollbacks = 0
        self.commits = 0
        self.writes = []

    async def execute(self, stmt=None, *_a, **_k):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        if getattr(stmt, "is_select", False):
            value = next((v for t, v in self._by_table.items() if f"FROM {t}" in sql), None)
        else:
            # Every non-SELECT is a write hydrate_fork issued; keep its SQL so
            # tests can assert what was settled.
            value = None
            self.writes.append(sql)
        result = MagicMock()
        result.unique.return_value = result
        result.scalar_one_or_none.return_value = value
        return result

    def settled(self, table: str, status: str) -> list:
        """UPDATEs against `table` that set status=`status`."""
        return [w for w in self.writes
                if w.startswith(f"UPDATE {table} ") and f"status='{status}'" in w]

    async def get(self, _model, ident):
        return self.steps.get(str(ident))

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1
        # Position marker, so a test can assert WHICH commit a write rode on.
        self.writes.append("-- commit --")

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        self.sessions_opened += 1
        return self

    async def __aexit__(self, *_):
        return False


def _report():
    r = MagicMock()
    r.id = "fork-1"
    r.organization_id = "org-1"
    r.data_sources = []
    return r


async def _run_hydration(session, rerun_side_effect, pending, with_client=False,
                         client_fails=False):
    """Drive hydrate_fork against `session`, stubbing everything external.

    `with_client=True` gives the forker a usable client on one source — the
    difference between "this query broke" and "this identity has no access to
    the source at all", which is what decides whether the fork is retired.
    """
    svc = ForkService()
    rerun = AsyncMock(side_effect=rerun_side_effect)
    # `client_fails` is a source that WAS tried and yielded no client — the
    # absence of access itself, as opposed to there being no source to try.
    agents = [MagicMock()] if (with_client or client_fails) else []
    clients = {"agent:conn": MagicMock()} if with_client else {}
    construct = (AsyncMock(side_effect=Exception("403 forbidden")) if client_fails
                 else AsyncMock(return_value=clients))
    with patch("app.dependencies.async_session_maker", return_value=session), \
         patch("app.services.step_service.StepService.rerun_step", rerun), \
         patch("app.ai.tools.implementations.agent_focus_common.resolve_run_agents",
               AsyncMock(return_value=agents)), \
         patch("app.services.data_source_service.DataSourceService.construct_clients",
               construct), \
         patch("app.services.thumbnail_service.ThumbnailService.regenerate_for_report",
               AsyncMock(return_value=None)) as thumb:
        out = await svc.hydrate_fork(
            fork_id="fork-1", user_id="u-1", organization_id="org-1",
            pending_code=pending,
        )
    return out, rerun, thumb


@pytest.mark.asyncio
async def test_each_step_runs_with_the_source_code_as_the_forker():
    """The source SQL is executed, but handed in as an override — it is not on
    the step, which is the whole point of creating the fork without it."""
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    out, rerun, thumb = await _run_hydration(
        session, None, {"s1": "SELECT 1", "s2": "SELECT 2"},
    )

    assert out == {"succeeded": 2, "failed": 0, "deleted": False}
    overrides = {c.kwargs["code_override"] for c in rerun.await_args_list}
    assert overrides == {"SELECT 1", "SELECT 2"}
    # Credentials are the forker's, never the source owner's.
    assert all(c.kwargs["current_user"] is user for c in rerun.await_args_list)
    assert not session.deleted
    # Regenerated from the forker's own rows — the creator's was not copied.
    thumb.assert_awaited_once()
    # This run is the fork's first refresh: stamping it is what stops the
    # page's refresh-on-view from running every query a second time.
    assert any(w.startswith("UPDATE reports ") and "last_run_at" in w
               for w in session.writes)


@pytest.mark.asyncio
async def test_a_failed_run_is_rolled_back_so_its_code_is_never_persisted():
    """The security property. rerun_step assigns the override onto the step
    before executing, so a failure that is not rolled back would let the NEXT
    step's commit flush SQL onto a step whose run failed — handing the forker
    the very code the share withheld."""
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    async def _rerun(db, step_id, **kwargs):
        if step_id == "s2":
            raise RuntimeError('DAX query failed: HTTP 404 {"code":"PowerBIEntityNotFound","model":"shared_orders"}')
        return MagicMock()

    # s2 (refused) runs FIRST: a shared session's rollback used to expire the
    # fork, forker and org, so s1 then failed on them and the fork was archived.
    out, _, _ = await _run_hydration(session, _rerun, {"s2": "denied", "s1": "ok"})

    assert out == {"succeeded": 1, "failed": 1, "deleted": False}
    # setup + (heartbeat + run) per step + settle
    assert session.sessions_opened == 6
    assert session.rollbacks == 1, "a failed run left its code assignment in the session"
    # Partial access keeps the fork (option 3); each step leaves 'pending'.
    assert not session.deleted
    [err] = session.settled("steps", "error")
    [ok] = session.settled("steps", "success")
    assert "'s2'" in err and "'s1'" not in err
    assert "'s1'" in ok and "'s2'" not in ok


@pytest.mark.asyncio
async def test_refusal_and_broken_query_get_different_reasons():
    """A recognised provider refusal is owed a plain "no access"; a query that
    broke for any other reason must NOT be told that — a forker WITH access got
    "no access" on a query whose parameters the fork had dropped (KeyError).
    Neither ever relays the provider's text, which can name the refused model."""
    from app.services.access_errors import NO_ACCESS_REASON

    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    async def _rerun(db, step_id, **kwargs):
        if step_id == "refused":
            raise RuntimeError('DAX query failed: HTTP 401 {"model":"shared_orders"}')
        if step_id == "broken":
            raise KeyError("depot")
        return MagicMock()

    out, _, _ = await _run_hydration(
        session, _rerun, {"ok": "a", "refused": "b", "broken": "c"},
    )
    assert out == {"succeeded": 1, "failed": 2, "deleted": False}

    errors = session.settled("steps", "error")
    refused = next(w for w in errors if "'refused'" in w)
    broke = next(w for w in errors if "'broken'" in w)
    assert NO_ACCESS_REASON in refused and "'broken'" not in refused
    assert "failed when it was run for you" in broke
    assert "credentials" not in broke and "do not have access" not in broke
    for w in errors:
        assert "shared_orders" not in w and "HTTP 401" not in w


@pytest.mark.asyncio
async def test_no_access_at_all_deletes_the_fork():
    """A forker REFUSED every query has no access to the source: the fork
    would be empty charts with no way to fill them.

    Only refusals retire a fork — see the broken-query test below."""
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    async def _rerun(db, step_id, **kwargs):
        # The shape every client raises: "<operation> failed: HTTP <status>".
        raise RuntimeError('DAX query failed: HTTP 401 {"model":"shared_orders"}')

    with patch("app.services.report_service.ReportService.archive_report",
               AsyncMock()) as archive:
        out, _, thumb = await _run_hydration(session, _rerun, {"s1": "a", "s2": "b"})

    assert out == {"succeeded": 0, "failed": 2, "deleted": True}
    # Archived through the same path a user's delete takes — a hard delete
    # trips the NOT NULL report_id on the fork's children (see the real-DB e2e).
    archive.assert_awaited_once()
    assert archive.await_args.args[1] == "fork-1"
    assert not session.deleted
    assert session.rollbacks == 2
    thumb.assert_not_awaited()
    assert not any(w.startswith("UPDATE reports ") and "last_run_at" in w
                   for w in session.writes), (
        "a fork with nothing fresh was stamped as freshly run")

    # Settled AND archived in the same commit. Split across two, a status poll
    # landing between them sees no pending step, calls the fork ready, and
    # loads a report still reading 'draft' — so the page renders the empty
    # dashboard instead of the explanation this branch exists to give.
    archived = next(i for i, w in enumerate(session.writes)
                    if w.startswith("UPDATE reports ") and "status='archived'" in w)
    settled = next(i for i, w in enumerate(session.writes)
                   if w.startswith("UPDATE steps ") and "status='error'" in w)
    # No commit may fall BETWEEN them. (Not "before the first commit": the
    # per-step heartbeat commits on its own session as the pass runs, so by
    # here several commits have already gone by.)
    between = session.writes[min(settled, archived):max(settled, archived)]
    assert "-- commit --" not in between, (
        "the fork was left live in the window between settling and archiving")


@pytest.mark.asyncio
async def test_a_fork_whose_queries_merely_BROKE_is_kept():
    """Only a refusal retires a fork.

    `failed` catches every exception, so a provider timeout or a query that
    simply broke landed there beside a real refusal — and retiring on those
    destroyed the fork (nothing un-archives a report) while telling the forker
    "none of its queries could be run with your credentials", an affirmative
    claim about permissions the failure never supported. The refused/broke
    split already drawn for the per-step message is honoured here too.
    """
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    async def _rerun(db, step_id, **kwargs):
        raise TimeoutError("read timed out")

    with patch("app.services.report_service.ReportService.archive_report",
               AsyncMock()) as archive:
        out, _, _ = await _run_hydration(session, _rerun, {"s1": "a", "s2": "b"}, with_client=True)

    assert out == {"succeeded": 0, "failed": 2, "deleted": False}
    archive.assert_not_awaited()
    assert not any("status='archived'" in w for w in session.writes)
    # Each step still carries its own neutral reason, so the page can explain
    # the empty charts and the forker can retry instead of re-forking.
    assert len(session.settled("steps", "error")) == 1


@pytest.mark.asyncio
async def test_one_refusal_among_broken_queries_does_not_retire_the_fork():
    """The mixed case: a refusal AND a breakage, nothing succeeded. The
    breakage means "no access to anything" is not established, so the fork
    stays and each step keeps its own (different) reason."""
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    async def _rerun(db, step_id, **kwargs):
        if step_id == "s1":
            raise RuntimeError('DAX query failed: HTTP 403 {"model":"m"}')
        raise KeyError("region")

    with patch("app.services.report_service.ReportService.archive_report",
               AsyncMock()) as archive:
        out, _, _ = await _run_hydration(
            session, _rerun, {"s1": "a", "s2": "b"}, with_client=True,
        )

    assert out["deleted"] is False
    archive.assert_not_awaited()
    errs = session.settled("steps", "error")
    assert len(errs) == 2, "refusal and breakage must not share one reason"


@pytest.mark.asyncio
async def test_no_usable_client_still_retires_the_fork():
    """The primary retire case, and why it cannot be judged on the exception.

    A forker with no usable identity has no entry under the source's
    `ds_clients` key, so step code fails with a KeyError — which no
    provider-status check recognises. What proves the absence of access is that
    a source WAS tried and yielded no client at all, so that is what is tested
    here, not the shape of the error each query raised.
    """
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    async def _rerun(db, step_id, **kwargs):
        raise KeyError("agent:conn")

    with patch("app.services.report_service.ReportService.archive_report",
               AsyncMock()) as archive:
        out, _, _ = await _run_hydration(
            session, _rerun, {"s1": "a"}, client_fails=True,
        )

    assert out["deleted"] is True, out
    archive.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_steps_are_heartbeated_as_the_pass_runs():
    """`is_hydrating` ages a pending step out after a fixed window. Keyed on
    created_at that window was a budget for the WHOLE pass, so a hydration
    slower than it read as dead while still running and refresh-on-view
    re-ran the same steps as the owner underneath it. Each iteration now
    touches the steps still owed, so the window bounds the gap BETWEEN steps."""
    report, user, org = _report(), SimpleNamespace(id="u-1"), MagicMock()
    org.get_settings = AsyncMock(return_value=None)
    session = _Session(report, user, org)

    out, _, _ = await _run_hydration(
        session, AsyncMock(return_value=MagicMock()), {"s1": "a", "s2": "b", "s3": "c"},
    )

    assert out["succeeded"] == 3
    beats = [w for w in session.writes
             if w.startswith("UPDATE steps ") and "updated_at" in w
             and "status='success'" not in w]
    assert len(beats) == 3, "every step must be preceded by a heartbeat"
    # A beat only ever covers what is still OWED — never a step already run,
    # which would resurrect a settled row's pending clock.
    assert "'s1'" in beats[0] and "'s3'" in beats[0]
    assert "'s1'" not in beats[-1] and "'s3'" in beats[-1]


# ── rerun_step's own half of the contract ───────────────────────────────────


@pytest.mark.asyncio
async def test_code_override_is_committed_only_when_the_run_succeeds():
    """rerun_step assigns the override onto the step and lets the single commit
    at the end persist code and data together. An execution failure raises
    BEFORE that commit, so the step keeps neither — which is what lets fork
    hydration hand it code the forker has not yet proven they may run."""
    from app.services.step_service import StepService

    svc = StepService()
    svc._step_param_specs = MagicMock(return_value=[])
    svc._resolve_step_params = AsyncMock(return_value={})

    async def _run(committed_code):
        step = SimpleNamespace(
            id="s1", code="", applied_params=None, data=None,
        )
        report = SimpleNamespace(id="r1", organization_id="org-1")
        svc._load_step_for_rerun = AsyncMock(return_value=(step, report))
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock(side_effect=lambda: committed_code.append(step.code))
        db.refresh = AsyncMock()
        return step, db

    # Success → the code is on the step when the commit runs.
    committed = []
    step, db = await _run(committed)
    svc._execute_step_code = AsyncMock(return_value={"rows": [{"a": 1}]})
    with patch("app.schemas.step_schema.StepSchema.from_orm", MagicMock()):
        await svc.rerun_step(db, "s1", current_user=SimpleNamespace(id="u"),
                             code_override="SELECT 1")
    assert committed == ["SELECT 1"]
    assert step.data == {"rows": [{"a": 1}]}

    # Failure → raises before the commit, so nothing is persisted.
    committed = []
    step, db = await _run(committed)
    svc._execute_step_code = AsyncMock(side_effect=RuntimeError("no access"))
    with pytest.raises(RuntimeError, match="no access"):
        await svc.rerun_step(db, "s1", current_user=SimpleNamespace(id="u"),
                             code_override="SELECT 1")
    assert committed == [], "a failed run committed the overridden code"
    db.commit.assert_not_awaited()
