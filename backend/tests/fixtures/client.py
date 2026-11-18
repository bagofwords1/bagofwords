"""The ``test_client`` fixture and the event loop its requests run on.

By default every request of a test runs on one event loop (a blocking portal)
that starts at the test's first request and is stopped at the test boundary
(see ``stop_test_loop``), so within a test pooled DB connections are reused
instead of opened per request. Each request still ends the way it did when it
had a loop of its own: the tasks it spawned are cancelled when it returns.

A test that issues requests from more than one thread is marked
``@pytest.mark.per_request_loop``: its requests each get a loop of their own,
exactly as with ``_ReleasingTestClient``. ``BOW_TEST_PER_REQUEST_LOOP=1`` (or
true/yes) restores that for every test, for A/B runs and debugging.
"""
import asyncio
import contextlib
import contextvars
import itertools
import logging
import os
import sys
import threading
import time

import anyio
import pytest
from fastapi.testclient import TestClient
from main import app

logger = logging.getLogger(__name__)


def _logger():
    """This module's logger, re-enabled. alembic's ``fileConfig``
    (``alembic/env.py``, run by every migration-backed test) disables each
    logger that exists then, which would hide the loop's diagnostics for the
    rest of the worker's tests."""
    logger.disabled = False
    return logger


@contextlib.contextmanager
def _releasing_portal(client, open_portal):
    """One request on a loop of its own (``open_portal`` is Starlette's
    ``TestClient._portal_factory``). Close the pooled DB connections that
    loop opened while it is still running — asyncpg can only close a
    connection on its own loop — so they neither leak nor get offered to the
    next request's loop."""
    with open_portal() as portal:
        try:
            yield portal
        finally:
            if client.portal is None:
                from app.settings.database import release_test_connections

                portal.call(release_test_connections)


class _ReleasingTestClient(TestClient):
    """Each request runs on its own event loop (one blocking portal per
    request), its pooled connections released at its end."""

    @contextlib.contextmanager
    def _portal_factory(self):
        with _releasing_portal(self, super()._portal_factory) as portal:
            yield portal


# The request a task belongs to. Set in the calling thread for the duration of
# a request; anyio's portal schedules its call with call_soon_threadsafe,
# which copies the calling thread's context, and tasks inherit their parent's
# context, so everything a request spawns carries its id. Pinned behaviour of
# anyio 4.14.2: BlockingPortal.call -> _spawn_task_from_thread ->
# run_sync_from_thread -> loop.call_soon_threadsafe (no explicit context), and
# the task group's start_soon creates the task inside that callback
# (test_task_spawned_in_a_request_is_cancelled_at_request_end fails if that
# stops holding). Work handed to the loop from threads that did not copy the
# context (run_in_executor callbacks, raw threads) carries none: "ownerless".
_REQUEST_ID = contextvars.ContextVar("bow_test_request_id", default=None)

# How long a request's cleanup keeps cancelling before it gives up and marks
# the loop dirty (stopped and replaced once no request is in flight).
_CANCEL_BOUND_SECONDS = 5.0
# How long the test boundary waits for requests still running in other threads
# before it cancels every task left on the loop, and how long it then waits for
# those cancellations. Neither bounds the loop's final stop (asyncio.Runner
# shutdown): a task that swallows cancellation, or executor work, still delays
# it, exactly as the per-request loop's shutdown always has.
_BOUNDARY_WAIT_SECONDS = 60.0
_BOUNDARY_CANCEL_SECONDS = 5.0


class _LoopStats:
    """Per-process counters: the tests read them, and the problem counters
    (``PROBLEMS``) are reported at session end."""

    PROBLEMS = (
        "bound_hits",
        "dirty_restarts",
        "cleanup_errors",
        "cancel_errors",
        "cross_thread_requests",
        "boundary_wait_timeouts",
    )
    # The problems that fail the test they happened in (see the root
    # conftest's pytest_runtest_teardown). Bound hits and dirty restarts only
    # mean a task swallowed cancellation and its loop was replaced, which the
    # per-request loop's shutdown would have waited out just as silently:
    # they are reported in the summary, not failed.
    FAILING = (
        "cleanup_errors",
        "cancel_errors",
        "cross_thread_requests",
        "boundary_wait_timeouts",
    )
    FIELDS = (
        "portal_starts",
        "owned_cancelled",
        "ownerless_cancelled",
        "ownerless_skipped_in_flight",
    ) + PROBLEMS

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.counts = dict.fromkeys(self.FIELDS, 0)

    def add(self, field, n=1):
        if n:
            with self._lock:
                self.counts[field] += n

    def snapshot(self):
        with self._lock:
            return dict(self.counts)


class _TestLoop:
    """One lazily started blocking portal shared by concurrent requests.

    A single condition guards lazy start, the in-flight requests (tracked per
    portal), stop and restart. Nothing holds it while waiting on a loop that a
    request's cleanup could be running on, so the cleanup (which takes it
    briefly, on the loop thread) cannot deadlock against a stop.

    ``test_thread`` is the current test's thread (set by the root conftest;
    None turns the check off). A request that starts while a request from
    another thread is in flight on the same loop is counted as
    ``cross_thread_requests``, whichever thread came first (the test's or
    another): the test issues requests from more than one thread and must be
    marked ``per_request_loop``. A thread's request alone in flight is not
    counted, so a fixture may still run one request in a thread and end it in
    its teardown.
    """

    def __init__(self, stats):
        self.stats = stats
        self.test_thread = None
        self._cond = threading.Condition()
        self._cm = None
        self._portal = None
        self._in_flight = {}  # request id -> (portal it runs on, its thread)
        self._dirty = False
        self._stopping = set()  # events of detached loops still stopping
        self._ids = itertools.count(1)

    # -- introspection (tests) ------------------------------------------------
    @property
    def portal(self):
        return self._portal

    @property
    def in_flight(self):
        with self._cond:
            return len(self._in_flight)

    def _busy_locked(self, portal):
        return any(p is portal for p, _ in self._in_flight.values())

    # -- lifecycle --------------------------------------------------------------
    def _start_locked(self):
        # SelectorEventLoop explicitly: neither a uvloop policy a uvicorn test
        # installed nor any other policy decides the shared loop's type.
        cm = anyio.from_thread.start_blocking_portal(
            "asyncio", backend_options={"loop_factory": asyncio.SelectorEventLoop}
        )
        portal = cm.__enter__()
        # The portal's own tasks (its main task), never cancelled by a sweep.
        # Stored on the portal so a sweep always uses its own portal's
        # snapshot; relies on anyio 4.14.2's BlockingPortal having no
        # __slots__ (an AttributeError here means that changed).
        portal._bow_own_tasks = frozenset(portal.call(_all_tasks_now))
        portal.call(_count_shutdown_errors, self.stats)
        self._cm, self._portal, self._dirty = cm, portal, False
        self.stats.add("portal_starts")

    def _detach_locked(self):
        """Take the loop out of service; the caller stops it (outside the
        lock) with ``_stop_detached``. Until that is done ``stop`` waits on
        it, whichever thread is stopping it. Call it through ``_detaching``,
        which makes sure that stop happens."""
        cm, portal = self._cm, self._portal
        self._cm = self._portal = None
        self._dirty = False
        stopped = None
        if cm is not None:
            stopped = threading.Event()
            self._stopping.add(stopped)
        return cm, portal, stopped

    @contextlib.contextmanager
    def _detaching(self):
        """Yield ``detach``, to call under the lock in place of
        ``_detach_locked``. Every loop it detached is stopped when the block
        is left, however it is left: otherwise an interrupt in between (a
        KeyboardInterrupt, pytest-timeout's signal-method failure) would leave
        its stop pending and every later ``stop`` waiting on it forever. When
        the block is left by an exception, what is still running on the loop
        (a request stuck in flight) is cancelled rather than waited for."""
        detached = []

        def detach():
            detached.append(self._detach_locked())

        try:
            yield detach
        except BaseException as exc:
            for loop in detached:
                self._stop_detached(*loop, interrupted=exc)
            raise
        for loop in detached:
            self._stop_detached(*loop)

    def _stop_detached(self, cm, portal, stopped, interrupted=None):
        """Release the loop's pooled connections on the loop, then stop it the
        normal way (asyncio.Runner cancels and awaits what is left, closes async
        generators and joins the default executor). With ``interrupted`` (the
        exception that is unwinding the stop), the portal cancels the calls
        still running on it instead of waiting for them."""
        if cm is None:
            return
        from app.settings.database import release_test_connections

        try:
            try:
                portal.call(release_test_connections)
            except Exception as exc:  # the loop is going away regardless
                self.stats.add("cleanup_errors")
                print(f"Warning: release_test_connections failed: {exc!r}", file=sys.stderr)
            if interrupted is None:
                cm.__exit__(None, None, None)
            else:
                cm.__exit__(type(interrupted), interrupted, interrupted.__traceback__)
        finally:
            with self._cond:
                self._stopping.discard(stopped)
            stopped.set()

    def _wait_for_detached_stops(self, timeout=None):
        """Wait for the loops still stopping (at most ``timeout`` seconds in
        all, if given; then warn and return)."""
        with self._cond:
            stopping = list(self._stopping)
        deadline = None if timeout is None else time.monotonic() + timeout
        for stopped in stopping:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not stopped.wait(remaining):
                print(
                    "Warning: a test_client loop another thread is stopping is "
                    f"still stopping after {timeout}s; not waiting for it here",
                    file=sys.stderr,
                )
                return

    def stop(self, if_idle=False):
        """With ``if_idle``, stop only if no request is in flight on the loop
        (else leave it running). Otherwise: wait (at most
        _BOUNDARY_WAIT_SECONDS) for requests in flight on the loop, then
        release its connections and stop it. On a timeout every task left on
        the loop is cancelled first (waiting at most _BOUNDARY_CANCEL_SECONDS);
        the stuck request then fails in its own thread and its cleanup finds
        the loop gone. The timeout is counted (``boundary_wait_timeouts``),
        and the root conftest fails the test it happened in. If the loop is
        replaced while it waits (a dirty restart in another thread, and a new
        request started the next loop), the new one is stopped the same way,
        within the same bound.

        It then waits for any loop still stopping, including one another
        thread detached (a dirty restart), so an engine dispose that follows
        never races a loop still releasing its connections. With ``if_idle``
        (the stop before a test's finalizers, one of which may be what lets
        such a stop finish) it waits for those at most
        _BOUNDARY_WAIT_SECONDS; the full stop after the finalizers waits for
        them in any case."""
        try:
            self._stop(if_idle)
        finally:
            self._wait_for_detached_stops(_BOUNDARY_WAIT_SECONDS if if_idle else None)

    def _stop(self, if_idle):
        deadline = time.monotonic() + _BOUNDARY_WAIT_SECONDS
        while True:
            with self._detaching() as detach:
                with self._cond:
                    portal = self._portal
                    if portal is None or (if_idle and self._busy_locked(portal)):
                        return
                    timed_out = not self._cond.wait_for(
                        lambda: self._portal is not portal or not self._busy_locked(portal),
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                    if self._portal is not portal:  # replaced meanwhile
                        continue
                    detach()
                if timed_out:
                    self.stats.add("boundary_wait_timeouts")
                    print(
                        "Warning: test_client request(s) still running at the test "
                        "boundary; cancelling every task on their loop and stopping it",
                        file=sys.stderr,
                    )
                    try:
                        portal.call(_cancel_all_but_own, portal, self.stats)
                    except Exception as exc:
                        self.stats.add("cleanup_errors")
                        print(f"Warning: boundary cancel failed: {exc!r}", file=sys.stderr)
            return

    # -- per request ----------------------------------------------------------------
    def enter(self):
        """Register a request; return (request id, portal). A dirty loop is
        replaced first, but only while no request is in flight on it."""
        me = threading.get_ident()
        while True:
            with self._detaching() as detach:
                with self._cond:
                    if not (self._dirty and not self._busy_locked(self._portal)):
                        if self.test_thread is not None and any(
                            p is self._portal and thread != me
                            for p, thread in self._in_flight.values()
                        ):
                            self.stats.add("cross_thread_requests")
                            print(
                                "Warning: test_client request while a request "
                                "from another thread is in flight; mark the test "
                                "@pytest.mark.per_request_loop",
                                file=sys.stderr,
                            )
                        if self._portal is None:
                            self._start_locked()
                        rid = next(self._ids)
                        self._in_flight[rid] = (self._portal, me)
                        return rid, self._portal
                    detach()
                    self.stats.add("dirty_restarts")

    def exit(self, rid, portal):
        """End a request: cancel what it left behind, then deregister it. If
        its loop was already detached (a boundary timeout stopped it), there is
        nothing left to clean."""
        bound_hit = False
        try:
            with self._cond:
                current = portal is self._portal
            if current:
                try:
                    bound_hit = portal.call(self._cleanup, rid, portal)
                except Exception as exc:
                    with self._cond:
                        current = portal is self._portal
                    if current:  # not just stopped under us
                        self.stats.add("cleanup_errors")
                        print(
                            f"Warning: test_client request cleanup failed: {exc!r}",
                            file=sys.stderr,
                        )
                        bound_hit = True
                    else:  # the boundary stopped its loop (already counted)
                        _logger().warning(
                            "test_client request %s cleanup failed after its loop "
                            "was stopped under it: %r",
                            rid,
                            exc,
                        )
        finally:
            with self._detaching() as detach:
                with self._cond:
                    del self._in_flight[rid]
                    if portal is self._portal:
                        if bound_hit:
                            self._dirty = True
                        if self._dirty and not self._busy_locked(portal):
                            detach()
                            self.stats.add("dirty_restarts")
                    self._cond.notify_all()

    async def _cleanup(self, rid, portal):
        """Runs on the loop. Cancel the request's own tasks, and, when no other
        request is in flight on this loop, every task no request owns, except
        the portal's own and this one. Sweep again until nothing is left, which
        catches tasks spawned while cancelling. A task that catches
        CancelledError and keeps running is not ended by sweeping again: it
        holds the request up to the bound (_CANCEL_BOUND_SECONDS), and then
        its loop is replaced. Returns True if the bound was hit."""
        me = asyncio.current_task()
        own_tasks = portal._bow_own_tasks
        deadline = time.monotonic() + _CANCEL_BOUND_SECONDS
        skipped = set()  # counted once per request, however many sweeps
        try:
            while True:
                with self._cond:
                    others = {
                        other
                        for other, (p, _) in self._in_flight.items()
                        if other != rid and p is portal
                    }
                    targets = []
                    owned = ownerless = 0
                    for task in asyncio.all_tasks():
                        if task is me or task in own_tasks or task.done():
                            continue
                        owner = task.get_context().get(_REQUEST_ID)
                        if owner == rid:
                            owned += 1
                        elif owner in others:
                            continue
                        elif others:
                            skipped.add(task)
                            continue
                        else:
                            ownerless += 1
                        targets.append(task)
                    # Cancel while holding the lock, so no request can start
                    # (and spawn work this sweep would wrongly take for
                    # ownerless) in between the decision and the cancel.
                    for task in targets:
                        task.cancel()
                self.stats.add("owned_cancelled", owned)
                self.stats.add("ownerless_cancelled", ownerless)
                if not targets:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _, pending = await asyncio.wait(targets, timeout=remaining)
                _retrieve_exceptions(targets, self.stats)
                if pending:
                    break
        finally:
            self.stats.add("ownerless_skipped_in_flight", len(skipped))
        self.stats.add("bound_hits")
        print(
            f"Warning: test_client request {rid} left tasks that did not settle "
            f"within {_CANCEL_BOUND_SECONDS}s; its loop will be replaced",
            file=sys.stderr,
        )
        return True


def _all_tasks_now():
    return set(asyncio.all_tasks())


# What asyncio.Runner reports (to the loop's exception handler) for each task
# that raised when its shutdown cancelled it. Pinned for CPython 3.13
# (asyncio.runners._cancel_all_tasks);
# test_a_task_failing_at_the_loops_final_stop_is_counted fails if it changes.
_SHUTDOWN_REPORT = "unhandled exception during asyncio.run() shutdown"


def _count_shutdown_errors(stats):
    """Runs on the shared loop once, at its start. A task that raises when the
    loop's final stop cancels it never reaches ``_retrieve_exceptions``;
    asyncio.Runner reports it to the loop's exception handler instead. Count
    those (``cancel_errors``) too, and log them with their traceback on this
    module's logger (asyncio's own is disabled once alembic has configured
    logging). They are counted on the loop thread before its stop returns, so
    before the test's teardown compares the counts. Anything else goes to the
    default handler."""

    def handler(loop, context):
        exc = context.get("exception")
        if context.get("message") == _SHUTDOWN_REPORT and exc is not None:
            stats.add("cancel_errors")
            _logger().error(
                "test_client: %s, task %r",
                _SHUTDOWN_REPORT,
                context.get("task"),
                exc_info=exc,
            )
            return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(handler)


def _retrieve_exceptions(tasks, stats):
    """Log the exception of each task that failed while being cancelled (or
    while its cleanup waited), with its traceback, and count it
    (``cancel_errors``). Retrieving it here keeps GC from logging it again as
    "Task exception was never retrieved"; the per-request loop's shutdown
    reported the same exceptions through the loop's exception handler."""
    for task in tasks:
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                stats.add("cancel_errors")
                _logger().warning(
                    "test_client: task %s raised while being cancelled",
                    task.get_name(),
                    exc_info=exc,
                )


async def _cancel_all_but_own(portal, stats):
    me = asyncio.current_task()
    tasks = asyncio.all_tasks() - portal._bow_own_tasks - {me}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=_BOUNDARY_CANCEL_SECONDS)
        _retrieve_exceptions(tasks, stats)


_STATS = _LoopStats()
_LOOP = _TestLoop(_STATS)


def stop_test_loop(if_idle=False):
    """Stop the shared test loop if one is running (test teardown, before an
    engine dispose, session end), and wait for any loop still stopping. Cheap
    when none is. With ``if_idle``, leave it running while a request is in
    flight on it."""
    _LOOP.stop(if_idle=if_idle)


def watch_test_thread(ident):
    """The thread the current test runs in, or None between tests (see
    ``_TestLoop``: requests from other threads are counted)."""
    _LOOP.test_thread = ident


def failing_problems():
    """This process's counts so far of the problems that fail a test."""
    counts = _STATS.snapshot()
    return {name: counts[name] for name in _LoopStats.FAILING}


def loop_problems(stats=None):
    """This process's non-zero problem counters, for the session summary."""
    counts = (stats or _STATS).snapshot()
    return {name: counts[name] for name in _LoopStats.PROBLEMS if counts[name]}


def loop_health_lines(reports, mode):
    """One summary line per worker; ``reports`` maps a worker name to its
    ``loop_problems()``, or to None when the worker sent none; ``mode`` is
    ``loop_mode()``."""
    lines = []
    for worker in sorted(reports, key=lambda w: (len(w), w)):  # gw2 before gw10
        problems = reports[worker]
        if problems is None:
            status = "no report"
        elif not problems:
            status = "no problems"
        else:
            status = " ".join(f"{name}={n}" for name, n in problems.items())
        lines.append(f"test_client loop ({mode}) {worker}: {status}")
    return lines


_KILL_SWITCH_ON = {"1", "true", "yes"}
_KILL_SWITCH_OFF = {"", "0", "false", "no"}


def per_request_loop_enabled():
    """``BOW_TEST_PER_REQUEST_LOOP``; any value but the known ones is an
    error, so a typo never silently runs the other mode."""
    raw = os.environ.get("BOW_TEST_PER_REQUEST_LOOP", "")
    value = raw.strip().lower()
    if value in _KILL_SWITCH_ON:
        return True
    if value in _KILL_SWITCH_OFF:
        return False
    raise ValueError(
        f"BOW_TEST_PER_REQUEST_LOOP={raw!r}: use 1/true/yes for a loop per "
        "request, or 0/false/no (or unset) for the shared per-test loop"
    )


def loop_mode():
    return "per-request" if per_request_loop_enabled() else "shared"


# Set by the root conftest for the duration of a test marked
# ``per_request_loop``: its requests each get a loop of their own.
_per_request_this_test = False


def use_per_request_loop(enabled):
    global _per_request_this_test
    _per_request_this_test = bool(enabled)


class _PersistentLoopTestClient(TestClient):
    """Requests run on the shared per-test loop (``_LOOP``). Each request owns
    the tasks it spawns (``_REQUEST_ID``), and they are cancelled when it ends.
    Pooled connections are released when the loop stops, not per request.
    In a test marked ``per_request_loop`` it acts as ``_ReleasingTestClient``."""

    def __init__(self, *args, test_loop=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._test_loop = test_loop if test_loop is not None else _LOOP

    # Overrides a private method. Pinned behaviour of Starlette 1.3.1:
    # TestClient builds its transport with portal_factory=self._portal_factory
    # and enters it once per HTTP request (and once per websocket session);
    # outside `with client:` its `portal` is None and the base opens a fresh
    # blocking portal each time.
    @contextlib.contextmanager
    def _portal_factory(self):
        if self.portal is not None:  # inside `with client:` (lifespan)
            yield self.portal
            return
        if _per_request_this_test:
            with _releasing_portal(self, super()._portal_factory) as portal:
                yield portal
            return
        rid, portal = self._test_loop.enter()
        token = _REQUEST_ID.set(rid)
        try:
            yield portal
        finally:
            try:
                self._test_loop.exit(rid, portal)
            finally:
                try:
                    _REQUEST_ID.reset(token)
                except ValueError:
                    # The generator was finished in another context than the
                    # one it started in (e.g. closed by GC): clear the id there
                    # rather than fail; the original context is not reachable.
                    _REQUEST_ID.set(None)


def make_test_client(asgi_app=app):
    if per_request_loop_enabled():
        return _ReleasingTestClient(asgi_app)
    return _PersistentLoopTestClient(asgi_app)


@pytest.fixture(scope="session")
def test_client():
    return make_test_client()
