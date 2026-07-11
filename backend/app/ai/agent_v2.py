import asyncio
import json
import logging
import os
import time as _time
import uuid as _uuid_mod
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, Optional
from pydantic import ValidationError
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)


# Substring triggers that bump a completion's reasoning_effort to "high".
# Matched case-insensitive against the user-submitted prompt text only —
# not system prompts, instructions, or rendered context. See
# _detect_thinking_trigger / _resolve_reasoning_effort below.
THINKING_TRIGGERS = (
    "think hard",
    "think harder",
    "ultrathink",
    "think step by step",
    "think carefully",
    "think deeply",
    "deep dive",
    "be thorough",
)

# Map a user-facing effort level to the Anthropic ``thinking`` request param.
# "off" returns None (no thinking sent). Anthropic 4.6+ supports
# ``adaptive`` (model decides budget); older 4.x needs an explicit
# budget_tokens. On Sonnet 5 / Opus 4.7+ / Fable 5, budget_tokens is removed
# from the API (400 if sent) — adaptive is the only thinking mode, so those
# models must always get adaptive regardless of effort.
def _effort_to_thinking_config(effort: Optional[str], model_id: Optional[str]) -> Optional[dict]:
    if not effort or effort == "off":
        return None
    e = str(effort).lower()
    supports_adaptive = bool(model_id) and any(
        tag in model_id
        for tag in (
            "sonnet-4-6", "opus-4-6", "opus-4-7", "sonnet-4-7",
            "sonnet-5", "opus-4-8", "fable-5", "mythos",
        )
    )
    if supports_adaptive:
        return {"type": "adaptive"}
    if e == "low":
        return {"type": "enabled", "budget_tokens": 1024}
    if e == "medium":
        return {"type": "enabled", "budget_tokens": 5000}
    if e == "high":
        return {"type": "enabled", "budget_tokens": 15000}
    return None


def _detect_thinking_trigger(prompt_text: Optional[str]) -> bool:
    if not prompt_text:
        return False
    p = prompt_text.lower()
    return any(kw in p for kw in THINKING_TRIGGERS)


def _observation_failed(observation) -> bool:
    """True when a tool observation signals failure.

    Tools report failure in two ways: a truthy ``error`` payload, or an explicit
    ``success: False`` with no ``error`` key (e.g. execute_mcp on a tool-level
    MCP error). Checking only ``error`` mislabels the latter as success, which is
    why failed MCP calls used to show a green ✓ in the trace. Treat either as a
    failure.
    """
    if not observation:
        return False
    if observation.get("error"):
        return True
    if observation.get("success") is False:
        return True
    return False


def _observation_error_message(observation) -> Optional[str]:
    """Best-effort human-readable error string from a failed observation.

    Handles both the structured ``error: {message: ...}`` shape and the flatter
    ``success: False`` + ``summary`` shape that execute_mcp and friends emit.
    """
    if not observation:
        return None
    err = observation.get("error")
    if isinstance(err, dict):
        return err.get("message") or None
    if isinstance(err, str) and err.strip():
        return err
    if observation.get("success") is False:
        return observation.get("error_message") or observation.get("summary") or None
    return None


# Tools whose invocations may overlap when the planner emits several tool
# calls in one decision. Everything else forces the batch back to serial:
# tools outside this set haven't been audited for concurrent side-effects
# (shared clients, artifact state, email sends, ...).
_PARALLEL_SAFE_TOOLS = frozenset({"inspect_data", "create_data"})

# Tools that start a fresh created-objects scope: each invocation gets an
# empty ToolInvocationState instead of inheriting the previous tool's
# query/step/visualization (mirrors the old per-action self.current_* reset).
_INVOCATION_RESET_TOOLS = frozenset({"create_widget", "create_data", "describe_entity"})


class ToolInvocationState:
    """Created-object state for one tool invocation.

    Replaces the agent-instance-wide ``self.current_query/step/...`` fields
    for the duration of a tool call so concurrent invocations can't
    cross-attribute each other's created queries/steps/visualizations. The
    agent adopts the last invocation's state after each batch (in action
    order) to preserve the legacy "current = latest created" semantics for
    the next loop iteration.
    """

    __slots__ = (
        "current_query",
        "current_step",
        "current_step_id",
        "current_visualization",
        "current_widget",
    )

    def __init__(self, *, query=None, step=None, step_id=None, visualization=None, widget=None):
        self.current_query = query
        self.current_step = step
        self.current_step_id = step_id
        self.current_visualization = visualization
        self.current_widget = widget


class _AgentStateProxy:
    """Adapter exposing the agent's legacy ``self.current_*`` fields through
    the ToolInvocationState attribute interface. Used by call sites that
    predate per-invocation state (e.g. the knowledge-harness tool loop) so
    reads/writes keep landing on the agent instance."""

    __slots__ = ("_agent",)

    def __init__(self, agent):
        object.__setattr__(self, "_agent", agent)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_agent"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_agent"), name, value)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def _resolve_reasoning_effort(
    *,
    per_completion: Optional[str],
    prompt_text: Optional[str],
    model_default: Optional[str],
) -> str:
    """Resolution order: per-completion > trigger words > model default > off."""
    if per_completion:
        return per_completion.lower()
    if _detect_thinking_trigger(prompt_text):
        return "high"
    if model_default:
        return str(model_default).lower()
    return "off"


from app.ai.agents.planner import PlannerV2, PlannerV3
from app.ai.context import ContextHub, ContextBuildSpec
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.registry import ToolRegistry, ToolCatalogFilter
from app.schemas.ai.planner import PlannerInput, ToolDescriptor
from app.schemas.sse_schema import SSEEvent
from app.serializers.completion_v2 import serialize_block_v2
from app.schemas.completion_v2_schema import ArtifactChangeSchema
from app.streaming.text_streamer import PlanningTextStreamer
from app.streaming.completion_stream import CompletionEventQueue
from app.websocket_manager import websocket_manager
from app.ai.runner.tool_runner import ToolRunner
from app.ai.runner.policies import RetryPolicy, TimeoutPolicy
from app.ai.tools.officejs_registry import pending_officejs_registry
from app.project_manager import ProjectManager
from app.models.step import Step
from app.models.widget import Widget
from app.models.completion import Completion
from app.models.report import Report
from app.ai.agents.reporter.reporter import Reporter
from sqlalchemy import select, func, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tool_execution import ToolExecution
from app.models.agent_execution import AgentExecution
from app.ai.agents.judge.judge import Judge
from app.ai.agents.suggest_instructions import InstructionTriggerEvaluator
from app.dependencies import async_session_maker
from app.core.telemetry import telemetry
from app.ai.utils.token_counter import count_tokens
from app.services.instruction_usage_service import InstructionUsageService
from app.ai.llm.types import ImageInput
from app.ai.llm.usage_attribution import set_usage_attribution, reset_usage_attribution
from app.services.usage_policy_service import UsageLimitContext
from app.core.otel import get_tracer

INDEX_LIMIT = 1000  # Number of tables to include in the index
tracer = get_tracer(__name__)


class AgentV2:
    """Enhanced orchestrator with intelligent research/action flow."""

    def __init__(self, db=None, organization=None, organization_settings=None, report=None,
                 model=None, small_model=None, mode=None, platform=None, platform_context=None,
                 messages=[], head_completion=None, system_completion=None, widget=None, step=None, event_queue=None, clients=None, build_id=None,
                 session_maker=None):
        self.db = db
        # session_maker lets fragile post-tool / post-decision paths open
        # short-lived sessions instead of leaning on `self.db` (which can
        # die mid-run when an asyncio.wait_for cancels a greenlet on it,
        # closing the asyncpg transport — that's the "I/O operation on
        # closed file" → MissingGreenlet cascade we used to see). Falls
        # back to the singleton session_maker if not provided.
        if session_maker is None:
            from app.dependencies import async_session_maker as _default_sm
            session_maker = _default_sm
        self._session_maker = session_maker
        self.build_id = build_id
        # True when this AgentV2 instance is running inside a TestRun. The
        # ``run_eval`` tool refuses nested invocations against this flag.
        # Derived from report_type rather than plumbed through every call
        # site — TestRunService stubs reports as ``report_type="test"``.
        self.is_eval_run = bool(report and getattr(report, 'report_type', None) == 'test')
        self.organization = organization
        self.organization_settings = organization_settings
        self.top_k_schema = organization_settings.get_config("top_k_schema").value
        self.top_k_metadata_resources = organization_settings.get_config("top_k_metadata_resources").value
        self.mode = mode
        # Platform context: derive from explicit param or fall back to completion's external_platform
        self.platform = platform or getattr(head_completion, "external_platform", None)
        self.platform_context = platform_context
        self.training_build_id = None  # Track build ID for training mode instruction creation

        self.ai_analyst_name = organization_settings.config.get('general', {}).get('ai_analyst_name', "AI Analyst")
        # Org timezone for the planner's per-turn "current time" (None -> server-local).
        self.org_timezone = organization_settings.config.get('timezone') if organization_settings else None
        # Org locale + first-day-of-week for the planner's per-turn week convention
        # (Hebrew/Arabic orgs default to a Sunday-start work week; None -> auto/ISO).
        self.org_locale = organization_settings.config.get('locale') if organization_settings else None
        self.org_week_start = organization_settings.config.get('week_start') if organization_settings else None

        self.report = report
        self.report_type = getattr(report, 'report_type', 'regular')
        self.model = model
        self.small_model = small_model
        self.head_completion = head_completion
        self.system_completion = system_completion
        self.widget = widget
        self.step = step
        _quota_org_id = str(getattr(self.organization, "id", "") or "")
        _quota_user_id = str(
            getattr(self.head_completion, "user_id", "")
            or getattr(getattr(self.head_completion, "user", None), "id", "")
            or ""
        )
        self.usage_limit_context = (
            UsageLimitContext(
                organization_id=_quota_org_id,
                user_id=_quota_user_id,
                source="agent",
                source_ref_id=str(getattr(self.head_completion, "id", "") or ""),
                session_maker=async_session_maker,
            )
            if _quota_org_id and _quota_user_id
            else None
        )

        # Initialize data sources and clients (mirror agent.py pattern)
        if report:
            # Handle case where data_sources or files might be None. The
            # report's attachments are a creation-time snapshot — drop agents
            # disabled/deactivated since so their schema never enters the
            # context, even for callers that pass no clients. (Local import:
            # data_source_service pulls in app.ai modules at import time.)
            from app.services.data_source_service import DataSourceService
            self.data_sources = [
                ds for ds in (getattr(report, 'data_sources', []) or [])
                if DataSourceService.is_execution_live(ds)
            ]
            self.clients = clients
            # Drop data sources that produced no client. The caller builds
            # `clients` via DataSourceService.construct_clients, which now 403s
            # for sources the running user can't access — so a source still on
            # the report's (possibly stale) snapshot that the user lost access
            # to has no client. Without this, its schema would still flow into
            # the agent context and the agent would try to query a source it
            # can't reach, erroring mid-run. Silently dropping it keeps the
            # context aligned with what's actually queryable. Only filter when
            # clients were supplied (some non-query callers pass none).
            if clients:
                def _has_client(ds):
                    name = getattr(ds, 'name', None)
                    if not name:
                        return False
                    prefix = f"{name}:"
                    return any(k == name or k.startswith(prefix) for k in clients)
                self.data_sources = [ds for ds in self.data_sources if _has_client(ds)]

                # Symmetrically, drop any client whose data source is NOT live.
                # `clients` is aggregated report-wide by the caller, and on the
                # streaming/background completion paths it is not pre-filtered by
                # is_execution_live — so a data source disabled (or deactivated)
                # after being snapshotted onto the report can still contribute a
                # client here. Left in, that client stays in the `ds_clients`
                # dict handed to generated code even though its schema is already
                # excluded above via self.data_sources — letting the model query
                # (or blindly "try each client" against) a source it was never
                # shown. That is the "wrong client" failure: e.g. a stale
                # `SBODemoIL:SBODemoIL` connection whose login no longer works.
                # Keep the client set aligned with the live/queryable sources.
                _live_ds_names = {getattr(ds, 'name', None) for ds in self.data_sources}
                _live_ds_names.discard(None)

                def _client_is_live(key: str) -> bool:
                    return any(key == n or key.startswith(f"{n}:") for n in _live_ds_names)

                self.clients = {k: v for k, v in clients.items() if _client_is_live(k)}
            all_files = getattr(report, 'files', []) or []
            # Split files: images go to LLM vision, everything else goes through existing flow
            self.image_files = [f for f in all_files if (getattr(f, 'content_type', '') or '').startswith('image/')]
            self.analysis_files = [f for f in all_files if not (getattr(f, 'content_type', '') or '').startswith('image/')]
        else:
            self.data_sources = []
            self.clients = {}
            self.image_files = []
            self.analysis_files = []

        self.sigkill_event = asyncio.Event()
        websocket_manager.add_handler(self._handle_completion_update)

        # SSE event queue for streaming
        self.event_queue = event_queue

        # Agent execution tracking
        self.project_manager = ProjectManager()
        self.current_execution = None

        # Background DB writes scheduled during the loop. Drained before the
        # final `completion.finished` SSE so the API doesn't return a "done"
        # signal while writes are still in flight. Failed bg writes are
        # logged with `[agent.bg_write]` and counted in
        # `_bg_write_failures` for observability.
        self._pending_writes: list[asyncio.Task] = []
        self._bg_write_failures: int = 0

        # Coalesce rebuild_completion_from_blocks requests. Used to fire
        # twice per loop iteration (once after plan_decision saved, once
        # after tool_execution saved). They read the same set of blocks
        # — the post-tool rebuild fully supersedes the post-plan one.
        # Now we keep at most one rebuild in flight per agent: if a new
        # request arrives while one is running, we mark "another wanted"
        # and spawn a single follow-up after the current one finishes.
        self._rebuild_task: Optional[asyncio.Task] = None
        self._rebuild_pending: bool = False

        # Single dedicated write session for the entire agent run.
        # When BOW_AGENT_SINGLE_WRITE_SESSION is set, main_execution opens
        # this once and writes route through it sequentially. Eliminates the
        # multi-session write contention that produced silent state
        # corruption on SQLite under load. None means legacy multi-session
        # mode. See docs/design/single-writer-agent-refactor.md.
        self._writes: Optional[AsyncSession] = None

        # Widget/step state management
        self.current_widget = None
        self.current_step = None
        self.current_step_id = None
        self.current_widget_title = None  # Store widget title for progressive creation

        self.current_query = None

        # Serializes every touch of the shared long-lived DB session
        # (self.db / self._writes) across concurrently-running tool
        # invocations. SQLAlchemy AsyncSession is not safe for concurrent
        # use, and single-writer mode routes ALL writes through one session
        # by design — so tool bodies overlap only on their LLM/codegen and
        # code-execution awaits, never on DB work.
        self._tool_db_lock = asyncio.Lock()

        # create_dashboard streaming state (in-memory, no layout persistence)
        self._dashboard_blocks: list[dict] = []
        self._dashboard_block_sigs: set[str] = set()

        # Streaming text state per block_id
        self._block_text_cache: dict[str, dict[str, str]] = {}
        self._last_planner_prompt_tokens: int | None = None

        # Initialize ContextHub for centralized context management
        self.context_hub = ContextHub(
            db=self.db,
            organization=self.organization,
            report=self.report,
            data_sources=self.data_sources,
            user=getattr(self.head_completion, 'user', None) if self.head_completion else None,
            head_completion=self.head_completion,
            widget=self.widget,
            organization_settings=self.organization_settings,
            build_id=build_id
        )
        # Enhanced registry with metadata-driven filtering
        self.registry = ToolRegistry()

        # Capabilities exposed by the report's attached connections — used to
        # gate file-source tools (list_files / read_file / search_files) so
        # they only appear in the catalog when at least one connection
        # actually exposes those capabilities. Avoids polluting a SQL-only
        # agent with file tools that can never resolve.
        available_capabilities: set[str] = set()
        try:
            from app.schemas.data_source_registry import resolve_client_class
            report = getattr(self, "report", None)
            for ds in (getattr(report, "data_sources", None) or []):
                for conn in (getattr(ds, "connections", None) or []):
                    try:
                        cls = resolve_client_class(conn.type)
                        for cap in getattr(cls, "capabilities", set()) or set():
                            available_capabilities.add(getattr(cap, "value", str(cap)))
                    except Exception:
                        continue
        except Exception:
            pass

        # Start with all available tools for the planner to see, filtered by mode and platform
        all_catalog_dicts = self.registry.get_catalog_for_plan_type(
            "action", self.organization, mode=self.mode, platform=self.platform,
            available_capabilities=available_capabilities,
        )
        all_catalog_dicts.extend(self.registry.get_catalog_for_plan_type(
            "research", self.organization, mode=self.mode, platform=self.platform,
            available_capabilities=available_capabilities,
        ))

        # Hide tools that read raw data when the org has disabled LLM data access.
        # The tool itself also self-blocks at runtime, but excluding it from the
        # catalog keeps the planner from advertising/attempting it.
        allow_llm_see_data_cfg = self.organization_settings.get_config("allow_llm_see_data") if self.organization_settings else None
        allow_llm_see_data = getattr(allow_llm_see_data_cfg, "value", True) if allow_llm_see_data_cfg is not None else True
        if not allow_llm_see_data:
            all_catalog_dicts = [t for t in all_catalog_dicts if t['name'] != 'inspect_data']

        # Remove duplicates (for tools with category="both")
        seen_tools = set()
        unique_catalog = []
        for tool in all_catalog_dicts:
            if tool['name'] not in seen_tools:
                unique_catalog.append(tool)
                seen_tools.add(tool['name'])

        tool_catalog = [ToolDescriptor(**tool) for tool in unique_catalog]
        # BOW_PLANNER selects the planner implementation. Default v3 (native
        # tool_use). Set BOW_PLANNER=v2 to fall back to the legacy JSON
        # envelope planner. Other values fall back to v3 with a warning.
        planner_version = os.environ.get("BOW_PLANNER", "v3").strip().lower()
        if planner_version in ("v2", "2"):
            logger.info("[agent] using planner_v2 (legacy JSON envelope)")
            self.planner = PlannerV2(
                model=self.model,
                tool_catalog=tool_catalog,
                usage_session_maker=async_session_maker,
                usage_context=self.usage_limit_context,
            )
        else:
            if planner_version not in ("v3", "3", ""):
                logger.warning(
                    "[agent] unknown BOW_PLANNER=%r, falling back to v3",
                    planner_version,
                )
            self.planner = PlannerV3(
                model=self.model,
                tool_catalog=tool_catalog,
                usage_session_maker=async_session_maker,
                usage_context=self.usage_limit_context,
            )
        
        # Tool runner with enhanced policies
        self.tool_runner = ToolRunner(
            retry=RetryPolicy(max_attempts=2, backoff_ms=500, backoff_multiplier=2.0, jitter_ms=200),
            timeout=TimeoutPolicy(start_timeout_s=10, idle_timeout_s=180, hard_timeout_s=300),
        )
        
        # Initialize Reporter for title generation
        self.reporter = Reporter(
            model=self.small_model,
            organization_settings=self.organization_settings,
            usage_session_maker=async_session_maker,
            usage_context=self.usage_limit_context,
        )
        # Initialize Judge using ContextHub's instruction builder
        self.judge = Judge(
            model=self.small_model,
            organization_settings=self.organization_settings,
            instruction_context_builder=self.context_hub.instruction_builder,
            usage_session_maker=async_session_maker,
            # Do NOT pass usage_context here. The Judge scores via
            # asyncio.to_thread(llm.inference) (a worker thread), which routes the
            # sync quota check through UsageLimitContext.run_blocking(). With no
            # loop bound on the context that spins up a throwaway event loop and
            # contends for the context's _cache_lock (created on the main loop),
            # raising "Lock is bound to a different event loop" mid-run. Token
            # recording still works via usage_session_maker.
        )

        # Knowledge harness phase replaces the legacy SuggestInstructions post-loop generator.
        # See _run_knowledge_harness for the agentic post-analysis reflection flow.

    async def _resolve_user_profile(self) -> tuple[Optional[str], Optional[str]]:
        """Return (user_name, user_note) for the asker.

        ``user_note`` is the per-org admin-managed note on the asker's
        Membership row — same source of truth as the members table UI.
        Returns ``(None, None)`` for system/non-user runs.
        """
        user = getattr(self.head_completion, 'user', None) if self.head_completion else None
        if not user or not self.organization:
            return None, None
        user_name = getattr(user, 'name', None)
        user_note = None
        try:
            from app.models.membership import Membership
            result = await self.db.execute(
                select(Membership.note).where(
                    Membership.user_id == user.id,
                    Membership.organization_id == self.organization.id,
                )
            )
            user_note = result.scalar_one_or_none()
        except Exception:
            user_note = None
        return user_name, user_note

    async def _build_available_steps_context(self) -> str:
        """Render this report's loadable steps for the planner prompt.

        Mirrors the coder's <available_steps> so the planner knows create_data
        can reuse prior results via load_step instead of re-deriving them.
        """
        if not self.report:
            return ""
        try:
            from app.ai.code_execution.loadables import LoadablesResolver
            resolver = LoadablesResolver(
                self.db,
                self.organization,
                self.report,
                getattr(self.head_completion, 'user', None) if self.head_completion else None,
            )
            section = await resolver.list_for_discovery()
            return section.render() if section else ""
        except Exception:
            return ""

    async def _get_active_artifact(self) -> Optional[dict]:
        """Get the most recent artifact for the current report, enriched with
        visualization-level state so the planner treats it as the starting
        material for the next turn (not a stale label)."""
        if not self.report:
            return None
        try:
            from app.models.artifact import Artifact
            from app.models.visualization import Visualization
            result = await self.db.execute(
                select(Artifact)
                .where(
                    Artifact.report_id == str(self.report.id),
                    Artifact.status == "completed",
                    # Docs (mode='doc') must never occupy the active-artifact slot:
                    # dashboard continuity rules and edit_artifact routing bind to it.
                    Artifact.mode.in_(("page", "slides")),
                )
                .order_by(Artifact.created_at.desc())
                .limit(1)
            )
            artifact = result.scalar_one_or_none()
            if not artifact:
                return None

            viz_ids = []
            if isinstance(artifact.content, dict):
                raw_ids = artifact.content.get("visualization_ids") or []
                viz_ids = [str(v) for v in raw_ids if v]

            visualizations = []
            if viz_ids:
                viz_rows = await self.db.execute(
                    select(Visualization).where(Visualization.id.in_(viz_ids))
                )
                viz_by_id = {str(v.id): v for v in viz_rows.scalars().all()}
                for vid in viz_ids:
                    viz = viz_by_id.get(vid)
                    if not viz:
                        continue
                    step = None
                    try:
                        q = viz.query
                        step = q.default_step if q and q.default_step else (q.steps[-1] if q and q.steps else None)
                    except Exception:
                        step = None

                    columns = []
                    row_count = None
                    step_type = None
                    if step is not None:
                        step_type = step.type
                        data_model = step.data_model if isinstance(step.data_model, dict) else None
                        if data_model:
                            cols = data_model.get("columns") or []
                            columns = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")]
                        data_payload = step.data if isinstance(step.data, dict) else None
                        if data_payload:
                            rows = data_payload.get("rows")
                            if isinstance(rows, list):
                                row_count = len(rows)
                            if not columns:
                                data_cols = data_payload.get("columns") or []
                                columns = [
                                    c.get("field") or c.get("name")
                                    for c in data_cols
                                    if isinstance(c, dict) and (c.get("field") or c.get("name"))
                                ]

                    visualizations.append({
                        "viz_id": vid,
                        "viz_title": viz.title or "",
                        "step_type": step_type,
                        "row_count": row_count,
                        "columns": columns,
                    })

            return {
                "artifact_id": str(artifact.id),
                "title": artifact.title,
                "mode": artifact.mode,
                "version": artifact.version,
                "generation_prompt": artifact.generation_prompt,
                "visualizations": visualizations,
            }
        except Exception:
            logger.exception("_get_active_artifact failed")
            return None

    async def _build_scheduled_context(self) -> Optional[dict]:
        """Build scheduled execution context if this completion is from a scheduled prompt."""
        sp_id = getattr(self.head_completion, 'scheduled_prompt_id', None)
        if not sp_id:
            return None
        try:
            from app.models.scheduled_prompt import ScheduledPrompt
            from sqlalchemy import func as sa_func

            sp = await self.db.get(ScheduledPrompt, sp_id)
            if not sp:
                return None

            past_run_count = await self.db.scalar(
                select(sa_func.count(Completion.id))
                .where(Completion.scheduled_prompt_id == sp_id)
                .where(Completion.id != self.head_completion.id)
            )

            cron_labels = {
                '*/15 * * * *': 'Every 15 minutes',
                '0 * * * *': 'Hourly',
                '0 8 * * *': 'Daily at 8 AM',
                '0 0 * * *': 'Daily at midnight',
                '0 8 * * 1': 'Weekly on Monday at 8 AM',
                '0 0 * * 1': 'Weekly on Monday at midnight',
            }

            return {
                "cron_schedule": sp.cron_schedule,
                "cron_label": cron_labels.get(sp.cron_schedule, sp.cron_schedule),
                "total_past_runs": past_run_count or 0,
                "last_run_at": sp.last_run_at.isoformat() if sp.last_run_at else None,
                "created_at": sp.created_at.isoformat() if sp.created_at else None,
            }
        except Exception:
            return None

    async def _load_images_as_input(self) -> list[ImageInput]:
        """Load image files as base64-encoded ImageInput objects for vision models.

        Only loads images that haven't been consumed by a previous completion
        (i.e. where completion_id is NULL in report_file_association).
        """
        import base64
        import aiofiles
        from app.models.report_file_association import report_file_association

        # Load images that belong to the current completion
        current_cid = str(self.head_completion.id) if self.head_completion else None
        eligible_files = self.image_files
        if current_cid and self.image_files and self.db and self.report:
            try:
                image_file_ids = [str(f.id) for f in self.image_files]
                result = await self.db.execute(
                    select(report_file_association.c.file_id).where(
                        report_file_association.c.report_id == str(self.report.id),
                        report_file_association.c.file_id.in_(image_file_ids),
                        report_file_association.c.completion_id == current_cid,
                    )
                )
                current_ids = {row[0] for row in result.fetchall()}
                eligible_files = [f for f in self.image_files if str(f.id) in current_ids]
            except Exception as e:
                logger.warning(f"Failed to filter images by completion, loading all: {e}")

        images: list[ImageInput] = []
        for f in eligible_files:
            try:
                file_path = getattr(f, 'path', None)
                if not file_path:
                    continue
                async with aiofiles.open(file_path, 'rb') as file:
                    content = await file.read()
                data = base64.b64encode(content).decode('utf-8')
                media_type = getattr(f, 'content_type', 'image/png') or 'image/png'
                images.append(ImageInput(data=data, media_type=media_type, source_type='base64'))
            except Exception as e:
                logger.warning(f"Failed to load image file {getattr(f, 'id', 'unknown')}: {e}")
        return images

    async def estimate_prompt_tokens(self) -> dict:
        """Approximate the total planner prompt tokens without executing tools."""
        try:
            await self.context_hub.prime_static()
            await self.context_hub.refresh_warm()
            try:
                await self.context_hub.build_context()
            except Exception as e:
                logger.warning(f"Failed to build context during token estimation: {e}", exc_info=True)
            prompt_text = await self._build_planner_prompt_text()
            prompt_tokens = count_tokens(prompt_text, getattr(self.model, "model_id", None))

            model_limit = getattr(self.model, "context_window_tokens", None)
            remaining_tokens = None
            if model_limit is not None:
                remaining_tokens = max(model_limit - prompt_tokens, 0)

            return {
                "prompt_tokens": prompt_tokens,
                "model_limit": model_limit,
                "remaining_tokens": remaining_tokens,
            }
        finally:
            try:
                websocket_manager.remove_handler(self._handle_completion_update)
            except Exception as e:
                logger.debug(f"Failed to remove websocket handler during cleanup: {e}")

    async def _resolve_file_references(self):
        """Materialize this report's pinned connector file references (A3) into
        fresh, per-user session files for the current turn, appended to
        analysis_files. The reference is durable; the bytes are fetched under the
        current user each run (never cached) — fresh + per-user-correct."""
        if not (self.db and self.report):
            return
        from sqlalchemy import select
        from app.models.file_reference import FileReference
        from app.models.user import User
        from app.services.file_reference_service import ensure_materialized

        refs = (await self.db.execute(
            select(FileReference).where(FileReference.report_id == str(self.report.id))
        )).scalars().all()
        if not refs:
            return
        uid = getattr(self.head_completion, "user_id", None)
        user = await self.db.get(User, uid) if uid else None
        for ref in refs:
            try:
                f = await ensure_materialized(self.db, ref, user, self.report, self.organization)
                if f and all(getattr(x, "id", None) != f.id for x in self.analysis_files):
                    self.analysis_files.append(f)
            except Exception as e:
                logger.warning(f"_resolve_file_references: ref {getattr(ref, 'id', '?')} failed: {e}")

    async def _run_early_scoring_background(self, planner_input: PlannerInput):
        """Run instructions/context scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            # Score once, up-front. The Judge LLM call is expensive and must NOT
            # sit inside the DB retry loop below — a locked-SQLite write should
            # only retry the write, never re-run the model.
            if self.organization_settings.get_config("enable_llm_judgement") and self.organization_settings.get_config("enable_llm_judgement").value and self.report_type == 'regular':
                judge = Judge(
                    model=self.model,
                    organization_settings=self.organization_settings,
                    usage_session_maker=async_session_maker,
                    # No usage_context: Judge runs in a worker thread; routing the
                    # sync quota check through run_blocking() would contend for the
                    # context's asyncio.Lock across event loops. See note above.
                )
                instructions_score, context_score, ic_reasoning = await judge.score_instructions_and_context_from_planner_input(planner_input)
            else:
                instructions_score = 3
                context_score = 3
                ic_reasoning = ""
        except Exception as e:
            logger.warning(f"Failed to score instructions/context in background: {e}", exc_info=True)
            return

        await self._persist_completion_score_with_retry(
            label="early scoring",
            persist=lambda session, completion: self.project_manager.update_completion_scores(
                session, completion, instructions_score, context_score, reasoning=ic_reasoning
            ),
        )

    async def _run_late_scoring_background(self, messages_context: str, observation_data: dict):
        """Run response scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            # Score once, up-front — keep the Judge LLM call out of the DB retry
            # loop so a locked-SQLite write never triggers a redundant model call.
            if self.organization_settings.get_config("enable_llm_judgement") and self.organization_settings.get_config("enable_llm_judgement").value and self.report_type == 'regular':
                judge = Judge(
                    model=self.model,
                    organization_settings=self.organization_settings,
                    usage_session_maker=async_session_maker,
                    # No usage_context: see note above (cross-loop _cache_lock).
                )
                original_prompt = self.head_completion.prompt.get("content", "") if getattr(self.head_completion, "prompt", None) else ""
                response_score, response_reasoning = await judge.score_response_quality(original_prompt, messages_context, observation_data=observation_data)
            else:
                response_score = 3
                response_reasoning = ""
        except Exception as e:
            logger.warning(f"Failed to score response quality in background: {e}", exc_info=True)
            return

        await self._persist_completion_score_with_retry(
            label="late scoring",
            persist=lambda session, completion: self.project_manager.update_completion_response_score(
                session, completion, response_score, reasoning=response_reasoning
            ),
        )

    async def _persist_completion_score_with_retry(self, label: str, persist):
        """Persist a score to the head completion, retrying only on SQLite lock.

        The LLM scoring is done by the caller before this runs; this helper owns
        the fresh DB session and the locked-database backoff so a contended write
        never re-runs the model. `persist(session, completion)` performs the write.
        """
        import asyncio as _asyncio
        _max_attempts = 4
        for _attempt in range(_max_attempts):
            try:
                SessionLocal = self._session_maker
                async with SessionLocal() as session:
                    # Re-fetch completion to avoid using objects from another session
                    completion = await session.get(Completion, str(self.head_completion.id))
                    if completion is not None:
                        await persist(session, completion)
                return  # success (also returns when completion is gone)
            except Exception as e:
                _is_locked = "database is locked" in str(e).lower()
                if _is_locked and _attempt < _max_attempts - 1:
                    _backoff = 2 ** _attempt  # 1s, 2s, 4s
                    logger.warning(f"SQLite locked in {label} (attempt {_attempt + 1}), retrying in {_backoff}s")
                    await _asyncio.sleep(_backoff)
                    continue
                logger.warning(f"Failed to persist {label} result in background: {e}", exc_info=True)
                return

    async def _run_knowledge_harness(self, conditions: list):
        """Run the Knowledge Harness sub-loop after the main analysis completes.

        This is the agentic replacement for _stream_suggestions_inline. It spins up
        a small planner sub-loop in mode="knowledge" with access to:
        - search_instructions (research existing instructions)
        - describe_tables / inspect_data (verify a fact, sparingly)
        - create_instruction / edit_instruction (capture learnings)

        All instructions land in a draft AI build that is submitted for review
        (matches the existing _stream_suggestions_inline semantics).
        """
        from app.ai.agents.planner import PlannerV2
        from app.ai.agents.suggest_instructions.trigger import InstructionTriggerEvaluator, TriggerCondition

        # Budget: 1 search + up to 2 verify (inspect_data/describe_tables) + up to
        # 4 create/edit + 1 exit. The knowledge prompt biases toward capturing, so
        # the harness needs enough room to search, optionally verify, then write
        # one or more instructions.
        MAX_KNOWLEDGE_HARNESS_STEPS = 10

        # Skip if training mode (training mode finalizes its own build via _finalize_training_build)
        if self.mode == "training":
            return
        if not conditions:
            return

        ai_build = None
        drafts: list = []
        # Collected evidence strings from successful create/edit_instruction
        # tool calls — concatenated into the build's description (commit
        # message style) at the end of the harness run.
        harness_evidence: list = []
        prior_mode = self.mode

        try:
            seq_si = await self.project_manager.next_seq(self.db, self.current_execution)
            await self._emit_sse_event(SSEEvent(
                event="instructions.suggest.started",
                completion_id=str(self.system_completion.id),
                agent_execution_id=str(self.current_execution.id),
                seq=seq_si,
                data={}
            ))
        except Exception as e:
            logger.debug(f"Failed to emit harness started event: {e}")

        try:
            # === Lazy draft creation ===
            # Don't pre-seed an AI build here. If the harness actually runs
            # create_instruction / edit_instruction, those tools will lazily
            # create the draft on the first add_to_build call and write the
            # id back into runtime_ctx['training_build_id'], which we capture
            # below. This avoids accumulating empty drafts when the harness
            # runs but doesn't make any actual edits.
            self.training_build_id = None

            # === Build a knowledge-mode tool catalog ===
            knowledge_catalog_dicts = self.registry.get_catalog_for_plan_type(
                "action", self.organization, mode="knowledge", platform=self.platform
            )
            knowledge_catalog_dicts.extend(
                self.registry.get_catalog_for_plan_type(
                    "research", self.organization, mode="knowledge", platform=self.platform
                )
            )
            seen = set()
            unique = []
            for t in knowledge_catalog_dicts:
                if t['name'] not in seen:
                    unique.append(t)
                    seen.add(t['name'])
            knowledge_tool_catalog = [ToolDescriptor(**t) for t in unique]

            if not knowledge_tool_catalog:
                logger.warning("Knowledge harness has no tools available; aborting")
                return

            # === Spin up a planner instance with the knowledge catalog ===
            knowledge_planner = PlannerV2(
                model=self.small_model or self.model,
                tool_catalog=knowledge_tool_catalog,
                usage_session_maker=async_session_maker,
            )

            # Format trigger reasons for prompt injection
            trigger_block = TriggerCondition.format_for_prompt(conditions)
            trigger_reason = "; ".join(c.get("name", "") for c in conditions) if conditions else ""

            # Use existing context view (already includes full session history)
            view = self.context_hub.get_view()
            instructions_text = view.static.instructions.render() if view.static.instructions else ""
            schemas_text = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
            try:
                messages_section = await self.context_hub.message_builder.build(max_messages=20)
                messages_context = messages_section.render() if messages_section else ""
            except Exception:
                messages_context = ""

            # Switch into knowledge mode for tool runner / mode checks
            self.mode = "knowledge"

            observation = None
            step_count = 0

            for step in range(MAX_KNOWLEDGE_HARNESS_STEPS):
                if self.sigkill_event.is_set():
                    break
                step_count += 1

                user_name, user_note = await self._resolve_user_profile()
                planner_input = PlannerInput(
                    organization_name=self.organization.name,
                    organization_ai_analyst_name=self.ai_analyst_name,
                    timezone=self.org_timezone,
                    locale=self.org_locale,
                    week_start=self.org_week_start,
                    instructions=instructions_text,
                    user_message=self.head_completion.prompt.get("content", "") if self.head_completion and self.head_completion.prompt else "",
                    schemas_combined=schemas_text,
                    messages_context=messages_context,
                    last_observation=observation,
                    past_observations=self.context_hub.observation_builder.tool_observations,
                    tool_catalog=knowledge_tool_catalog,
                    mode="knowledge",
                    trigger_conditions=trigger_block,
                    external_platform=self.platform,
                    user_name=user_name,
                    user_note=user_note,
                )

                # Run the planner and capture the final decision
                final_decision = None
                async for evt in knowledge_planner.execute(planner_input, self.sigkill_event):
                    if evt.type == "planner.decision.final":
                        final_decision = evt.data
                        break

                if not final_decision:
                    break

                # === Persist the harness plan_decision + decision block ===
                # Use a distinct loop_index namespace so the harness blocks don't
                # collide with main-loop blocks in upsert_block_for_decision's lookup.
                harness_loop_index = 1000 + step
                harness_plan_decision = None
                try:
                    decision_seq_h = await self.project_manager.next_seq(self.db, self.current_execution)
                    harness_plan_decision = await self.project_manager.save_plan_decision_from_model(
                        self.db,
                        agent_execution=self.current_execution,
                        seq=decision_seq_h,
                        loop_index=harness_loop_index,
                        planner_decision_model=final_decision,
                        phase="knowledge_harness",
                    )
                except Exception as _pd_exc:
                    logger.warning(f"Knowledge harness: save_plan_decision_from_model failed: {_pd_exc!r}")

                harness_decision_block = None
                if harness_plan_decision is not None:
                    try:
                        harness_decision_block = await self.project_manager.upsert_block_for_decision(
                            self.db,
                            completion=self.system_completion,
                            agent_execution=self.current_execution,
                            plan_decision=harness_plan_decision,
                        )
                        if harness_decision_block is not None:
                            try:
                                block_schema = await serialize_block_v2(self.db, harness_decision_block)
                                seq_blk = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="block.upsert",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq_blk,
                                    data={"block": block_schema.model_dump()},
                                ))
                            except Exception:
                                pass
                    except Exception as _blk_exc:
                        logger.warning(f"Knowledge harness: upsert_block_for_decision failed: {_blk_exc!r}")

                # Done?
                if getattr(final_decision, "analysis_complete", False) and not getattr(final_decision, "action", None):
                    break

                action = getattr(final_decision, "action", None)
                if not action:
                    break

                tool_name = action.name
                tool_input = action.arguments or {}

                tool = self.registry.get(tool_name)
                if not tool:
                    logger.warning(f"Knowledge harness: unknown tool '{tool_name}'")
                    observation = {
                        "summary": f"Unknown tool '{tool_name}'",
                        "error": {"code": "unknown_tool", "message": tool_name},
                    }
                    continue

                # === Start tool execution tracking (persisted row + tool.started SSE) ===
                tool_execution = await self.project_manager.start_tool_execution_from_models(
                    self.db,
                    agent_execution=self.current_execution,
                    plan_decision_id=(str(harness_plan_decision.id) if harness_plan_decision else None),
                    tool_name=tool_name,
                    tool_action=getattr(action, "type", None),
                    tool_input_model=tool_input,
                )

                runtime_ctx = {
                    "db": self.db,
                    "organization": self.organization,
                    "user": getattr(self.head_completion, 'user', None) if self.head_completion else None,
                    "settings": self.organization_settings,
                    "report": self.report,
                    "head_completion": self.head_completion,
                    "system_completion": self.system_completion,
                    "project_manager": self.project_manager,
                    "model": self.model,
                    "sigkill_event": self.sigkill_event,
                    "observation_context": self.context_hub.observation_builder.to_dict(),
                    "context_view": view,
                    "context_hub": self.context_hub,
                    "ds_clients": self.clients,
                    "usage_limit_context": self.usage_limit_context,
                    "training_build_id": self.training_build_id,
                    "agent_execution_id": str(self.current_execution.id) if self.current_execution else None,
                    "mode": "knowledge",
                    "is_eval_run": self.is_eval_run,
                    "platform": self.platform,
                    "platform_context": self.platform_context,
                    "tool_call_id": str(tool_execution.id) if tool_execution else None,
                    "pending_officejs_registry": pending_officejs_registry,
                }
                try:
                    seq_ts = await self.project_manager.next_seq(self.db, self.current_execution)
                    await self._emit_sse_event(SSEEvent(
                        event="tool.started",
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        seq=seq_ts,
                        data={"tool_name": tool_name, "arguments": tool_input},
                    ))
                except Exception:
                    pass

                # Forward tool streaming events (tool.progress / stdout / partial / error)
                # to the UI, same as the main loop.
                async def _harness_emit(ev: dict, _tn=tool_name, _ti=tool_input):
                    try:
                        await self._handle_streaming_event(_tn, ev, _ti)
                    except Exception:
                        pass
                    if ev.get("type") in ("tool.progress", "tool.error", "tool.partial", "tool.stdout", "tool.confirmation"):
                        try:
                            seq_ev = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event=ev.get("type", "tool.progress"),
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq_ev,
                                data={"tool_name": _tn, "payload": ev.get("payload", {})},
                            ))
                        except Exception:
                            pass

                tool_output = None
                try:
                    tool_result = await self.tool_runner.run(tool, tool_input, runtime_ctx, _harness_emit)
                except Exception as run_err:
                    logger.warning(f"Knowledge harness tool '{tool_name}' raised: {run_err}")
                    observation = {
                        "summary": f"{tool_name} raised an error",
                        "error": {"code": "tool_error", "message": str(run_err)},
                    }
                    tool_result = None

                # Capture lazily-created training_build_id back from the tool
                # so subsequent harness tool calls share the same draft and the
                # final submit step can act on it.
                if runtime_ctx.get("training_build_id") and not self.training_build_id:
                    self.training_build_id = runtime_ctx["training_build_id"]

                if tool_result is not None:
                    if isinstance(tool_result, dict) and "observation" in tool_result:
                        observation = tool_result.get("observation")
                        tool_output = tool_result.get("output")
                    else:
                        observation = tool_result
                        tool_output = None

                # === Finish tool execution tracking + upsert block + emit tool.finished ===
                try:
                    _is_stopped = bool(observation and observation.get("stopped"))
                    await self.project_manager.finish_tool_execution_from_models(
                        self.db,
                        tool_execution=tool_execution,
                        result_model=tool_output,
                        summary=observation.get("summary", "") if observation else "",
                        error_message=_observation_error_message(observation),
                        success=bool(observation and not _observation_failed(observation) and not _is_stopped),
                    )
                except Exception as _fin_err:
                    logger.warning(f"Knowledge harness: finish_tool_execution failed: {_fin_err!r}")

                # Update the existing harness decision block with tool info (same
                # helper used by the main loop — merges tool_execution into the
                # decision block rather than creating a second block).
                try:
                    updated_block = await self.project_manager.upsert_block_for_tool(
                        self.db,
                        completion=self.system_completion,
                        agent_execution=self.current_execution,
                        tool_execution=tool_execution,
                    )
                    if updated_block is not None:
                        try:
                            block_schema = await serialize_block_v2(self.db, updated_block)
                            seq_blk = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event="block.upsert",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq_blk,
                                data={"block": block_schema.model_dump()},
                            ))
                        except Exception:
                            pass
                except Exception as _btu_exc:
                    logger.warning(f"Knowledge harness: upsert_block_for_tool failed: {_btu_exc!r}")

                try:
                    _is_stopped = bool(observation and observation.get("stopped"))
                    _tool_status = "stopped" if _is_stopped else ("error" if _observation_failed(observation) else "success")
                    seq_fin = await self.project_manager.next_seq(self.db, self.current_execution)
                    safe_result_json = None
                    if tool_output is not None:
                        try:
                            safe_result_json = json.loads(json.dumps(tool_output, default=str))
                        except Exception:
                            safe_result_json = {"summary": observation.get("summary", "") if observation else ""}
                    await self._emit_sse_event(SSEEvent(
                        event="tool.finished",
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        seq=seq_fin,
                        data={
                            "tool_name": tool_name,
                            "tool_execution_id": str(tool_execution.id) if tool_execution is not None else None,
                            "status": _tool_status,
                            "result_summary": observation.get("summary", "") if observation else "",
                            "result_json": safe_result_json,
                            "duration_ms": getattr(tool_execution, "duration_ms", None),
                        },
                    ))
                except Exception:
                    pass

                if tool_result is None:
                    # tool raised — skip the rest of this iteration but loop continues
                    continue

                # Capture training_build_id if the tool created one
                if runtime_ctx.get("training_build_id") and not self.training_build_id:
                    self.training_build_id = runtime_ctx["training_build_id"]

                # Collect evidence from successful create/edit calls so we can
                # stitch a build description ("commit message") at the end.
                if tool_name in ("create_instruction", "edit_instruction"):
                    if isinstance(tool_output, dict) and tool_output.get("success") and isinstance(tool_input, dict):
                        ev_text = tool_input.get("evidence")
                        if ev_text:
                            verb = "Added" if tool_name == "create_instruction" else "Edited"
                            title = tool_output.get("title") or tool_input.get("title") or "instruction"
                            harness_evidence.append(f"- **{verb} {title}**: {ev_text}")

                # Stream a partial event for create/edit instruction successes
                if tool_name in ("create_instruction", "edit_instruction"):
                    inst_id = None
                    if isinstance(tool_output, dict):
                        inst_id = tool_output.get("instruction_id")
                    if inst_id:
                        try:
                            from app.models.instruction import Instruction
                            from sqlalchemy import select as _select
                            from sqlalchemy.orm import lazyload as _lazyload
                            # Only column reads (trigger_reason, ai_source) — suppress cascade
                            res = await self.db.execute(
                                _select(Instruction).where(Instruction.id == inst_id).options(_lazyload("*"))
                            )
                            inst = res.scalar_one_or_none()
                        except Exception:
                            inst = None
                        if inst is not None:
                            # Tag the instruction with trigger metadata if not already set
                            try:
                                if trigger_reason and not getattr(inst, 'trigger_reason', None):
                                    inst.trigger_reason = trigger_reason
                                if not getattr(inst, 'ai_source', None):
                                    inst.ai_source = "completion"
                                await self.db.commit()
                            except Exception:
                                await self.db.rollback()

                            draft_payload = {
                                "id": str(inst.id),
                                "title": inst.title,
                                "text": inst.text,
                                "category": inst.category,
                                "status": inst.status,
                                "private_status": getattr(inst, 'private_status', None),
                                "global_status": getattr(inst, 'global_status', None),
                                "is_seen": getattr(inst, 'is_seen', None),
                                "can_user_toggle": getattr(inst, 'can_user_toggle', None),
                                "user_id": getattr(inst, 'user_id', None),
                                "organization_id": str(inst.organization_id),
                                "agent_execution_id": str(inst.agent_execution_id) if getattr(inst, 'agent_execution_id', None) else None,
                                "trigger_reason": getattr(inst, 'trigger_reason', None),
                                "created_at": inst.created_at.isoformat() if getattr(inst, 'created_at', None) else None,
                                "updated_at": inst.updated_at.isoformat() if getattr(inst, 'updated_at', None) else None,
                                "ai_source": getattr(inst, 'ai_source', None),
                                "build_id": str(ai_build.id) if ai_build else None,
                            }
                            drafts.append(draft_payload)
                            try:
                                seq_p = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="instructions.suggest.partial",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq_p,
                                    data={"instruction": draft_payload}
                                ))
                            except Exception as e:
                                logger.debug(f"Failed to emit harness partial event: {e}")

                # If the planner also flagged completion this turn, exit
                if getattr(final_decision, "analysis_complete", False):
                    break

            # === Submit AI build for review (don't auto-publish) ===
            # Only fires if a tool actually lazy-created a draft this harness run.
            if self.training_build_id and len(drafts) > 0:
                try:
                    from app.services.build_service import BuildService
                    build_service = BuildService()
                    # Attach a description built from tool-call evidence
                    # strings, if any. Kept simple — no second LLM call.
                    if harness_evidence:
                        try:
                            description = "\n".join(harness_evidence)
                            await build_service.update_build_description(
                                self.db, self.training_build_id, description
                            )
                        except Exception as desc_err:
                            logger.warning(f"Failed to set build description: {desc_err}")
                    await build_service.submit_build(self.db, self.training_build_id)
                    logger.info(
                        f"Knowledge harness submitted AI build {self.training_build_id} for approval "
                        f"with {len(drafts)} instructions ({step_count} steps)"
                    )
                    # Self Learning: react to this brand-new suggestion build per
                    # each affected agent's policy (auto-approve / auto-eval).
                    # Fire-and-forget; never blocks the harness. This is the
                    # knowledge-harness creation site — distinct from report
                    # training-mode finalize, which is intentionally NOT hooked.
                    try:
                        from app.services.agent_reliability_service import AgentReliabilityService
                        _sl_user_id = getattr(self.head_completion, "user_id", None) if self.head_completion else None
                        AgentReliabilityService().schedule_for_suggestion(
                            organization_id=str(self.organization.id),
                            build_id=str(self.training_build_id),
                            user_id=str(_sl_user_id) if _sl_user_id else None,
                        )
                    except Exception as sl_err:
                        logger.warning(f"Self Learning schedule failed for build {self.training_build_id}: {sl_err}")
                except Exception as submit_err:
                    logger.warning(f"Failed to submit AI build for approval: {submit_err}")

            try:
                seq_f = await self.project_manager.next_seq(self.db, self.current_execution)
                await self._emit_sse_event(SSEEvent(
                    event="instructions.suggest.finished",
                    completion_id=str(self.system_completion.id),
                    agent_execution_id=str(self.current_execution.id),
                    seq=seq_f,
                    data={"instructions": drafts}
                ))
            except Exception as e:
                logger.debug(f"Failed to emit harness finished event: {e}")

        except Exception as e:
            logger.warning(f"Knowledge harness failed (non-critical): {e}", exc_info=True)
            try:
                seq_e = await self.project_manager.next_seq(self.db, self.current_execution)
                await self._emit_sse_event(SSEEvent(
                    event="instructions.suggest.finished",
                    completion_id=str(self.system_completion.id),
                    agent_execution_id=str(self.current_execution.id),
                    seq=seq_e,
                    data={"instructions": drafts, "error": str(e)}
                ))
            except Exception:
                pass
        finally:
            # Restore the original mode
            self.mode = prior_mode

    async def _generate_title_background(self, messages_context: str, plan_info: list, report_id: str):
        """Generate and persist the report title in its own DB session.

        Awaited inline by the caller (see main_execution) rather than spawned as a
        fire-and-forget task — a discarded asyncio.create_task is only weakly
        referenced by the loop and was routinely garbage-collected on Postgres
        (pooled connections recycle the instant the response finishes) before its
        LLM call returned, silently skipping the title.

        `report_id` is passed in as a plain string and the report is re-fetched in
        this method's own session, so we never touch a `self.report` that may be
        detached from a closed session ("Instance is not bound to a Session").
        """
        import logging
        logger = logging.getLogger(__name__)
        try:
            SessionLocal = self._session_maker
            async with SessionLocal() as session:
                try:
                    title = await self.reporter.generate_report_title(messages_context, plan_info)
                    if not title or not title.strip():
                        logger.warning("Title generation returned empty result")
                        return
                    title = title.strip()
                    # Re-fetch report using select query (more reliable than session.get with UUID).
                    # lazyload("*") suppresses Report's lazy="selectin" cascade (14 rels +
                    # downstream DS/widget/query graph) — update_report_title only touches title.
                    from sqlalchemy.orm import lazyload as _lazyload
                    stmt = select(Report).where(Report.id == report_id).options(_lazyload("*"))
                    result = await session.execute(stmt)
                    report = result.scalar_one_or_none()
                    if not report:
                        logger.warning(f"Report not found for title update: {report_id}")
                        return
                    # Only write while the title is still a placeholder. The caller
                    # now gates on the same condition, but it can run on multiple
                    # turns (value-gated, self-healing); re-checking here under a
                    # fresh read avoids clobbering a real title a concurrent turn
                    # may have just set.
                    existing = (report.title or "").strip()
                    if existing.lower() not in ("", "untitled report"):
                        logger.info(f"Report {report_id} already titled; skipping")
                        return
                    await self.project_manager.update_report_title(session, report, title)
                    logger.info(f"Report title updated to: {title}")
                except Exception as e:
                    logger.error(f"Failed to generate/update report title: {e}")
        except Exception as e:
            logger.error(f"Failed to create session for title generation: {e}")

    def _follow_ups_enabled(self) -> bool:
        """True only for web sessions (platform is None) when the org's
        enable_follow_ups setting is on. Slack/Teams/Email/Excel/scheduled runs
        all carry a non-null platform and are excluded."""
        if self.platform is not None:
            return False
        if not self.organization_settings:
            return False
        cfg = self.organization_settings.get_config("enable_follow_ups")
        if not cfg:
            return False
        if getattr(cfg, "state", None) and cfg.state != "enabled":
            return False
        return bool(getattr(cfg, "value", False))

    async def _generate_and_emit_follow_ups(self):
        """Generate follow-up questions on the small model, persist them on the
        system completion, and emit a `completion.follow_ups` SSE event so the UI
        renders chips live (before [DONE]). Best-effort: never raises."""
        import logging
        logger = logging.getLogger(__name__)

        try:
            messages_section = await self.context_hub.message_builder.build(max_messages=8)
            messages_context = messages_section.render()
        except Exception:
            try:
                messages_context = await self.context_hub.get_messages_context(max_messages=8)
            except Exception:
                messages_context = ""

        if not messages_context:
            return

        # Gather grounding context for both modes. Chat/deep suggestions reference
        # real schema dimensions; training suggestions reason about what the
        # instruction set covers vs. the schema (gaps/conflicts/overlap), so both
        # need schema + instructions. The per-mode framing happens in the Reporter.
        mode = self.mode or "chat"
        schemas_context = ""
        instructions_context = ""
        try:
            schemas_ctx = await self.context_hub.schema_builder.build(with_stats=False)
            schemas_context = schemas_ctx.render_combined(
                top_k_per_ds=self.top_k_schema, index_limit=INDEX_LIMIT
            )
        except Exception:
            try:
                view = self.context_hub.get_view()
                schemas_context = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
            except Exception:
                schemas_context = ""
        try:
            instr_section = await self.context_hub.instruction_builder.build()
            instructions_context = instr_section.render()
        except Exception:
            instructions_context = ""
        # Keep the small-model call cheap — cap the grounding context. Training
        # mode is about curating instructions, so give it more instruction budget.
        schemas_context = (schemas_context or "")[:6000]
        instructions_context = (instructions_context or "")[: (6000 if mode == "training" else 3000)]

        questions = await self.reporter.generate_follow_ups(
            messages_context,
            mode=mode,
            schemas_context=schemas_context,
            instructions_context=instructions_context,
        )
        if not questions:
            return

        # Persist on the system completion (reassign so SQLAlchemy detects the
        # JSON change — same pattern as judge_json).
        try:
            self.system_completion.follow_ups = list(questions)
            self.db.add(self.system_completion)
            await self.db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist follow_ups: {e}")
            try:
                await self.db.rollback()
            except Exception:
                pass

        # Emit live so the chips appear without waiting for a reload.
        if self.event_queue:
            try:
                await self.event_queue.put(SSEEvent(
                    event="completion.follow_ups",
                    completion_id=str(self.system_completion.id),
                    data={"questions": list(questions)},
                ))
            except Exception as e:
                logger.warning(f"Failed to emit follow_ups event: {e}")

    def _build_slim_context_snapshot(self, view, top_k_schema: int = 10) -> dict:
        """
        Build a slim context snapshot that only includes usage tracking data.
        
        Excludes full schemas and instructions to avoid redundant storage.
        Only saves what was actually sent to the LLM.
        """
        # Start with full view but we'll replace large sections
        data = view.model_dump()
        
        try:
            # Replace full schemas with usage tracking only
            if view.static.schemas:
                schemas_usage = view.static.schemas.get_usage_snapshot(top_k_per_ds=top_k_schema)
                data["schemas_usage"] = schemas_usage.model_dump()
                # Remove full schemas to save space
                if "static" in data and "schemas" in data["static"]:
                    data["static"]["schemas"] = None
            
            # Replace full instructions with usage tracking only
            if view.static.instructions and view.static.instructions.items:
                data["instructions_usage"] = [
                    item.model_dump() for item in view.static.instructions.items
                ]
                # Remove full instructions to save space
                if "static" in data and "instructions" in data["static"]:
                    data["static"]["instructions"] = None
        except Exception:
            pass  # Usage tracking is optional, don't fail if it errors
        
        return data

    async def _save_context_snapshot_background(self, kind: str, context_view_json: dict, prompt_text: str = ""):
        """Save context snapshot. Routes through _writes_session so single-
        writer mode shares self._writes (no fresh session, no contention),
        while legacy mode opens a fresh short-lived session as before."""
        try:
            async with self._writes_session() as session:
                try:
                    # Re-fetch agent execution in this session
                    agent_execution = await session.get(type(self.current_execution), self.current_execution.id)
                    if agent_execution:
                        await self.project_manager.save_context_snapshot(
                            session,
                            agent_execution=agent_execution,
                            kind=kind,
                            context_view_json=context_view_json,
                            prompt_text=prompt_text,
                        )
                except Exception:
                    pass
        except Exception:
            pass

    async def _record_instruction_usage_background(self, instruction_items: list):
        """Record instruction usage events. Routes through _writes_session
        so single-writer mode shares self._writes (no extra session, no
        contention); legacy mode opens a fresh short-lived session."""
        if not instruction_items:
            return
        try:
            async with self._writes_session() as session:
                try:
                    service = InstructionUsageService()
                    items_data = []
                    for item in instruction_items:
                        # Handle both Pydantic models and dicts
                        if hasattr(item, 'model_dump'):
                            item_dict = item.model_dump()
                        elif hasattr(item, 'dict'):
                            item_dict = item.dict()
                        elif isinstance(item, dict):
                            item_dict = item
                        else:
                            continue
                        items_data.append(item_dict)
                    
                    if items_data:
                        user_id = str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None
                        await service.record_batch_usage(
                            db=session,
                            org_id=str(self.organization.id),
                            report_id=str(self.report.id) if self.report else None,
                            user_id=user_id,
                            items=items_data,
                            user_role=None,  # Role not easily accessible here
                        )
                except Exception:
                    pass
        except Exception:
            pass

    async def _handle_completion_update(self, message: str):
        # Mirror existing sigkill behavior
        try:
            import json
            data = json.loads(message)
            if (
                data.get("event") == "update_completion"
                and data.get("completion_id") == str(self.system_completion.id)
                and data.get("sigkill") is not None
            ):
                self.sigkill_event.set()
        except Exception:
            pass

    async def _persist_planning_block_partial(
        self,
        block_id: str,
        loop_index: int,
        block_index: int,
        reasoning: str | None,
        content: str | None,
    ):
        """Persist partial planning reasoning/content into the (pre-created)
        decision block, inserting the skeleton row on first call.

        Called on the PlanningTextStreamer snapshot cadence (~1.2s) so a client
        resuming from persisted state — page refresh, watch-endpoint DB tail —
        sees live partial text instead of an empty block. decision.final's
        upsert_block_for_decision finds this row by (agent_execution_id,
        loop_index, source_type='decision') and completes it in place, keeping
        the block id stable with the streamed placeholder.
        """
        try:
            if not self.current_execution or not self.system_completion or not block_id:
                return
            from app.models.completion_block import CompletionBlock
            block = await self.db.get(CompletionBlock, block_id)
            if block is None:
                block = CompletionBlock(
                    id=block_id,
                    completion_id=str(self.system_completion.id),
                    agent_execution_id=str(self.current_execution.id),
                    source_type='decision',
                    plan_decision_id=None,
                    tool_execution_id=None,
                    block_index=block_index,
                    loop_index=loop_index,
                    title='Planning (action)',
                    status='in_progress',
                    icon='🧠',
                    started_at=datetime.utcnow(),
                )
            if content is not None and content.strip():
                block.content = content
            if reasoning is not None and reasoning.strip():
                block.reasoning = reasoning
            self.db.add(block)
            await self.db.commit()
        except Exception:
            # Best-effort; never disrupt the planning stream
            try:
                await self.db.rollback()
            except Exception:
                pass

    async def _capture_telemetry_background(self, event_name: str, properties: dict):
        """Capture telemetry in background to avoid blocking main execution."""
        try:
            await telemetry.capture(
                event_name,
                properties,
                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                org_id=str(self.organization.id) if self.organization else None,
            )
        except Exception:
            pass

    async def _update_context_token_metadata_background(self, view):
        """Update context token metadata in background."""
        try:
            await self._update_context_token_metadata(view)
        except Exception:
            pass

    async def _apply_tool_permission_filter(self) -> None:
        """Remove tools from the planner catalog whose required_permissions the user doesn't hold on any DS."""
        if not self.db or not self.organization:
            return
        user = getattr(self.head_completion, 'user', None)
        if not user:
            return

        restricted: dict[str, list[str]] = {}
        for t in (self.planner.tool_catalog or []):
            meta = self.registry.get_metadata(t.name)
            for perm in getattr(meta, 'required_permissions', []):
                restricted.setdefault(perm, []).append(t.name)

        if not restricted:
            return

        from app.core.permission_resolver import get_ds_ids_with_permission
        denied_tools: set[str] = set()
        for perm, tool_names in restricted.items():
            is_full_admin, ds_ids = await get_ds_ids_with_permission(
                self.db, str(user.id), str(self.organization.id), perm
            )
            if not is_full_admin and not ds_ids:
                denied_tools.update(tool_names)

        if denied_tools:
            self.planner.tool_catalog = [
                t for t in self.planner.tool_catalog
                if t.name not in denied_tools
            ]

    async def _apply_email_availability_filter(self) -> None:
        """Hide ``send_email`` from the planner catalog when no outbound email
        transport resolves for this org.

        The tool is registered as always-active (so it stays executable), but it
        must only be advertised to the planner when email can actually be sent.
        Availability mirrors the send path's resolver (AI mailbox → org SMTP →
        global), so a tool configured via the UI (org SMTP) counts even when the
        global bow-config SMTP is empty.
        """
        catalog = self.planner.tool_catalog or []
        if not any(t.name == "send_email" for t in catalog):
            return

        available = False
        if self.db and self.organization:
            try:
                from app.services.email_client_resolver import is_outbound_available
                available = await is_outbound_available(
                    self.db, str(self.organization.id), purpose="analyst"
                )
            except Exception:
                logger.warning("[agent] email availability check failed", exc_info=True)
                available = False

        if not available:
            self.planner.tool_catalog = [t for t in catalog if t.name != "send_email"]

    def _schedule_bg_write(self, label: str, coro):
        """Schedule a background DB write coroutine.

        The coroutine is wrapped so failures are logged with
        `[agent.bg_write]` rather than escaping (which would crash the
        event loop). The task is tracked in ``self._pending_writes`` so
        ``_drain_bg_writes`` can wait on it before
        ``completion.finished`` is emitted.
        """
        async def _runner():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._bg_write_failures += 1
                logger.error(
                    "[agent.bg_write] %s failed: %r (failures so far: %d)",
                    label, exc, self._bg_write_failures,
                    exc_info=True,
                )
        task = asyncio.create_task(_runner(), name=f"agent.bg_write.{label}")
        self._pending_writes.append(task)
        return task

    def _use_single_write_session(self) -> bool:
        """Whether this agent run should route writes through the single
        dedicated `self._writes` session (the single-writer architecture
        from docs/design/single-writer-agent-refactor.md).

        Always on for SQLite: SQLite permits only one write transaction at a
        time, so running the agent with multiple concurrent write sessions
        produces "database is locked" and dropped writes — e.g. the create_data
        step finalize (UPDATE steps SET code/data/status) times out against the
        busy_timeout and the step is left an empty draft ("No data to display").
        Single-writer serializes all writes through one connection, which is the
        only correct mode for SQLite. On other backends (Postgres) it remains
        opt-in via BOW_AGENT_SINGLE_WRITE_SESSION.
        """
        if self._is_sqlite_backend():
            return True
        return os.environ.get(
            "BOW_AGENT_SINGLE_WRITE_SESSION", ""
        ).lower() in ("1", "true", "yes")

    def _is_sqlite_backend(self) -> bool:
        """True when the agent's DB sessions are bound to a SQLite engine."""
        try:
            bind = getattr(self._session_maker, "kw", {}).get("bind")
            name = getattr(getattr(bind, "dialect", None), "name", "") or ""
            if name:
                return name == "sqlite"
        except Exception:
            pass
        try:
            from app.settings.config import settings as _settings
            return "sqlite" in (_settings.bow_config.database.get_url() or "").lower()
        except Exception:
            return False

    @asynccontextmanager
    async def _writes_session(self):
        """Yield a session for write operations.

        - When ``BOW_AGENT_SINGLE_WRITE_SESSION`` is on AND ``self._writes``
          is open: yield ``self._writes`` directly (no enter/exit). All
          writers in this run share one session, eliminating the
          multi-session contention that produced silent state corruption.
        - Otherwise: open a fresh short-lived session via
          ``self._session_maker()`` and close it on exit. Mirrors current
          behavior; safe rollback path while phase 2 migrations land.

        Caller responsibilities:
        - Use ``async with self._writes_session() as db: ...``
        - Do NOT close the yielded session yourself in single-writer mode;
          the context manager keeps it open until run completion.
        - Commit explicitly when needed; the manager doesn't auto-commit.
        """
        if self._use_single_write_session() and self._writes is not None:
            yield self._writes
        else:
            async with self._session_maker() as db:
                yield db

    async def _drain_bg_writes(self, *, timeout_s: float = 10.0):
        """Wait for all scheduled background writes to complete.

        Called before emitting ``completion.finished`` so the API doesn't
        return a "done" signal while writes are still in flight.
        Bounded by ``timeout_s`` to avoid hanging the user-facing response
        on a stuck DB; any tasks still pending after the timeout are
        logged but not awaited further (they continue running on the loop).
        """
        # Pull in the rebuild task too — `completion.finished` shouldn't
        # land before the transcript reflects the latest blocks.
        if self._rebuild_task is not None and not self._rebuild_task.done():
            self._pending_writes.append(self._rebuild_task)
        if not self._pending_writes:
            return
        pending = list(self._pending_writes)
        self._pending_writes = []
        try:
            done, still_pending = await asyncio.wait(
                pending,
                timeout=timeout_s,
                return_when=asyncio.ALL_COMPLETED,
            )
            if still_pending:
                names = [t.get_name() for t in still_pending]
                logger.warning(
                    "[agent.bg_write] drain timeout after %.1fs; %d task(s) still pending: %s",
                    timeout_s, len(still_pending), names,
                )
                # Re-park them so the next drain (or another caller) can wait
                self._pending_writes.extend(still_pending)
        except Exception as exc:
            logger.error("[agent.bg_write] drain failed: %r", exc, exc_info=True)

    def _request_rebuild_transcript(self):
        """Coalesced rebuild_completion_from_blocks scheduler.

        Two call sites used to spawn a rebuild task each — once after
        plan_decision was saved, once after tool_execution was saved.
        rebuild_completion_from_blocks reads ALL blocks for the current
        agent execution; the second rebuild fully supersedes the first.
        Under load this doubled the bg-write traffic against the singleton
        pool for no gain.

        Now we keep at most one rebuild task in flight per agent:
          - if no task is running, spawn one immediately
          - if a task is running, set `_rebuild_pending` so the running
            task chains a follow-up after it completes
          - the follow-up captures any state that landed during the
            previous run, so we never miss a request

        Drains via `_drain_bg_writes` so `completion.finished` doesn't
        race ahead of the final transcript build.
        """
        # If a task is already running, just mark that another is wanted.
        if self._rebuild_task is not None and not self._rebuild_task.done():
            self._rebuild_pending = True
            return

        if not self.system_completion or not self.current_execution:
            return

        comp_id = str(self.system_completion.id)
        exec_id = str(self.current_execution.id)

        async def _runner(_loop_index=getattr(self, "_loop_index_marker", None)):
            from app.models.agent_execution import AgentExecution as _AE
            from app.models.completion import Completion as _Comp
            _max_attempts = 4
            for _attempt in range(_max_attempts):
                try:
                    async with self._session_maker() as bg_db:
                        bg_exec = await bg_db.get(_AE, exec_id)
                        bg_comp = await bg_db.get(_Comp, comp_id)
                        if bg_exec and bg_comp:
                            await self.project_manager.rebuild_completion_from_blocks(
                                bg_db, bg_comp, bg_exec
                            )
                    break
                except Exception as exc:
                    if "database is locked" in str(exc).lower() and _attempt < _max_attempts - 1:
                        await asyncio.sleep(2 ** _attempt)
                        continue
                    logger.warning(f"[agent] rebuild_completion failed: {exc!r}")
                    break
            # Chain a follow-up if requests piled up during this run.
            if self._rebuild_pending:
                self._rebuild_pending = False
                self._rebuild_task = asyncio.create_task(
                    _runner(), name="agent.rebuild_transcript"
                )

        self._rebuild_task = asyncio.create_task(_runner(), name="agent.rebuild_transcript")

    async def _rebuild_completion_sync_if_single_writer(self) -> bool:
        """Run rebuild_completion_from_blocks synchronously on self._writes.

        Returns True when single-writer mode handled the rebuild (caller
        should skip _request_rebuild_transcript). Returns False in legacy
        mode so the caller falls through to the bg-task scheduler.

        No retry-on-lock loop — by construction nothing else writes to the
        DB while this runs, so the lock is always free.
        """
        if not (self._use_single_write_session() and self._writes is not None):
            return False
        if not self.system_completion or not self.current_execution:
            return True  # claim handled — nothing to rebuild
        try:
            from app.models.agent_execution import AgentExecution as _AE
            from app.models.completion import Completion as _Comp
            sw_exec = await self._writes.get(_AE, str(self.current_execution.id))
            sw_comp = await self._writes.get(_Comp, str(self.system_completion.id))
            if sw_exec and sw_comp:
                await self.project_manager.rebuild_completion_from_blocks(
                    self._writes, sw_comp, sw_exec
                )
        except Exception as _e:
            logger.warning(f"[agent.single_writer] rebuild failed: {_e!r}")
        return True

    async def _release_db_between_steps(self) -> None:
        """Commit the agent's main session so its pooled DB connection is returned
        to the pool during the upcoming long awaits (planner LLM call, tool /
        code execution). Without this the connection sits 'idle in transaction'
        for the whole iteration, and concurrent completions exhaust the pool
        (QueuePool timeout -> 500 / mid-stream 'network error'). The session uses
        expire_on_commit=False so already-loaded ORM objects stay usable, and the
        single-writer model is preserved (still one writer, just committing
        between steps — which on SQLite also releases the WAL writer lock).
        Toggle with BOW_AGENT_RELEASE_DB_BETWEEN_STEPS (default on)."""
        if os.getenv("BOW_AGENT_RELEASE_DB_BETWEEN_STEPS", "1") != "1":
            return
        try:
            await self.db.commit()
        except Exception as e:
            logger.warning(f"[agent] _release_db_between_steps commit failed: {e!r}")

    # ------------------------------------------------------------------
    # Multi-tool dispatch helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _max_actions_per_decision() -> int:
        """Accept-cap: how many tool calls from one planner decision we honor.
        Actions beyond the cap are NOT executed silently dropped — they're
        reported back to the planner as not-executed so it can re-issue them."""
        return _env_int("BOW_AGENT_MAX_ACTIONS_PER_DECISION", 10, 1, 32)

    def _tool_concurrency(self) -> int:
        """In-flight cap for concurrent tool invocations within one decision.
        The org setting `ai_tool_concurrency` governs (default 1 = serial —
        today's behavior); the BOW_AGENT_TOOL_CONCURRENCY env var, when set,
        overrides it (sandbox/ops escape hatch). Kept well below the
        process-wide code-exec pool (min(8, cpu*2)) shared by ALL completions."""
        if (os.environ.get("BOW_AGENT_TOOL_CONCURRENCY") or "").strip():
            return _env_int("BOW_AGENT_TOOL_CONCURRENCY", 1, 1, 8)
        try:
            settings = getattr(self, "organization_settings", None)
            cfg = settings.get_config("ai_tool_concurrency") if settings else None
            val = int(getattr(cfg, "value", 1) or 1)
        except (TypeError, ValueError):
            val = 1
        return max(1, min(8, val))

    def _new_invocation_state(self, tool_name: str) -> ToolInvocationState:
        """Fresh per-invocation created-objects scope.

        Tools in _INVOCATION_RESET_TOOLS historically reset
        self.current_query/step/visualization before running; other tools
        inherited whatever the previous tool created. Seed accordingly.
        """
        if tool_name in _INVOCATION_RESET_TOOLS:
            return ToolInvocationState(widget=self.current_widget)
        return ToolInvocationState(
            query=self.current_query,
            step=self.current_step,
            step_id=self.current_step_id,
            visualization=getattr(self, "current_visualization", None),
            widget=self.current_widget,
        )

    @staticmethod
    def _batch_failure_rollup(outcomes: list) -> dict:
        """Per-tool failure verdict for one dispatched batch.

        Returns {tool_name: all_actions_failed}. Used by the failure circuit
        breaker so a batch counts as at most ONE failed round per tool — the
        breaker's threshold means consecutive failed planner iterations, and
        any success within the batch resets the streak (matching the serial
        loop, where a later success deleted the counter)."""
        rollup: dict = {}
        for _o in outcomes:
            if not _o or _o.get("skipped"):
                continue
            _tn = _o.get("tool_name")
            _failed = _observation_failed(_o.get("observation"))
            rollup[_tn] = rollup.get(_tn, True) and _failed
        return rollup

    @staticmethod
    def _aggregate_batch_observation(outcomes: list, dropped_actions: list) -> Optional[dict]:
        """Build the planner-facing observation for a dispatched batch.

        Single action, nothing dropped → that action's observation verbatim
        (exact parity with the serial path). Multiple actions → a compact
        aggregate: per-action summaries + ids only; full observations are in
        past_observations (one entry per action). Images are hoisted to the
        top level so the vision-extraction path keeps working.
        """
        if not outcomes and not dropped_actions:
            return None
        if len(outcomes) == 1 and not dropped_actions:
            return outcomes[0].get("observation")

        entries = []
        images: list = []
        ok = 0
        failed = 0
        analysis_complete = False
        final_answer = None
        for o in outcomes:
            obs = o.get("observation") or {}
            entry: dict = {
                "tool_name": o.get("tool_name"),
                "summary": obs.get("summary", ""),
            }
            if _observation_failed(obs):
                failed += 1
                err = obs.get("error")
                entry["error"] = err if isinstance(err, (dict, str)) else {
                    "message": _observation_error_message(obs) or "failed"
                }
            else:
                ok += 1
            for key in ("step_id", "widget_id", "query_id", "artifact_id", "created_visualization_ids"):
                if obs.get(key):
                    entry[key] = obs[key]
            if isinstance(obs.get("images"), list):
                images.extend(obs["images"])
                # Avoid duplicating large base64 payloads inside the aggregate
                entry["images_provided_as_vision"] = True
            if obs.get("analysis_complete"):
                analysis_complete = True
                if obs.get("final_answer"):
                    final_answer = obs.get("final_answer")
            entries.append(entry)

        aggregate: dict = {
            "summary": (
                f"Executed {len(outcomes)} tool calls in parallel: "
                f"{ok} succeeded, {failed} failed"
            ),
            "parallel_actions": entries,
        }
        if failed and ok == 0:
            # Only mark the whole batch as failed when EVERY action failed —
            # a partial failure keeps per-action errors in parallel_actions so
            # the planner retries just those, and _observation_failed() stays
            # False for the batch (otherwise one flaky call out of ten would
            # trip the per-tool failure circuit breaker on every iteration).
            aggregate["error"] = {
                "code": "parallel_all_failed",
                "message": f"All {len(outcomes)} parallel tool calls failed; see parallel_actions",
            }
        if images:
            aggregate["images"] = images
        if analysis_complete:
            aggregate["analysis_complete"] = True
            if final_answer:
                aggregate["final_answer"] = final_answer
        if dropped_actions:
            aggregate["not_executed"] = [
                {
                    "tool_name": getattr(a, "name", "unknown"),
                    "note": "dropped by the per-decision action cap — re-issue if still needed",
                }
                for a in dropped_actions
            ]
        return aggregate

    async def _dispatch_action_batch(self, actions_list: list, block_ids: list, run_one) -> list:
        """Dispatch one decision's actions through `run_one`, serially or
        concurrently, returning outcomes in ACTION order.

        run_one(tool_index, action, block_id, inv) -> outcome dict.

        Concurrency policy:
        - cap comes from the ai_tool_concurrency org setting (env override);
        - a batch containing any tool outside _PARALLEL_SAFE_TOOLS runs
          serial regardless of the cap (unaudited side-effects);
        - same-source actions overlap freely. A per-source lock used to
          serialize them on the assumption that the shared client object
          might not be thread-safe; a sweep of the connector clients showed
          execute_query opens a fresh connection (or issues a fresh HTTP
          request) per call, and same-source queries already run
          concurrently across completions with no coordination — so the
          lock guarded nothing and made single-source workspaces (the
          common case) fully serial. Any genuinely stateful client (e.g.
          verticapy's module-level connection) must guard internally.
        - a crashed action becomes an error outcome, never a lost one.
        """
        concurrency = 1
        if len(actions_list) > 1:
            concurrency = self._tool_concurrency()
            if any(a.name not in _PARALLEL_SAFE_TOOLS for a in actions_list):
                concurrency = 1

        outcomes: list = []
        if concurrency <= 1:
            for _ti, _act in enumerate(actions_list):
                if self.sigkill_event.is_set():
                    break
                _outcome = await run_one(
                    _ti, _act,
                    block_ids[_ti] if _ti < len(block_ids) else None,
                    self._new_invocation_state(_act.name),
                )
                outcomes.append(_outcome)
                # Serial chaining parity: adopt this action's created
                # objects immediately so the NEXT action in the batch
                # seeds from them (old for-loop behavior). Concurrent
                # batches intentionally all seed from pre-batch state.
                if not _outcome.get("skipped"):
                    self._adopt_invocation_outcomes([_outcome])
            return outcomes

        logger.info(
            "[agent] dispatching %d tool calls concurrently (cap=%d): %s",
            len(actions_list), concurrency,
            ", ".join(a.name for a in actions_list),
        )
        sem = asyncio.Semaphore(concurrency)

        async def _guarded(_ti: int, _act, _bid):
            async with sem:
                if self.sigkill_event.is_set():
                    return {
                        "index": _ti, "tool_name": _act.name,
                        "tool_input": getattr(_act, "arguments", None),
                        "action": _act, "skipped": True, "inv": None,
                        "observation": {"summary": "Stopped before execution", "stopped": True},
                    }
                return await run_one(
                    _ti, _act, _bid, self._new_invocation_state(_act.name),
                )

        results = await asyncio.gather(
            *(
                _guarded(_ti, _act, block_ids[_ti] if _ti < len(block_ids) else None)
                for _ti, _act in enumerate(actions_list)
            ),
            return_exceptions=True,
        )
        for _ti, _res in enumerate(results):
            if isinstance(_res, BaseException):
                logger.error(f"[agent] concurrent action {_ti} crashed: {_res!r}", exc_info=_res)
                outcomes.append({
                    "index": _ti,
                    "tool_name": actions_list[_ti].name,
                    "tool_input": getattr(actions_list[_ti], "arguments", None),
                    "action": actions_list[_ti],
                    "skipped": True,
                    "inv": None,
                    "observation": {
                        "summary": f"Tool '{actions_list[_ti].name}' crashed: {_res}",
                        "error": {"code": "runtime_error", "message": str(_res)},
                    },
                })
            else:
                outcomes.append(_res)
        return outcomes

    def _adopt_invocation_outcomes(self, outcomes: list) -> None:
        """Fold per-invocation created-object state back onto the agent, in
        action order, so cross-iteration readers (next runtime_ctx, artifact
        tools, follow-ups) keep the legacy "current = latest created" view."""
        if any(o.get("tool_name") in _INVOCATION_RESET_TOOLS for o in outcomes):
            # Mirror the old pre-action reset: a reset-scope tool ran, so a
            # stale step from a previous iteration must not survive the batch.
            self.current_query = None
            self.current_step = None
            self.current_step_id = None
            self.current_visualization = None
        for o in outcomes:
            inv = o.get("inv")
            if inv is None:
                continue
            if inv.current_query is not None:
                self.current_query = inv.current_query
            if inv.current_step is not None:
                self.current_step = inv.current_step
            if inv.current_step_id is not None:
                self.current_step_id = inv.current_step_id
            if inv.current_visualization is not None:
                self.current_visualization = inv.current_visualization
            if inv.current_widget is not None:
                self.current_widget = inv.current_widget

    async def main_execution(self):
        # Single-writer mode: route all migrated writers through self.db
        # (the agent's existing main session) via self._writes_session().
        # We deliberately do NOT open a separate session — that would
        # create two concurrent writers (self.db for plan_decision /
        # block_upsert / completion status, plus the new session) which
        # contend on the SQLite WAL writer lock and produce the same
        # silent state corruption the refactor is meant to eliminate.
        # Reusing self.db means every write in the main coroutine is
        # serialized through one connection — the only writer in flight
        # at a time. Legacy mode keeps self._writes=None so writers fall
        # back to opening fresh short-lived sessions.
        if self._use_single_write_session():
            self._writes = self.db
        # Stamp ambient attribution for every LLM call made during this run so
        # the Cost console can break spend down by user / report / data source.
        # data_source_id is stamped only when the report has exactly one source
        # (unambiguous); multi-source reports are split across their sources at
        # query time via the report join. Snapshotting happens at record time
        # (see app.ai.llm.usage_attribution), so this also covers tool LLM calls
        # and the worker-thread judge (asyncio.to_thread copies the context).
        _attr_org = str(getattr(self.organization, "id", "") or "") or None
        _attr_user = str(getattr(self.head_completion, "user_id", "") or "") or None
        _attr_report = str(self.report.id) if self.report else None
        _single_ds = self.data_sources[0] if len(self.data_sources) == 1 else None
        _attr_ds = str(getattr(_single_ds, "id", "")) if _single_ds is not None else None
        _attribution_token = set_usage_attribution(
            {
                "organization_id": _attr_org,
                "user_id": _attr_user,
                "report_id": _attr_report,
                "data_source_id": _attr_ds,
            }
        )
        try:
            import time as _time
            _t0 = _time.monotonic()
            _rid = str(self.report.id)[:8] if self.report else "?"
            def _mlog(label):
                logger.info(f"[agent:{_rid}] {label} +{(_time.monotonic()-_t0)*1000:.0f}ms")

            # Start agent execution tracking
            self.current_execution = await self.project_manager.start_agent_execution(
                self.db,
                completion_id=str(self.system_completion.id),
                organization_id=str(self.organization.id),
                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                report_id=str(self.report.id) if self.report else None,
                build_id=self.build_id,
                is_eval_run=self.is_eval_run,
            )
            _mlog("execution_tracking_started")

            # Resolve any pinned connector file references for this report into
            # fresh, per-user session files (A3). Best-effort — never block the run.
            try:
                await self._resolve_file_references()
            except Exception as e:
                logger.warning(f"file reference resolution failed: {e}")
            _mlog("file_references_resolved")

            # Telemetry in background (non-blocking)
            asyncio.create_task(self._capture_telemetry_background(
                "agent_execution_started",
                {
                    "agent_execution_id": str(self.current_execution.id),
                    "report_id": str(self.report.id) if self.report else None,
                    "model_id": self.model.model_id if self.model else None,
                },
            ))

            # Extract user prompt early for intelligent instruction search
            prompt_text = self.head_completion.prompt.get("content", "") if self.head_completion.prompt else ""

            # Resolve extended-thinking effort once per completion. Order:
            #   per-completion prompt.reasoning_effort > trigger words > model.config.reasoning_effort > "off"
            # Only Anthropic honors the resulting thinking config today;
            # other providers receive None / ignore. See _effort_to_thinking_config.
            _per_completion_effort = (
                self.head_completion.prompt.get("reasoning_effort")
                if self.head_completion.prompt else None
            )
            _model_default_effort = None
            try:
                _mcfg = getattr(self.model, "config", None) or {}
                if isinstance(_mcfg, dict):
                    _model_default_effort = _mcfg.get("reasoning_effort")
            except Exception:
                _model_default_effort = None
            self._reasoning_effort = _resolve_reasoning_effort(
                per_completion=_per_completion_effort,
                prompt_text=prompt_text,
                model_default=_model_default_effort,
            )
            _model_id = getattr(self.model, "model_id", None) if self.model else None
            self._thinking_config = _effort_to_thinking_config(self._reasoning_effort, _model_id)
            logger.info(
                "[agent] reasoning_effort resolved=%s thinking=%s model=%s "
                "(per_completion=%s trigger=%s model_default=%s)",
                self._reasoning_effort,
                self._thinking_config,
                _model_id,
                _per_completion_effort,
                _detect_thinking_trigger(prompt_text),
                _model_default_effort,
            )

            # Prime static and refresh warm in parallel for faster startup
            # Pass prompt_text to enable intelligent instruction search
            with tracer.start_as_current_span("agent.context_initial_load") as span:
                span.set_attribute("agent.context.phase", "initial_prime_and_refresh")
                if self.report is not None:
                    span.set_attribute("report.id", str(self.report.id))
                await asyncio.gather(
                    self.context_hub.prime_static(query=prompt_text),
                    self.context_hub.refresh_warm(),
                )
            _mlog("context_primed")
            view = self.context_hub.get_view()
            # Token metadata update in background (non-blocking)
            asyncio.create_task(self._update_context_token_metadata_background(view))
            
            # Record instruction usage in background (non-blocking)
            if view.static.instructions and view.static.instructions.items:
                if self._use_single_write_session():
                    await self._record_instruction_usage_background(view.static.instructions.items)
                else:
                    asyncio.create_task(self._record_instruction_usage_background(view.static.instructions.items))
                # Emit instructions.context SSE so frontend knows which instructions were loaded
                try:
                    seq_inst = await self.project_manager.next_seq(self.db, self.current_execution)
                    await self._emit_sse_event(SSEEvent(
                        event="instructions.context",
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        seq=seq_inst,
                        data={
                            "source": "context_build",
                            "instructions": [
                                {
                                    "id": item.id,
                                    "title": item.title or (item.text[:60].split('\n')[0] if item.text else None),
                                    "category": item.category,
                                    "load_mode": item.load_mode,
                                    "load_reason": item.load_reason,
                                    "source_type": item.source_type,
                                }
                                for item in view.static.instructions.items
                            ],
                        }
                    ))
                except Exception:
                    pass
                # Persist loaded instructions metadata on system completion for hydration on refresh
                try:
                    from sqlalchemy.orm.attributes import flag_modified
                    _li = [
                        {"id": item.id, "load_mode": item.load_mode, "load_reason": item.load_reason}
                        for item in view.static.instructions.items
                    ]
                    comp_data = self.system_completion.completion if isinstance(self.system_completion.completion, dict) else {}
                    comp_data["loaded_instructions"] = _li
                    self.system_completion.completion = comp_data
                    flag_modified(self.system_completion, "completion")
                except Exception:
                    pass

            # Build slim context snapshot with only usage tracking (excludes full schemas/instructions)
            context_view_data = self._build_slim_context_snapshot(view, top_k_schema=self.top_k_schema)

            # Single-writer mode: run sync inline (sharing self._writes across
            # concurrent asyncio tasks isn't safe — SQLAlchemy AsyncSession is
            # task-bound). Legacy mode: fire-and-forget bg task on a fresh
            # session (each bg task opens its own fresh session via the
            # _writes_session() fallback path).
            if self._use_single_write_session():
                await self._save_context_snapshot_background(
                    kind="initial",
                    context_view_json=context_view_data,
                    prompt_text=prompt_text,
                )
            else:
                asyncio.create_task(self._save_context_snapshot_background(
                    kind="initial",
                    context_view_json=context_view_data,
                    prompt_text=prompt_text,
                ))
            
            # Use cached schemas from prime_static() - no duplicate build
            schemas_ctx = view.static.schemas
            try:
                schemas_excerpt = schemas_ctx.render_combined(top_k_per_ds=self.top_k_schema, index_limit=INDEX_LIMIT) if schemas_ctx else ""
            except Exception:
                schemas_excerpt = schemas_ctx.render() if schemas_ctx else ""
            _mlog(f"schemas_rendered len={len(schemas_excerpt)}")

            # Use cached resources from prime_static() - no duplicate build
            resources_ctx = view.static.resources
            try:
                resources_combined = resources_ctx.render_combined(top_k_per_repo=self.top_k_metadata_resources, index_limit=INDEX_LIMIT) if resources_ctx else ""
            except Exception:
                resources_combined = resources_ctx.render() if resources_ctx else ""
            _mlog(f"resources_rendered len={len(resources_combined)}")

            # History summary based on observation context only
            history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

            # Compute previous tool call before this user message (DB-based, robust)
            prev_tool_name_before_last_user = None
            try:
                report_id = str(self.report.id) if self.report else None
                completion_created_at = getattr(self.system_completion, "created_at", None)
                if report_id:
                    stmt = (
                        select(ToolExecution.tool_name, ToolExecution.started_at)
                        .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                        .where(AgentExecution.report_id == report_id)
                    )
                    if completion_created_at is not None:
                        # Only consider tool executions strictly before this system completion
                        stmt = stmt.where(
                            (ToolExecution.started_at == None) | (ToolExecution.started_at < completion_created_at)
                        )
                    stmt = stmt.order_by(ToolExecution.started_at.desc()).limit(1)
                    res = await self.db.execute(stmt)
                    row = res.first()
                    if row is not None:
                        prev_tool_name_before_last_user = row[0]
            except Exception:
                prev_tool_name_before_last_user = None

            # Use cached instructions from prime_static() - no duplicate build
            inst_section = view.static.instructions
            instructions = inst_section.render() if inst_section else ""

            observation: Optional[dict] = None
            active_artifact = await self._get_active_artifact()
            # Training mode needs more iterations for thorough exploration
            step_limit = 100 if self.mode == "training" else 100

            current_plan_decision = None
            invalid_retry_count = 0
            max_invalid_retries = 2
            
            # Circuit breaker for repeated tool failures
            failed_tool_count = {}
            max_tool_failures = 3
            
            # Circuit breaker for repeated successful actions (infinite success loop)
            # Training mode needs more headroom — iterative create_data calls are expected
            successful_tool_actions = []
            max_repeated_successes = 10 if self.mode == "training" else 2

            # Circuit breaker for consecutive calls to the same artifact tool (regardless of arguments)
            consecutive_artifact_tool_count = 0
            last_artifact_tool_name = None
            max_consecutive_artifact_calls = 1

            # Circuit breaker for total artifact calls across the entire execution
            total_artifact_calls = 0
            max_total_artifact_calls = 2
            
            # Track whether completion.finished has been emitted to avoid duplicates
            completion_finished_emitted = False
            # Track whether the completion terminated in an error (e.g. an LLM
            # call failure that exhausted retries). The post-analysis tasks run
            # on this path too, so we use this to suppress follow-up suggestions
            # for a turn that ended on an error.
            completion_errored = False
            
            # Lazy draft build: don't pre-seed. The first create_instruction
            # or edit_instruction tool call lazy-creates the draft and writes
            # the id back into runtime_ctx; we capture it after each tool call
            # below. This avoids accumulating empty drafts when a training
            # session runs but doesn't actually edit anything.

            # Early scoring will be launched as a background task using an isolated session
            await self._apply_tool_permission_filter()
            await self._apply_email_availability_filter()
            _mlog("loop_starting")

            for loop_index in range(step_limit):
                if self.sigkill_event.is_set():
                    break

                # Release the pooled DB connection before this iteration's long
                # planner LLM call + tool execution so concurrent completions
                # don't starve the connection pool (idle-in-transaction).
                await self._release_db_between_steps()

                # Refresh warm context (skip on first loop - already done above)
                if loop_index > 0:
                    view = await self._refresh_warm_traced("loop_start", loop_index=loop_index)
                    await self._update_context_token_metadata(view)
                
                # Save pre-tool context snapshot in background (skip first loop - initial snapshot already saved)
                if loop_index > 0:
                    pre_tool_view_data = self._build_slim_context_snapshot(view, top_k_schema=self.top_k_schema)
                    if self._use_single_write_session():
                        await self._save_context_snapshot_background(
                            kind="pre_tool",
                            context_view_json=pre_tool_view_data,
                        )
                    else:
                        asyncio.create_task(self._save_context_snapshot_background(
                            kind="pre_tool",
                            context_view_json=pre_tool_view_data,
                        ))

                # Build enhanced planner input with validation and retry on failure
                try:
                    # Get messages context for detailed conversation history
                    # On first loop, use cached messages from refresh_warm(); rebuild on subsequent loops
                    if loop_index == 0 and view.warm.messages:
                        messages_section = view.warm.messages
                    else:
                        messages_section = await self.context_hub.message_builder.build(max_messages=20)
                    messages_context = messages_section.render() if messages_section else ""
                    # Use cached resources from prime_static() - static, no need to rebuild
                    resources_section = view.static.resources
                    resources_context = resources_section.render() if resources_section else ""
                    # Smaller combined excerpt to control tokens per-iteration
                    try:
                        resources_combined_small = resources_section.render_combined(top_k_per_repo=10, index_limit=200) if resources_section else ""
                    except Exception:
                        resources_combined_small = resources_context
                    # Files context (uploaded files schemas/metadata) - use cached
                    files_context = view.static.files.render() if getattr(view.static, "files", None) else ""
                    # Mentions context (current user turn mentions)
                    mentions_context = (view.warm.mentions.render() if getattr(view.warm, "mentions", None) else "")
                    # Entities context (catalog entities relevant to this turn)
                    entities_context = (view.warm.entities.render() if getattr(view.warm, "entities", None) else "")
                    # Active scheduled tasks for this report (for dedupe + cancellation)
                    scheduled_tasks_context = (view.warm.scheduled_tasks.render() if getattr(view.warm, "scheduled_tasks", None) else "")
                    # Loadable prior steps (so the planner prefers reuse via load_step)
                    available_steps_context = await self._build_available_steps_context()

                    # Load user-uploaded images for vision models (only on first loop iteration)
                    user_images = await self._load_images_as_input() if loop_index == 0 else []

                    # Extract images from observation (tool screenshots, etc.)
                    # After extraction, strip from observation to avoid duplicating
                    # the large base64 data in the JSON-serialized last_observation text.
                    observation_images: list[ImageInput] = []
                    if observation and isinstance(observation, dict) and observation.get("images"):
                        for img in observation["images"]:
                            if isinstance(img, dict) and img.get("data"):
                                observation_images.append(ImageInput(
                                    data=img["data"],
                                    media_type=img.get("media_type", "image/png"),
                                    source_type=img.get("source_type", "base64"),
                                ))
                        del observation["images"]
                        observation["images_provided_as_vision"] = True

                    # Combine user images + observation images
                    all_images = user_images + observation_images
                    user_name, user_note = await self._resolve_user_profile()
                    planner_input = PlannerInput(
                        organization_name=self.organization.name,
                        organization_ai_analyst_name=self.ai_analyst_name,
                        timezone=self.org_timezone,
                        locale=self.org_locale,
                        week_start=self.org_week_start,
                        instructions=instructions,
                        user_message=self.head_completion.prompt["content"],
                        schemas_excerpt=None,
                        schemas_combined=schemas_excerpt,
                        schemas_names_index=None,
                        files_context=files_context,
                        mentions_context=mentions_context,
                        entities_context=entities_context,
                        available_steps_context=available_steps_context,
                        scheduled_tasks_context=scheduled_tasks_context,
                        history_summary=history_summary,
                        messages_context=messages_context,
                        resources_context=resources_context,
                        resources_combined=(resources_combined_small if 'resources_combined' not in locals() else resources_combined),
                        last_observation=observation,
                        past_observations=self.context_hub.observation_builder.tool_observations,
                        external_platform=self.platform,
                        tool_catalog=self.planner.tool_catalog,
                        mode=self.mode,
                        platform_context=self.platform_context,
                        images=all_images if all_images else None,
                        active_artifact=active_artifact,
                        limit_row_count=int(self.organization_settings.get_config("limit_row_count").value) if self.organization_settings.get_config("limit_row_count") and self.organization_settings.get_config("limit_row_count").value else None,
                        allow_llm_see_data=bool(getattr(self.organization_settings.get_config("allow_llm_see_data"), "value", True)),
                        mcp_tools_enabled=bool(getattr(self.organization_settings.get_config("enable_mcp_tools"), "value", False)),
                        web_fetch_enabled=bool(getattr(self.organization_settings.get_config("enable_web_fetch"), "value", False)),
                        web_search_enabled=self._web_search_enabled(),
                        web_search_domains=self._web_search_domains(),
                        scheduled_context=await self._build_scheduled_context(),
                        user_name=user_name,
                        user_note=user_note,
                        # Org setting drives parallel emission end-to-end: cap > 1
                        # relaxes the one-tool-per-turn prompt rule and lifts the
                        # provider parallel_tool_calls restriction. The knowledge
                        # harness / title paths keep the default (False) — their
                        # simpler loops dispatch one tool at a time.
                        parallel_tools_enabled=self._tool_concurrency() > 1,
                    )
                    # Trim context if it exceeds the model's token budget
                    from app.ai.context.context_hub import trim_context_to_budget
                    trim_context_to_budget(
                        planner_input,
                        model_context_window=getattr(self.model, "context_window_tokens", None),
                    )
                    # Kick off early scoring in background without blocking the loop (isolated DB session).
                    # Only on the first planner step: this scores the *initial* instructions/context
                    # effectiveness for the turn. It previously fired every iteration, doing N redundant
                    # Judge LLM calls + DB sessions that all overwrote the same completion.
                    if loop_index == 0:
                        asyncio.create_task(self._run_early_scoring_background(planner_input))
                except ValidationError as ve:
                    if invalid_retry_count >= max_invalid_retries:
                        # Too many retries, exit loop
                        break
                    observation = {
                        "summary": "Planner input invalid; retrying",
                        "error": {"code": "input_validation_error", "message": str(ve)},
                    }
                    invalid_retry_count += 1
                    try:
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event(SSEEvent(
                            event="planner.retry",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq,
                            data={
                                "reason": "input_validation_error",
                                "attempt": invalid_retry_count,
                            }
                        ))
                    except Exception:
                        pass
                    # Retry next loop iteration
                    continue

                # PLAN: pre-create a skeleton planning block so tokens can stream immediately
                analysis_done = False
                current_block_id = None
                token_accumulator = {"reasoning": "", "content": ""}
                plan_streamer = None
                # Stable sequence for the entire planner decision lifespan
                decision_seq = None

                # Pre-create a placeholder block — emit SSE immediately, persist DB in background.
                pre_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                decision_seq = pre_seq
                # Generate stable IDs in-memory so SSE fires without waiting for DB.
                _pre_block_id = str(_uuid_mod.uuid4())

                try:
                    await self._emit_sse_event(SSEEvent(
                        event="block.upsert",
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        seq=pre_seq,
                        data={"block": {
                            "id": _pre_block_id,
                            "source_type": "decision",
                            "loop_index": loop_index,
                            "status": "in_progress",
                            "title": "Planning (action)",
                            "icon": "🧠",
                            "content": None,
                            "reasoning": None,
                            "plan_decision_id": None,
                            "tool_execution_id": None,
                            "started_at": None,
                            "completed_at": None,
                        }}
                    ))
                    current_block_id = _pre_block_id
                except Exception as _emit_exc:
                    logger.warning(f"[agent] Failed to emit pre-create block.upsert: {_emit_exc!r}")
                    current_block_id = None

                # Initialize throttled text streamer immediately with the in-memory block ID.
                if current_block_id:
                    async def _next_seq():
                        return await self.project_manager.next_seq(self.db, self.current_execution)
                    async def _persist_partials(
                        reasoning: str,
                        content: str,
                        _bid=current_block_id,
                        _li=loop_index,
                        _bi=int(pre_seq * 100),
                    ):
                        await self._persist_planning_block_partial(
                            block_id=_bid,
                            loop_index=_li,
                            block_index=_bi,
                            reasoning=reasoning,
                            content=content,
                        )
                    plan_streamer = PlanningTextStreamer(
                        emit=self._emit_sse_event,
                        seq_fn=_next_seq,
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        block_id=current_block_id,
                        persist=_persist_partials,
                    )
                else:
                    plan_streamer = None

                # Write-on-complete: no skeleton PlanDecision written here.
                # The final PlanDecision + CompletionBlock are written once at planner.decision.final.

                async def _cancel_skeleton_block(reason: str):
                    """Emit a cancelled block.upsert for the pre-created skeleton so the UI
                    doesn't leave an empty 'Planning (action)' card hanging when a retry or
                    interrupt path skips the decision.final persist."""
                    if not current_block_id:
                        return
                    try:
                        _c_seq = await self.project_manager.next_seq(
                            self.db, self.current_execution
                        )
                        await self._emit_sse_event(SSEEvent(
                            event="block.upsert",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=_c_seq,
                            data={"block": {
                                "id": current_block_id,
                                "source_type": "decision",
                                "loop_index": loop_index,
                                "status": "cancelled",
                                "title": "Planning (cancelled)",
                                "icon": "🧠",
                                "content": None,
                                "reasoning": None,
                                "plan_decision_id": None,
                                "tool_execution_id": None,
                                "started_at": None,
                                "completed_at": None,
                                "cancel_reason": reason,
                            }}
                        ))
                    except Exception as _cexc:
                        logger.debug(f"[agent] cancel_skeleton emit failed: {_cexc!r}")
                    # Remove the persisted partial-text skeleton (if the
                    # snapshot cadence wrote one) so a reload doesn't show a
                    # stale in-progress planning card for a cancelled attempt.
                    try:
                        from app.models.completion_block import CompletionBlock as _CB
                        _skel = await self.db.get(_CB, current_block_id)
                        if _skel is not None and _skel.plan_decision_id is None and _skel.source_type == 'decision':
                            await self.db.delete(_skel)
                            await self.db.commit()
                    except Exception:
                        try:
                            await self.db.rollback()
                        except Exception:
                            pass

                _ws_block_count = 0  # native web-search tool blocks emitted this turn
                _ws_tool_execs = []  # (tool_execution, block) per web search, for citation backfill
                async for evt in self._iter_planner_events_with_span(planner_input, loop_index):
                    if self.sigkill_event.is_set():
                        await _cancel_skeleton_block("sigkill")
                        break

                    # Handle typed events
                    if evt.type == "planner.tokens":
                        # Do not forward raw JSON tokens; deltas will be emitted from decision partials
                        continue

                    elif evt.type == "planner.web_search":
                        # Native (provider-executed) web search finished during
                        # planning. Record it as a real tool execution + block so
                        # it renders like other tools (query in arguments_json).
                        try:
                            _q = evt.query or (", ".join(evt.queries) if evt.queries else "")
                            _queries = evt.queries or ([evt.query] if evt.query else [])
                            _te = await self.project_manager.start_tool_execution(
                                self.db,
                                agent_execution=self.current_execution,
                                plan_decision_id=None,
                                tool_name="web_search",
                                tool_action="search",
                                arguments_json={"query": _q, "queries": _queries},
                            )
                            # The provider reports per-call status; treat anything
                            # other than 'completed' (e.g. 'failed', 'incomplete')
                            # as an error so it doesn't render as a silent success.
                            _ws_ok = (evt.status or "completed") == "completed"
                            await self.project_manager.finish_tool_execution(
                                self.db,
                                tool_execution=_te,
                                status="success" if _ws_ok else "error",
                                success=_ws_ok,
                                result_summary=(f"Searched: {_q}" if _q else "Web search") if _ws_ok else f"Web search {evt.status or 'failed'}",
                                error_message=None if _ws_ok else f"web search {evt.status or 'failed'}",
                                result_json={"query": _q, "queries": _queries, "status": evt.status},
                            )
                            # Order the searches just before the planning/answer
                            # block of this turn (which sits at decision_seq*100),
                            # in execution order (first search on top).
                            _base_seq = decision_seq if decision_seq is not None else 1
                            _ws_bi = int(_base_seq) * 100 - 50 + _ws_block_count
                            _ws_block_count += 1
                            _ws_block = await self.project_manager.insert_standalone_tool_block(
                                self.db,
                                completion=self.system_completion,
                                agent_execution=self.current_execution,
                                tool_execution=_te,
                                loop_index=loop_index,
                                title="Web search",
                                icon="🔍",
                                block_index=_ws_bi,
                            )
                            _ws_schema = await serialize_block_v2(self.db, _ws_block)
                            _ws_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event="block.upsert",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=_ws_seq,
                                data={"block": _ws_schema.model_dump()},
                            ))
                            _ws_tool_execs.append((_te, _ws_block))
                        except Exception as _ws_exc:
                            logger.warning(f"[agent] web_search tool block failed: {_ws_exc!r}")
                        continue

                    elif evt.type == "planner.decision.partial":
                        decision = evt.data  # Already validated PlannerDecision from planner_v2

                        # Store latest decision in memory for final persist (NO DB writes during streaming)
                        current_plan_decision_data = decision

                        # Capture a stable sequence for the eventual persisted decision.
                        # Text streaming uses PlanningTextStreamer below; avoid assigning
                        # an SSE sequence for every text-only planner partial.
                        if decision_seq is None:
                            decision_seq = await self.project_manager.next_seq(self.db, self.current_execution)

                        # Emit incremental, throttled token deltas for reasoning/content.
                        # final_answer and assistant_message are mutually exclusive by prompt contract:
                        # - assistant_message: set only when analysis_complete=False (brief action status)
                        # - final_answer: set only when analysis_complete=True (detailed user response)
                        # Stream whichever is present — never mix them to avoid delta collision.
                        try:
                            new_reasoning = getattr(decision, "reasoning_message", None) or ""
                            new_content = getattr(decision, "final_answer", None) or getattr(decision, "assistant_message", None) or ""
                            if plan_streamer:
                                await plan_streamer.update(new_reasoning, new_content, reset_on_source_change=True)
                        except Exception:
                            pass

                        # Emit decision.partial only for action metadata. Text already
                        # streams through block.delta.token/block.delta.text; repeating
                        # cumulative reasoning/assistant/final_answer here can dominate
                        # SSE bandwidth for long answers.
                        action_present = decision.action is not None
                        if action_present:
                            event_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event="decision.partial",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=event_seq,
                                data={
                                    "plan_type": decision.plan_type,
                                    "reasoning": None,
                                    "assistant": None,
                                    "final_answer": None,
                                    "action": decision.action.model_dump() if decision.action else None,
                                }
                            ))
                    
                    elif evt.type == "planner.decision.final":
                        decision = evt.data  # Already validated PlannerDecision from planner_v2
                        self._record_planner_token_metadata_from_decision(decision, view=view)
                        # Track whether analysis is complete
                        analysis_done = bool(getattr(decision, "analysis_complete", False))
                        
                        # Retry flow: invalid planner output OR underlying LLM error
                        if getattr(decision, "error", None):
                            err_code = getattr(decision.error, "code", "validation_error")
                            err_msg = getattr(decision.error, "message", "Invalid planner output")
                            # If the underlying error is an LLM call failure
                            # (auth/rate_limit/etc), surface a structured
                            # llm.error SSE so the UI can show a real toast
                            # instead of the user seeing a "completed" run
                            # with empty blocks.
                            llm_err_payload = None
                            if err_code == "stream_error":
                                try:
                                    from app.ai.llm.errors import classify as _llm_classify
                                    _provider = getattr(getattr(self.model, "provider", None), "provider_type", None) or "unknown"
                                    _classified = _llm_classify(
                                        Exception(err_msg),
                                        provider=_provider,
                                        model=getattr(self.model, "model_id", None) if self.model else None,
                                    )
                                    llm_err_payload = _classified.to_dict()
                                except Exception as _classify_exc:
                                    logger.warning(f"[agent] llm error classification failed: {_classify_exc!r}")

                            if llm_err_payload:
                                try:
                                    seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                    await self._emit_sse_event(SSEEvent(
                                        event="llm.error",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=seq,
                                        data={**llm_err_payload, "context": "planner", "attempt": invalid_retry_count + 1},
                                    ))
                                except Exception:
                                    pass

                            if invalid_retry_count >= max_invalid_retries:
                                # Too many retries, treat as final error.
                                # Also flip completion to error status with a
                                # human-readable message so refresh shows it.
                                analysis_done = True
                                completion_errored = True
                                await _cancel_skeleton_block("max_invalid_retries")
                                # Mark completion_finished_emitted before the try so that even
                                # if update_message fails, the success path at the end of the
                                # outer loop is NOT taken (which would emit status='success').
                                completion_finished_emitted = True
                                if self.system_completion:
                                    try:
                                        # Compose a persisted message that preserves the actual
                                        # provider error text — never abstract-only. Prefer
                                        # `summary: provider_message` so refresh shows both
                                        # the friendly headline and what really came back.
                                        _summary = (llm_err_payload or {}).get("summary")
                                        _pmsg = (llm_err_payload or {}).get("provider_message")
                                        if _summary and _pmsg:
                                            _final_msg = f"{_summary}: {_pmsg}"
                                        else:
                                            _final_msg = _summary or _pmsg or err_msg or "Planner failed"
                                        await self.project_manager.update_completion_status(
                                            self.db, self.system_completion, 'error'
                                        )
                                        await self.project_manager.update_message(
                                            self.db, self.system_completion, message=_final_msg
                                        )
                                        if self.event_queue:
                                            await self.event_queue.put(SSEEvent(
                                                event="completion.finished",
                                                completion_id=str(self.system_completion.id),
                                                data={
                                                    "status": "error",
                                                    "error": {**(llm_err_payload or {"code": "validation_error", "summary": _final_msg, "provider_message": err_msg or ""}), "message": _final_msg},
                                                },
                                            ))
                                    except Exception as _stop_exc:
                                        logger.warning(f"[agent] terminal-error completion update failed: {_stop_exc!r}")
                                        # Still emit completion.finished with error so the UI doesn't hang
                                        try:
                                            if self.event_queue:
                                                await self.event_queue.put(SSEEvent(
                                                    event="completion.finished",
                                                    completion_id=str(self.system_completion.id) if self.system_completion else None,
                                                    data={
                                                        "status": "error",
                                                        "error": {**(llm_err_payload or {}), "message": err_msg or "Planner failed"},
                                                    },
                                                ))
                                        except Exception:
                                            pass
                                break
                            observation = {
                                "summary": "Planner output invalid; retrying",
                                "error": {
                                    "code": err_code,
                                    "message": err_msg,
                                },
                            }
                            invalid_retry_count += 1
                            # Emit retry event
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="planner.retry",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "reason": "invalid_output",
                                        "attempt": invalid_retry_count,
                                    }
                                ))
                            except Exception:
                                pass
                            # Cancel the skeleton block so the UI doesn't keep an empty
                            # "Planning (action)" card from the previous attempt.
                            await _cancel_skeleton_block("validation_error")
                            # Stop streaming loop; outer loop will attempt again
                            break
                        
                        # Get next sequence number for SSE event ordering (in-memory, no DB)
                        event_seq = await self.project_manager.next_seq(self.db, self.current_execution)

                        if decision_seq is None:
                            decision_seq = event_seq

                        # Persist final PlanDecision (with timeout + retry).
                        # Wrapped in try/except so a DB failure doesn't block SSE.
                        try:
                            current_plan_decision = await self.project_manager.save_plan_decision_from_model(
                                self.db,
                                agent_execution=self.current_execution,
                                seq=decision_seq,
                                loop_index=loop_index,
                                planner_decision_model=decision,
                            )
                        except Exception as _pd_exc:
                            logger.error(
                                f"[agent] save_plan_decision_from_model failed (loop={loop_index}): {_pd_exc!r}",
                                exc_info=True,
                            )
                            current_plan_decision = None

                        # Emit decision.final FIRST — UI renders immediately, no DB wait.
                        await self._emit_sse_event(SSEEvent(
                            event="decision.final",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=event_seq,
                            data={
                                "analysis_complete": decision.analysis_complete,
                                "final_answer": decision.final_answer,
                                "metrics": decision.metrics.model_dump() if decision.metrics else None,
                            }
                        ))

                        # Finalize plan streamer (no DB needed).
                        try:
                            if plan_streamer:
                                await plan_streamer.complete()
                        except Exception:
                            pass

                        # Upsert the CompletionBlock synchronously — tool execution needs it in DB.
                        # upsert_block_for_decision has a 5s timeout so it won't hang the stream.
                        # Only rebuild_completion_from_blocks goes to a background task.
                        if current_plan_decision is not None:
                            try:
                                block = await self.project_manager.upsert_block_for_decision(
                                    self.db,
                                    self.system_completion,
                                    self.current_execution,
                                    current_plan_decision,
                                    preferred_id=_pre_block_id,  # Reuse the ID sent to the UI
                                )
                                current_block_id = str(block.id)
                                # Emit updated block snapshot now that it's confirmed in DB.
                                try:
                                    block_schema = await serialize_block_v2(self.db, block)
                                    _blk_seq = await self.project_manager.next_seq(
                                        self.db, self.current_execution
                                    )
                                    await self._emit_sse_event(SSEEvent(
                                        event="block.upsert",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=_blk_seq,
                                        data={"block": block_schema.model_dump()}
                                    ))
                                except Exception as _blk_emit_exc:
                                    logger.warning(
                                        f"[agent] block.upsert emit failed: {_blk_emit_exc!r}"
                                    )
                            except Exception as _upsert_exc:
                                logger.error(
                                    f"[agent] upsert_block_for_decision failed (loop={loop_index}): {_upsert_exc!r}",
                                    exc_info=True,
                                )
                                block = None

                            # Backfill web-search results: native web search only
                            # surfaces the cited sources at the END of the turn
                            # (annotations on the answer), so attach them to the
                            # last search record now that the turn is complete.
                            try:
                                if _ws_tool_execs:
                                    _cites = getattr(decision, "web_search_citations", None) or []
                                    _last_te, _last_blk = _ws_tool_execs[-1]
                                    _last_te.result_json = {
                                        **(_last_te.result_json or {}),
                                        "sources": _cites,
                                    }
                                    _last_te.result_summary = (
                                        f"{len(_cites)} source(s) found" if _cites else "No results found"
                                    )
                                    self.db.add(_last_te)
                                    await self.db.commit()
                                    _bs = await serialize_block_v2(self.db, _last_blk)
                                    _bseq = await self.project_manager.next_seq(self.db, self.current_execution)
                                    await self._emit_sse_event(SSEEvent(
                                        event="block.upsert",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=_bseq,
                                        data={"block": _bs.model_dump()},
                                    ))
                            except Exception as _ws_cite_exc:
                                logger.warning(f"[agent] web_search citation backfill failed: {_ws_cite_exc!r}")

                            # Rebuild transcript. Single-writer mode runs sync
                            # on self._writes; legacy mode schedules a bg task
                            # (coalesced with the post-tool rebuild below).
                            if not await self._rebuild_completion_sync_if_single_writer():
                                self._request_rebuild_transcript()
                        else:
                            # plan_decision save failed — warn so it's observable.
                            try:
                                _warn_seq = await self.project_manager.next_seq(
                                    self.db, self.current_execution
                                )
                                await self._emit_sse_event(SSEEvent(
                                    event="agent.warning",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=_warn_seq,
                                    data={"message": "Planning state could not be persisted; retrying may help"},
                                ))
                            except Exception:
                                pass
                        
                        # IMPORTANT: Check for action FIRST before checking analysis_complete.
                        # The LLM sometimes sets analysis_complete=true when it means "this is the
                        # final step" rather than "no action needed". If there's an action, execute it.
                        # Multi-tool: planner_v3 already collects all tool_use blocks emitted in
                        # one assistant message into decision.actions. Today we keep
                        # parallel_tool_calls=False / disable_parallel_tool_use=True at the
                        # provider level, so this list almost always has length 1 — but
                        # Bedrock and Gemini do not honor those flags, and Anthropic can
                        # occasionally violate them, so dispatch the full list correctly
                        # instead of dropping the tail. Order is preserved (model intent).
                        actions_list: list = list(getattr(decision, "actions", None) or [])
                        if not actions_list and decision.action is not None:
                            actions_list = [decision.action]
                        # Accept-cap: honor at most N tool calls from one decision.
                        # The tail is NOT silently dropped — it's reported back to
                        # the planner as not_executed so it can re-issue.
                        _dropped_actions: list = []
                        _max_actions = self._max_actions_per_decision()
                        if len(actions_list) > _max_actions:
                            _dropped_actions = actions_list[_max_actions:]
                            actions_list = actions_list[:_max_actions]
                            logger.warning(
                                "[agent] decision emitted %d tool calls; capping at %d (%d deferred)",
                                _max_actions + len(_dropped_actions), _max_actions, len(_dropped_actions),
                            )
                        # `action` keeps its name for back-compat with downstream branches
                        # below that haven't been moved into the dispatch path.
                        action = actions_list[0] if actions_list else None

                        # Only treat analysis_complete as terminal if there's NO action
                        if decision.analysis_complete and not action:
                            # Final answer path (no tool to execute)
                            invalid_retry_count = 0

                            # === IMMEDIATE: Emit completion.finished so UI updates instantly ===
                            # This unblocks thumbs up/debug icons and stop→submit button.
                            # We previously drained bg writes BEFORE emitting finished,
                            # adding ~2-3s of perceived latency for what is effectively
                            # transcript-rewrite + tool_executions FK persistence. The
                            # user-visible content has already streamed; finishing the
                            # SSE event sooner lets the UI flip out of "thinking" state
                            # immediately. The drain still happens — just in parallel
                            # with the rest of the SSE stream's tail (the trailing
                            # block.upsert from _bg_persist_tool lands a moment later).
                            if self.system_completion and not completion_finished_emitted:
                                await self.project_manager.update_completion_status(
                                    self.db,
                                    self.system_completion,
                                    'success'
                                )
                                if self.event_queue:
                                    await self.event_queue.put(SSEEvent(
                                        event="completion.finished",
                                        completion_id=str(self.system_completion.id),
                                        data={"status": "success"}
                                    ))
                                completion_finished_emitted = True
                                # Drain in the background so the queue stays open
                                # until persist_tool/rebuild land, but we don't
                                # block on them before signalling done.
                                asyncio.create_task(
                                    self._drain_bg_writes(),
                                    name="agent.post_finished_drain",
                                )

                            break
                        # Retry flow: action plan with missing action
                        if (getattr(decision, "plan_type", None) == "action") and not action:
                            if invalid_retry_count >= max_invalid_retries:
                                # Too many retries, exit
                                break
                            observation = {
                                "summary": "Planner chose action plan but returned no tool/action; retrying",
                                "error": {"code": "missing_action", "message": "Choose a tool and arguments"},
                            }
                            invalid_retry_count += 1
                            # Emit retry event
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="planner.retry",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "reason": "missing_action",
                                        "attempt": invalid_retry_count,
                                    }
                                ))
                            except Exception:
                                pass
                            # End streaming loop so outer loop can retry
                            break
                        if not action:
                            continue

                        # === Multi-tool dispatch loop ===
                        # parallel_tool_calls=False / disable_parallel_tool_use=True keep
                        # actions_list at length 1 in the common case. The loop is here so
                        # that if a model violates the flag (Bedrock and Gemini do not
                        # honor it) every emitted tool runs with its own block + tool_execution
                        # row, instead of being silently dropped.
                        if not actions_list:
                            continue
                        _action_block_ids: list = [current_block_id]
                        # Pre-create extra blocks (one per additional action) so each
                        # action has a stable block id we can attach the tool_execution to.
                        for _ai in range(1, len(actions_list)):
                            try:
                                _extra_block = await self.project_manager.upsert_block_for_decision(
                                    self.db, self.system_completion, self.current_execution,
                                    current_plan_decision, force_insert=True, tool_index=_ai,
                                )
                                _action_block_ids.append(str(_extra_block.id) if _extra_block else None)
                                if _extra_block is not None:
                                    try:
                                        _eb_schema = await serialize_block_v2(self.db, _extra_block)
                                        _eb_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                        await self._emit_sse_event(SSEEvent(
                                            event="block.upsert",
                                            completion_id=str(self.system_completion.id),
                                            agent_execution_id=str(self.current_execution.id),
                                            seq=_eb_seq,
                                            data={"block": _eb_schema.model_dump()},
                                        ))
                                    except Exception as _ebx:
                                        logger.warning(f"[agent] extra-block emit failed: {_ebx!r}")
                            except Exception as _eb_exc:
                                logger.warning(f"[agent] extra-block upsert failed: {_eb_exc!r}")
                                _action_block_ids.append(None)
                        async def _run_one(tool_index: int, action, _block_id_for_action, _inv, _view):
                            """Run ONE planner action end-to-end and return its outcome.

                            DB work (tool_execution row, context refresh, output handling,
                            persistence, SSE seq) runs under self._tool_db_lock — the shared
                            long-lived session is not safe for concurrent use and single-writer
                            mode funnels all writes through it by design. Only the tool's own
                            execution (LLM codegen + sandboxed code run) happens outside the
                            lock, which is where concurrent invocations actually overlap.

                            Loop-level aggregation (circuit breakers, analysis_complete,
                            planner observation, current_* adoption) happens AFTER the batch,
                            in action order — never in here.
                            """
                            tool_name = action.name
                            tool_input = action.arguments

                            # Validate tool availability for chosen plan_type
                            if not self._validate_tool_for_plan_type(tool_name, decision.plan_type):
                                return {
                                    "index": tool_index, "tool_name": tool_name, "tool_input": tool_input,
                                    "action": action, "skipped": True, "inv": _inv,
                                    "observation": {
                                        "summary": f"Tool '{tool_name}' not available for plan_type '{decision.plan_type}'",
                                        "error": {"code": "resolve_error", "message": "tool/plan_type mismatch"},
                                    },
                                }

                            tool = self.registry.get(tool_name)
                            if not tool:
                                return {
                                    "index": tool_index, "tool_name": tool_name, "tool_input": tool_input,
                                    "action": action, "skipped": True, "inv": _inv,
                                    "observation": {
                                        "summary": f"Tool '{tool_name}' unavailable",
                                        "error": {"code": "resolve_error", "message": "not registered"},
                                    },
                                }

                            async with self._tool_db_lock:
                                # Start tool execution tracking
                                tool_execution = await self.project_manager.start_tool_execution_from_models(
                                    self.db,
                                    agent_execution=self.current_execution,
                                    plan_decision_id=current_plan_decision.id if current_plan_decision else None,
                                    tool_name=tool_name,
                                    tool_action=action.type,
                                    tool_input_model=tool_input,
                                )
                                # Telemetry: tool started
                                try:
                                    await telemetry.capture(
                                        "agent_tool_started",
                                        {
                                            "agent_execution_id": str(self.current_execution.id),
                                            "tool_name": tool_name,
                                            "tool_action": action.type,
                                        },
                                        user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                                        org_id=str(self.organization.id) if self.organization else None,
                                    )
                                except Exception:
                                    pass
                        
                                # Emit tool start event
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="tool.started",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "tool_name": tool_name,
                                        "arguments": tool_input,
                                        "block_id": _block_id_for_action,
                                        "tool_execution_id": str(tool_execution.id) if tool_execution is not None else None,
                                    }
                                ))
                        
                                # Refresh warm context to include the latest planner decision blocks in messages
                                try:
                                    _view = await self._refresh_warm_traced("pre_tool_decision_blocks", loop_index=loop_index)
                                except Exception:
                                    pass
                                try:
                                    with tracer.start_as_current_span("agent.schema_context_build") as span:
                                        span.set_attribute("agent.context.phase", "pre_tool")
                                        span.set_attribute("agent.loop_index", loop_index)
                                        if self.report is not None:
                                            span.set_attribute("report.id", str(self.report.id))
                                        schemas_ctx = await self.context_hub.schema_builder.build(
                                            with_stats=True,
                                        )
                                    schemas_excerpt = schemas_ctx.render_combined(top_k_per_ds=10, index_limit=200)
                                except Exception:
                                    schemas_excerpt = _view.static.schemas.render() if getattr(_view.static, "schemas", None) else ""
                                # Refresh history summary with updated context
                                history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

                                # RUN TOOL with enhanced context tracking
                                runtime_ctx = {
                                    "db": self.db,
                                    "organization": self.organization,
                                    "user": getattr(self.head_completion, 'user', None) if self.head_completion else None,
                                    "settings": self.organization_settings,
                                    "report": self.report,
                                    "head_completion": self.head_completion,
                                    "system_completion": self.system_completion,
                                    "widget": self.widget,
                                    "step": self.step,
                                    "current_widget": _inv.current_widget,
                                    "current_query": _inv.current_query,
                                    "current_step": _inv.current_step,
                                    "current_step_id": _inv.current_step_id,
                                    "project_manager": self.project_manager,
                                    "model": self.model,
                                    "sigkill_event": self.sigkill_event,
                                    "observation_context": self.context_hub.observation_builder.to_dict(),
                                    "context_view": _view,
                                    "context_hub": self.context_hub,
                                    "ds_clients": self.clients,
                                    "excel_files": self.analysis_files,
                                    "training_build_id": self.training_build_id,  # For training mode instruction creation
                                    "agent_execution_id": str(self.current_execution.id) if self.current_execution else None,
                                    "mode": self.mode,  # Current agent mode (chat/training/deep) for tool access control
                                    "is_eval_run": self.is_eval_run,
                                    "platform": self.platform,
                                    "platform_context": self.platform_context,
                                    "tool_call_id": str(tool_execution.id) if tool_execution else None,
                                    "usage_limit_context": self.usage_limit_context,
                                    "pending_officejs_registry": pending_officejs_registry,
                                }

                                # Emit generic output event for tools that stream results (inspect_data)
                                if tool_name == "inspect_data":
                                    # Ensure streaming stdout is enabled by default for this tool
                                    pass


                            async def emit(ev: dict):
                                # Streaming side-effects (query/step/viz creation) write through
                                # the shared session guard; created objects land on _inv so a
                                # concurrent sibling invocation can't cross-attribute them.
                                async with self._tool_db_lock:
                                    await self._handle_streaming_event(tool_name, ev, tool_input, inv=_inv)
                                # Forward events to UI — keyed by block/tool_execution so the
                                # frontend can route concurrent streams to the right card.
                                if ev.get("type") in ["tool.progress", "tool.error", "tool.partial", "tool.stdout", "tool.confirmation"]:
                                    seq_ev = await self.project_manager.next_seq(self.db, self.current_execution)
                                    await self._emit_sse_event(SSEEvent(
                                        event=ev.get("type", "tool.progress"),
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=seq_ev,
                                        data={
                                            "tool_name": tool_name,
                                            "payload": ev.get("payload", {}),
                                            "block_id": _block_id_for_action,
                                            "tool_execution_id": str(tool_execution.id) if tool_execution is not None else None,
                                        }
                                    ))

                            async with self._tool_db_lock:
                                # Release the pooled connection before the (often
                                # multi-second) tool / code execution so it isn't held
                                # idle-in-transaction while the pool starves.
                                await self._release_db_between_steps()

                            with tracer.start_as_current_span("agent.tool_run") as span:
                                span.set_attribute("tool.name", tool_name)
                                span.set_attribute("agent.loop_index", loop_index)
                                if self.report is not None:
                                    span.set_attribute("report.id", str(self.report.id))
                                if tool_execution is not None:
                                    span.set_attribute("tool_execution.id", str(tool_execution.id))
                                tool_result = await self.tool_runner.run(tool, tool_input, runtime_ctx, emit)
                                span.set_attribute("tool.result_type", type(tool_result).__name__)


                            async with self._tool_db_lock:
                                # Capture training_build_id if set by create_instruction tool
                                if runtime_ctx.get("training_build_id") and not self.training_build_id:
                                    self.training_build_id = runtime_ctx["training_build_id"]

                                # Extract observation, output, and sub_timings from tool result
                                if isinstance(tool_result, dict) and "observation" in tool_result:
                                    observation = tool_result["observation"]
                                    tool_output = tool_result.get("output")
                                    tool_sub_timings = tool_result.get("sub_timings")
                                else:
                                    observation = tool_result
                                    tool_output = None
                                    tool_sub_timings = None

                                # Handle tool outputs and manage widget/step state
                                await self._handle_tool_output(tool_name, tool_input, observation, tool_output, inv=_inv)

                                # Extract created objects from observation, with fallback to orchestrator state
                                created_widget_id = None
                                created_step_id = None
                                if observation and "widget_id" in observation:
                                    created_widget_id = observation["widget_id"]
                                if observation and "step_id" in observation:
                                    created_step_id = observation["step_id"]
                                # Fallback to orchestrator's current_step_id for tools that trigger step creation via progress events
                                if not created_step_id and _inv.current_step_id:
                                    created_step_id = _inv.current_step_id

                                # Refresh context (needed for next planner iteration — in-memory, no DB write here)
                                post_view = await self._refresh_warm_traced("post_tool_before_block_update", loop_index=loop_index)
                                try:
                                    await self._build_context_traced("post_tool_before_block_update", loop_index=loop_index)
                                except Exception:
                                    pass
                                post_view = self.context_hub.get_view()
                                await self._update_context_token_metadata(post_view)

                                # Build created_visualization_ids with fallback to orchestrator state
                                created_visualization_ids = (observation.get("created_visualization_ids") if observation else None)
                                if not created_visualization_ids and getattr(_inv, 'current_visualization', None):
                                    created_visualization_ids = [str(_inv.current_visualization.id)]

                                # Finish tool execution tracking + update the related
                                # completion block — both run in one background task so
                                # the next loop iteration's planner call can start
                                # immediately. Order matters: the tool_executions INSERT
                                # must land before the completion_blocks UPDATE (which
                                # sets the FK to tool_executions.id).
                                # We set the tool_execution fields here first so the
                                # synchronous tool.finished SSE below can read in-memory
                                # values like duration_ms; the bg task only handles the
                                # DB writes and the block.upsert SSE.
                                _success_flag = bool(
                                    observation
                                    and not _observation_failed(observation)
                                    and not (observation and observation.get("stopped"))
                                )
                                _error_msg = _observation_error_message(observation)
                                _summary = observation.get("summary", "") if observation else ""

                                # Mutate the in-memory tool_execution synchronously so
                                # downstream sync code (tool.finished SSE) can read its
                                # final fields. The actual DB INSERT happens in bg.
                                try:
                                    self.project_manager._configure_finished_tool_execution(
                                        tool_execution,
                                        result_model=tool_output,
                                        summary=_summary,
                                        created_widget_id=created_widget_id,
                                        created_step_id=created_step_id,
                                        created_visualization_ids=created_visualization_ids,
                                        error_message=_error_msg,
                                        success=_success_flag,
                                        sub_timings_json=tool_sub_timings,
                                    )
                                except AttributeError:
                                    # Fallback if helper isn't wired yet — keep behavior
                                    await self.project_manager.finish_tool_execution_from_models(
                                        self.db,
                                        tool_execution=tool_execution,
                                        result_model=tool_output,
                                        summary=_summary,
                                        created_widget_id=created_widget_id,
                                        created_step_id=created_step_id,
                                        created_visualization_ids=created_visualization_ids,
                                        error_message=_error_msg,
                                        context_snapshot_id=None,
                                        success=_success_flag,
                                        sub_timings_json=tool_sub_timings,
                                    )

                                # Save post-tool context snapshot in background (not user-facing, not needed for next loop).
                                _post_snap_exec_id = str(self.current_execution.id)
                                _post_snap_tool_exec_id = str(tool_execution.id)
                                _post_snap_data = self._build_slim_context_snapshot(post_view, top_k_schema=self.top_k_schema)

                                async def _bg_post_snap():
                                    try:
                                        from app.models.agent_execution import AgentExecution as _AE
                                        from app.models.tool_execution import ToolExecution as _TE
                                        async with self._writes_session() as bg_db:
                                            bg_exec = await bg_db.get(_AE, _post_snap_exec_id)
                                            if bg_exec:
                                                snap = await self.project_manager.save_context_snapshot(
                                                    bg_db, agent_execution=bg_exec,
                                                    kind="post_tool", context_view_json=_post_snap_data,
                                                )
                                                # Back-fill context_snapshot_id onto the tool execution row
                                                bg_te = await bg_db.get(_TE, _post_snap_tool_exec_id)
                                                if bg_te and snap:
                                                    bg_te.context_snapshot_id = str(snap.id)
                                                    bg_db.add(bg_te)
                                                    await bg_db.commit()
                                    except Exception as _e:
                                        logger.warning(f"[agent] post_snap failed: {_e!r}")

                                if self._use_single_write_session():
                                    await _bg_post_snap()
                                else:
                                    asyncio.create_task(_bg_post_snap())

                                # Telemetry: tool finished
                                try:
                                    await telemetry.capture(
                                        "agent_tool_finished",
                                        {
                                            "agent_execution_id": str(self.current_execution.id),
                                            "tool_name": tool_name,
                                            "status": "error" if _observation_failed(observation) else "success",
                                            "duration_ms": getattr(tool_execution, "duration_ms", None),
                                        },
                                        user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                                        org_id=str(self.organization.id) if self.organization else None,
                                    )
                                except Exception:
                                    pass

                                # Persist tool_executions (INSERT) + completion_blocks
                                # (UPDATE with FK → tool_executions.id) in one bg task.
                                # Order matters: the INSERT must land first or the
                                # FK reference fails on Postgres. Both run in the same
                                # bg session so they share a transaction-ish boundary.
                                # The block.upsert SSE moves into the bg task too —
                                # serialize_block_v2 needs the block in DB.
                                _bg_comp_id = str(self.system_completion.id)
                                _bg_exec_id = str(self.current_execution.id)
                                _bg_tool_exec = tool_execution  # in-memory, configured

                                # Bind per-action block_id eagerly so the bg closure sees this iteration's value
                                _bg_block_id_local = _block_id_for_action
                                async def _bg_persist_tool(_block_id=_bg_block_id_local):
                                    from app.models.agent_execution import AgentExecution as _AE
                                    from app.models.completion import Completion as _Comp
                                    _max_retries = 5
                                    _retry_delay = 0.5
                                    for _attempt in range(_max_retries):
                                        try:
                                            SessionLocal = self._session_maker
                                            async with SessionLocal() as bg_db:
                                                bg_exec = await bg_db.get(_AE, _bg_exec_id)
                                                bg_comp = await bg_db.get(_Comp, _bg_comp_id)
                                                if not (bg_exec and bg_comp):
                                                    return
                                                # Atomic INSERT(tool_executions) + UPDATE(completion_blocks)
                                                # in a single transaction: previously these were two
                                                # separate commits, and a failure between them left the
                                                # block's FK NULL on every subsequent refresh.
                                                block = await self.project_manager.commit_tool_and_attach_block(
                                                    bg_db, bg_comp, bg_exec, _bg_tool_exec,
                                                    block_id=_block_id,
                                                )
                                                if block is None:
                                                    return
                                                try:
                                                    block_schema = await serialize_block_v2(bg_db, block)
                                                    seq_blk = await self.project_manager.next_seq(bg_db, bg_exec)
                                                    await self._emit_sse_event(SSEEvent(
                                                        event="block.upsert",
                                                        completion_id=_bg_comp_id,
                                                        agent_execution_id=_bg_exec_id,
                                                        seq=seq_blk,
                                                        data={"block": block_schema.model_dump()},
                                                    ))
                                                except Exception as _e:
                                                    logger.warning(
                                                        f"[agent.bg_write] block.upsert serialize/emit failed: {_e!r}"
                                                    )
                                                return  # success
                                        except Exception as _retry_exc:
                                            _is_lock = "database is locked" in str(_retry_exc) or "PendingRollback" in type(_retry_exc).__name__
                                            if _is_lock and _attempt < _max_retries - 1:
                                                logger.warning(
                                                    "[agent.bg_write] persist_tool locked, retry %d/%d in %.1fs",
                                                    _attempt + 1, _max_retries, _retry_delay,
                                                )
                                                await asyncio.sleep(_retry_delay)
                                                _retry_delay = min(_retry_delay * 2, 4.0)
                                                continue
                                            raise

                                # Single-writer mode: persist sync on the dedicated
                                # write session — no bg task, no retries, no race
                                # because no other writer is running concurrently.
                                # Legacy mode keeps the bg-task + retry pattern.
                                if self._use_single_write_session() and self._writes is not None:
                                    try:
                                        from app.models.agent_execution import AgentExecution as _AE
                                        from app.models.completion import Completion as _Comp
                                        sw_exec = await self._writes.get(_AE, _bg_exec_id)
                                        sw_comp = await self._writes.get(_Comp, _bg_comp_id)
                                        if sw_exec and sw_comp:
                                            block = await self.project_manager.commit_tool_and_attach_block(
                                                self._writes, sw_comp, sw_exec, _bg_tool_exec,
                                                block_id=_bg_block_id_local,
                                            )
                                            if block is not None:
                                                try:
                                                    block_schema = await serialize_block_v2(self._writes, block)
                                                    seq_blk = await self.project_manager.next_seq(self._writes, sw_exec)
                                                    await self._emit_sse_event(SSEEvent(
                                                        event="block.upsert",
                                                        completion_id=_bg_comp_id,
                                                        agent_execution_id=_bg_exec_id,
                                                        seq=seq_blk,
                                                        data={"block": block_schema.model_dump()},
                                                    ))
                                                except Exception as _e:
                                                    logger.warning(
                                                        f"[agent.single_writer] persist_tool block.upsert emit failed: {_e!r}"
                                                    )
                                    except Exception as _persist_exc:
                                        logger.error(
                                            f"[agent.single_writer] persist_tool failed: {_persist_exc!r}",
                                            exc_info=True,
                                        )
                                else:
                                    self._schedule_bg_write("persist_tool", _bg_persist_tool())
                                # Rebuild transcript — coalesced with any pending
                                # rebuild from the post-plan_decision path above.
                                # Single-writer mode runs sync on self._writes.
                                if not await self._rebuild_completion_sync_if_single_writer():
                                    self._request_rebuild_transcript()

                                # Emit tool.finished with result
                                _is_stopped = bool(observation and observation.get("stopped"))
                                _tool_status = "stopped" if _is_stopped else ("error" if _observation_failed(observation) else "success")
                                seq_fin = await self.project_manager.next_seq(self.db, self.current_execution)
                                safe_result_json = None
                                if tool_output is not None:
                                    try:
                                        safe_result_json = json.loads(json.dumps(tool_output, default=str))
                                    except Exception:
                                        safe_result_json = {"summary": observation.get("summary", "") if observation else ""}
                                await self._emit_sse_event(SSEEvent(
                                    event="tool.finished",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq_fin,
                                    data={
                                        "tool_name": tool_name,
                                        "tool_execution_id": str(tool_execution.id) if tool_execution is not None else None,
                                        "block_id": _block_id_for_action,
                                        "status": _tool_status,
                                        "result_summary": observation.get("summary", "") if observation else "",
                                        # Include query_id for hydration in frontend previews when available
                                        "result_json": ({**safe_result_json, "query_id": (str(_inv.current_query.id) if getattr(_inv, "current_query", None) else None), "created_visualization_ids": created_visualization_ids} if isinstance(safe_result_json, dict) else safe_result_json),
                                        "duration_ms": tool_execution.duration_ms,
                                        "created_widget_id": created_widget_id,
                                        "created_step_id": created_step_id,
                                        "created_visualization_ids": created_visualization_ids,
                                    }
                                ))

                                # Emit instructions.context if the tool loaded related instructions
                                try:
                                    _tool_instructions = (safe_result_json or {}).get("related_instructions") if isinstance(safe_result_json, dict) else None
                                    if _tool_instructions:
                                        _tool_instr_items = [
                                            {
                                                "id": i.get("id"),
                                                "title": i.get("title"),
                                                "category": i.get("category"),
                                                "load_mode": i.get("load_mode"),
                                                "load_reason": "table_reference",
                                                "source_type": i.get("source_type"),
                                            }
                                            for i in _tool_instructions
                                        ]
                                        seq_ti = await self.project_manager.next_seq(self.db, self.current_execution)
                                        await self._emit_sse_event(SSEEvent(
                                            event="instructions.context",
                                            completion_id=str(self.system_completion.id),
                                            agent_execution_id=str(self.current_execution.id),
                                            seq=seq_ti,
                                            data={
                                                "source": f"tool:{tool_name}",
                                                "instructions": _tool_instr_items,
                                            }
                                        ))
                                        # Persist tool-loaded instructions to completion JSON (append, deduplicate)
                                        try:
                                            from sqlalchemy.orm.attributes import flag_modified
                                            comp_data = self.system_completion.completion if isinstance(self.system_completion.completion, dict) else {}
                                            existing = comp_data.get("loaded_instructions") or []
                                            existing_ids = {li.get("id") for li in existing}
                                            for ti in _tool_instr_items:
                                                if ti.get("id") and ti["id"] not in existing_ids:
                                                    existing.append({"id": ti["id"], "load_mode": ti.get("load_mode"), "load_reason": ti.get("load_reason")})
                                                    existing_ids.add(ti["id"])
                                            comp_data["loaded_instructions"] = existing
                                            self.system_completion.completion = comp_data
                                            flag_modified(self.system_completion, "completion")
                                        except Exception:
                                            pass
                                except Exception:
                                    pass


                            return {
                                "index": tool_index,
                                "tool_name": tool_name,
                                "tool_input": tool_input,
                                "action": action,
                                "observation": observation,
                                "tool_output": tool_output,
                                "tool_execution": tool_execution,
                                "block_id": _block_id_for_action,
                                "inv": _inv,
                                "created_widget_id": created_widget_id,
                                "created_step_id": created_step_id,
                                "created_visualization_ids": created_visualization_ids,
                                "skipped": False,
                            }

                        # ---- Dispatch: serial by default, concurrent when enabled ----
                        async def _run_for_batch(_ti: int, _act, _bid, _inv):
                            return await _run_one(_ti, _act, _bid, _inv, view)

                        outcomes = await self._dispatch_action_batch(
                            actions_list, _action_block_ids, _run_for_batch,
                        )

                        # ---- Aggregate outcomes (in action order, matching serial semantics) ----
                        # Failure circuit breaker counts PER BATCH, not per action:
                        # the 3-strike threshold means "consecutive failed planner
                        # iterations". Counting each batch member would let a single
                        # 5-action batch (e.g. the model guessed a wrong table name
                        # in all five) trip the breaker and end the turn before the
                        # planner ever sees the errors and corrects course. A tool
                        # counts one failed round only when ALL its actions in the
                        # batch failed; any success resets it (serial parity: one
                        # action behaves exactly as before).
                        for _tn, _fails in self._batch_failure_rollup(outcomes).items():
                            if _fails:
                                failed_tool_count[_tn] = failed_tool_count.get(_tn, 0) + 1
                            else:
                                failed_tool_count.pop(_tn, None)
                        for _o in outcomes:
                            if _o.get("skipped"):
                                continue
                            _obs = _o.get("observation")
                            _tn = _o.get("tool_name")
                            _ti_args = _o.get("tool_input")
                            if _observation_failed(_obs):
                                if failed_tool_count.get(_tn, 0) >= max_tool_failures:
                                    analysis_done = True
                                    _obs.update({
                                        "analysis_complete": True,
                                        "final_answer": f"Unable to complete the task. The {_tn} tool failed {failed_tool_count[_tn]} times with errors. Please check the tool configuration or try a different approach."
                                    })
                            else:
                                action_signature = f"{_tn}:{json.dumps(_ti_args, sort_keys=True)}"
                                successful_tool_actions.append(action_signature)
                                if len(successful_tool_actions) >= max_repeated_successes:
                                    recent_actions = successful_tool_actions[-max_repeated_successes:]
                                    if len(set(recent_actions)) == 1:
                                        analysis_done = True
                                        _obs.update({
                                            "analysis_complete": True,
                                            "final_answer": f"Task completed successfully. The {_tn} tool has been executed {max_repeated_successes} times with the same parameters, indicating the goal has been achieved."
                                        })

                                # Circuit breaker: consecutive calls to the same artifact tool (even with different args)
                                if _tn in ("create_artifact", "edit_artifact"):
                                    total_artifact_calls += 1
                                    if _tn == last_artifact_tool_name:
                                        consecutive_artifact_tool_count += 1
                                    else:
                                        consecutive_artifact_tool_count = 1
                                        last_artifact_tool_name = _tn
                                    if consecutive_artifact_tool_count > max_consecutive_artifact_calls or total_artifact_calls > max_total_artifact_calls:
                                        analysis_done = True
                                        _obs.update({
                                            "analysis_complete": True,
                                            "final_answer": f"The dashboard has been created successfully."
                                        })
                                else:
                                    consecutive_artifact_tool_count = 0
                                    last_artifact_tool_name = None

                            if _obs and _obs.get("analysis_complete"):
                                analysis_done = True

                                # If tool provides final_answer, update completion and block content
                                final_answer_from_tool = _obs.get("final_answer")
                                if final_answer_from_tool and self.system_completion:
                                    # Update completion message
                                    await self.project_manager.update_message(
                                        self.db, self.system_completion, message=final_answer_from_tool
                                    )
                                    # Update block content so UI shows it
                                    if current_plan_decision:
                                        current_plan_decision.final_answer = final_answer_from_tool
                                        current_plan_decision.analysis_complete = True
                                        try:
                                            block = await self.project_manager.upsert_block_for_decision(
                                                self.db, self.system_completion, self.current_execution, current_plan_decision
                                            )
                                            await self.project_manager.rebuild_completion_from_blocks(
                                                self.db, self.system_completion, self.current_execution
                                            )
                                            # Emit updated block to frontend
                                            if block:
                                                block_schema = await serialize_block_v2(self.db, block)
                                                seq_blk = await self.project_manager.next_seq(self.db, self.current_execution)
                                                await self._emit_sse_event(SSEEvent(
                                                    event="block.upsert",
                                                    completion_id=str(self.system_completion.id),
                                                    agent_execution_id=str(self.current_execution.id),
                                                    seq=seq_blk,
                                                    data={"block": block_schema.model_dump()}
                                                ))
                                        except Exception:
                                            pass

                                # Emit completion.finished immediately so UI updates.
                                # Drain pending bg writes in the BACKGROUND — the
                                # user-visible content has already streamed; the
                                # drain is just rebuild_completion_from_blocks +
                                # tool_execution FK persistence and shouldn't
                                # gate the "answer ready" signal. See the
                                # analysis_complete branch above for full rationale.
                                if self.system_completion and not completion_finished_emitted:
                                    await self.project_manager.update_completion_status(
                                        self.db,
                                        self.system_completion,
                                        'success'
                                    )
                                    if self.event_queue:
                                        await self.event_queue.put(SSEEvent(
                                            event="completion.finished",
                                            completion_id=str(self.system_completion.id),
                                            data={"status": "success"}
                                        ))
                                    completion_finished_emitted = True
                                    asyncio.create_task(
                                        self._drain_bg_writes(),
                                        name="agent.post_finished_drain",
                                    )

                            # Track tool observation for history
                            try:
                                meta = self.registry.get_metadata(_tn)
                                if not meta or getattr(meta, "observation_policy", "on_trigger") != "never":
                                    self.context_hub.observation_builder.add_tool_observation(_tn, _ti_args, _obs, loop_index=loop_index)
                            except Exception:
                                pass

                        observation = self._aggregate_batch_observation(outcomes, _dropped_actions)
                        self._adopt_invocation_outcomes([_o for _o in outcomes if not _o.get("skipped")])

                        # Reset invalid retry counter
                        invalid_retry_count = 0

                        # Refresh for next iteration
                        view = await self._refresh_warm_traced("post_tool_next_iteration", loop_index=loop_index)
                        schemas_excerpt = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
                        history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

                        # Refresh active_artifact after tools that create/edit artifacts
                        if any(_o.get("tool_name") in ("create_artifact", "edit_artifact") for _o in outcomes):
                            active_artifact = await self._get_active_artifact()

                        # End of dispatch — exit the planner stream so the outer
                        # agent loop runs the next planner iteration.
                        break

                # If planner finalized analysis, stop the outer loop as well
                if analysis_done:
                    break

            # === Post-analysis tasks ===
            # Runs once after the outer loop exits, regardless of whether the
            # terminating decision had an action (e.g. create_data with
            # analysis_complete=True) or was a final_answer-only decision.
            if self.mode == "training":
                # Training mode: finalize the build with all created instructions
                await self._finalize_training_build()
            else:
                # Normal mode: Run knowledge harness sub-loop if triggers fired.
                # Harness creates/edits instructions and submits them as a draft AI build for review.
                try:
                    res = await self._should_suggest_instructions(prev_tool_name_before_last_user)
                    if res.get("decision", False):
                        await self._run_knowledge_harness(res.get("conditions", []))
                except Exception as _harness_exc:
                    logger.warning(f"[agent] knowledge harness dispatch failed: {_harness_exc!r}")

            # Save final context snapshot (recompute metadata so counts/tokens are up to date)
            view = await self._refresh_warm_traced("final_snapshot")
            try:
                await self._build_context_traced("final_snapshot")
            except Exception:
                pass
            view = self.context_hub.get_view()
            await self._update_context_token_metadata(view)

            # Save final context snapshot in background (not user-facing).
            _final_snap_exec_id = str(self.current_execution.id)
            _final_snap_data = self._build_slim_context_snapshot(view, top_k_schema=self.top_k_schema)
            async def _bg_final_snap():
                try:
                    from app.models.agent_execution import AgentExecution as _AE
                    async with self._writes_session() as bg_db:
                        bg_exec = await bg_db.get(_AE, _final_snap_exec_id)
                        if bg_exec:
                            await self.project_manager.save_context_snapshot(
                                bg_db, agent_execution=bg_exec,
                                kind="final", context_view_json=_final_snap_data,
                            )
                except Exception as _e:
                    logger.warning(f"[agent] final_snap failed: {_e!r}")
            if self._use_single_write_session():
                await _bg_final_snap()
            else:
                asyncio.create_task(_bg_final_snap())
            
            # Generate report title while the report still has no real title.
            #
            # Run INLINE (awaited) — like follow-ups above, and unlike the old
            # fire-and-forget asyncio.create_task. A discarded create_task keeps
            # only a weak reference in the loop, so on Postgres — where the
            # request's pooled connection is recycled the moment the response
            # finishes — the suspended task was routinely garbage-collected before
            # its small-model LLM call returned, leaving the report stuck on the
            # placeholder title. Awaiting here keeps self.db alive and lands the
            # write before main_execution returns.
            #
            # Gate on the title VALUE (empty or the frontend's "untitled report"
            # placeholder), not on "is this the first completion". The old
            # first-completion gate made generation one-shot: a single transient
            # failure left the report untitled forever. Value-gating is
            # self-healing — a later turn retries until a real title sticks.
            try:
                current_title = (getattr(self.report, "title", "") or "").strip() if self.report else ""
                if self.head_completion and self.report and current_title.lower() in ("", "untitled report"):
                    # Generate title (small model)
                    messages_section = await self.context_hub.message_builder.build(max_messages=5)
                    messages_context = messages_section.render()

                    # Extract plan information from current execution
                    plan_info = []
                    if current_plan_decision:
                        if hasattr(current_plan_decision, 'action_name') and current_plan_decision.action_name:
                            plan_info.append({"action": current_plan_decision.action_name})

                    # Capture the report id as a plain string NOW, while self.db is
                    # still open. _generate_title_background re-fetches by this id in
                    # its own session, so reading self.report.id later (after the
                    # session closes) can't raise "Instance is not bound to a Session"
                    # (the bug that silently skipped title generation, esp. on Postgres).
                    report_id_for_title = str(self.report.id)

                    await self._generate_title_background(messages_context, plan_info, report_id_for_title)
            except Exception as e:
                # Don't fail the entire execution if title generation fails
                import logging
                _fallback_logger = logging.getLogger(__name__)
                _fallback_logger.warning(f"Failed to start title generation: {e}")

            # Follow-up suggestions (web sessions only, when org setting is on).
            # Generated INLINE here — not as a fire-and-forget task like the title
            # — so it's reliable: self.db is alive and the SSE event is enqueued
            # before main_execution returns (i.e. before [DONE]). Persisted on the
            # completion so the chips also survive a page reload.
            # Skip when the turn ended on an error — suggesting follow-ups under
            # an error message reads as if the turn succeeded.
            try:
                if not completion_errored and self._follow_ups_enabled() and self.system_completion:
                    await self._generate_and_emit_follow_ups()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Follow-up suggestions failed: {e}")

            # Late scoring (non-blocking): capture context string and observation snapshot, then run in isolated session
            try:
                final_messages_context = await self.context_hub.get_messages_context(max_messages=20)
            except Exception:
                final_messages_context = ""
            observation_snapshot = self.context_hub.observation_builder.to_dict()
            asyncio.create_task(self._run_late_scoring_background(final_messages_context, observation_snapshot))

            # Finish agent execution
            status = 'sigkill' if self.sigkill_event.is_set() else 'success'
            await self.project_manager.finish_agent_execution(
                self.db,
                agent_execution=self.current_execution,
                status=status,
            )
            # Bump conversation activity so the finalized turn re-floats the report
            # to the top of the list. Targeted UPDATE by id (not a mutation of
            # self.report, which may be detached from self.db here — see the title
            # generation note below) and best-effort so it never fails the turn.
            try:
                if self.report is not None:
                    await self.db.execute(
                        sa_update(Report)
                        .where(Report.id == str(self.report.id))
                        .values(last_activity_at=datetime.utcnow())
                    )
                    await self.db.commit()
            except Exception as e:
                logger.warning(f"Failed to bump report last_activity_at: {e}")
            # Telemetry: agent execution completed
            try:
                await telemetry.capture(
                    "agent_execution_completed",
                    {
                        "agent_execution_id": str(self.current_execution.id),
                        "status": status,
                    },
                    user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                    org_id=str(self.organization.id) if self.organization else None,
                )
            except Exception:
                pass
            
            # Update system completion status and emit event if not already done.
            # Success case is typically handled earlier in the analysis_complete block for faster UI response.
            # Drain runs in the background (post-finished) — see comment in
            # the analysis_complete branch above for rationale.
            if self.system_completion and not completion_finished_emitted:
                completion_status = 'stopped' if self.sigkill_event.is_set() else 'success'
                await self.project_manager.update_completion_status(
                    self.db,
                    self.system_completion,
                    completion_status
                )

                # Emit completion finished event
                if self.event_queue:
                    finished_event = SSEEvent(
                        event="completion.finished",
                        completion_id=str(self.system_completion.id),
                        data={"status": completion_status}
                    )
                    await self.event_queue.put(finished_event)
                completion_finished_emitted = True
                asyncio.create_task(
                    self._drain_bg_writes(),
                    name="agent.post_finished_drain",
                )
            
        except Exception as e:
            # Handle errors and finish execution with error status
            if self.current_execution:
                error_payload = {"message": str(e), "type": type(e).__name__}
                await self.project_manager.finish_agent_execution(
                    self.db,
                    agent_execution=self.current_execution,
                    status='error',
                    error_json=error_payload,
                )
                # Telemetry: agent execution failed
                try:
                    await telemetry.capture(
                        "agent_execution_failed",
                        {
                            "agent_execution_id": str(self.current_execution.id),
                            "error_type": type(e).__name__,
                        },
                        user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                        org_id=str(self.organization.id) if self.organization else None,
                    )
                except Exception:
                    pass
                # Persist error on completion and latest block for UI
                try:
                    # Update completion record with status and message
                    if self.system_completion:
                        await self.project_manager.update_completion_status(self.db, self.system_completion, 'error')
                        await self.project_manager.update_message(self.db, self.system_completion, message=error_payload.get('message'), reasoning=None)
                    # Mark last block as error with message
                    await self.project_manager.mark_error_on_latest_block(self.db, self.current_execution, error_payload.get('message'))
                except Exception:
                    pass
            
            # Update system completion status on error
            if self.system_completion:
                await self.project_manager.update_completion_status(
                    self.db, 
                    self.system_completion, 
                    'error'
                )
            # Emit a final completion.finished event with error details for UI consumption.
            # Best-effort drain of bg writes — bounded so we don't hang the error path.
            try:
                await self._drain_bg_writes(timeout_s=3.0)
            except Exception:
                pass
            try:
                if self.event_queue:
                    await self.event_queue.put(SSEEvent(
                        event="completion.finished",
                        completion_id=str(self.system_completion.id) if self.system_completion else None,
                        data={
                            "status": "error",
                            "error": error_payload,
                        }
                    ))
            except Exception:
                pass
            raise
        finally:
            # Drop the ambient LLM usage attribution set at run start.
            reset_usage_attribution(_attribution_token)
            # Single-writer mode: drop the self._writes alias. self.db's
            # lifecycle is owned by the caller (FastAPI dependency); we
            # only ever aliased to it, never opened/owned a separate
            # session. So nothing to close here.
            self._writes = None
            # Cleanup
            try:
                websocket_manager.remove_handler(self._handle_completion_update)
            except Exception:
                pass
            # Schedule the quota flush as a fire-and-forget bg task so it
            # cannot stall the response. Per-LLM-call writes are now
            # buffered on the context (cheap in-memory adds) instead of
            # taking a SELECT FOR UPDATE on the counter row, so a single
            # flush at end-of-run is sufficient. If session_maker is None
            # or pending=0 the flush is a no-op.
            if self.usage_limit_context is not None:
                async def _bg_flush(_ctx=self.usage_limit_context):
                    try:
                        await _ctx.flush()
                    except Exception:
                        logger.debug("usage_limit_context flush failed", exc_info=True)
                try:
                    asyncio.create_task(_bg_flush(), name="agent.quota_flush")
                except Exception:
                    pass

    async def _build_planner_prompt_text(self, view=None) -> str:
        if view is None:
            view = self.context_hub.get_view()

        instructions_section = await self.context_hub.instruction_builder.build()
        instructions = instructions_section.render()

        history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

        try:
            schemas_ctx = await self.context_hub.schema_builder.build(
                with_stats=True,
            )
            schemas_combined = schemas_ctx.render_combined(top_k_per_ds=self.top_k_schema, index_limit=INDEX_LIMIT)
        except Exception:
            schemas_combined = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""

        messages_section = await self.context_hub.message_builder.build(max_messages=20)
        messages_context = messages_section.render()

        resources_section = await self.context_hub.resource_builder.build()
        resources_context = resources_section.render()
        try:
            resources_combined_small = resources_section.render_combined(top_k_per_repo=self.top_k_metadata_resources, index_limit=INDEX_LIMIT)
        except Exception:
            resources_combined_small = resources_context

        files_context = view.static.files.render() if getattr(view.static, "files", None) else ""
        mentions_context = (view.warm.mentions.render() if getattr(view.warm, "mentions", None) else "")
        entities_context = (view.warm.entities.render() if getattr(view.warm, "entities", None) else "")
        scheduled_tasks_context = (view.warm.scheduled_tasks.render() if getattr(view.warm, "scheduled_tasks", None) else "")
        available_steps_context = await self._build_available_steps_context()

        user_message = (self.head_completion.prompt or {}).get("content", "")

        active_artifact = await self._get_active_artifact()

        user_name, user_note = await self._resolve_user_profile()
        planner_input = PlannerInput(
            organization_name=self.organization.name,
            organization_ai_analyst_name=self.ai_analyst_name,
            timezone=self.org_timezone,
            locale=self.org_locale,
            week_start=self.org_week_start,
            instructions=instructions,
            user_message=user_message,
            schemas_excerpt=None,
            schemas_combined=schemas_combined,
            schemas_names_index=None,
            files_context=files_context,
            mentions_context=mentions_context,
            entities_context=entities_context,
            available_steps_context=available_steps_context,
            scheduled_tasks_context=scheduled_tasks_context,
            history_summary=history_summary,
            messages_context=messages_context,
            resources_context=resources_context,
            resources_combined=resources_combined_small,
            last_observation=None,
            past_observations=self.context_hub.observation_builder.tool_observations,
            external_platform=self.platform,
            tool_catalog=self.planner.tool_catalog,
            mode=self.mode,
            active_artifact=active_artifact,
            limit_row_count=int(self.organization_settings.get_config("limit_row_count").value) if self.organization_settings.get_config("limit_row_count") and self.organization_settings.get_config("limit_row_count").value else None,
            allow_llm_see_data=bool(getattr(self.organization_settings.get_config("allow_llm_see_data"), "value", True)),
            mcp_tools_enabled=bool(getattr(self.organization_settings.get_config("enable_mcp_tools"), "value", False)),
            web_fetch_enabled=bool(getattr(self.organization_settings.get_config("enable_web_fetch"), "value", False)),
            web_search_enabled=self._web_search_enabled(),
            web_search_domains=self._web_search_domains(),
            scheduled_context=await self._build_scheduled_context(),
            user_name=user_name,
            user_note=user_note,
        )

        from app.ai.context.context_hub import trim_context_to_budget
        trim_context_to_budget(
            planner_input,
            model_context_window=getattr(self.model, "context_window_tokens", None),
        )

        return self.planner.prompt_builder.build_prompt(planner_input)

    def _publish_context_metadata_to_view(self, view=None):
        try:
            if view is not None and isinstance(getattr(view, "meta", None), dict):
                view.meta.update(self.context_hub.metadata.model_dump())
        except Exception:
            pass

    def _record_planner_token_metadata_from_decision(self, decision, view=None):
        """Record prompt token metadata from the actual planner call.

        This keeps live execution metadata useful without rebuilding the full
        planner prompt just to count it.
        """
        try:
            metrics = getattr(decision, "metrics", None)
            token_usage = getattr(metrics, "token_usage", None) if metrics else None
            prompt_tokens = getattr(token_usage, "prompt_tokens", None) if token_usage else None
            if prompt_tokens is None:
                return
            prompt_tokens = int(prompt_tokens or 0)
            self._last_planner_prompt_tokens = prompt_tokens
            metadata = self.context_hub.metadata
            section_sizes = dict(metadata.section_sizes or {})
            section_sizes["_planner_prompt_total"] = prompt_tokens
            metadata.section_sizes = section_sizes
            metadata.total_tokens = prompt_tokens
            self._publish_context_metadata_to_view(view)
        except Exception:
            pass

    async def _update_context_token_metadata(self, view=None):
        try:
            metadata = self.context_hub.metadata
            section_sizes = dict(metadata.section_sizes or {})
            if self._last_planner_prompt_tokens is not None:
                section_sizes["_planner_prompt_total"] = self._last_planner_prompt_tokens
                metadata.section_sizes = section_sizes
                metadata.total_tokens = self._last_planner_prompt_tokens
            elif not metadata.total_tokens and metadata.section_sizes:
                metadata.total_tokens = sum(int(v or 0) for v in metadata.section_sizes.values())
            self._publish_context_metadata_to_view(view)
        except Exception:
            pass

    async def _refresh_warm_traced(self, phase: str, *, loop_index: int | None = None):
        with tracer.start_as_current_span("agent.context_refresh") as span:
            span.set_attribute("agent.context.phase", phase)
            if loop_index is not None:
                span.set_attribute("agent.loop_index", loop_index)
            if self.report is not None:
                span.set_attribute("report.id", str(self.report.id))
            await self.context_hub.refresh_warm()
            return self.context_hub.get_view()

    async def _build_context_traced(self, phase: str, *, loop_index: int | None = None):
        with tracer.start_as_current_span("agent.context_build") as span:
            span.set_attribute("agent.context.phase", phase)
            if loop_index is not None:
                span.set_attribute("agent.loop_index", loop_index)
            if self.report is not None:
                span.set_attribute("report.id", str(self.report.id))
            return await self.context_hub.build_context()

    async def _iter_planner_events_with_span(self, planner_input: PlannerInput, loop_index: int):
        # Do not use start_as_current_span here: this async generator yields
        # back to the caller many times, and contextvars-backed "current span"
        # tokens can be detached from a different async context on generator
        # close. A plain span still captures duration/counts without adding
        # OpenTelemetry detach noise.
        span = tracer.start_span("agent.planner_stream")
        span.set_attribute("agent.loop_index", loop_index)
        span.set_attribute("agent.mode", self.mode or "")
        span.set_attribute("agent.tool_catalog.size", len(self.planner.tool_catalog or []))
        if self.report is not None:
            span.set_attribute("report.id", str(self.report.id))
        if self.model is not None:
            span.set_attribute("llm.model_id", getattr(self.model, "model_id", "") or "")
        counts: dict[str, int] = {}
        try:
            async for evt in self.planner.execute(
                planner_input,
                self.sigkill_event,
                thinking=self._thinking_config,
            ):
                event_type = getattr(evt, "type", "unknown")
                counts[event_type] = counts.get(event_type, 0) + 1
                yield evt
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            raise
        finally:
            span.set_attribute("planner.events.total", sum(counts.values()))
            for event_type, count in counts.items():
                span.set_attribute(f"planner.events.{event_type}", count)
            span.end()

    async def _emit_sse_event(self, event: SSEEvent):
        """Emit SSE event via event queue and optionally websocket."""
        with tracer.start_as_current_span("agent.sse_enqueue") as span:
            span.set_attribute("sse.event", event.event)
            span.set_attribute("sse.queue_present", bool(self.event_queue))
            if event.seq is not None:
                span.set_attribute("sse.seq", event.seq)
            started = _time.monotonic()
            try:
                # Add to streaming queue for new streaming API
                if self.event_queue:
                    await self.event_queue.put(event)
                span.set_attribute("sse.enqueue_ms", round((_time.monotonic() - started) * 1000.0, 3))
            except Exception as e:
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                print(f"Error emitting SSE event: {e}")

    async def _finalize_training_build(self):
        """Leave the training build in draft for the user to review and approve.

        Called at the end of a training mode session. The build accumulates all
        create_instruction / edit_instruction changes as draft versions; the
        user sees a pill and explicitly approves to promote the build to main.
        Previously this auto-published the build which bypassed human approval.
        """
        if not self.training_build_id:
            logger.info("Training mode ended with no instructions created - no build to finalize")
            return

        try:
            from app.services.build_service import BuildService

            build_service = BuildService()
            build = await build_service.get_build(self.db, self.training_build_id)

            logger.info(
                f"Training build {self.training_build_id} left in draft for user approval "
                f"(status={build.status if build else 'unknown'})"
            )

            if self.event_queue:
                try:
                    await self.event_queue.put(SSEEvent(
                        event="training.build_finalized",
                        completion_id=str(self.system_completion.id) if self.system_completion else None,
                        data={
                            "build_id": self.training_build_id,
                            "status": build.status if build else "draft",
                            "awaiting_approval": True,
                        }
                    ))
                except Exception:
                    pass

        except Exception as e:
            logger.exception(f"Failed to finalize training build {self.training_build_id}: {e}")
            # Still emit an error event so frontend knows something went wrong
            if self.event_queue:
                try:
                    await self.event_queue.put(SSEEvent(
                        event="training.build_error",
                        completion_id=str(self.system_completion.id) if self.system_completion else None,
                        data={
                            "build_id": self.training_build_id,
                            "error": str(e),
                        }
                    ))
                except Exception:
                    pass

    async def _should_suggest_instructions(self, prev_tool_name_before_last_user: Optional[str]) -> Dict[str, object]:
        """Decide whether to run suggest_instructions based on report history.

        Delegates to InstructionTriggerEvaluator for condition evaluation.
        Returns: {"decision": bool, "conditions": [{"name": str, "hint": str}, ...]}
        """
        try:
            # Get user message for condition evaluation
            user_message = ""
            if self.head_completion and self.head_completion.prompt:
                user_message = self.head_completion.prompt.get("content", "")
            
            evaluator = InstructionTriggerEvaluator(
                db=self.db,
                organization_settings=self.organization_settings,
                report_id=str(self.report.id) if self.report else None,
                current_execution_id=str(self.current_execution.id) if self.current_execution else None,
                user_message=user_message,
                mode=self.mode,
                completion_id=str(self.system_completion.id) if self.system_completion else None,
            )
            return await evaluator.evaluate(prev_tool_name_before_last_user)
        except Exception:
            return {"decision": False, "conditions": []}

    def _web_search_enabled(self) -> bool:
        """Effective gate for native, provider-executed web search.

        Two layers must agree (per the agreed design):
          1) Org master switch — reuse the existing `enable_web_fetch` setting,
             which governs all outbound web egress for the org.
          2) Per-provider opt-in — `additional_config.enable_web_search`, set by
             an admin only on a provider whose endpoint actually supports the
             Responses `web_search` tool.

        Plus a capability guard: the tool only exists on the OpenAI Responses
        path. That's OpenAI (no custom base_url → Responses client) or Azure
        (routed to the Responses client when enable_web_search is set). Any other
        provider — or an OpenAI provider pinned to a Chat Completions base_url —
        cannot serve it, so we report it disabled to keep the planner directive
        honest.
        """
        try:
            settings = self.organization_settings
            if not settings:
                return False
            org_cfg = settings.get_config("enable_web_fetch")
            if not bool(getattr(org_cfg, "value", False)):
                return False
            provider = getattr(self.model, "provider", None)
            if not provider:
                return False
            add = getattr(provider, "additional_config", None) or {}
            if not bool(add.get("enable_web_search", False)):
                return False
            ptype = getattr(provider, "provider_type", None)
            if ptype == "azure":
                # Web search needs the Responses API, which the admin opts into.
                return bool(add.get("use_responses_api"))
            if ptype == "openai":
                return not bool(add.get("base_url"))
            return False
        except Exception:
            return False

    def _web_search_domains(self) -> list:
        """Domains parsed from URLs in the current user turn, passed to web
        search as filters.allowed_domains so it opens/reads those pages directly
        (open_page) instead of relying on snippet search. Empty when no URL."""
        try:
            head = getattr(self, "head_completion", None)
            msg = (getattr(head, "prompt", None) or {}).get("content", "") if head else ""
            if not msg or "http" not in msg:
                return []
            import re as _re
            hosts = []
            for m in _re.findall(r"https?://([^/\s\"'>]+)", msg):
                h = m.strip().lower()
                if h and h not in hosts:
                    hosts.append(h)
            return hosts[:20]
        except Exception:
            return []

    def _validate_tool_for_plan_type(self, tool_name: str, plan_type: str) -> bool:
        """Validate that tool is available for the chosen plan type.
        
        NOTE: We no longer enforce strict plan_type matching. The plan_type is a
        reasoning signal for the LLM, not a hard constraint. Strict validation
        was causing loops where the LLM couldn't call action tools during research.
        """
        metadata = self.registry.get_metadata(tool_name)
        if not metadata:
            return False
        
        # Always allow - plan_type is advisory, not enforced
        return True

    async def _handle_streaming_event(self, tool_name: str, event: dict, tool_input: dict = None, inv=None):
        """Handle real-time streaming events for widget/step management.

        Same rationale as _handle_tool_output: this method writes to several
        ORM tables (queries, steps, visualizations) on every progress event
        of every running tool. Doing those writes on the long-lived self.db
        is what produced the SQLite-N=10 cascade — one INSERT got the
        OperationalError "database is locked", the agent's session entered
        PendingRollback, and every subsequent self.db.* in the run failed.
        On Postgres the same pattern surfaces as a stale connection from
        an asyncio.wait_for cancellation, with identical cascade. We open
        a fresh short-lived session per call instead. ORM instances that
        outlive this call (inv.current_step, inv.current_visualization,
        inv.current_query) are kept around for downstream code that only
        reads `.id` off them — after the fresh session exits they are
        detached, but the PK columns remain readable.

        `inv` scopes the created query/step/visualization to ONE tool
        invocation so concurrent tool calls can't cross-attribute each
        other's objects. Callers that predate per-invocation state pass
        None and fall back to the agent-instance fields.
        """
        if inv is None:
            inv = _AgentStateProxy(self)
        event_type = event.get("type")
        payload = event.get("payload", {})

        if event_type != "tool.progress":
            return

        stage = payload.get("stage")

        # Skip the DB session entirely for events that don't write. This method
        # fires on *every* tool.progress event, but only a handful of stages
        # actually persist state. Opening a fresh pooled session + 3 .get()
        # queries for the (frequent) non-writing events was the dominant source
        # of QueuePool exhaustion under concurrency (30 of ~59 timeouts in the
        # 50-concurrent load test). Bail out before touching the pool.
        _DATA_TOOLS = ("create_widget", "create_data", "describe_entity", "write_csv")
        _DATA_WRITE_STAGES = (
            "data_model_type_determined", "column_added", "series_configured",
            "validating_code", "widget_creation_needed", "generated_code", "executing_code",
        )
        _writes_for_event = (
            (tool_name in _DATA_TOOLS and stage in _DATA_WRITE_STAGES)
            or (tool_name == "create_dashboard" and stage in ("init", "block.completed"))
        )
        if not _writes_for_event:
            return

        # IDs read from any pre-existing ORM references attached to a now-stale
        # session. They're plain strings/UUIDs, so this read can't trigger
        # lazy loading even if the original session has died.
        cur_step_id = inv.current_step_id
        cur_viz_id = str(inv.current_visualization.id) if getattr(inv, 'current_visualization', None) else None
        cur_query_id = str(inv.current_query.id) if getattr(inv, 'current_query', None) else None
        exec_id = str(self.current_execution.id) if getattr(self, 'current_execution', None) else None
        report_id = str(self.report.id) if self.report else None
        sys_completion_id = str(self.system_completion.id) if self.system_completion else None
        widget_id_for_artifact = str(inv.current_widget.id) if getattr(inv, 'current_widget', None) else None

        try:
            async with self._writes_session() as fresh_db:
                # Re-fetch what we actually need into the fresh session so
                # any subsequent update_*/refresh ops bind to a live conn.
                exec_obj = await fresh_db.get(AgentExecution, exec_id) if exec_id else None
                report_obj = await fresh_db.get(Report, report_id) if report_id else None
                cur_step = await fresh_db.get(Step, cur_step_id) if cur_step_id else None

                if tool_name in ["create_widget", "create_data", "describe_entity", "write_csv"]:
                    if stage == "data_model_type_determined":
                        # Create Query, Step and Visualization early when we know the type
                        data_model_type = payload.get("data_model_type")
                        # Accept either payload.query_title (preferred) or tool_input.title/widget_title for backward-compat
                        query_title = (
                            (payload.get("query_title") if isinstance(payload, dict) else None)
                            or (tool_input and (tool_input.get("title") or tool_input.get("widget_title")))
                            or "Untitled Query"
                        )

                        if data_model_type and report_obj and not cur_step:
                            # Create query (transitional service may still create a widget under the hood)
                            try:
                                inv.current_query = await self.project_manager.create_query_v2(
                                    fresh_db, report_obj, query_title
                                )
                            except Exception:
                                inv.current_query = None

                            # Create step under the query
                            initial_data_model = {"type": data_model_type, "columns": [], "series": []}
                            inv.current_step = await self.project_manager.create_step_for_query(
                                fresh_db, inv.current_query, query_title, "chart", initial_data_model
                            )
                            inv.current_step_id = str(inv.current_step.id)
                            await self.project_manager.set_query_default_step_if_empty(fresh_db, inv.current_query, inv.current_step_id)

                            # Create visualization (draft) with only type in view
                            try:
                                inv.current_visualization = await self.project_manager.create_visualization_v2(
                                    fresh_db, str(report_obj.id), str(inv.current_query.id), query_title, view={"type": data_model_type}, status="draft"
                                )
                            except Exception:
                                inv.current_visualization = None

                            # Emit early query/visualization creation events
                            try:
                                seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                await self._emit_sse_event(SSEEvent(
                                    event="query.created",
                                    completion_id=sys_completion_id,
                                    agent_execution_id=exec_id,
                                    seq=seq,
                                    data={
                                        "query_id": str(inv.current_query.id) if inv.current_query else None,
                                        "report_id": report_id,
                                        "title": query_title,
                                    }
                                ))
                            except Exception:
                                pass
                            try:
                                if inv.current_visualization:
                                    seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                    await self._emit_sse_event(SSEEvent(
                                        event="visualization.created",
                                        completion_id=sys_completion_id,
                                        agent_execution_id=exec_id,
                                        seq=seq,
                                        data={
                                            "visualization_id": str(inv.current_visualization.id),
                                            "query_id": str(inv.current_query.id) if inv.current_query else None,
                                            "report_id": report_id,
                                            "step_id": str(inv.current_step.id),
                                            "view": {"type": data_model_type},
                                        }
                                    ))
                            except Exception:
                                pass

                            # Emit artifact delta for step data_model.type
                            try:
                                seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                change = ArtifactChangeSchema(
                                    type="step",
                                    step_id=str(inv.current_step.id),
                                    partial=True,
                                    changed_fields=["data_model.type"],
                                    fields={"data_model": {"type": data_model_type}},
                                )
                                await self._emit_sse_event(SSEEvent(
                                    event="block.delta.artifact",
                                    completion_id=sys_completion_id,
                                    agent_execution_id=exec_id,
                                    seq=seq,
                                    data={"change": change.model_dump()}
                                ))
                            except Exception:
                                pass

                    elif stage == "column_added":
                        # Update current step's data model with new column
                        column = payload.get("column", {})
                        if cur_step and column:
                            current_data_model = getattr(cur_step, "data_model", {}) or {}
                            current_data_model.setdefault("columns", [])
                            # Add column if not already present
                            if not any(col.get("generated_column_name") == column.get("generated_column_name")
                                     for col in current_data_model["columns"]):
                                current_data_model["columns"].append(column)
                                await self.project_manager.update_step_with_data_model(
                                    fresh_db, cur_step, current_data_model
                                )
                                # Emit artifact delta per column
                                try:
                                    seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                    change = ArtifactChangeSchema(
                                        type="step",
                                        step_id=str(cur_step.id),
                                        widget_id=widget_id_for_artifact,
                                        partial=True,
                                        changed_fields=["data_model.columns"],
                                        fields={"data_model": {"columns": [column]}},
                                    )
                                    await self._emit_sse_event(SSEEvent(
                                        event="block.delta.artifact",
                                        completion_id=sys_completion_id,
                                        agent_execution_id=exec_id,
                                        seq=seq,
                                        data={"change": change.model_dump()}
                                    ))
                                except Exception:
                                    pass

                    elif stage == "series_configured":
                        # Update current step's data model with series
                        series = payload.get("series", [])
                        if cur_step and series:
                            current_data_model = getattr(cur_step, "data_model", {}) or {}
                            current_data_model["series"] = series
                            await self.project_manager.update_step_with_data_model(
                                fresh_db, cur_step, current_data_model
                            )
                            # Emit artifact delta for series update
                            try:
                                seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                change = ArtifactChangeSchema(
                                    type="step",
                                    step_id=str(cur_step.id),
                                    widget_id=widget_id_for_artifact,
                                    partial=True,
                                    changed_fields=["data_model.series"],
                                    fields={"data_model": {"series": series}},
                                )
                                await self._emit_sse_event(SSEEvent(
                                    event="block.delta.artifact",
                                    completion_id=sys_completion_id,
                                    agent_execution_id=exec_id,
                                    seq=seq,
                                    data={"change": change.model_dump()}
                                ))
                            except Exception:
                                pass
                    elif stage == "validating_code":
                        # If validation fails, mark the step as error with the validation message
                        try:
                            is_valid = payload.get("valid", None)
                            if is_valid is False and cur_step:
                                error_msg = payload.get("error") or "Validation failed"
                                await self.project_manager.update_step_status(
                                    fresh_db, cur_step, "error", status_reason=str(error_msg)
                                )
                        except Exception:
                            pass

                    elif stage == "widget_creation_needed":
                        # Update step with final complete data_model
                        data_model = payload.get("data_model", {})
                        query_title = (tool_input and tool_input.get("widget_title")) or payload.get("widget_title") or "Untitled Query"

                        # If for some reason earlier streaming did not create query/step/visualization, create them now
                        if data_model and not cur_step and report_obj:
                            try:
                                inv.current_query = await self.project_manager.create_query_v2(fresh_db, report_obj, query_title)
                                inv.current_step = await self.project_manager.create_step_for_query(fresh_db, inv.current_query, query_title, "chart", {"type": data_model.get("type"), "columns": [], "series": []})
                                inv.current_step_id = str(inv.current_step.id)
                                await self.project_manager.set_query_default_step_if_empty(fresh_db, inv.current_query, inv.current_step_id)
                                inv.current_visualization = await self.project_manager.create_visualization_v2(fresh_db, str(report_obj.id), str(inv.current_query.id), query_title, view={"type": data_model.get("type")}, status="draft")
                                # Emit creation events
                                seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                await self._emit_sse_event(SSEEvent(event="query.created", completion_id=sys_completion_id, agent_execution_id=exec_id, seq=seq, data={"query_id": str(inv.current_query.id), "report_id": report_id, "title": query_title}))
                                if inv.current_visualization:
                                    seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                    await self._emit_sse_event(SSEEvent(event="visualization.created", completion_id=sys_completion_id, agent_execution_id=exec_id, seq=seq, data={"visualization_id": str(inv.current_visualization.id), "query_id": str(inv.current_query.id), "report_id": report_id, "step_id": str(inv.current_step.id), "view": {"type": data_model.get("type")}}))
                            except Exception:
                                pass
                elif tool_name == "create_data":
                    # Code-first path: create query/step/visualization early so outputs can be persisted
                    if stage in ["generated_code", "executing_code"]:
                        try:
                            query_title = (tool_input and (tool_input.get("title") or tool_input.get("widget_title"))) or "Untitled Query"
                            if not cur_step and report_obj:
                                # Create query and step with a default table view
                                try:
                                    inv.current_query = await self.project_manager.create_query_v2(
                                        fresh_db, report_obj, query_title
                                    )
                                except Exception:
                                    inv.current_query = None

                                inv.current_step = await self.project_manager.create_step_for_query(
                                    fresh_db,
                                    inv.current_query,
                                    query_title,
                                    "chart",
                                    {"type": "table", "columns": [], "series": []},
                                )
                                inv.current_step_id = str(inv.current_step.id)
                                await self.project_manager.set_query_default_step_if_empty(fresh_db, inv.current_query, inv.current_step_id)

                                # Create a draft visualization with table view
                                try:
                                    inv.current_visualization = await self.project_manager.create_visualization_v2(
                                        fresh_db,
                                        str(report_obj.id),
                                        str(inv.current_query.id),
                                        query_title,
                                        view={"type": "table"},
                                        status="draft",
                                    )
                                except Exception:
                                    inv.current_visualization = None

                                # Emit creation events
                                try:
                                    seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                    await self._emit_sse_event(SSEEvent(
                                        event="query.created",
                                        completion_id=sys_completion_id,
                                        agent_execution_id=exec_id,
                                        seq=seq,
                                        data={
                                            "query_id": str(inv.current_query.id) if inv.current_query else None,
                                            "report_id": report_id,
                                            "title": query_title,
                                        }
                                    ))
                                except Exception:
                                    pass
                                try:
                                    if inv.current_visualization:
                                        seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                        await self._emit_sse_event(SSEEvent(
                                            event="visualization.created",
                                            completion_id=sys_completion_id,
                                            agent_execution_id=exec_id,
                                            seq=seq,
                                            data={
                                                "visualization_id": str(inv.current_visualization.id),
                                                "query_id": str(inv.current_query.id) if inv.current_query else None,
                                                "report_id": report_id,
                                                "step_id": str(inv.current_step.id),
                                                "view": {"type": "table"},
                                            }
                                        ))
                                except Exception:
                                    pass
                        except Exception:
                            pass

                elif tool_name == "create_dashboard":
                    # Stream-only handling: append blocks into active layout via ProjectManager
                    if stage == "init":
                        # Clear existing blocks before generating new dashboard layout
                        if report_obj:
                            await self.project_manager.clear_active_layout_blocks(
                                fresh_db, str(report_obj.id)
                            )
                    elif stage == "block.completed":
                        block = payload.get("block") or {}
                        if isinstance(block, dict) and report_obj:
                            try:
                                await self.project_manager.append_block_to_active_dashboard_layout(
                                    fresh_db, str(report_obj.id), block
                                )
                            except Exception:
                                pass
                    # No persistence outside layout service; finalization happens on tool end
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error handling streaming event {stage} for {tool_name}: {e}")
            # Don't re-raise; this is streaming and shouldn't break the main flow

    async def _handle_tool_output(self, tool_name: str, tool_input: dict, observation: dict, tool_output: dict = None, inv=None):
        """Handle tool outputs and manage final state updates.

        Runs entirely in a SHORT-LIVED session opened from session_maker
        rather than the long-lived self.db. This was the dominant source
        of "I/O operation on closed file" → MissingGreenlet cascades:
        an asyncio.wait_for inside one of the project_manager helpers
        could cancel a greenlet on the long-lived connection mid-flight,
        closing its asyncpg transport. Every subsequent self.db.* in the
        same agent run then died. By scoping this whole block to a fresh
        session, a transport death here can't poison the rest of the run.

        `inv` carries the invocation's created query/step/visualization
        (see _handle_streaming_event); None falls back to agent fields.
        """
        if inv is None:
            inv = _AgentStateProxy(self)
        if not observation or _observation_failed(observation):
            return  # Don't process failed tool executions

        # All ORM references that come into this method (inv.current_step,
        # inv.current_visualization, self.current_execution, self.report,
        # self.head_completion) are attached to self.db. We re-fetch by ID
        # inside the fresh session, then operate exclusively on the new
        # instances. Identity values (ids, user_id) are read off the old
        # instances first since those don't trip lazy-loading.
        step_id = inv.current_step_id
        viz_id = str(inv.current_visualization.id) if getattr(inv, 'current_visualization', None) else None
        exec_id = str(self.current_execution.id) if getattr(self, 'current_execution', None) else None
        report_id = str(self.report.id) if self.report else None
        sys_completion_id = str(self.system_completion.id) if self.system_completion else None
        head_user_id = str(getattr(self.head_completion, 'user_id', None)) if (
            self.head_completion and getattr(self.head_completion, 'user_id', None)
        ) else None

        try:
            async with self._writes_session() as fresh_db:
                # Re-fetch only the rows we'll need; cheaper than refreshing
                # every relationship and bounded to this method's scope.
                report_obj = await fresh_db.get(Report, report_id) if report_id else None
                exec_obj = await fresh_db.get(AgentExecution, exec_id) if exec_id else None

                if tool_name in ["create_widget", "create_data", "describe_entity", "write_csv"]:
                    # Update current step with code and data using tool_output
                    if not tool_output:
                        return

                    code = tool_output.get("code", "")
                    widget_data = tool_output.get("widget_data", {}) or tool_output.get("data", {})
                    success = tool_output.get("success", False)
                    data_model_from_tool = tool_output.get("data_model") or {}
                    view_options_from_tool = tool_output.get("view_options") or {}

                    step_obj = None
                    if step_id:
                        step_obj = await fresh_db.get(Step, step_id)

                    if step_obj and success and widget_data:
                        # If tool provided a minimal data_model (type/series), merge it into the step before deriving view
                        try:
                            if isinstance(data_model_from_tool, dict) and data_model_from_tool:
                                existing_dm = (getattr(step_obj, "data_model", {}) or {}).copy()
                                merged = existing_dm.copy()
                                # Preserve existing type; only set if missing
                                if not merged.get("type") and data_model_from_tool.get("type"):
                                    merged["type"] = data_model_from_tool.get("type")
                                # Exception: create_data demotes an unresolvable
                                # single-value card to a table (no value column /
                                # no row selector). Adopt that so the step's
                                # data_model stays consistent with the view.
                                elif (
                                    data_model_from_tool.get("type") == "table"
                                    and merged.get("type") in ("count", "metric_card")
                                ):
                                    merged["type"] = "table"
                                # Merge series/grouping fields. `filters` MUST be
                                # included: it carries the default filter that
                                # narrows a melted/long KPI table to the asked-for
                                # row for single-value cards — dropping it makes
                                # count/metric_card render row 0 (the date/label).
                                for key in ("series", "group_by", "sort", "limit", "filters"):
                                    if data_model_from_tool.get(key) is not None:
                                        merged[key] = data_model_from_tool.get(key)
                                await self.project_manager.update_step_with_data_model(fresh_db, step_obj, merged)
                                # Refresh the object to read the updated data_model
                                await fresh_db.refresh(step_obj)
                        except Exception:
                            pass
                        # Update step with code
                        await self.project_manager.update_step_with_code(
                            fresh_db, step_obj, code
                        )
                        # Update step with full data (not just preview)
                        await self.project_manager.update_step_with_data(
                            fresh_db, step_obj, widget_data
                        )

                        # Update step status
                        await self.project_manager.update_step_status(
                            fresh_db, step_obj, "success"
                        )

                        # Emit table usage events based on the step's data model (align with legacy agent)
                        try:
                            await self.project_manager.emit_table_usage(
                                db=fresh_db,
                                report=report_obj,
                                step=step_obj,
                                data_model=getattr(step_obj, "data_model", {}) or {},
                                user_id=head_user_id,
                                user_role=None
                            )
                        except Exception:
                            pass

                        # Fallback for create_data: if no columns in data_model, emit usage from tool_input.tables_by_source
                        try:
                            if tool_name == "create_data":
                                dm = getattr(step_obj, "data_model", {}) or {}
                                cols = dm.get("columns") if isinstance(dm, dict) else None
                                has_columns = isinstance(cols, list) and len(cols) > 0
                                if not has_columns and isinstance(tool_input, dict):
                                    tbs = tool_input.get("tables_by_source")
                                    if tbs:
                                        await self.project_manager.emit_table_usage_from_tables_by_source(
                                            db=fresh_db,
                                            report=report_obj,
                                            step=step_obj,
                                            tables_by_source=tbs,
                                            user_id=head_user_id,
                                            user_role=None,
                                            source_type="sql",
                                        )
                        except Exception:
                            pass

                        # Finalize visualization view.encoding and status
                        try:
                            dm = getattr(step_obj, "data_model", {}) or {}
                            viz_obj = None
                            if viz_id:
                                from app.models.visualization import Visualization as _Viz
                                viz_obj = await fresh_db.get(_Viz, viz_id)
                            if viz_obj:
                                # Prefer tool-provided view (ViewSchema v2) if available
                                view_from_tool = tool_output.get("view")
                                if isinstance(view_from_tool, dict) and view_from_tool.get("version") == "v2":
                                    # Use the new ViewSchema v2 format directly
                                    view = view_from_tool
                                else:
                                    # Legacy fallback: compute encoding from step.data_model.series
                                    enc = self.project_manager.derive_encoding_from_data_model(dm)
                                    view = {"type": dm.get("type")}
                                    if enc:
                                        view["encoding"] = enc
                                    # Merge any tool-provided view options (e.g., colors palette)
                                    try:
                                        if isinstance(view_options_from_tool, dict) and view_options_from_tool:
                                            current_options = (view.get("options") or {})
                                            merged_options = {**current_options, **view_options_from_tool}
                                            view["options"] = merged_options
                                    except Exception:
                                        pass
                                await self.project_manager.update_visualization_view(fresh_db, viz_obj, view)
                                await self.project_manager.set_visualization_status(fresh_db, viz_obj, "success")
                                # Emit visualization.updated
                                try:
                                    seq = await self.project_manager.next_seq(fresh_db, exec_obj)
                                    await self._emit_sse_event(SSEEvent(
                                        event="visualization.updated",
                                        completion_id=sys_completion_id,
                                        agent_execution_id=exec_id,
                                        seq=seq,
                                        data={
                                            "visualization_id": viz_id,
                                            "view": view,
                                            "status": "success",
                                        }
                                    ))
                                except Exception:
                                    pass
                                # Add created_visualization_ids to observation result for tool.finished
                                observation.setdefault("created_visualization_ids", [])
                                observation["created_visualization_ids"].append(viz_id)
                        except Exception:
                            pass

                        # Ensure observation carries ids for auditing/tracking
                        observation["step_id"] = step_id

                elif tool_name == "inspect_data":
                    # Track table usage for inspection
                    try:
                        if isinstance(tool_input, dict):
                            tbs = tool_input.get("tables_by_source")
                            if tbs:
                                await self.project_manager.emit_table_usage_from_tables_by_source(
                                    db=fresh_db,
                                    report=report_obj,
                                    step=None,
                                    tables_by_source=tbs,
                                    user_id=head_user_id,
                                    user_role=None,
                                    source_type="sql",
                                )
                    except Exception:
                        pass

                elif tool_name == "create_dashboard":
                    # Finalize: ensure observation has the latest active layout blocks
                    try:
                        if report_id:
                            blocks = await self.project_manager.get_active_dashboard_layout_blocks(
                                fresh_db, report_id
                            )
                            observation.setdefault("layout", {})
                            observation["layout"]["blocks"] = blocks
                    except Exception:
                        pass

                    # Optional: publish widgets per input (kept from previous behavior)
                    try:
                        widget_ids = []
                        use_all_widgets = True
                        if isinstance(tool_input, dict):
                            widget_ids = tool_input.get("widget_ids") or []
                            use_all_widgets = tool_input.get("use_all_widgets", True)

                        if widget_ids:
                            for wid in widget_ids:
                                w = await fresh_db.get(Widget, str(wid))
                                if w and str(getattr(w, "report_id", "")) == report_id:
                                    w.status = "published"
                                    fresh_db.add(w)
                        elif use_all_widgets and report_id:
                            res = await fresh_db.execute(select(Widget).where(Widget.report_id == report_id))
                            for w in res.scalars().all():
                                if w.status != "published":
                                    w.status = "published"
                                    fresh_db.add(w)
                        await fresh_db.commit()
                    except Exception:
                        pass
        except Exception as e:
            # Import logging if not already available
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error handling tool output for {tool_name}: {e}")
            # The fresh session is closed by the `async with` exit. self.db
            # was never touched in this block, so the agent's main loop can
            # continue on its own session without rollback ceremony.
