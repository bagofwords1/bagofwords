"""The four agent-sandbox tools: create_sandbox, run_in_sandbox,
sandbox_status and delete_sandbox.

They are thin: everything about states, quotas, placement, the record of
runs and audit is in ``app.services.sandbox.service``. What lives here is the
agent-facing part: reading report files into the sandbox, saving what a run
wrote as report files, and streaming output to the report thread.

The tools ship dark. They are in the planner's catalog only when the
deployment flag and the organization switch are on, the user holds
``use_agent_sandbox`` and may use an environment, and a compute can run it
(see ``agent_v2`` and ``services/sandbox/access.py``). The service checks all
of that again on every call, so a tool that is named without being offered
is refused.

Each call works on its own database session: tools can run in parallel
batches on the agent's shared session, and a run holds its session for as
long as the code takes.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolProgressEvent, ToolStartEvent, ToolStdoutEvent
from app.ai.tools.schemas.agent_sandbox import (
    CreateSandboxInput,
    CreateSandboxOutput,
    DeleteSandboxOutput,
    RunInSandboxInput,
    RunInSandboxOutput,
    SandboxArtifact,
    SandboxEmptyInput,
    SandboxStatusOutput,
)

logger = logging.getLogger(__name__)

LANGUAGES = ["python", "node", "go", "bash"]
# The longest a run may take (an environment may allow up to 60 minutes), plus
# time to create or resume the sandbox and to collect artifacts.
RUN_TOOL_TIMEOUT_SECONDS = 3600 + 600
HEARTBEAT_SECONDS = 30
INPUT_CHUNK_BYTES = 1024 * 1024
# The planner's "permission anywhere" check hides the tools from users without it.
REQUIRED_PERMISSIONS = ["use_agent_sandbox"]


def _session_maker(runtime_ctx: Dict[str, Any]):
    from app.dependencies import async_session_maker

    return runtime_ctx.get("session_maker") or async_session_maker


def _context(runtime_ctx: Dict[str, Any]):
    """(organization, report, acting user id, report reference) or raises SandboxError."""
    from app.services.sandbox import errors
    from app.services.sandbox.errors import SandboxError
    from app.services.sandbox.feature import sandbox_feature_enabled
    from app.services.sandbox.service import ReportRef

    if not sandbox_feature_enabled():
        raise errors.disabled()
    organization = runtime_ctx.get("organization")
    report = runtime_ctx.get("report")
    if organization is None or report is None:
        raise SandboxError(errors.INVALID, "The sandbox is only available inside a report.")
    user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
    # Every action is attributed to the user who triggered it. A run nobody
    # triggered in person (a scheduled run) acts as the report's owner.
    actor_id = str(user.id) if user is not None and getattr(user, "id", None) else None
    if actor_id is None and getattr(report, "user_id", None):
        actor_id = str(report.user_id)
    data_source_ids = [str(ds.id) for ds in (runtime_ctx.get("run_agents") or []) if getattr(ds, "id", None)]
    ref = ReportRef(
        id=str(report.id), organization_id=str(organization.id), title=getattr(report, "title", "") or "",
        owner_id=str(report.user_id) if getattr(report, "user_id", None) else None, data_source_ids=data_source_ids,
    )
    return organization, report, user, actor_id, ref


def _failure(model: Type[BaseModel], tool: str, exc: Exception) -> ToolEndEvent:
    """A refusal or failure as a tool result the agent can read and act on."""
    code = getattr(exc, "code", "sandbox.error")
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return ToolEndEvent(type="tool.end", payload={
        "output": model(success=False, error_code=code, error_message=message).model_dump(),
        "observation": {"summary": f"{tool} failed: {message}", "success": False, "error_code": code},
    })


def _completion_id(runtime_ctx: Dict[str, Any]) -> Optional[str]:
    completion = runtime_ctx.get("system_completion") or runtime_ctx.get("head_completion")
    return str(completion.id) if completion is not None and getattr(completion, "id", None) else None


class CreateSandboxTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_sandbox",
            description="""
Purpose:
Create this report's sandbox: an isolated, persistent container for running code.
Returns the existing sandbox when the report already has one.

Use when:
    - You want to choose a specific environment before the first run
    - You want to know the sandbox's limits and network access before writing code

You usually do not need it: run_in_sandbox creates the sandbox on first use.

Returns: sandbox_id, state, environment, network access, available languages, limits.
            """,
            category="action",
            version="1.0.0",
            input_schema=CreateSandboxInput.model_json_schema(),
            output_schema=CreateSandboxOutput.model_json_schema(),
            required_permissions=list(REQUIRED_PERMISSIONS),
            allowed_modes=["chat"],
            timeout_seconds=300,
            tags=["sandbox", "code", "execution"],
            digest_keys=["sandbox_id", "state", "environment", "network_policy"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateSandboxInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateSandboxOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        from app.services.sandbox.errors import SandboxError
        from app.services.sandbox.prompt import describe_network
        from app.services.sandbox.service import SandboxService

        data = CreateSandboxInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": "Creating sandbox"})
        try:
            _, _, _, actor_id, ref = _context(runtime_ctx)
            async with _session_maker(runtime_ctx)() as db:
                sandbox, flags = await SandboxService().ensure(db, ref, actor_id, environment=data.environment)
                limits = dict(sandbox.limits or {})
                output = CreateSandboxOutput(
                    success=True, sandbox_id=sandbox.id, state=sandbox.state, environment=limits.get("environment"),
                    network_policy=limits.get("network_policy"), network=describe_network(limits.get("network_policy")),
                    languages=LANGUAGES, created=bool(flags["created"] or flags["recreated"]),
                    limits={k: limits.get(k) for k in ("cpus", "memory_mb", "disk_mb", "run_timeout_seconds")},
                )
        except SandboxError as exc:
            yield _failure(CreateSandboxOutput, "create_sandbox", exc)
            return
        verb = "Created" if output.created else "Using the existing"
        yield ToolEndEvent(type="tool.end", payload={
            "output": output.model_dump(),
            "observation": {
                "summary": f"{verb} sandbox (environment '{output.environment}', {output.state}). Network: {output.network}.",
                "success": True, "sandbox_id": output.sandbox_id, "state": output.state,
                "environment": output.environment, "network_policy": output.network_policy, "limits": output.limits,
            },
        })


class RunInSandboxTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="run_in_sandbox",
            description="""
Purpose:
Run code in this report's sandbox, an isolated persistent container with its own
filesystem. Creates the sandbox on first use and resumes it if it was stopped.

Use when:
    - A task needs real code execution on files: converting or processing an
      uploaded file, running a script that needs a package, producing a file the
      user asked for (a chart image, a spreadsheet, a document)

Do not use when:
    - You need data from a connected data source. The sandbox has no data source
      credentials: use create_data / inspect_data for that.

How it works:
    - `code` is written to a file and run with `language`; or give a shell `command`.
    - Files of this report reach the sandbox only through `input_file_ids`; they
      appear under /workspace/inputs.
    - Write results under /workspace/outputs. Every new or changed file there is
      saved to the report after the run and returned with a file_id.
    - `packages` are installed first with pip / npm / go get, and only if the
      sandbox's network access allows the package registries.
    - A run ends when the program it started exits. To leave something running
      in the background (a server), send its output elsewhere, e.g.
      `nohup python -m http.server 8000 >server.log 2>&1 &`; otherwise the run
      waits a few more seconds for output. What is left running is reported.
    - Files and installed packages persist between runs, but only what is under
      /workspace/outputs is durable (it is saved to the report). The rest is a
      cache: after a deletion or a failure the sandbox starts fresh, and only
      the report's own files are put back. One run at a time.

Returns: exit_code, the end of stdout and stderr, and the saved artifacts.
A non-zero exit_code is a normal result: read stderr and fix the code.
            """,
            category="action",
            version="1.0.0",
            input_schema=RunInSandboxInput.model_json_schema(),
            output_schema=RunInSandboxOutput.model_json_schema(),
            required_permissions=list(REQUIRED_PERMISSIONS),
            allowed_modes=["chat"],
            timeout_seconds=RUN_TOOL_TIMEOUT_SECONDS,
            max_retries=0,  # a run has side effects: never repeat it automatically
            idempotent=False,
            tags=["sandbox", "code", "execution"],
            digest_keys=["run_id", "status", "exit_code", "artifacts", "sandbox_id"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return RunInSandboxInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return RunInSandboxOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        from app.services.sandbox.errors import SandboxError
        from app.services.sandbox.service import RunRequest, SandboxService

        data = RunInSandboxInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": f"Running {data.language} in the sandbox"})
        try:
            organization, report, user, actor_id, ref = _context(runtime_ctx)
            inputs = await _input_files(runtime_ctx, data.input_file_ids)
        except SandboxError as exc:
            yield _failure(RunInSandboxOutput, "run_in_sandbox", exc)
            return

        request = RunRequest(
            language=data.language, code=data.code, command=data.command,
            files={f.path: f.content for f in data.files}, packages=list(data.packages), inputs=inputs,
            timeout_seconds=data.timeout_seconds,
        )

        # An environment can ask the person in the chat before a run starts.
        decision: Dict[str, Any] = {}
        async for event in _confirm_run(runtime_ctx, ref, actor_id, data, decision):
            yield event
        if decision.get("refused"):
            yield _declined(RunInSandboxOutput, decision)
            return

        events: asyncio.Queue = asyncio.Queue()
        saved_files: List[Any] = []

        async def on_output(kind: str, chunk: bytes) -> None:
            await events.put(ToolStdoutEvent(type="tool.stdout", payload={
                "stream": kind, "text": chunk.decode("utf-8", errors="replace"),
            }))

        async def on_progress(stage: str) -> None:
            await events.put(ToolProgressEvent(type="tool.progress", payload={"stage": stage}))

        async def run() -> Any:
            async with _session_maker(runtime_ctx)() as db:
                async def save_artifact(name: str, size: int, chunks: AsyncIterator[bytes]):
                    return await _save_artifact(
                        db, name, size, chunks, user=user, organization=organization, report_id=ref.id,
                        completion_id=_completion_id(runtime_ctx), saved=saved_files,
                    )

                async def load_file(file_id: str):
                    return await _report_file(runtime_ctx, file_id)

                return await SandboxService().run(
                    db, ref, actor_id, request, on_output=on_output, on_progress=on_progress,
                    cancel=runtime_ctx.get("sigkill_event"), save_artifact=save_artifact,
                    completion_id=_completion_id(runtime_ctx), tool_execution_id=runtime_ctx.get("tool_execution_id"),
                    load_file=load_file,
                )

        task = asyncio.ensure_future(run())
        pending_event: Optional[asyncio.Future] = None
        try:
            while not task.done():
                pending_event = pending_event or asyncio.ensure_future(events.get())
                done, _ = await asyncio.wait({task, pending_event}, timeout=HEARTBEAT_SECONDS,
                                             return_when=asyncio.FIRST_COMPLETED)
                if pending_event in done:
                    yield pending_event.result()
                    pending_event = None
                elif not done:
                    # A quiet run must not look like a hung tool to the runner.
                    yield ToolProgressEvent(type="tool.progress", payload={"stage": "running"})
            if pending_event is not None:
                if pending_event.done():
                    yield pending_event.result()
                else:
                    pending_event.cancel()
            while not events.empty():
                yield events.get_nowait()
            outcome = await task
        except SandboxError as exc:
            yield _failure(RunInSandboxOutput, "run_in_sandbox", exc)
            return
        finally:
            if not task.done():
                task.cancel()

        # The files just saved are usable by the tools that run after this one in the same turn.
        await _register_saved_files(runtime_ctx, report, [a.file_id for a in outcome.artifacts if a.file_id])

        notes: List[str] = []
        note_codes: List[str] = []
        if outcome.recreated:
            note_codes.append("restored_snapshot" if outcome.restored_snapshot_at else "recreated")
            if outcome.restored_files:
                note_codes.append("files_restored")
            restored = (
                f" These files of the report were put back: {', '.join(outcome.restored_files[:20])}"
                + (f" and {len(outcome.restored_files) - 20} more." if len(outcome.restored_files) > 20 else ".")
            ) if outcome.restored_files else ""
            if outcome.restored_snapshot_at:
                notes.append(
                    f"The sandbox had to be recreated and was restored from a snapshot taken at "
                    f"{outcome.restored_snapshot_at[:16].replace('T', ' ')} UTC: files and packages are as they were "
                    "then. Anything changed after that, outside outputs/, is gone." + restored
                )
            else:
                notes.append(
                    "The sandbox had to be recreated: its filesystem is fresh. Installed packages and files outside "
                    "outputs/ are gone." + restored
                )
        elif outcome.created:
            note_codes.append("created")
            notes.append("A new sandbox was created for this report.")
        elif outcome.resumed:
            note_codes.append("resumed")
            notes.append("The sandbox was stopped and has been resumed with its files and packages.")
        if outcome.status == "timeout":
            note_codes.append("timeout")
            notes.append("The run was stopped at its time limit.")
        if outcome.status == "cancelled":
            note_codes.append("cancelled")
            notes.append("The run was cancelled.")
        if outcome.skipped_artifacts:
            note_codes.append("skipped_artifacts")
        if outcome.background_processes:
            note_codes.append("background_processes")
        if outcome.notices:
            note_codes.append("deletion_scheduled")
        if outcome.skipped_artifacts:
            notes.append(f"{len(outcome.skipped_artifacts)} output file(s) were not saved; see skipped_artifacts.")
        if outcome.background_processes:
            notes.append(
                "Still running in the sandbox after this run: "
                + "; ".join(outcome.background_processes[:5])
                + ". They keep using its CPU and memory until they exit or the sandbox stops."
            )
        notes.extend(outcome.notices)

        output = RunInSandboxOutput(
            success=outcome.status == "succeeded", run_id=outcome.run_id, sandbox_id=outcome.sandbox_id,
            status=outcome.status, exit_code=outcome.exit_code, stdout=outcome.stdout, stderr=outcome.stderr,
            stdout_truncated=outcome.stdout_truncated, stderr_truncated=outcome.stderr_truncated,
            artifacts=[SandboxArtifact(file_id=a.file_id, name=a.name, size=a.size, type=a.content_type) for a in outcome.artifacts],
            skipped_artifacts=outcome.skipped_artifacts, duration_seconds=outcome.duration_seconds,
            environment=outcome.environment, network_policy=outcome.network_policy, notes=notes,
            note_codes=note_codes, background_processes=outcome.background_processes,
            restored_files=outcome.restored_files,
        )
        produced = ", ".join(f"{a.name} (file_id={a.file_id})" for a in output.artifacts) or "none"
        yield ToolEndEvent(type="tool.end", payload={
            "output": output.model_dump(),
            "observation": {
                "summary": (
                    f"Run {output.status} (exit code {output.exit_code}) in {output.duration_seconds}s. "
                    f"Files saved to the report: {produced}." + (" " + " ".join(notes) if notes else "")
                ),
                "success": output.success, "run_id": output.run_id, "sandbox_id": output.sandbox_id,
                "status": output.status, "exit_code": output.exit_code,
                "stdout": output.stdout, "stderr": output.stderr,
                "stdout_truncated": output.stdout_truncated, "stderr_truncated": output.stderr_truncated,
                "artifacts": [a.model_dump() for a in output.artifacts],
                "skipped_artifacts": output.skipped_artifacts, "notes": notes,
            },
        })


class SandboxStatusTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="sandbox_status",
            description="""
Purpose:
Show this report's sandbox: its state, environment, network access, limits,
how long it has existed, when it expires, and its recent runs. Also lists the
environments you may use. Changes nothing.
            """,
            category="both",
            version="1.0.0",
            input_schema=SandboxEmptyInput.model_json_schema(),
            output_schema=SandboxStatusOutput.model_json_schema(),
            required_permissions=list(REQUIRED_PERMISSIONS),
            allowed_modes=["chat"],
            timeout_seconds=60,
            idempotent=True,
            tags=["sandbox", "code", "execution"],
            digest_keys=["exists", "state", "environment", "network_policy"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SandboxEmptyInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SandboxStatusOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        from app.services.sandbox import errors
        from app.services.sandbox.errors import SandboxError
        from app.services.sandbox.prompt import describe_network
        from app.services.sandbox.service import SandboxService
        from app.services.sandbox.settings import load_settings

        yield ToolStartEvent(type="tool.start", payload={"title": "Checking the sandbox"})
        try:
            _, _, _, actor_id, ref = _context(runtime_ctx)
            service = SandboxService()
            async with _session_maker(runtime_ctx)() as db:
                if not (await load_settings(db, ref.organization_id)).enabled:
                    raise errors.disabled()
                await service.access.require_use(db, ref.organization_id, actor_id, ref)
                described = await service.describe(db, ref)
                environments = [
                    {"name": e.name, "description": e.description, "network": describe_network(e.network_policy),
                     "network_policy": e.network_policy,
                     "is_default": bool(e.is_default)}
                    for e in await service.access.usable_environments(db, ref.organization_id, actor_id)
                ]
        except SandboxError as exc:
            yield _failure(SandboxStatusOutput, "sandbox_status", exc)
            return
        if described is None:
            output = SandboxStatusOutput(success=True, exists=False, available_environments=environments)
            summary = "This report has no sandbox yet. run_in_sandbox creates one on first use."
        else:
            output = SandboxStatusOutput(
                success=True, exists=True, sandbox_id=described["sandbox_id"], state=described["state"],
                environment=described["environment"], network_policy=described["network_policy"],
                network=describe_network(described["network_policy"]), limits=described["limits"],
                uptime_seconds=described["uptime_seconds"], expires_at=described["expires_at"],
                recent_runs=described["recent_runs"], available_environments=environments,
            )
            summary = (f"Sandbox is {output.state} (environment '{output.environment}'). Network: {output.network}. "
                       f"{len(output.recent_runs)} recent run(s).")
        yield ToolEndEvent(type="tool.end", payload={
            "output": output.model_dump(),
            "observation": {"summary": summary, "success": True, **output.model_dump(exclude={"success"})},
        })


class DeleteSandboxTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="delete_sandbox",
            description="""
Purpose:
Delete this report's sandbox with everything in it: files, installed packages,
anything not already saved to the report. The next run_in_sandbox starts a
fresh one. Files already saved to the report are kept.

Use when:
    - The user asks to reset or remove the sandbox
    - The sandbox's filesystem is in a state you cannot recover from
            """,
            category="action",
            version="1.0.0",
            input_schema=SandboxEmptyInput.model_json_schema(),
            output_schema=DeleteSandboxOutput.model_json_schema(),
            required_permissions=list(REQUIRED_PERMISSIONS),
            allowed_modes=["chat"],
            timeout_seconds=120,
            max_retries=0,
            tags=["sandbox", "code", "execution"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SandboxEmptyInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return DeleteSandboxOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        from app.services.sandbox import errors
        from app.services.sandbox.errors import SandboxError
        from app.services.sandbox.service import SandboxService
        from app.services.sandbox.settings import load_settings

        yield ToolStartEvent(type="tool.start", payload={"title": "Deleting the sandbox"})
        try:
            _, _, _, actor_id, ref = _context(runtime_ctx)
            service = SandboxService()
            async with _session_maker(runtime_ctx)() as db:
                if not (await load_settings(db, ref.organization_id)).enabled:
                    raise errors.disabled()
                await service.access.require_use(db, ref.organization_id, actor_id, ref)
                existing = await service.get_for_report(db, ref.id)
                if existing is not None:
                    await service.access.require_environment(db, ref.organization_id, actor_id, existing.environment_id)
                    if existing.state == "busy":
                        raise errors.busy()
            if existing is not None:
                # Deleting loses everything outside outputs/: always ask first.
                decision: Dict[str, Any] = {}
                async for event in _ask(runtime_ctx, "delete_sandbox", {
                    "action": "delete_sandbox",
                    "environment": (existing.limits or {}).get("environment"),
                    "reason": "Deleting the sandbox removes its files and installed packages. "
                              "Files already saved to the report are kept.",
                }, decision):
                    yield event
                if decision.get("refused"):
                    yield _declined(DeleteSandboxOutput, decision, noun="deletion")
                    return
            async with _session_maker(runtime_ctx)() as db:
                deleted = await service.delete_for_report(db, ref, actor_id, reason="requested by the agent")
        except SandboxError as exc:
            yield _failure(DeleteSandboxOutput, "delete_sandbox", exc)
            return
        summary = "The sandbox was deleted." if deleted else "This report has no sandbox; nothing to delete."
        yield ToolEndEvent(type="tool.end", payload={
            "output": DeleteSandboxOutput(success=True, deleted=deleted).model_dump(),
            "observation": {"summary": summary, "success": True, "deleted": deleted},
        })


# ── asking the person in the chat first ──────────────────────────────────


async def _ask(runtime_ctx: Dict[str, Any], tool_name: str, payload: Dict[str, Any], decision: Dict[str, Any]):
    """Pause for an Allow / Deny in the chat. Yields the tool events and fills
    ``decision``: ``refused`` (bool), ``timed_out`` (bool), ``feedback``."""
    from app.ai.tools.confirmation import KIND_BUILTIN_TOOL, stream_user_confirmation

    result: Dict[str, Any] = {}
    async for event in stream_user_confirmation(
        runtime_ctx, kind=KIND_BUILTIN_TOOL, tool_name=tool_name, payload=payload, result=result,
    ):
        yield event
    answer = (result.get("response") or {}).get("response") or {}
    decision.update(
        refused=not result.get("approved"), timed_out=bool(result.get("timed_out")),
        feedback=((answer.get("feedback") or "").strip() or None) if isinstance(answer, dict) else None,
    )


async def _confirm_run(runtime_ctx: Dict[str, Any], ref: Any, actor_id: Optional[str], data: Any, decision: Dict[str, Any]):
    """Ask before a run when the environment's policy says so. The card shows
    what will run; the code is on it only for someone who may see code."""
    from app.core.code_visibility import code_visible_now
    from app.services.sandbox import environments as environment_service
    from app.services.sandbox.service import SandboxService

    try:
        async with _session_maker(runtime_ctx)() as db:
            env = await SandboxService().environment_for(db, ref, actor_id)
    except Exception:  # noqa: BLE001 - the run itself reports what is wrong
        return
    if env is not None:
        # Known before anything runs, so the block in the chat can say where it runs from the start.
        yield ToolProgressEvent(type="tool.progress", payload={
            "stage": "starting", "environment": env.name, "network_policy": env.network_policy,
        })
    reason = environment_service.needs_confirmation(env, installs_packages=bool(data.packages))
    if not reason:
        return
    show_code = code_visible_now()
    async for event in _ask(runtime_ctx, "run_in_sandbox", {
        "action": "run_in_sandbox", "reason": reason,
        "environment": env.name, "network_policy": env.network_policy,
        "language": data.language, "packages": list(data.packages),
        "input_files": len(data.input_file_ids),
        "code": (data.code if show_code else None), "command": (data.command if show_code else None),
    }, decision):
        yield event


def _declined(output_model: Type[BaseModel], decision: Dict[str, Any], noun: str = "run") -> ToolEndEvent:
    if decision.get("timed_out"):
        message = f"The {noun} was not confirmed in time, so nothing happened. Ask the user whether to try again."
    else:
        message = f"The user declined the {noun}; nothing happened."
        if decision.get("feedback"):
            message += f" Their feedback: \"{decision['feedback']}\"."
    return ToolEndEvent(type="tool.end", payload={
        "output": output_model(success=False, error_code="sandbox.declined", error_message=message).model_dump(),
        "observation": {"summary": message, "success": False, "error": {"code": "sandbox.declined", "message": message}},
    })


# ── report files in, artifacts out ───────────────────────────────────────


async def _input_files(runtime_ctx: Dict[str, Any], file_ids: List[str]) -> List[Any]:
    """Resolve ``input_file_ids`` against the report's own files and nothing else."""
    from app.ai.tools.implementations._file_tool_common import resolve_session_file
    from app.services import storage
    from app.services.sandbox import errors
    from app.services.sandbox.errors import SandboxError
    from app.services.sandbox.service import InputFile

    inputs: List[Any] = []
    for file_id in dict.fromkeys(str(i).strip() for i in file_ids if str(i).strip()):
        file = resolve_session_file(runtime_ctx, file_id)
        if file is None:
            raise SandboxError(
                errors.INPUT_NOT_ALLOWED,
                f"File '{file_id}' is not a file of this report, so it cannot be passed into the sandbox.", status=403,
            )
        await storage.aensure_local(file.path)
        if not os.path.exists(file.path):
            raise SandboxError(errors.INVALID, f"File '{file.filename}' is no longer available.")
        inputs.append(InputFile(file_id=str(file.id), name=file.filename, data=_read_chunks(file.path)))
    return inputs


async def _report_file(runtime_ctx: Dict[str, Any], file_id: str):
    """One of the report's own files as sandbox input, or None when it is not
    (or no longer) a file of this report. Used to restore files into a
    recreated sandbox; never raises."""
    from app.ai.tools.implementations._file_tool_common import resolve_session_file
    from app.services import storage
    from app.services.sandbox.service import InputFile

    try:
        file = resolve_session_file(runtime_ctx, file_id)
        if file is None:
            return None
        await storage.aensure_local(file.path)
        if not os.path.exists(file.path):
            return None
        return InputFile(file_id=str(file.id), name=file.filename, data=_read_chunks(file.path))
    except Exception:  # noqa: BLE001
        logger.warning("could not read report file %s for the sandbox", file_id)
        return None


async def _read_chunks(path: str) -> AsyncIterator[bytes]:
    import aiofiles

    async with aiofiles.open(path, "rb") as handle:
        while True:
            chunk = await handle.read(INPUT_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


async def _save_artifact(db, name: str, size: int, chunks: AsyncIterator[bytes], *, user, organization, report_id: str,
                         completion_id: Optional[str], saved: List[Any]):
    """Save one file the run wrote as a report file (``source_kind="artifact"``)."""
    from app.services.file_service import FileService
    from app.services.sandbox.service import Artifact

    content = b"".join([chunk async for chunk in chunks])
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    db_file = await FileService().save_bytes_as_file(
        db=db, content=content, filename=os.path.basename(name), content_type=content_type,
        current_user=user, organization=organization, report_id=report_id, completion_id=completion_id,
    )
    # A tool product, not a user attachment: keeps it off the prompt box's pending-attachment chips.
    db_file.source_kind = "artifact"
    await db.commit()
    saved.append(db_file)
    return Artifact(name=name, size=len(content), file_id=str(db_file.id), content_type=content_type)


async def _register_saved_files(runtime_ctx: Dict[str, Any], report: Any, file_ids: List[str]) -> None:
    """Make the saved files visible to the tools that run after this one in the
    same turn. They were written on this tool's own session, so they are loaded
    into the agent's session (under its lock) before being registered there."""
    if not file_ids:
        return
    from app.ai.tools.implementations.execute_mcp import _register_same_turn
    from app.models.file import File

    db = runtime_ctx.get("db")
    if db is None:
        return
    lock = runtime_ctx.get("tool_db_lock")
    try:
        if lock is not None:
            async with lock:
                files = [await db.get(File, file_id) for file_id in file_ids]
        else:
            files = [await db.get(File, file_id) for file_id in file_ids]
        for file in files:
            if file is not None:
                _register_same_turn(file, runtime_ctx, report)
    except Exception:  # noqa: BLE001 - the files are saved either way; a later turn sees them
        logger.exception("could not register sandbox artifacts for the current turn")
