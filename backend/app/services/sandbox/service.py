"""The sandbox service: one persistent sandbox per report.

It owns the states, choosing a backend, quotas, the record of runs and
events, and audit. It reaches infrastructure only through a
:class:`~.drivers.base.SandboxDriver` and the runtime client, so it works the
same on every backend type.

States::

    provisioning ──► ready ⇄ busy
                       │
                       ▼
                     idle ──► stopped ──► deleted
                       ▲         │
                       └─────────┘  (resumed by the next run)
            any state ──► failed    (recreated on the next run)

Every state change writes a ``sandbox_events`` row; creating, stopping,
resuming, deleting, every run and every input file write an audit entry.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets as secrets_module
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence, Union

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import Sandbox, SandboxBackend, SandboxEnvironment, SandboxEvent, SandboxRun
from app.services.sandbox import backends as backend_service
from app.services.sandbox import environments as environment_service
from app.services.sandbox import errors
from app.services.sandbox.audit import audit
from app.services.sandbox.drivers.base import (
    CAP_STOP_RESUME,
    DOMAIN_FILTERED_POLICIES,
    POLICY_ALLOWLIST,
    POLICY_NONE,
    POLICY_REGISTRIES,
    STATUS_MISSING,
    STATUS_STOPPED,
    SandboxDriver,
    SandboxDriverError,
    SandboxHandle,
    SandboxNotFound,
    SandboxSpec,
)
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.settings import SandboxSettings, ensure_instance_id, load_settings

logger = logging.getLogger(__name__)

RUNNING_STATES = backend_service.RUNNING_STATES

# What may follow what. ``failed`` and ``deleted`` are reachable from anywhere.
TRANSITIONS: Dict[str, frozenset] = {
    "provisioning": frozenset({"ready"}),
    "ready": frozenset({"busy", "idle", "stopped"}),
    # A run normally ends with the sandbox ready; it ends stopped when the
    # sandbox itself died under it (out of memory, a host reboot).
    "busy": frozenset({"ready", "stopped"}),
    "idle": frozenset({"busy", "ready", "stopped"}),
    "stopped": frozenset({"ready"}),
    "failed": frozenset(),
    "deleted": frozenset(),
}

KILL_GRACE_SECONDS = 5  # between SIGTERM and SIGKILL when an admin stops a run

WORKSPACE_OUTPUTS = "outputs"
WORKSPACE_INPUTS = "inputs"
RUN_DIRECTORY = ".bow/runs"

# Defaults from the design.
PIDS_LIMIT = 512
MODEL_OUTPUT_TAIL_BYTES = 20 * 1024       # what the model gets back of each stream
STORED_OUTPUT_LIMIT_CHARS = 2 * 1024 * 1024  # what is kept with the run, per stream
OUTPUT_FLUSH_SECONDS = 1.0
OUTPUT_FLUSH_BYTES = 64 * 1024
MAX_ARTIFACTS_PER_RUN = 50
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_RUN_ARTIFACT_BYTES = 500 * 1024 * 1024
PACKAGE_INSTALL_TIMEOUT_SECONDS = 600
SETUP_SCRIPT_TIMEOUT_SECONDS = 900
HEALTH_DEADLINE_SECONDS = 60

_TRUNCATED_MARK = "[... earlier output truncated ...]\n"

# language → (file name, command to run that file, package install command prefix or None)
LANGUAGES: Dict[str, tuple] = {
    "python": ("main.py", ["python", "{file}"],
               ["python", "-m", "pip", "install", "--user", "--no-input", "--disable-pip-version-check"]),
    "node": ("main.js", ["node", "{file}"], ["npm", "install", "--no-audit", "--no-fund"]),
    "go": ("main.go", ["go", "run", "{file}"], ["go", "get"]),
    "bash": ("main.sh", ["bash", "{file}"], None),
}
_PACKAGE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._@/:=<>~!\[\],+-]{0,199}$")
_SAFE_PATH = re.compile(r"^[^\x00]+$")


# ── What callers pass in and get back ────────────────────────────────────


@dataclass
class ReportRef:
    """The report a sandbox belongs to, as much of it as the service needs."""

    id: str
    organization_id: str
    title: str = ""
    owner_id: Optional[str] = None
    # The agents (data sources) on the report, for the agent default environment.
    data_source_ids: Sequence[str] = ()


@dataclass
class InputFile:
    """A report file passed into the sandbox with ``input_file_ids``."""

    file_id: str
    name: str
    data: Union[bytes, AsyncIterator[bytes]]


@dataclass
class RunRequest:
    language: str = "python"
    code: Optional[str] = None
    command: Optional[str] = None
    # Extra files to write before the run: {path relative to /workspace: content}.
    files: Dict[str, Union[str, bytes]] = field(default_factory=dict)
    packages: Sequence[str] = ()
    inputs: Sequence[InputFile] = ()
    timeout_seconds: Optional[int] = None
    environment: Optional[str] = None


@dataclass
class Artifact:
    name: str
    size: int
    file_id: Optional[str] = None
    content_type: Optional[str] = None


@dataclass
class RunOutcome:
    run_id: str
    sandbox_id: str
    status: str
    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    artifacts: List[Artifact] = field(default_factory=list)
    # Files under outputs/ that were not saved, with the reason.
    skipped_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0
    environment: Optional[str] = None
    network_policy: Optional[str] = None
    # The sandbox was created, resumed, or recreated (fresh filesystem) for this run.
    created: bool = False
    resumed: bool = False
    recreated: bool = False


# save_artifact(name, size, chunks) -> the saved file's id (or None when not saved)
ArtifactSaver = Callable[[str, int, AsyncIterator[bytes]], Awaitable[Optional[Artifact]]]
OutputCallback = Callable[[str, bytes], Union[None, Awaitable[None]]]


class _OutputSink:
    """Appends a run's output to its row in batches, on its own short sessions,
    so an admin page on any worker can tail a run in progress."""

    def __init__(self, session_factory, run_id: str):
        self._session_factory = session_factory
        self._run_id = run_id
        self._pending: Dict[str, List[bytes]] = {"stdout": [], "stderr": []}
        self._pending_bytes = 0
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()

    async def add(self, kind: str, chunk: bytes) -> None:
        self._pending[kind].append(chunk)
        self._pending_bytes += len(chunk)
        if self._pending_bytes >= OUTPUT_FLUSH_BYTES or time.monotonic() - self._last_flush >= OUTPUT_FLUSH_SECONDS:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._pending_bytes:
                return
            batch = {kind: b"".join(chunks).decode("utf-8", errors="replace") for kind, chunks in self._pending.items()}
            self._pending = {"stdout": [], "stderr": []}
            self._pending_bytes = 0
            self._last_flush = time.monotonic()
            try:
                async with self._session_factory() as session:
                    run = await session.get(SandboxRun, self._run_id)
                    if run is None:
                        return
                    for kind, text_value in batch.items():
                        if text_value:
                            setattr(run, kind, _capped((getattr(run, kind) or "") + text_value))
                    await session.commit()
            except Exception:  # noqa: BLE001 - losing a batch of stored output must not fail the run
                logger.exception("could not append sandbox run output (run=%s)", self._run_id)


def _capped(text_value: str) -> str:
    if len(text_value) <= STORED_OUTPUT_LIMIT_CHARS:
        return text_value
    return _TRUNCATED_MARK + text_value[-(STORED_OUTPUT_LIMIT_CHARS - len(_TRUNCATED_MARK)):]


def _tail(data: bytes, limit: Optional[int] = None) -> str:
    limit = MODEL_OUTPUT_TAIL_BYTES if limit is None else limit
    return data[-limit:].decode("utf-8", errors="replace")


def _now() -> datetime:
    return datetime.utcnow()


def _clean_path(path: str) -> str:
    cleaned = str(path).strip().lstrip("/")
    if not cleaned or not _SAFE_PATH.match(cleaned) or ".." in cleaned.split("/"):
        raise SandboxError(errors.INVALID, f"'{path}' is not a valid file path inside the sandbox")
    return cleaned


class SandboxService:
    """Stateless; safe to share. ``session_factory`` opens the short sessions
    used to append run output (defaults to the app's)."""

    def __init__(self, session_factory=None, access=None):
        self._session_factory = session_factory
        self._access = access

    # ── plumbing ──

    def _sessions(self):
        if self._session_factory is None:
            from app.dependencies import async_session_maker

            self._session_factory = async_session_maker
        return self._session_factory

    @property
    def access(self):
        if self._access is None:
            from app.services.sandbox.access import SandboxAccess

            self._access = SandboxAccess()
        return self._access

    async def _event(self, db: AsyncSession, sandbox_id: str, type_: str, actor_id: Optional[str],
                     details: Optional[Dict[str, Any]] = None) -> None:
        db.add(SandboxEvent(sandbox_id=sandbox_id, type=type_, actor_user_id=actor_id, details=details or {}))

    async def _set_state(self, db: AsyncSession, sandbox: Sandbox, state: str, actor_id: Optional[str], *,
                         event: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
        """Move to ``state`` and record it. Every state change writes an event."""
        previous = sandbox.state
        if state == previous:
            return
        if state not in ("failed", "deleted") and state not in TRANSITIONS.get(previous, frozenset()):
            raise SandboxError(errors.INVALID_STATE, f"A sandbox cannot go from {previous} to {state}", status=409)
        sandbox.state = state
        if state == "stopped":
            sandbox.stopped_at = _now()
        elif state == "deleted":
            sandbox.deleted_at = _now()
            sandbox.token = None
        if state in ("ready", "busy"):
            sandbox.stopped_at = None
        payload = {"from": previous, "to": state}
        payload.update(details or {})
        await self._event(db, sandbox.id, event or state, actor_id, payload)

    async def mark_idle(self, db: AsyncSession, sandbox: Sandbox) -> bool:
        """ready → idle, only if it is still ready: a run may have claimed it
        since the caller read it. Returns whether it was marked."""
        marked = await db.execute(
            update(Sandbox).where(Sandbox.id == sandbox.id, Sandbox.state == "ready").values(state="idle")
        )
        if marked.rowcount == 1:
            await self._event(db, sandbox.id, "idle", None, {"from": "ready", "to": "idle"})
        await db.commit()
        await db.refresh(sandbox)
        return marked.rowcount == 1

    async def _driver(self, db: AsyncSession, sandbox: Sandbox) -> tuple:
        backend = await db.get(SandboxBackend, sandbox.backend_id) if sandbox.backend_id else None
        if backend is None:
            raise SandboxError(errors.UNAVAILABLE, "The backend this sandbox ran on no longer exists.", status=409)
        return backend, await backend_service.build_driver(db, backend)

    @staticmethod
    async def _title(db: AsyncSession, sandbox: Sandbox, title: Optional[str] = None) -> str:
        """The report's name for ``details.title`` (audit search finds entries by it)."""
        if title:
            return title
        from app.models.report import Report

        found = (await db.execute(select(Report.title).where(Report.id == sandbox.report_id))).scalar_one_or_none()
        return found or sandbox.report_id

    @staticmethod
    def _handle(sandbox: Sandbox) -> SandboxHandle:
        return SandboxHandle(sandbox_id=sandbox.id, ref=dict(sandbox.driver_ref or {}))

    def _runtime(self, driver: SandboxDriver, sandbox: Sandbox):
        import dataclasses

        from app.services.sandbox.runtime_client import SandboxRuntimeClient

        endpoint = dataclasses.replace(driver.endpoint(self._handle(sandbox)), token=sandbox.token)
        return SandboxRuntimeClient(endpoint)

    # ── lookups ──

    async def get_for_report(self, db: AsyncSession, report_id: str) -> Optional[Sandbox]:
        return (await db.execute(
            select(Sandbox).where(Sandbox.report_id == str(report_id), Sandbox.deleted_at.is_(None))
        )).scalar_one_or_none()

    async def get(self, db: AsyncSession, organization_id: str, sandbox_id: str) -> Sandbox:
        sandbox = (await db.execute(
            select(Sandbox).where(Sandbox.id == str(sandbox_id), Sandbox.organization_id == str(organization_id))
        )).scalar_one_or_none()
        if sandbox is None:
            raise SandboxError(errors.NOT_FOUND, "Sandbox not found", status=404)
        return sandbox

    async def recent_runs(self, db: AsyncSession, sandbox_id: str, limit: int = 5) -> List[SandboxRun]:
        rows = await db.execute(
            select(SandboxRun).where(SandboxRun.sandbox_id == sandbox_id)
            .order_by(SandboxRun.started_at.desc()).limit(limit)
        )
        return list(rows.scalars().all())

    # ── quotas and placement ──

    async def _check_quotas(self, db: AsyncSession, organization_id: str, actor_id: Optional[str],
                            settings: SandboxSettings) -> None:
        running = select(func.count()).select_from(Sandbox).where(
            Sandbox.organization_id == str(organization_id), Sandbox.deleted_at.is_(None),
            Sandbox.state.in_(RUNNING_STATES),
        )
        if actor_id:
            mine = (await db.execute(running.where(Sandbox.user_id == str(actor_id)))).scalar() or 0
            if mine >= settings.max_running_per_user:
                raise SandboxError(
                    errors.QUOTA_USER,
                    f"You already have {mine} running sandboxes, which is the limit. "
                    "Stop or delete one, or wait for one to stop when idle.", status=429,
                )
        if settings.max_running_total is not None:
            total = (await db.execute(running)).scalar() or 0
            if total >= settings.max_running_total:
                raise SandboxError(
                    errors.QUOTA_TOTAL, "The limit of running sandboxes for this organization is reached. "
                    "Try again when one has stopped.", status=429,
                )

    async def choose_backend(self, db: AsyncSession, organization_id: str, env: SandboxEnvironment) -> SandboxBackend:
        """The least-loaded enabled, healthy backend that fits the environment."""
        candidates = [
            b for b in await backend_service.list_backends(db, organization_id)
            if b.enabled and b.bootstrap_status == "ready" and b.status != "unreachable"
        ]
        if not candidates:
            raise SandboxError(errors.NO_BACKEND, "No sandbox backend is enabled and healthy.", status=503)
        fitting = [b for b in candidates if environment_service.backend_fits(b, env)]
        if not fitting:
            raise SandboxError(
                errors.NO_BACKEND,
                f"No sandbox backend can run the environment '{env.name}' (labels or required features do not match).",
                status=503,
            )
        counts = await backend_service.running_counts(db, organization_id)
        free = [b for b in fitting if counts.get(b.id, 0) < (b.capacity or 0)]
        if not free:
            raise SandboxError(
                errors.NO_BACKEND, "Every sandbox backend that fits is at capacity. Try again when a sandbox has stopped.",
                status=503,
            )
        return min(free, key=lambda b: (counts.get(b.id, 0) / max(b.capacity, 1), counts.get(b.id, 0), b.name))

    # ── create ──

    def _spec(self, sandbox: Sandbox, env: SandboxEnvironment, settings: SandboxSettings, token: str) -> SandboxSpec:
        if env.network_policy == POLICY_REGISTRIES:
            domains = list(settings.registry_domains)
        elif env.network_policy == POLICY_ALLOWLIST:
            domains = list(env.allowed_domains or [])
        else:
            domains = []
        return SandboxSpec(
            sandbox_id=sandbox.id,
            image=environment_service.pinned_image(env),
            token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            cpus=float(env.cpus), memory_mb=int(env.memory_mb), disk_mb=int(env.disk_mb), pids_limit=PIDS_LIMIT,
            env=dict(env.env_vars or {}), secrets=dict(env.secrets or {}),
            network_policy=env.network_policy, allowed_domains=domains,
            require_strong_isolation=bool(env.require_strong_isolation), ports=list(env.ports or []),
            registry_username=env.registry_username, registry_password=env.registry_password,
            labels={"report": sandbox.report_id},
        )

    async def ensure(self, db: AsyncSession, report: ReportRef, actor_id: Optional[str], *,
                     environment: Optional[str] = None) -> tuple:
        """The report's sandbox, created if there is none. Returns ``(sandbox, flags)``
        where flags has ``created`` / ``recreated``."""
        settings = await load_settings(db, report.organization_id)
        if not settings.enabled:
            raise errors.disabled()
        await self.access.require_use(db, report.organization_id, actor_id, report)

        flags = {"created": False, "recreated": False}
        existing = await self.get_for_report(db, report.id)
        if existing is not None and existing.state == "failed":
            # Could not start, or lost: start over with a fresh filesystem.
            await self._remove(db, existing, actor_id, reason="recreated")
            flags["recreated"] = True
            existing = None
        if existing is not None:
            await self.access.require_environment(db, report.organization_id, actor_id, existing.environment_id)
            return existing, flags

        usable = await self.access.usable_environments(db, report.organization_id, actor_id)
        env = environment_service.choose_environment(
            usable, requested=environment,
            agent_default_id=await environment_service.agent_default_environment_id(db, report.data_source_ids),
            org_default_id=settings.default_environment_id,
        )
        await self._check_quotas(db, report.organization_id, actor_id, settings)
        backend = await self.choose_backend(db, report.organization_id, env)

        token = secrets_module.token_urlsafe(32)
        sandbox = Sandbox(
            organization_id=str(report.organization_id), report_id=str(report.id), user_id=actor_id,
            backend_id=backend.id, environment_id=env.id, image_digest=env.image_digest, state="provisioning",
            token=token, expires_at=_now() + timedelta(seconds=settings.max_lifetime_seconds),
            limits={
                "cpus": env.cpus, "memory_mb": env.memory_mb, "disk_mb": env.disk_mb,
                "run_timeout_seconds": env.run_timeout_seconds, "network_policy": env.network_policy,
                "environment": env.name,
            },
        )
        db.add(sandbox)
        try:
            await db.commit()
        except IntegrityError:
            # Another worker created the report's sandbox first: use that one.
            await db.rollback()
            winner = await self.get_for_report(db, report.id)
            if winner is None:
                raise
            return winner, flags

        await self._provision(db, sandbox, env, backend, settings, token, report, actor_id)
        flags["created"] = True
        return sandbox, flags

    async def _provision(self, db: AsyncSession, sandbox: Sandbox, env: SandboxEnvironment, backend: SandboxBackend,
                         settings: SandboxSettings, token: str, report: ReportRef, actor_id: Optional[str]) -> None:
        driver = await backend_service.build_driver(db, backend)
        try:
            try:
                handle = await driver.create(self._spec(sandbox, env, settings, token))
                sandbox.driver_ref = dict(handle.ref)
                async with self._runtime(driver, sandbox) as runtime:
                    await runtime.wait_healthy(HEALTH_DEADLINE_SECONDS)
                    await runtime.make_dir(WORKSPACE_OUTPUTS)
                    await runtime.make_dir(WORKSPACE_INPUTS)
                    if env.setup_script:
                        await self._run_setup_script(db, runtime, sandbox, env, actor_id)
            except (SandboxDriverError, SandboxError) as exc:
                await self._fail(db, sandbox, actor_id, getattr(exc, "message", str(exc)), driver=driver)
                raise SandboxError(
                    errors.UNAVAILABLE, f"The sandbox could not be started: {getattr(exc, 'message', exc)}", status=503,
                ) from exc
            except Exception as exc:  # noqa: BLE001 - the runtime client's errors and anything unexpected
                await self._fail(db, sandbox, actor_id, _reason(exc), driver=driver)
                raise SandboxError(errors.UNAVAILABLE, f"The sandbox could not be started: {_reason(exc)}", status=503) from exc
        finally:
            await driver.close()

        sandbox.last_seen_at = sandbox.last_used_at = _now()
        await self._set_state(db, sandbox, "ready", actor_id, event="created", details={
            "environment": env.name, "backend": backend.name, "network_policy": env.network_policy,
        })
        await audit(db, report.organization_id, "sandbox.created", actor_id, "sandbox", sandbox.id, {
            "title": report.title or report.id, "report_id": report.id, "environment": env.name,
            "backend": backend.name, "network_policy": env.network_policy,
        })
        await db.commit()

    async def _run_setup_script(self, db: AsyncSession, runtime, sandbox: Sandbox, env: SandboxEnvironment,
                                actor_id: Optional[str]) -> None:
        await runtime.write_file(".bow/setup.sh", env.setup_script.encode("utf-8"))
        result = await runtime.run(["sh", ".bow/setup.sh"], timeout=SETUP_SCRIPT_TIMEOUT_SECONDS)
        await self._event(db, sandbox.id, "setup_script", actor_id, {
            "status": result.status, "exit_code": result.exit_code,
            "output": _tail(result.stdout + result.stderr, 4000),
        })
        if not result.ok:
            raise SandboxError(
                errors.UNAVAILABLE,
                f"The environment's setup script failed (exit code {result.exit_code}).", status=503,
            )

    async def _fail(self, db: AsyncSession, sandbox: Sandbox, actor_id: Optional[str], reason: str, *,
                    driver: Optional[SandboxDriver] = None) -> None:
        """Mark failed and remove what was created. A failed sandbox is recreated on the next run."""
        if driver is not None and sandbox.driver_ref:
            try:
                await driver.delete(self._handle(sandbox))
            except Exception:  # noqa: BLE001 - reconcile removes whatever is left
                logger.warning("could not remove failed sandbox %s from its backend", sandbox.id)
        sandbox.error = reason[:1000]
        await self._set_state(db, sandbox, "failed", actor_id, details={"reason": reason[:500]})
        await db.commit()

    # ── resume ──

    async def _ensure_running(self, db: AsyncSession, sandbox: Sandbox, report: ReportRef,
                              actor_id: Optional[str]) -> bool:
        """Make sure the sandbox answers; resume it if it was stopped. Returns
        whether it was resumed. Raises ``_Lost`` when it is gone from its backend."""
        # Another call may be creating this sandbox or running in it right now
        # (parallel tool calls, another worker). Its daemon may not answer yet,
        # or answer slowly under load: that is not "lost", and nothing here may
        # touch it. Only a sandbox at rest is health-checked, resumed or recreated.
        await db.refresh(sandbox)
        if sandbox.state == "provisioning":
            raise errors.starting()
        if sandbox.state == "busy":
            raise errors.busy()
        backend, driver = await self._driver(db, sandbox)
        handle = self._handle(sandbox)
        resumed = False
        try:
            if sandbox.state != "stopped":
                async with self._runtime(driver, sandbox) as runtime:
                    if await runtime.health():
                        sandbox.last_seen_at = _now()
                        return False
                status = await driver.status(handle)
                # Re-read: a run may have claimed it while the checks above were in flight.
                await db.refresh(sandbox)
                if sandbox.state == "busy":
                    raise errors.busy()
                if status.state == STATUS_MISSING:
                    raise _Lost("it is no longer on its backend")
                if status.state != STATUS_STOPPED:
                    raise _Lost("it is running but does not answer")
                # Stopped behind our back (a reboot, an admin): record it, then resume.
                await self._set_state(db, sandbox, "stopped", None, details={"reason": "found stopped"})

            settings = await load_settings(db, sandbox.organization_id)
            await self._check_quotas(db, sandbox.organization_id, actor_id, settings)
            counts = await backend_service.running_counts(db, sandbox.organization_id)
            if counts.get(backend.id, 0) >= (backend.capacity or 0):
                raise SandboxError(
                    errors.NO_BACKEND, "The sandbox's backend is at capacity. Try again when a sandbox has stopped.",
                    status=503,
                )
            try:
                await driver.start(handle)
            except SandboxNotFound as exc:
                raise _Lost("it is no longer on its backend") from exc
            async with self._runtime(driver, sandbox) as runtime:
                await runtime.wait_healthy(HEALTH_DEADLINE_SECONDS)
            sandbox.last_seen_at = _now()
            await self._set_state(db, sandbox, "ready", actor_id, event="resumed")
            await audit(db, sandbox.organization_id, "sandbox.resumed", actor_id, "sandbox", sandbox.id,
                        {"title": report.title or report.id, "report_id": report.id})
            await db.commit()
            resumed = True
        except SandboxDriverError as exc:
            raise SandboxError(errors.UNAVAILABLE, f"The sandbox could not be reached: {exc.message}", status=503) from exc
        finally:
            await driver.close()
        return resumed

    # ── run ──

    async def run(
        self,
        db: AsyncSession,
        report: ReportRef,
        actor_id: Optional[str],
        request: RunRequest,
        *,
        on_output: Optional[OutputCallback] = None,
        on_progress: Optional[Callable[[str], Union[None, Awaitable[None]]]] = None,
        cancel: Optional[asyncio.Event] = None,
        save_artifact: Optional[ArtifactSaver] = None,
        completion_id: Optional[str] = None,
        tool_execution_id: Optional[str] = None,
    ) -> RunOutcome:
        """Run code in the report's sandbox: create or resume it, upload the
        code and files, install packages, stream the output, collect artifacts."""
        plan = _plan(request)

        async def progress(stage: str) -> None:
            if on_progress is not None:
                outcome = on_progress(stage)
                if asyncio.iscoroutine(outcome):
                    await outcome

        sandbox, flags = await self.ensure(db, report, actor_id, environment=request.environment)
        if flags["created"]:
            await progress("created")
        resumed = False
        try:
            resumed = await self._ensure_running(db, sandbox, report, actor_id)
        except _Lost as lost:
            # The sandbox is gone: recreate it. The caller is told the filesystem is fresh.
            await self._fail(db, sandbox, None, f"lost: {lost}")
            sandbox, _ = await self.ensure(db, report, actor_id, environment=request.environment)
            flags = {"created": False, "recreated": True}
        if resumed:
            await progress("resumed")

        env = await db.get(SandboxEnvironment, sandbox.environment_id) if sandbox.environment_id else None
        policy = (sandbox.limits or {}).get("network_policy") or (env.network_policy if env else POLICY_NONE)
        if plan.packages and policy == POLICY_NONE:
            raise SandboxError(
                errors.PACKAGES_NEED_NETWORK,
                "This sandbox has no internet access, so it cannot install packages. Use what is already "
                "installed, or ask an admin for an environment that allows the package registries.",
            )

        # One run at a time: claim the sandbox atomically, across workers.
        claimed = await db.execute(
            update(Sandbox).where(Sandbox.id == sandbox.id, Sandbox.state.in_(("ready", "idle")))
            .values(state="busy", last_used_at=_now(), stopped_at=None)
        )
        if claimed.rowcount != 1:
            await db.rollback()
            raise errors.busy()
        limit = int((sandbox.limits or {}).get("run_timeout_seconds") or (env.run_timeout_seconds if env else 600))
        timeout = min(int(request.timeout_seconds), limit) if request.timeout_seconds else limit
        run = SandboxRun(
            sandbox_id=sandbox.id, organization_id=str(report.organization_id), report_id=str(report.id),
            user_id=actor_id, completion_id=completion_id, tool_execution_id=tool_execution_id,
            language=request.language, command=plan.command, code=request.code,
            code_sha256=hashlib.sha256((request.code or request.command or "").encode("utf-8")).hexdigest(),
            status="running", input_file_ids=[i.file_id for i in request.inputs], artifact_file_ids=[],
            started_at=_now(),
        )
        db.add(run)
        await self._event(db, sandbox.id, "busy", actor_id, {"from": "ready", "to": "busy"})
        await db.commit()
        await db.refresh(sandbox)
        await self._event(db, sandbox.id, "run_started", actor_id, {"run_id": run.id, "language": request.language})
        await db.commit()

        outcome = RunOutcome(
            run_id=run.id, sandbox_id=sandbox.id, status="failed", exit_code=None,
            environment=(sandbox.limits or {}).get("environment"), network_policy=policy,
            created=flags["created"], resumed=resumed, recreated=flags["recreated"],
        )
        try:
            return await self._run_claimed(
                db, report, actor_id, request, plan, sandbox, run, outcome, policy, timeout,
                progress=progress, on_output=on_output, cancel=cancel, save_artifact=save_artifact,
            )
        except asyncio.CancelledError:
            # The caller's task was cancelled (the turn was torn down, the client
            # went away). Nothing after this point in the cancelled task runs, so
            # put the sandbox back in a fresh session, shielded from the same
            # cancellation; otherwise it stays busy until the lifecycle job
            # recovers it, and every run on the report is refused meanwhile.
            await asyncio.shield(self._release_cancelled(sandbox.id, run.id, actor_id))
            raise

    async def _release_cancelled(self, sandbox_id: str, run_id: str, actor_id: Optional[str]) -> None:
        try:
            async with self._sessions()() as db:
                now = _now()
                finished = await db.execute(
                    update(SandboxRun).where(SandboxRun.id == run_id, SandboxRun.status == "running")
                    .values(status="cancelled", finished_at=now)
                )
                released = await db.execute(
                    update(Sandbox).where(Sandbox.id == sandbox_id, Sandbox.state == "busy")
                    .values(state="ready", last_used_at=now)
                )
                if finished.rowcount:
                    await self._event(db, sandbox_id, "run_finished", actor_id, {
                        "run_id": run_id, "status": "cancelled", "error": "the run was cancelled",
                    })
                if released.rowcount:
                    await self._event(db, sandbox_id, "ready", actor_id, {
                        "from": "busy", "to": "ready", "reason": "the run was cancelled",
                    })
                await db.commit()
        except Exception:  # noqa: BLE001 - the lifecycle job recovers an abandoned run as a last resort
            logger.exception("could not release sandbox %s after a cancelled run", sandbox_id)

    async def _run_claimed(self, db: AsyncSession, report: ReportRef, actor_id: Optional[str], request: RunRequest,
                           plan: "_Plan", sandbox: Sandbox, run: SandboxRun, outcome: RunOutcome, policy: str,
                           timeout: int, *, progress, on_output, cancel, save_artifact) -> RunOutcome:
        """The part of a run that happens while the sandbox is claimed (busy)."""
        started = time.monotonic()
        sink = _OutputSink(self._sessions(), run.id)
        error: Optional[SandboxError] = None
        lost_reason: Optional[str] = None
        try:
            _, driver = await self._driver(db, sandbox)
            try:
                async with self._runtime(driver, sandbox) as runtime:
                    await self._upload(db, runtime, report, sandbox, actor_id, request, plan, run.id)
                    if plan.packages:
                        await progress("installing_packages")
                        await self._install_packages(db, runtime, sandbox, actor_id, request, plan, policy)
                    before = await _snapshot(runtime)
                    run_began = await _sandbox_clock(runtime)

                    async def forward(kind: str, chunk: bytes) -> None:
                        await sink.add(kind, chunk)
                        if on_output is not None:
                            result = on_output(kind, chunk)
                            if asyncio.iscoroutine(result):
                                await result

                    async def started_process(process_id: int) -> None:
                        # Recorded so that an admin's "stop this run" works from any
                        # worker: the process is addressed by its id in the sandbox.
                        async with self._sessions()() as side:
                            await self._event(side, sandbox.id, "process_started", actor_id,
                                              {"run_id": run.id, "process_id": int(process_id)})
                            await side.commit()

                    await progress("running")
                    result = await runtime.run(plan.command, timeout=timeout, on_output=forward, cancel=cancel,
                                               on_started=started_process)
                    await sink.flush()
                    outcome.status, outcome.exit_code = result.status, result.exit_code
                    outcome.stdout, outcome.stderr = _tail(result.stdout), _tail(result.stderr)
                    outcome.stdout_truncated = result.stdout_bytes > MODEL_OUTPUT_TAIL_BYTES
                    outcome.stderr_truncated = result.stderr_bytes > MODEL_OUTPUT_TAIL_BYTES
                    await progress("collecting_artifacts")
                    await self._collect_artifacts(runtime, before, outcome, save_artifact, run_began)
            finally:
                await driver.close()
        except SandboxError as exc:
            error = exc
        except SandboxDriverError as exc:
            error = SandboxError(errors.UNAVAILABLE, f"The sandbox could not be reached: {exc.message}", status=503)
            lost_reason = exc.message
        except Exception as exc:  # noqa: BLE001 - runtime client errors
            from app.services.sandbox.runtime_client import SandboxRuntimeError

            if isinstance(exc, SandboxRuntimeError) and exc.code in ("unreachable", "unauthenticated"):
                error = SandboxError(errors.UNAVAILABLE, f"The sandbox stopped answering: {exc.message}", status=503)
                lost_reason = exc.message
            elif isinstance(exc, SandboxRuntimeError):
                error = SandboxError(errors.INVALID, f"The command could not be run: {exc.message}")
            else:
                logger.exception("sandbox run crashed (run=%s)", run.id)
                error = SandboxError(errors.UNAVAILABLE, f"The run failed unexpectedly: {type(exc).__name__}", status=500)
        finally:
            await sink.flush()

        died_stopped = False
        if lost_reason is not None:
            # The sandbox stopped answering mid-run. If its container merely
            # stopped (typically: it ran out of memory), it resumes on the next
            # run with its files; only a sandbox that is really gone is recreated.
            died_stopped = await self._stopped_on_backend(db, sandbox)
            if died_stopped:
                memory = (sandbox.limits or {}).get("memory_mb")
                error = SandboxError(
                    errors.UNAVAILABLE,
                    "The sandbox stopped while the code was running, most likely because it used more memory "
                    f"than its limit{f' ({memory} MB)' if memory else ''}. It restarts on the next run and "
                    "keeps its files; use less memory or process the data in smaller pieces.", status=503,
                )

        killed_by_admin = await self._was_killed(db, sandbox.id, run.id)
        if killed_by_admin and not died_stopped and lost_reason is None:
            outcome.status = "cancelled"
            error = SandboxError(errors.INVALID_STATE, "The run was stopped by an admin.", status=409)
        outcome.duration_seconds = round(time.monotonic() - started, 3)
        run.status = "cancelled" if killed_by_admin else (outcome.status if error is None else "failed")
        run.exit_code = outcome.exit_code
        run.finished_at = _now()
        run.artifact_file_ids = [a.file_id for a in outcome.artifacts if a.file_id]
        await self._event(db, sandbox.id, "run_finished", actor_id, {
            "run_id": run.id, "status": run.status, "exit_code": run.exit_code,
            "duration_seconds": outcome.duration_seconds, "artifacts": len(outcome.artifacts),
            "error": error.message[:300] if error else None,
        })
        await audit(db, report.organization_id, "sandbox.run", actor_id, "sandbox", sandbox.id, {
            "title": report.title or report.id, "report_id": report.id, "run_id": run.id,
            "environment": outcome.environment, "language": request.language, "code_sha256": run.code_sha256,
            "status": run.status, "exit_code": run.exit_code, "duration_seconds": outcome.duration_seconds,
            "files_produced": [a.name for a in outcome.artifacts][:MAX_ARTIFACTS_PER_RUN],
            "input_file_ids": list(run.input_file_ids or []),
        }, outcome="success" if (error is None and run.status == "succeeded") else "failure")
        await db.refresh(sandbox, attribute_names=["state"])
        if sandbox.state != "busy":
            # An admin deleted it while the run was ending: the record of the
            # run is kept, the sandbox is left as it now is.
            await db.commit()
            if error is not None:
                raise error
            return outcome
        if died_stopped:
            await self._set_state(db, sandbox, "stopped", None, details={"reason": "stopped during a run"})
        elif lost_reason is not None:
            sandbox.error = lost_reason[:1000]
            await self._set_state(db, sandbox, "failed", None, details={"reason": lost_reason[:500]})
        else:
            sandbox.last_used_at = sandbox.last_seen_at = _now()
            await self._set_state(db, sandbox, "ready", actor_id)
        await db.commit()
        if error is not None:
            raise error
        return outcome

    @staticmethod
    async def _was_killed(db: AsyncSession, sandbox_id: str, run_id: str) -> bool:
        rows = await db.execute(
            select(SandboxEvent).where(SandboxEvent.sandbox_id == sandbox_id, SandboxEvent.type == "run_killed")
        )
        return any((e.details or {}).get("run_id") == run_id for e in rows.scalars().all())

    # ── what an admin can do to one sandbox ──

    async def list_live(self, db: AsyncSession, organization_id: str) -> List[Sandbox]:
        """Every sandbox of the organization that still exists, newest first."""
        rows = await db.execute(
            select(Sandbox).where(Sandbox.organization_id == str(organization_id), Sandbox.deleted_at.is_(None),
                                  Sandbox.state != "deleted")
            .order_by(Sandbox.created_at.desc())
        )
        return list(rows.scalars().all())

    async def running_run(self, db: AsyncSession, sandbox_id: str) -> Optional[SandboxRun]:
        rows = await db.execute(
            select(SandboxRun).where(SandboxRun.sandbox_id == sandbox_id, SandboxRun.status == "running")
            .order_by(SandboxRun.started_at.desc())
        )
        return rows.scalars().first()

    async def kill_run(self, db: AsyncSession, sandbox: Sandbox, actor_id: Optional[str], *,
                       title: Optional[str] = None) -> SandboxRun:
        """Stop the run in progress: SIGTERM, then SIGKILL after a grace period.

        Works from any worker. The worker that started the run sees its process
        end, records the run as cancelled and gives the sandbox back; nothing
        here touches the sandbox's state."""
        run = await self.running_run(db, sandbox.id)
        if run is None or sandbox.state != "busy":
            raise SandboxError(errors.INVALID_STATE, "This sandbox is not running anything.", status=409)
        events = await db.execute(
            select(SandboxEvent).where(SandboxEvent.sandbox_id == sandbox.id, SandboxEvent.type == "process_started")
        )
        process_id = next(
            ((e.details or {}).get("process_id") for e in events.scalars().all()
             if (e.details or {}).get("run_id") == run.id), None,
        )
        if process_id is None:
            raise SandboxError(
                errors.INVALID_STATE,
                "The run has not started its program yet (it is uploading files or installing packages). "
                "Try again in a moment.", status=409,
            )
        # Recorded first: the worker running it reads this to call the run cancelled.
        await self._event(db, sandbox.id, "run_killed", actor_id, {"run_id": run.id})
        await audit(db, sandbox.organization_id, "sandbox.run_killed", actor_id, "sandbox", sandbox.id,
                    {"title": await self._title(db, sandbox, title), "report_id": sandbox.report_id, "run_id": run.id})
        await db.commit()
        _, driver = await self._driver(db, sandbox)
        try:
            async with self._runtime(driver, sandbox) as runtime:
                await runtime.send_signal(int(process_id), "SIGTERM")
                for _ in range(int(KILL_GRACE_SECONDS * 2)):
                    await asyncio.sleep(0.5)
                    await db.refresh(run)
                    if run.status != "running":
                        return run
                await runtime.send_signal(int(process_id), "SIGKILL")
        except SandboxDriverError as exc:
            raise SandboxError(errors.UNAVAILABLE, f"The sandbox could not be reached: {exc.message}", status=503) from exc
        except Exception as exc:  # noqa: BLE001 - runtime client errors
            raise SandboxError(errors.UNAVAILABLE, f"The run could not be stopped: {_reason(exc)}", status=503) from exc
        finally:
            await driver.close()
        return run

    async def stats(self, db: AsyncSession, sandboxes: Sequence[Sandbox]) -> Dict[str, Any]:
        """Live CPU, memory and disk per sandbox id, for the ones that are
        running on a reachable backend that can report it. Best effort: a
        backend that does not answer simply contributes nothing."""
        out: Dict[str, Any] = {}
        by_backend: Dict[str, List[Sandbox]] = {}
        for sandbox in sandboxes:
            if sandbox.backend_id and sandbox.driver_ref and sandbox.state in ("ready", "busy", "idle"):
                by_backend.setdefault(sandbox.backend_id, []).append(sandbox)
        for backend_id, group in by_backend.items():
            try:
                backend = await db.get(SandboxBackend, backend_id)
                if backend is None or backend.deleted_at is not None or backend.status == "unreachable":
                    continue
                driver = await backend_service.build_driver(db, backend)
                try:
                    out.update(await driver.stats([self._handle(s) for s in group]))
                finally:
                    await driver.close()
            except Exception:  # noqa: BLE001 - usage numbers are optional
                logger.debug("could not read sandbox stats from backend %s", backend_id, exc_info=True)
        return out

    async def _stopped_on_backend(self, db: AsyncSession, sandbox: Sandbox) -> bool:
        """Whether the sandbox still exists on its backend but is not running."""
        try:
            _, driver = await self._driver(db, sandbox)
            try:
                return (await driver.status(self._handle(sandbox))).state == STATUS_STOPPED
            finally:
                await driver.close()
        except (SandboxDriverError, SandboxError):
            return False

    async def _upload(self, db: AsyncSession, runtime, report: ReportRef, sandbox: Sandbox, actor_id: Optional[str],
                      request: RunRequest, plan: "_Plan", run_id: str) -> None:
        for path, content in plan.files.items():
            await runtime.write_file(path, content if isinstance(content, bytes) else str(content).encode("utf-8"))
        if plan.code_path:
            await runtime.write_file(plan.code_path, (request.code or "").encode("utf-8"))
        for item in request.inputs:
            # Only the base name is used: an input always lands directly in inputs/.
            name = _clean_path(str(item.name).replace("\\", "/").rsplit("/", 1)[-1])
            await runtime.write_file(f"{WORKSPACE_INPUTS}/{name}", item.data)
            await audit(db, report.organization_id, "sandbox.input_file", actor_id, "sandbox", sandbox.id, {
                "title": report.title or report.id, "report_id": report.id, "run_id": run_id,
                "file_id": item.file_id, "file_name": name,
            })
        if request.inputs:
            await db.commit()

    async def _install_packages(self, db: AsyncSession, runtime, sandbox: Sandbox, actor_id: Optional[str],
                                request: RunRequest, plan: "_Plan", policy: str) -> None:
        result = await runtime.run(plan.install_command, timeout=PACKAGE_INSTALL_TIMEOUT_SECONDS)
        await self._event(db, sandbox.id, "packages_installed", actor_id, {
            "packages": list(plan.packages), "language": request.language,
            "status": result.status, "exit_code": result.exit_code,
        })
        await db.commit()
        if not result.ok:
            hint = ""
            if policy in DOMAIN_FILTERED_POLICIES:
                hint = (" This sandbox can only reach the package registries on its environment's list; "
                        "a package that needs another host cannot be installed.")
            raise SandboxError(
                errors.PACKAGE_INSTALL_FAILED,
                f"Installing {', '.join(plan.packages)} failed (exit code {result.exit_code}).{hint}\n"
                + _tail(result.stderr or result.stdout, 2000),
            )

    async def _collect_artifacts(self, runtime, before: Dict[str, tuple], outcome: RunOutcome,
                                 save_artifact: Optional[ArtifactSaver], run_began: Optional[str] = None) -> None:
        """Save what the run wrote under outputs/: new files and changed ones.

        A file counts as changed when its size or modification time differs,
        or when it was modified at or after the moment the run began by the
        sandbox's own clock. The second test catches a file rewritten to the
        same length within the timestamp's precision (a whole second on some
        filesystems), which the first would miss."""
        after = await _snapshot(runtime)
        changed = sorted(
            name for name, stamp in after.items()
            if before.get(name) != stamp or _not_before(stamp[1], run_began)
        )
        total = 0
        for name in changed:
            size = after[name][0]
            if len(outcome.artifacts) >= MAX_ARTIFACTS_PER_RUN:
                outcome.skipped_artifacts.append({"name": name, "size": size, "reason": f"more than {MAX_ARTIFACTS_PER_RUN} files in one run"})
                continue
            if size > MAX_ARTIFACT_BYTES:
                outcome.skipped_artifacts.append({"name": name, "size": size, "reason": f"larger than {MAX_ARTIFACT_BYTES // (1024 * 1024)} MB"})
                continue
            if total + size > MAX_RUN_ARTIFACT_BYTES:
                outcome.skipped_artifacts.append({"name": name, "size": size, "reason": f"the run's files exceed {MAX_RUN_ARTIFACT_BYTES // (1024 * 1024)} MB in total"})
                continue
            total += size
            artifact = Artifact(name=name, size=size)
            if save_artifact is not None:
                try:
                    saved = await save_artifact(name, size, runtime.iter_file(f"{WORKSPACE_OUTPUTS}/{name}"))
                except Exception as exc:  # noqa: BLE001 - one bad file must not lose the rest
                    logger.exception("could not save sandbox artifact %s", name)
                    outcome.skipped_artifacts.append({"name": name, "size": size, "reason": f"could not be saved: {_reason(exc)}"})
                    continue
                if saved is None:
                    outcome.skipped_artifacts.append({"name": name, "size": size, "reason": "could not be saved"})
                    continue
                artifact = saved
            outcome.artifacts.append(artifact)

    # ── stop, delete, status ──

    async def stop(self, db: AsyncSession, sandbox: Sandbox, actor_id: Optional[str], *, reason: str = "requested",
                   title: Optional[str] = None) -> Sandbox:
        """Stop the container, keeping its files. Not possible while a run is in progress."""
        if sandbox.state == "stopped":
            return sandbox
        if sandbox.state == "busy":
            raise errors.busy()
        if sandbox.state not in ("ready", "idle"):
            raise SandboxError(errors.INVALID_STATE, f"A {sandbox.state} sandbox cannot be stopped", status=409)
        _, driver = await self._driver(db, sandbox)
        try:
            if CAP_STOP_RESUME not in driver.capabilities():
                raise SandboxError(errors.INVALID_STATE, "This backend cannot stop and resume sandboxes", status=409)
            # The state above was read earlier (the lifecycle job reads a whole
            # batch first). Claim the sandbox the way a run does, so a run that
            # started in between is never stopped under its feet, and no run can
            # start while the container is being stopped.
            # last_used_at moves with the claim so the lifecycle job's "abandoned
            # run" recovery does not take a long-idle sandbox for one; it is put back after.
            previous, previous_used = sandbox.state, sandbox.last_used_at
            claimed = await db.execute(
                update(Sandbox).where(Sandbox.id == sandbox.id, Sandbox.state.in_(("ready", "idle")))
                .values(state="busy", last_used_at=_now())
            )
            await db.commit()
            await db.refresh(sandbox)
            if claimed.rowcount != 1:
                if sandbox.state == "stopped":
                    return sandbox
                raise errors.busy()
            try:
                await driver.stop(self._handle(sandbox))
            except SandboxNotFound:
                await self._fail(db, sandbox, None, "lost: it is no longer on its backend")
                return sandbox
            except BaseException:
                # Not stopped: give the sandbox back as it was.
                await db.execute(
                    update(Sandbox).where(Sandbox.id == sandbox.id, Sandbox.state == "busy")
                    .values(state=previous, last_used_at=previous_used)
                )
                await db.commit()
                await db.refresh(sandbox)
                raise
        except SandboxDriverError as exc:
            raise SandboxError(errors.UNAVAILABLE, f"The sandbox could not be stopped: {exc.message}", status=503) from exc
        finally:
            await driver.close()
        sandbox.last_used_at = previous_used
        await self._set_state(db, sandbox, "stopped", actor_id, details={"reason": reason, "from": previous})
        await audit(db, sandbox.organization_id, "sandbox.stopped", actor_id, "sandbox", sandbox.id,
                    {"title": await self._title(db, sandbox, title), "report_id": sandbox.report_id, "reason": reason})
        await db.commit()
        return sandbox

    async def _remove(self, db: AsyncSession, sandbox: Sandbox, actor_id: Optional[str], *, reason: str,
                      title: Optional[str] = None) -> None:
        backend = await db.get(SandboxBackend, sandbox.backend_id) if sandbox.backend_id else None
        # Only a backend that is really gone means there is nothing to remove.
        # A backend that exists but whose driver cannot be built (or reached)
        # still holds the container: marking the row deleted would orphan it.
        if backend is not None and backend.deleted_at is None and sandbox.driver_ref:
            try:
                driver = await backend_service.build_driver(db, backend)
                try:
                    await driver.delete(self._handle(sandbox))
                finally:
                    await driver.close()
            except (SandboxDriverError, SandboxError) as exc:
                # "recreated" goes on: the report needs a working sandbox now,
                # and reconcile removes the old container once the backend answers.
                if reason not in ("recreated",):
                    raise SandboxError(
                        errors.UNAVAILABLE, f"The sandbox could not be deleted: {exc.message}", status=503,
                    ) from exc
        await self._set_state(db, sandbox, "deleted", actor_id, details={"reason": reason})
        await audit(db, sandbox.organization_id, "sandbox.deleted", actor_id, "sandbox", sandbox.id,
                    {"title": await self._title(db, sandbox, title), "report_id": sandbox.report_id, "reason": reason})
        await db.commit()

    async def delete(self, db: AsyncSession, sandbox: Sandbox, actor_id: Optional[str], *, reason: str = "requested",
                     title: Optional[str] = None) -> None:
        """Remove the container and its files. Idempotent."""
        if sandbox.state == "deleted":
            return
        await self._remove(db, sandbox, actor_id, reason=reason, title=title)

    async def delete_for_report(self, db: AsyncSession, report: ReportRef, actor_id: Optional[str], *,
                                reason: str = "requested") -> bool:
        sandbox = await self.get_for_report(db, report.id)
        if sandbox is None:
            return False
        await self.delete(db, sandbox, actor_id, reason=reason, title=report.title)
        return True

    async def describe(self, db: AsyncSession, report: ReportRef) -> Optional[Dict[str, Any]]:
        """What ``sandbox_status`` returns: state, environment, uptime, expiry, recent runs."""
        sandbox = await self.get_for_report(db, report.id)
        if sandbox is None:
            return None
        limits = dict(sandbox.limits or {})
        runs = await self.recent_runs(db, sandbox.id)
        return {
            "sandbox_id": sandbox.id,
            "state": sandbox.state,
            "environment": limits.get("environment"),
            "network_policy": limits.get("network_policy"),
            "limits": {k: limits.get(k) for k in ("cpus", "memory_mb", "disk_mb", "run_timeout_seconds")},
            "created_at": sandbox.created_at.isoformat() if sandbox.created_at else None,
            "uptime_seconds": int((_now() - sandbox.created_at).total_seconds()) if sandbox.created_at else None,
            "last_used_at": sandbox.last_used_at.isoformat() if sandbox.last_used_at else None,
            "expires_at": sandbox.expires_at.isoformat() if sandbox.expires_at else None,
            "error": sandbox.error,
            "recent_runs": [
                {"run_id": r.id, "language": r.language, "status": r.status, "exit_code": r.exit_code,
                 "started_at": r.started_at.isoformat() if r.started_at else None,
                 "artifacts": len(r.artifact_file_ids or [])}
                for r in runs
            ],
        }


class _Lost(Exception):
    """The sandbox is gone from its backend, or runs but does not answer."""


@dataclass
class _Plan:
    command: List[str]
    code_path: Optional[str]
    files: Dict[str, Union[str, bytes]]
    packages: List[str]
    install_command: Optional[List[str]]


def _plan(request: RunRequest) -> _Plan:
    """Turn a run request into what to write and what to execute. Validates it."""
    language = (request.language or "python").strip().lower()
    if language not in LANGUAGES:
        raise SandboxError(errors.INVALID, f"Unknown language '{request.language}'. Use one of: {', '.join(LANGUAGES)}.")
    if not (request.code or request.command):
        raise SandboxError(errors.INVALID, "Give the code to run, a command, or both.")
    file_name, runner, installer = LANGUAGES[language]
    # Each run's code goes in its own directory, out of the way of the workspace.
    code_path = f"{RUN_DIRECTORY}/{uuid.uuid4().hex[:12]}/{file_name}" if request.code else None
    if request.command:
        command = ["sh", "-c", request.command]
    else:
        # Through a shell, so an interpreter the image does not have is an
        # ordinary result ("python: not found", exit 127) the agent can read,
        # not a refusal to start.
        command = ["sh", "-c", 'exec "$0" "$@"'] + [code_path if part == "{file}" else part for part in runner]
    packages = [str(p).strip() for p in (request.packages or []) if str(p).strip()]
    install_command = None
    if packages:
        if installer is None:
            raise SandboxError(errors.INVALID, f"Packages cannot be installed for {language}.")
        for package in packages:
            if not _PACKAGE.match(package):
                raise SandboxError(errors.INVALID, f"'{package}' is not a valid package name.")
        install_command = list(installer) + packages
    files = {_clean_path(path): content for path, content in (request.files or {}).items()}
    return _Plan(command=command, code_path=code_path, files=files, packages=packages, install_command=install_command)


async def _snapshot(runtime) -> Dict[str, tuple]:
    """{file name under outputs/: (size, modified)} for every file there."""
    entries = await runtime.list_dir(WORKSPACE_OUTPUTS, recursive=True)
    return {e.name: (e.size, e.modified_at) for e in entries if e.is_file}


async def _sandbox_clock(runtime) -> Optional[str]:
    """Now, by the sandbox's clock, in the format its file listing uses: the
    modification time of a file written this instant. None if it cannot be read."""
    try:
        await runtime.write_file(f"{RUN_DIRECTORY}/.clock", b"")
        for entry in await runtime.list_dir(RUN_DIRECTORY):
            if entry.is_file and entry.name.rsplit("/", 1)[-1] == ".clock":
                return entry.modified_at
    except Exception:  # noqa: BLE001 - only sharpens change detection; size and time still apply
        logger.debug("could not read the sandbox clock", exc_info=True)
    return None


def _not_before(modified_at: Optional[str], moment: Optional[str]) -> bool:
    """Whether a file stamped ``modified_at`` may have been written at or after
    ``moment``, when the stamp alone cannot tell. Both are the sandbox's own
    timestamps (same clock, same format).

    Only whole-second stamps need this: with a fraction, a rewrite already
    changes the stamp. Anything that cannot be compared counts as "no"."""
    if not modified_at or not moment or len(modified_at) < 19 or len(moment) < 19:
        return False
    if modified_at[19:20] == ".":
        return False  # sub-second precision: the stamp itself changes on a rewrite
    if modified_at[19:].lstrip(".0123456789") != moment[19:].lstrip(".0123456789"):
        return False  # different zone notation: not comparable as text
    return modified_at[:19] >= moment[:19]


def _reason(exc: Exception) -> str:
    return str(getattr(exc, "message", None) or exc or type(exc).__name__)[:300]
