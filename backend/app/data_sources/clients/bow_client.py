"""A bounded sync facade over the authenticated async BOW service."""
import asyncio
from contextlib import asynccontextmanager


class BowInfrastructureError(RuntimeError):
    """An execution failure that generated code cannot repair."""
    terminal_execution_error = True


@asynccontextmanager
async def _writer_guard(lock):
    if lock is None:
        yield
    else:
        async with lock:
            yield


from app.schemas.bow_source_schema import BowQuery, SOURCE_ID


class BowClient:
    _manages_query_deadline = True
    relative_date_hint = "BOW: time_range={'relative':'7d'} is resolved on each execution; do not freeze today's date."
    description = '''BOW internal training data. Use ONLY execute_query with a Python dict (never SQL).
Example: ds_clients["bow"].execute_query({"dataset":"runs", "query":"tool:create_data tool.status:error", "time_range":{"relative":"7d"}}).
Datasets: runs (one row/run), tool_calls (one row/call). query uses the Diagnosis grammar: status:error, feedback:negative,
judge.confidence:<3, agent:"Name", tool:create_data tool.status:error, table:customers, tool.args:region.
columns selects fields; omit it for defaults. Select only relevant columns, not the entire schema catalog.
For one row per run use agent_ids/agent_names arrays, never agent_id/agent_name (those expand runs per agent).
Agent filters use actual names: agent:"sap-bo" is exact, agent:sap* matches a name prefix. Push filters into query, not pandas.
group_by and metrics aggregate all matching rows on the server:
{"dataset":"runs","group_by":["day","status"],"metrics":[{"op":"count","name":"runs"}]}.
Metrics: count (no field), count_distinct, sum, avg (field required); each needs a unique name.
sort=[{"field":"cost_usd","direction":"desc"}]; limit means explicitly requested top-N, not pagination.
Use columns from the supplied schema. Agent arrays are agent_ids/agent_names; agent_id or agent_name expands a run per agent,
so grouped agent counts are not additive. tool_calls queries apply positive tool filters to the returned calls.
Time bounds: relative hours/days, or timezone-aware start/end, maximum 366 days, default 30d.
Always state the time window in the table description and answer, especially for zero matches.
If asked for all history without a time window, request 366d and disclose that supported window.
10,000 rows / 1,000 aggregate groups maximum; aggregate or narrow on overflow. Unknown cost remains null.
Permissions are enforced by the service and cannot be supplied or changed by code. Results are saved as normal data tables.'''

    def __init__(self, session_factory, loop, organization_id, user_id, report_id, writer_db, writer_lock=None):
        self._factory, self._loop = session_factory, loop
        self._organization_id, self._user_id, self._report_id = organization_id, user_id, report_id
        self._bow_client_key = "bow"
        self._bow_source_id = SOURCE_ID
        self._bow_data_source_name = "BOW"
        self._bow_connection_query_timeout = 30
        self._bow_access = None
        self._writer_db, self._writer_lock = writer_db, writer_lock
        self._tasks = set()

    def execute_query(self, request):
        parsed = BowQuery.model_validate(request)
        future = asyncio.run_coroutine_threadsafe(self._execute(parsed), self._loop)
        # One deadline, owned by the async operation. Completion includes session
        # cleanup; the generic wrapper must not abandon a second timeout thread.
        return future.result()

    async def _cancel_pending(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, request):
        from app.models.organization import Organization
        from app.models.user import User
        from app.services.bow_source_service import BowSourceService
        from app.services.bow_source_access import protect_report, assert_read, report_access, merge_access
        from sqlalchemy.exc import OperationalError
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with asyncio.timeout(self._bow_connection_query_timeout):
                # This session only reads. Close it before using the coordinated
                # writer, so no cross-session write waits on the agent's transaction.
                async with self._factory() as db:
                    user = await db.get(User, self._user_id)
                    org = await db.get(Organization, self._organization_id)
                    if not user or not org:
                        raise ValueError("BOW execution requires an authenticated organization member")
                    await assert_read(db, await report_access(db, self._report_id), user)
                    df = await BowSourceService().query(db, org, user, request, exclude_report_id=self._report_id)
                    access = dict(df.attrs["bow_source"])
                # Persist before returning rows (including generated-code stdout).
                # The same lock/session used by agent tool events is the only writer.
                async with _writer_guard(self._writer_lock):
                    await protect_report(self._writer_db, self._report_id, access)
                self._bow_access = merge_access(self._bow_access, access)
                return df
        except TimeoutError as exc:
            raise BowInfrastructureError("BOW query timed out; execution was cancelled. Try again after the database is available.") from exc
        except OperationalError as exc:
            raise BowInfrastructureError("BOW database is busy or unavailable; the query could not complete. Try again shortly.") from exc
        finally:
            self._tasks.discard(task)


async def install_bow_client(db, organization, user, report, clients, *, mode=None, allow_saved=True, writer_lock=None):
    """Used by initial execution and every saved-query runner; no cached grants."""
    from app.core.console_access import resolve_console_scope
    from app.services.bow_source_access import report_access
    from sqlalchemy.ext.asyncio import async_sessionmaker
    saved = await report_access(db, getattr(report, "id", None))
    if (mode or getattr(report, "mode", None)) != "training" and not (saved and allow_saved):
        return
    if user is None or organization is None:
        return
    try:
        await resolve_console_scope(db, organization, user)
    except Exception:
        return
    clients["bow"] = BowClient(async_sessionmaker(db.bind, expire_on_commit=False), asyncio.get_running_loop(), str(organization.id), str(user.id), str(report.id), db, writer_lock)
