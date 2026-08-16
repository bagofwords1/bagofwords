"""
ContextHub - Main orchestrator for all agent context.
"""
import json
import logging
import time
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

_hub_logger = logging.getLogger(__name__)

# Process-wide cache for the heaviest piece of `prime_static` —
# the schema build. Keyed by (org_id, sorted ds-ids tuple, build_id).
# Schemas only change when a data source is re-introspected or
# table_stats roll up; in steady state they're stable for minutes.
# Without this, every completion paid ~1.6s rebuilding the same
# TablesSchemaContext from scratch (the dominant chunk of "setup").
# TTL keeps freshness loose — out-of-band schema refreshes catch up
# within the window. For correctness-critical change events, callers
# can call `invalidate_schema_cache(org_id)` directly.
_SCHEMA_CACHE: Dict[Tuple[str, Tuple[str, ...], Optional[str]], Tuple[float, Any]] = {}
# 5 minutes: schemas only change on re-introspection / stats rollup,
# and a typical user reads + types between prompts longer than 60s.
# Bump cautiously — table_stats updates won't show up until the next
# refresh or explicit invalidate_schema_cache() call.
_SCHEMA_CACHE_TTL_S: float = 300.0

# Instructions are keyed on query, so cross-prompt hits are rare; the
# main win is when the same prompt fires twice (retries, scheduled
# reruns). 2 minutes covers that without giving stale rankings a long
# tail. Always-load instructions are query-independent so they're
# always correct; only LLM-ranked load_mode="auto" entries lag.
_INSTRUCTIONS_CACHE: Dict[Tuple[str, Tuple[str, ...], Optional[str], str], Tuple[float, Any]] = {}
_INSTRUCTIONS_CACHE_TTL_S: float = 120.0


def invalidate_schema_cache(org_id: Optional[str] = None) -> None:
    """Drop cached schema entries. Pass org_id to scope; None drops all."""
    if org_id is None:
        _SCHEMA_CACHE.clear()
        return
    for k in list(_SCHEMA_CACHE.keys()):
        if k[0] == str(org_id):
            _SCHEMA_CACHE.pop(k, None)


def invalidate_instructions_cache(org_id: Optional[str] = None) -> None:
    """Drop cached instruction-build entries. Pass org_id to scope."""
    if org_id is None:
        _INSTRUCTIONS_CACHE.clear()
        return
    for k in list(_INSTRUCTIONS_CACHE.keys()):
        if k[0] == str(org_id):
            _INSTRUCTIONS_CACHE.pop(k, None)

from .context_specs import (
    ContextMetadata, ContextSnapshot, ContextBuildSpec,
    ContextObjectsSnapshot,
    SchemaContextConfig, MessageContextConfig, 
    WidgetContextConfig, InstructionContextConfig, CodeContextConfig,
    ResourceContextConfig
)
from .builders.schema_context_builder import SchemaContextBuilder
from .builders.files_context_builder import FilesContextBuilder
from .builders.message_context_builder import MessageContextBuilder
from .builders.widget_context_builder import WidgetContextBuilder
from .builders.query_context_builder import QueryContextBuilder
from .builders.instruction_context_builder import InstructionContextBuilder
from .builders.code_context_builder import CodeContextBuilder
from .builders.resource_context_builder import ResourceContextBuilder
from .builders.observation_context_builder import ObservationContextBuilder
from .context_view import ContextView, StaticSections, WarmSections
from .sections.messages_section import MessagesSection
from .sections.widgets_section import WidgetsSection
from .sections.observations_section import ObservationsSection
from .sections.resources_section import ResourcesSection
from .sections.code_section import CodeSection
from .builders.mention_context_builder import MentionContextBuilder
from .builders.entity_context_builder import EntityContextBuilder
from app.ai.utils.token_counter import count_tokens


# Default caps to keep planner prompt small and predictable.
# messages_max is a count fallback — the real bound on conversation history
# is the token-budgeted compaction geometry (see context_compaction_service),
# which folds older turns into the rolling summary well before 40 heavy turns.
DEFAULT_CONTEXT_LIMITS = {
    "messages_max": 40,        # last N messages
    "observations_max": 8    # last N observations
}

# Hard ceiling for the assembled prompt (tokens).  Bedrock Sonnet caps at
# 200 K; even Anthropic direct benefits from staying well under the limit.
DEFAULT_TOKEN_BUDGET = 200_000
# Reserve room for the model's completion output so we don't fill the
# entire context window with the prompt.
_OUTPUT_RESERVE = 8_000


def _truncate_list(items, max_items):
    if not isinstance(items, list) or not max_items:
        return items
    return items[-max_items:]


def _safe_setattr_list(section, attr, max_items):
    """Safely truncate a list attribute on a section object."""
    try:
        if section and hasattr(section, attr):
            value = getattr(section, attr)
            if isinstance(value, list):
                setattr(section, attr, _truncate_list(value, max_items))
    except Exception:
        # Best-effort truncation only; never fail the refresh/build
        pass


def _section_token_length(text: Optional[str]) -> int:
    """Measure a section's size using token counts with safe fallbacks."""
    if not text:
        return 0
    try:
        return count_tokens(text)
    except Exception:
        # As a last resort, approximate via character length
        return len(text)


def _estimate_tokens_fast(text: str) -> int:
    """Fast token estimate without tiktoken overhead.

    3 chars/token, deliberately conservative: dense content (numeric data,
    JSON, non-Latin text) tokenizes closer to 2-3 chars/token, and this
    estimate gates the pre-send trim — an optimistic 4 chars/token let
    prompts pass the trim and still be rejected by the provider.
    """
    return len(text) // 3 if text else 0


def _trim_text_tail(text: str, keep_ratio: float, label: str = "") -> str:
    """Trim text keeping the tail (newest content). Returns trimmed string."""
    if not text:
        return text
    char_limit = int(len(text) * keep_ratio)
    if char_limit >= len(text):
        return text
    trimmed = text[-char_limit:]
    prefix = f"... ({label} trimmed to fit context budget)\n" if label else "... (trimmed)\n"
    return prefix + trimmed


# Sections trim_context_to_budget may cut, first → last. Ordered by how
# recoverable the content is: file previews and old messages can be re-read
# on demand; schemas and instructions are cut only when nothing else is left.
# Every section IS cuttable — a section with no cutting stage is how a single
# oversized field (a multi-MB always-load instruction, a warehouse-wide
# schema render) used to sail through untouched and get the whole prompt
# rejected identically on every retry.
_TRIM_STAGES = (
    "files_context",
    "messages_context",
    "resources_combined",
    "resources_context",
    "schemas_combined",
    "schemas_excerpt",
    "entities_context",
    "mentions_context",
    "instructions",
)
# Don't bother halving a section below this size — the floor keeps a stub of
# every section so the model still knows the section exists, and guarantees
# the halving loop terminates.
_TRIM_FLOOR_CHARS = 2_000


def trim_context_to_budget(
    planner_input,
    model_context_window: Optional[int] = None,
) -> None:
    """Trim PlannerInput fields in priority order until the prompt fits.

    Mutates *planner_input* in place. Unlike the previous ratio-based
    best-effort (one fixed cut per section, then give up), this ENFORCES the
    budget: each section in ``_TRIM_STAGES`` is halved repeatedly — keeping
    the tail, newest content — until the total estimate fits or the section
    hits the floor, then the next section is attacked. A prompt only stays
    over budget when every section is already at its floor, which is logged
    as a warning instead of silently sending a prompt the provider will
    reject on every retry.
    """
    budget = (model_context_window or DEFAULT_TOKEN_BUDGET) - _OUTPUT_RESERVE
    if budget <= 0:
        budget = DEFAULT_TOKEN_BUDGET - _OUTPUT_RESERVE

    # Collect all string fields for a rough total estimate
    _str_fields = [
        "instructions", "schemas_combined", "schemas_excerpt",
        "files_context", "messages_context", "resources_context",
        "resources_combined", "mentions_context", "entities_context",
        "history_summary", "user_message",
    ]

    def _estimate_total() -> int:
        total = 0
        for f in _str_fields:
            total += _estimate_tokens_fast(getattr(planner_input, f, None) or "")
        # past_observations is a list of dicts – estimate via JSON dump
        past = getattr(planner_input, "past_observations", None)
        if past:
            try:
                total += _estimate_tokens_fast(json.dumps(past))
            except Exception:
                total += len(past) * 200  # rough fallback per observation
        # last_observation
        last = getattr(planner_input, "last_observation", None)
        if last:
            try:
                total += _estimate_tokens_fast(json.dumps(last))
            except Exception:
                total += 500
        return total

    if _estimate_total() <= budget:
        return  # nothing to do

    # --- Pass 0: drop old observations (cheap, biggest win on tool-heavy runs) ---
    past = getattr(planner_input, "past_observations", None)
    if past and len(past) > 2:
        planner_input.past_observations = past[-2:]
        if _estimate_total() <= budget:
            return

    # --- Halve sections in priority order until the estimate fits ---
    # Strict priority: a later section is only touched once every earlier one
    # is at its floor. Halving is geometric, so even a pathologically large
    # single section converges in ~log2(size/floor) steps.
    for field in _TRIM_STAGES:
        cut_from = None
        while _estimate_total() > budget:
            text = getattr(planner_input, field, None) or ""
            if not isinstance(text, str) or len(text) <= _TRIM_FLOOR_CHARS:
                break
            if cut_from is None:
                cut_from = len(text)
            setattr(planner_input, field, _trim_text_tail(text, 0.5, field))
        if cut_from is not None and field == "instructions":
            # Cutting into admin-authored instructions is a last resort the
            # org should hear about — it means the always-load corpus (or the
            # model's window) is undersized for this workspace.
            _hub_logger.warning(
                "[trim] context over budget after all other sections — cut "
                "<instructions> from %d chars to fit window (budget=%d tokens)",
                cut_from, budget,
            )
        if _estimate_total() <= budget:
            return

    # --- Everything at floor: drop to the single newest observation ---
    past = getattr(planner_input, "past_observations", None)
    if past and len(past) > 1:
        planner_input.past_observations = past[-1:]

    if _estimate_total() > budget:
        _hub_logger.warning(
            "[trim] prompt still over budget after trimming every section to "
            "floor (estimate=%d tokens, budget=%d) — provider may reject it",
            _estimate_total(), budget,
        )


class ContextHub:
    """
    Central hub for all agent context orchestration.
    
    Coordinates existing and new context builders to provide
    comprehensive context for agent execution.
    """
    
    def __init__(
        self,
        db: AsyncSession,
        organization,
        report,
        data_sources,
        user=None,
        head_completion=None,
        widget=None,
        organization_settings=None,
        build_id: Optional[str] = None
    ):
        self.db = db
        self.organization = organization
        self.organization_settings = organization_settings
        self.data_sources = data_sources
        self.report = report
        self.user = user
        self.head_completion = head_completion
        self.widget = widget
        self.prompt_content = head_completion.prompt if head_completion else ""
        # Build system: specific instruction build to use (None = main build)
        self.build_id = build_id
        
        # Initialize metadata
        self.metadata = ContextMetadata(
            organization_id=organization.id,
            user_id=user.id if user else None,
            report_id=report.id if report else None,
            completion_id=head_completion.id if head_completion else None,
            widget_id=widget.id if widget else None,
            external_platform=getattr(head_completion, 'external_platform', None) if head_completion else None,
            external_user_id=getattr(head_completion, 'external_user_id', None) if head_completion else None,
        )
        
        # Initialize builders
        self._init_builders()
        
        # Static context cache (will be added later)
        self._static_cache: Dict[str, Any] = {}
        self._warm_cache: Dict[str, Any] = {}
    
    def _init_builders(self):
        """Initialize all context builders."""
        
        # Existing builders (enhanced)
        # Resolve the current request mode (chat/deep/training) and delivery
        # channel so per-instruction applicable_modes / applicable_channels
        # scoping can be honored. A null external_platform is the in-app web
        # chat, which we represent as the 'app' channel.
        _mode = getattr(self.report, 'mode', None) if self.report else None
        _external_platform = getattr(self.head_completion, 'external_platform', None) if self.head_completion else None
        _channel = _external_platform or 'app'
        self.instruction_builder = InstructionContextBuilder(
            self.db,
            self.organization,
            # Without the caller, the builder's per-user table-accessibility
            # filter is dead code in the agent path: instructions that only
            # reference tables the caller cannot see would still be injected.
            current_user=self.user,
            organization_settings=self.organization_settings,
            data_source_ids=[str(ds.id) for ds in self.data_sources] if self.data_sources else None,
            mode=_mode,
            channel=_channel,
        )
        self.code_builder = CodeContextBuilder(self.db, self.organization)
        self.resource_builder = ResourceContextBuilder(self.db, self.data_sources, self.organization, self.prompt_content)
        self.files_builder = FilesContextBuilder(self.db, self.organization, self.report, head_completion=self.head_completion)
        
        # New builders (port from agent.py)
        self.schema_builder = SchemaContextBuilder(self.db, self.data_sources, self.organization, self.report, user=self.user, organization_settings=self.organization_settings)
        self.message_builder = MessageContextBuilder(self.db, self.organization, self.report, self.user)
        self.widget_builder = WidgetContextBuilder(self.db, self.organization, self.report)
        self.query_builder = QueryContextBuilder(self.db, self.organization, self.report)
        self.mention_builder = MentionContextBuilder(self.db, self.organization, self.report, self.head_completion, user=self.user)
        self.entity_builder = EntityContextBuilder(self.db, self.organization, self.report, user=self.user)
        
        # Observation context builder (tracks tool execution results)
        self.observation_builder = ObservationContextBuilder()

    def _schema_identity_key(self) -> str:
        """Identity component of the schema cache key.

        `SchemaContextBuilder` serves the caller's per-user overlay whenever a
        data source is backed by a `user_required` connection, so the built
        section is user-specific there and MUST NOT be shared between users.
        For purely `system_only` sources every caller gets the same canonical
        catalog, so they can all share one cache entry (which is where the cache
        pays for itself).

        Fails safe: if the connections aren't loaded / can't be inspected, key on
        the user (correct, just a lower hit rate).
        """
        identity_scoped = True
        try:
            identity_scoped = any(
                (getattr(conn, "auth_policy", None) or "system_only") == "user_required"
                for ds in (self.data_sources or [])
                for conn in (getattr(ds, "connections", None) or [])
            )
        except Exception:
            identity_scoped = True
        if not identity_scoped:
            return "system"
        return f"user:{getattr(self.user, 'id', None) or 'anonymous'}"

    async def build_context(
        self,
        spec: Optional[ContextBuildSpec] = None,
        research_context: Optional[Dict[str, Any]] = None,
        loop_index: int = 0
    ) -> ContextSnapshot:
        """
        Build comprehensive context snapshot for agent execution.
        
        Args:
            spec: What context sections to include
            research_context: Accumulated research findings
            loop_index: Current execution loop index
            
        Returns:
            Complete context snapshot with metadata
        """
        start_time = time.time()
        
        # Use default spec if not provided
        if spec is None:
            spec = ContextBuildSpec()
        
        # Update metadata
        self.metadata.loop_index = loop_index
        self.metadata.research_step_count = len(research_context or {})
        
        # Build context sections
        context = ContextSnapshot(metadata=self.metadata)
        section_sizes: Dict[str, int] = {}
        
        # Core sections
        if spec.include_schemas:
            # Build object using config params
            schema_cfg = spec.schema_config or SchemaContextConfig()
            schemas_section = await self.schema_builder.build(
                with_stats=schema_cfg.with_stats,
                top_k=schema_cfg.top_k,
                active_only=schema_cfg.active_only,
            )
            context.schemas_excerpt = schemas_section.render()
            # Prefer object-based count of tables if available; fallback to rendered lines
            try:
                data_sources = getattr(schemas_section, 'data_sources', []) or []
                table_count = 0
                for ds in data_sources:
                    tables = getattr(ds, 'tables', None)
                    if tables is None and isinstance(ds, dict):
                        tables = (ds.get('tables') or [])
                    if tables is None:
                        tables = []
                    try:
                        table_count += len(list(tables))
                    except Exception:
                        pass
                # Only set if we found a meaningful count; else fallback
                self.metadata.schemas_count = table_count if table_count > 0 else len(context.schemas_excerpt.split('\n'))
            except Exception:
                self.metadata.schemas_count = len(context.schemas_excerpt.split('\n'))
            section_sizes['schemas'] = _section_token_length(context.schemas_excerpt or '')
        
        if spec.include_messages:
            # Use new config or fallback to legacy parameters
            message_config = spec.message_config
            if not message_config:
                # Create config from legacy parameters
                message_config = MessageContextConfig(
                    max_messages=spec.max_messages or DEFAULT_CONTEXT_LIMITS["messages_max"],
                    role_filter=spec.message_role_filter
                )
            
            context.messages_context = await self.message_builder.build_context(
                max_messages=message_config.max_messages,
                role_filter=message_config.role_filter
            )
            self.metadata.messages_count = len(context.messages_context.split('\n'))
            section_sizes['messages'] = _section_token_length(context.messages_context or '')
        
        if spec.include_widgets:
            # Use new config or fallback to legacy parameters
            widget_config = spec.widget_config
            if not widget_config:
                widget_config = WidgetContextConfig(
                    max_widgets=spec.max_widgets or 5,
                    status_filter=spec.widget_status_filter
                )
            
            context.widgets_context = await self.widget_builder.build_context(
                max_widgets=widget_config.max_widgets,
                status_filter=widget_config.status_filter,
                include_data_preview=widget_config.include_data_preview
            )
            self.metadata.widgets_count = len(context.widgets_context.split('\n'))
            section_sizes['widgets'] = _section_token_length(context.widgets_context or '')
        
        if spec.include_instructions:
            # Build object, then render for legacy ContextSnapshot
            instruction_config = spec.instruction_config or InstructionContextConfig()
            # Use instruction_config.build_id if specified, otherwise fall back to hub's build_id
            effective_build_id = instruction_config.build_id or self.build_id
            inst_section = await self.instruction_builder.build(
                category=instruction_config.category,
                build_id=effective_build_id,
            )
            context.instructions_context = inst_section.render()
            section_sizes['instructions'] = _section_token_length(context.instructions_context or '')
        
        # Optional sections
        if spec.include_code:
            # CodeContextBuilder has complex interface, skip for now
            # TODO: Implement when code context is needed
            context.code_context = ""
            section_sizes['code'] = _section_token_length(context.code_context or '')
        
        if spec.include_resource:
            context.resource_context = await self.resource_builder.build()
            section_sizes['resources'] = _section_token_length(context.resource_context or '')

        # Files section (object cached, string rendered into legacy snapshot)
        if getattr(spec, 'include_files', True):
            files_section = await self.files_builder.build()
            # Not attached to ContextSnapshot, but its size is real prompt cost —
            # record it so the Context Browser shows a `files` line item.
            try:
                section_sizes['files'] = _section_token_length(files_section.render() if files_section else '')
                self.metadata.__dict__["files_count"] = len(getattr(files_section, 'files', []) or [])
            except Exception:
                pass

        # Entities section (delegated to builder; no inline heuristics)
        try:
            if getattr(spec, 'include_entities', False):
                # Get allow_llm_see_data setting
                allow_llm_see_data = True
                try:
                    org_settings = await self.organization.get_settings(self.db)
                    cfg = org_settings.get_config("allow_llm_see_data") if org_settings else None
                    allow_llm_see_data = bool(cfg.value) if cfg is not None else True
                except Exception:
                    pass
                
                ent_cfg = getattr(spec, 'entities_config', None)
                ent_section = await self.entity_builder.build_for_turn(
                    types=(getattr(ent_cfg, 'types', None) if ent_cfg else None),
                    top_k=(getattr(ent_cfg, 'top_k', 10) if ent_cfg else 10),
                    require_source_assoc=(getattr(ent_cfg, 'require_data_source_association', True) if ent_cfg else True),
                    keywords=(getattr(ent_cfg, 'keywords', None) if ent_cfg else None),
                    user_text=(self.prompt_content.get("content") if isinstance(self.prompt_content, dict) else str(self.prompt_content or "")),
                    allow_llm_see_data=allow_llm_see_data,
                )
                if ent_section:
                    context.entities_context = ent_section.render()
                    self._warm_cache["entities"] = ent_section
                    try:
                        self.metadata.entities_count = len(getattr(ent_section, 'items', []) or [])
                    except Exception:
                        pass
                    section_sizes['entities'] = _section_token_length(context.entities_context or '')
        except Exception:
            pass
        
        # Research context
        if spec.include_research_context and research_context:
            context.research_context = research_context
        
        # Build history summary (simplified for now)
        context.history_summary = await self._build_history_summary(context)
        
        # Update metadata  
        self.metadata.build_duration_ms = (time.time() - start_time) * 1000
        
        # Count warm section items from object-based cache (more accurate than text line counting)
        messages_section = self._warm_cache.get("messages", None)
        if messages_section and hasattr(messages_section, 'items'):
            self.metadata.messages_count = len(messages_section.items)
            # Add messages section size for total_tokens calculation
            messages_text = messages_section.render() if messages_section else ""
            section_sizes['messages'] = _section_token_length(messages_text)
        
        widgets_section = self._warm_cache.get("widgets", None)
        if widgets_section and hasattr(widgets_section, 'items'):
            self.metadata.widgets_count = len(widgets_section.items)
            # Add widgets section size for total_tokens calculation
            widgets_text = widgets_section.render() if widgets_section else ""
            section_sizes['widgets'] = _section_token_length(widgets_text)
        
        queries_section = self._warm_cache.get("queries", None)
        if queries_section and hasattr(queries_section, 'items'):
            self.metadata.queries_count = len(queries_section.items)
            # Add queries section size for total_tokens calculation
            queries_text = queries_section.render() if queries_section else ""
            section_sizes['queries'] = _section_token_length(queries_text)
        
        # Mentions section counts (mirror pattern used above)
        mentions_section = self._warm_cache.get("mentions", None)
        if mentions_section is not None:
            try:
                files_len = len(getattr(mentions_section, 'files', []) or [])
                ds_len = len(getattr(mentions_section, 'data_sources', []) or [])
                tables_len = len(getattr(mentions_section, 'tables', []) or [])
                entities_len = len(getattr(mentions_section, 'entities', []) or [])
                # Expose a total mentions count in metadata for diagnostics
                self.metadata.__dict__["mentions_count"] = files_len + ds_len + tables_len + entities_len
            except Exception:
                pass
            # Add mentions section size for total_tokens calculation
            try:
                mentions_text = mentions_section.render()
                section_sizes['mentions'] = _section_token_length(mentions_text or '')
            except Exception:
                pass
        
        # Expose section sizes for UI diagnostics and calculate total_tokens as sum
        try:
            self.metadata.section_sizes = section_sizes
            # Calculate total_tokens as sum of all section sizes
            self.metadata.total_tokens = sum(section_sizes.values())
        except Exception:
            pass
        context.metadata = self.metadata
        
        return context

    async def build(self, spec: Optional[ContextBuildSpec] = None, research_context: Optional[Dict[str, Any]] = None, loop_index: int = 0) -> ContextObjectsSnapshot:
        """Build and return object-based snapshot."""
        if spec is None:
            spec = ContextBuildSpec()
        self.metadata.loop_index = loop_index
        self.metadata.research_step_count = len(research_context or {})

        # Build sections as objects
        schemas_obj = None
        files_obj = None
        if spec.include_schemas:
            schema_cfg = spec.schema_config or SchemaContextConfig()
            schemas_obj = await self.schema_builder.build(
                with_stats=schema_cfg.with_stats,
                top_k=schema_cfg.top_k,
                active_only=schema_cfg.active_only,
            )

        # Files
        files_obj = await self.files_builder.build()

        snapshot = ContextObjectsSnapshot(
            schemas=schemas_obj,
            files=files_obj,
            metadata=self.metadata,
        )
        # Cache
        self._static_cache["schemas"] = schemas_obj or self._static_cache.get("schemas")
        self._static_cache["files"] = files_obj or self._static_cache.get("files")
        return snapshot

    # --------------------------------------------------------------
    # Simple lifecycle helpers to prime static and refresh warm
    # --------------------------------------------------------------
    async def _instruction_query(self, query: str | None, max_messages: int = 3) -> str | None:
        """Combine the current prompt with the last few user prompts of this
        report, so intelligent-instruction matching sees the conversation topic
        rather than just the newest message."""
        if not self.report:
            return query
        try:
            from sqlalchemy import select
            from app.models.completion import Completion
            result = await self.db.execute(
                select(Completion.prompt)
                .where(
                    Completion.report_id == self.report.id,
                    Completion.role == "user",
                    Completion.deleted_at.is_(None),
                )
                .order_by(Completion.created_at.desc())
                .limit(max_messages)
            )
            parts: list[str] = []
            for (prompt,) in result.all():
                content = prompt.get("content") if isinstance(prompt, dict) else None
                if content and isinstance(content, str):
                    parts.append(content)
            # Oldest first; the current prompt is usually the newest row already,
            # but include `query` explicitly in case it isn't persisted yet.
            parts.reverse()
            if query and (not parts or parts[-1] != query):
                parts.append(query)
            combined = " \n".join(p for p in parts if p).strip()
            return combined or query
        except Exception as e:
            _hub_logger.warning(f"[context_hub] _instruction_query failed: {e}")
            return query

    async def prime_static(self, query: str | None = None) -> None:
        """Build and cache static sections once (schemas, instructions, code, resources).

        Runs all builders in parallel for faster startup.

        Parameters
        ----------
        query : str | None, optional
            The user's query/prompt. If provided, enables intelligent instruction
            search to find relevant instructions beyond just 'always' load mode.
        """
        import asyncio
        _t0 = time.monotonic()

        async def _timed(name, coro):
            t = time.monotonic()
            result = await coro
            _hub_logger.info(f"[context_hub:prime_static] {name} done +{(time.monotonic()-t)*1000:.0f}ms")
            return result

        # Schema cache: by (org, ds-ids, build_id, identity). Schemas dominate
        # the prime_static cost (~1.6s of ~1.9s) and are stable across user
        # prompts; the `query` only affects instructions, not schemas.
        #
        # The identity component is REQUIRED for correctness: SchemaContextBuilder
        # is identity-scoped for `user_required` sources (it serves the caller's
        # per-user overlay), so the built section differs per user. Without it,
        # whichever user warmed the cache had their table list served to every
        # other user of the same org+agents for the whole TTL.
        org_id = str(self.organization.id) if self.organization else ""
        ds_ids: Tuple[str, ...] = tuple(sorted(str(d.id) for d in (self.data_sources or [])))
        cache_key = (
            org_id,
            ds_ids,
            str(self.build_id) if self.build_id else None,
            self._schema_identity_key(),
        )
        now = time.monotonic()
        cached = _SCHEMA_CACHE.get(cache_key)

        async def _build_or_get_schemas():
            if cached is not None and (now - cached[0]) < _SCHEMA_CACHE_TTL_S:
                _hub_logger.info(
                    f"[context_hub:prime_static] schemas cache hit (age={now - cached[0]:.1f}s)"
                )
                return cached[1]
            t = time.monotonic()
            built = await self.schema_builder.build()
            _hub_logger.info(
                f"[context_hub:prime_static] schemas done (cache miss) +{(time.monotonic()-t)*1000:.0f}ms"
            )
            _SCHEMA_CACHE[cache_key] = (time.monotonic(), built)
            return built

        # Enrich the instruction-search query with recent user prompts from this
        # report so follow-ups ("now break it down by month") keep matching the
        # instructions the opening message matched.
        instr_query = await self._instruction_query(query)

        # Same identity component as the schema cache: instructions are filtered
        # by per-user table accessibility, so they must not cross users either.
        # The builder's data_source_ids scope is part of the key: it is narrowed
        # to the roster focus per turn (globals only until an agent is picked),
        # so two turns with different focus must not share a cache entry.
        instr_scope = getattr(self.instruction_builder, "data_source_ids", None)
        instr_key = (
            org_id,
            ds_ids,
            str(self.build_id) if self.build_id else None,
            str(instr_query or ""),
            self._schema_identity_key(),
            tuple(sorted(instr_scope)) if instr_scope is not None else None,
        )
        instr_cached = _INSTRUCTIONS_CACHE.get(instr_key)

        async def _build_or_get_instructions():
            if instr_cached is not None and (now - instr_cached[0]) < _INSTRUCTIONS_CACHE_TTL_S:
                _hub_logger.info(
                    f"[context_hub:prime_static] instructions cache hit (age={now - instr_cached[0]:.1f}s)"
                )
                return instr_cached[1]
            t = time.monotonic()
            built = await self.instruction_builder.build(instr_query, build_id=self.build_id)
            _hub_logger.info(
                f"[context_hub:prime_static] instructions done (cache miss) +{(time.monotonic()-t)*1000:.0f}ms"
            )
            _INSTRUCTIONS_CACHE[instr_key] = (time.monotonic(), built)
            return built

        # Run static builders in parallel; schemas/instructions come from
        # the cache when warm.
        schemas, instructions, resources, files = await asyncio.gather(
            _build_or_get_schemas(),
            _build_or_get_instructions(),
            _timed("resources", self.resource_builder.build()),
            _timed("files", self.files_builder.build()),
            return_exceptions=True,
        )
        _hub_logger.info(f"[context_hub:prime_static] all_done +{(time.monotonic()-_t0)*1000:.0f}ms")

        # Store results (handle exceptions gracefully)
        self._static_cache["schemas"] = schemas if not isinstance(schemas, Exception) else None
        self._static_cache["instructions"] = instructions if not isinstance(instructions, Exception) else None
        self._static_cache["code"] = None
        self._static_cache["resources"] = resources if not isinstance(resources, Exception) else None
        self._static_cache["files"] = files if not isinstance(files, Exception) else None

    async def refresh_warm(self) -> None:
        """Rebuild warm sections each loop (messages, queries, observations, entities).

        Runs builders in parallel where possible for faster refresh.
        """
        import asyncio
        _t0 = time.monotonic()

        async def _timed(name, coro):
            t = time.monotonic()
            result = await coro
            _hub_logger.info(f"[context_hub:refresh_warm] {name} done +{(time.monotonic()-t)*1000:.0f}ms")
            return result

        # Get org settings first (needed for queries and entities)
        allow_llm_see_data = True
        try:
            org_settings = await self.organization.get_settings(self.db)
            cfg = org_settings.get_config("allow_llm_see_data") if org_settings else None
            allow_llm_see_data = bool(cfg.value) if cfg is not None else True
        except Exception:
            allow_llm_see_data = True

        # Extract user text for entity keyword matching
        user_text = ""
        try:
            if isinstance(self.prompt_content, dict):
                user_text = self.prompt_content.get("content", "")
            else:
                user_text = str(self.prompt_content or "")
        except Exception:
            user_text = ""

        # Run all warm builders in parallel
        messages, queries, mentions, entities = await asyncio.gather(
            _timed("messages", self.message_builder.build(max_messages=DEFAULT_CONTEXT_LIMITS["messages_max"])),
            _timed("queries", self.query_builder.build(max_queries=5, include_data_preview=allow_llm_see_data)),
            _timed("mentions", self.mention_builder.build()),
            _timed("entities", self.entity_builder.build_for_turn(
                top_k=5,
                require_source_assoc=True,
                user_text=user_text,
                allow_llm_see_data=allow_llm_see_data,
            )),
            return_exceptions=True,
        )
        _hub_logger.info(f"[context_hub:refresh_warm] all_done +{(time.monotonic()-_t0)*1000:.0f}ms")
        
        # Build observations synchronously (it's fast, no DB calls)
        observations = self.observation_builder.build()
        _safe_setattr_list(observations, "items", DEFAULT_CONTEXT_LIMITS["observations_max"])

        # Scheduled tasks for this report (warm: changes when created/cancelled mid-session)
        try:
            scheduled_tasks = await self._build_scheduled_tasks_section()
        except Exception as e:
            _hub_logger.warning(f"[context_hub:refresh_warm] scheduled_tasks failed: {e}")
            scheduled_tasks = None

        self._warm_cache.update({
            "messages": messages if not isinstance(messages, Exception) else None,
            "widgets": None,  # Deprecated
            "queries": queries if not isinstance(queries, Exception) else None,
            "observations": observations,
            "mentions": mentions if not isinstance(mentions, Exception) else None,
            "entities": entities if not isinstance(entities, Exception) else None,
            "scheduled_tasks": scheduled_tasks,
        })

    async def _build_scheduled_tasks_section(self):
        """Build the <scheduled_tasks> section for the current report.

        Lists active (non-deleted) recurring tasks so the agent can avoid
        duplicates and has task ids available for cancellation.
        """
        from app.ai.context.sections.scheduled_tasks_section import (
            ScheduledTasksSection,
            ScheduledTaskItem,
        )
        if not self.report:
            return ScheduledTasksSection(items=[])

        from sqlalchemy import select
        from app.models.scheduled_prompt import ScheduledPrompt

        cron_labels = {
            "0 * * * *": "Hourly",
            "0 8 * * *": "Daily at 8am",
            "0 9 * * *": "Daily at 9am",
            "0 0 * * *": "Daily at midnight",
            "0 9 * * 1": "Weekly on Monday at 9am",
            "0 8 * * 1": "Weekly on Monday at 8am",
            "0 0 * * 1": "Weekly on Monday at midnight",
        }

        result = await self.db.execute(
            select(ScheduledPrompt)
            .filter(ScheduledPrompt.report_id == self.report.id)
            .filter(ScheduledPrompt.is_active == True)  # noqa: E712
            .filter(ScheduledPrompt.deleted_at == None)  # noqa: E711
            .order_by(ScheduledPrompt.created_at.asc())
        )
        rows = list(result.scalars().all())

        items: list = []
        for sp in rows:
            content = ""
            try:
                content = (sp.prompt or {}).get("content", "") if isinstance(sp.prompt, dict) else ""
            except Exception:
                content = ""
            snippet = content.strip()
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            items.append(ScheduledTaskItem(
                id=str(sp.id),
                title=sp.title,
                cron_schedule=sp.cron_schedule,
                cron_label=cron_labels.get(sp.cron_schedule),
                prompt_snippet=snippet or None,
                last_run_at=sp.last_run_at.isoformat() if sp.last_run_at else None,
            ))
        return ScheduledTasksSection(items=items)

    def get_view(self) -> ContextView:
        """Return a read-only grouped view over current static + warm context."""
        static = StaticSections(
            schemas=self._static_cache.get("schemas", None),
            instructions=self._static_cache.get("instructions", None),
            resources=self._static_cache.get("resources", None),
            code=self._static_cache.get("code", None),
            files=self._static_cache.get("files", None),
        )
        warm = WarmSections(
            messages=self._warm_cache.get("messages", None),
            observations=self._warm_cache.get("observations", None),
            widgets=self._warm_cache.get("widgets", None),
            queries=self._warm_cache.get("queries", None),
            mentions=self._warm_cache.get("mentions", None),
            entities=self._warm_cache.get("entities", None),
            scheduled_tasks=self._warm_cache.get("scheduled_tasks", None),
        )
        meta = self.metadata.model_dump()
        return ContextView(static=static, warm=warm, meta=meta)
    
    async def _build_history_summary(self, context: ContextSnapshot) -> str:
        """Build a summary of conversation history for planner context."""
        # Simplified implementation - can be enhanced later
        summary_parts = []
        
        if context.messages_context:
            summary_parts.append(f"Previous conversation: {len(context.messages_context.split('user:'))} exchanges")
        
        if context.widgets_context:
            summary_parts.append(f"Created widgets: {self.metadata.widgets_count}")
        
        if context.research_context:
            research_tools = list(context.research_context.keys())
            summary_parts.append(f"Research completed: {', '.join(research_tools)}")
        
        return "; ".join(summary_parts) if summary_parts else "No previous context"
    
    async def get_schemas(self) -> str:
        """Quick access to schemas context only."""
        section = await self.schema_builder.build()
        return section.render()
    
    async def get_messages_context(self, max_messages: int = 20) -> str:
        """Quick access to messages context only."""
        section = await self.message_builder.build(max_messages=max_messages)
        return section.render()
    
    async def get_resources_context(self) -> str:
        """Quick access to resources context from metadata resources."""
        section = await self.resource_builder.build()
        return section.render()
    
    def get_history_summary(self, research_context: Optional[Dict[str, Any]] = None) -> str:
        """Quick access to history summary from cached warm sections.
        
        This is a fast synchronous method that uses already-cached data from
        refresh_warm() instead of rebuilding context via build_context().
        """
        summary_parts = []
        
        # Use cached messages section
        messages_section = self._warm_cache.get("messages")
        if messages_section:
            try:
                items = getattr(messages_section, 'items', []) or []
                user_count = sum(1 for m in items if getattr(m, 'role', '') == 'user')
                summary_parts.append(f"Previous conversation: {user_count} exchanges")
            except Exception:
                pass
        
        # Use cached widgets section
        widgets_section = self._warm_cache.get("widgets")
        if widgets_section:
            try:
                items = getattr(widgets_section, 'items', []) or []
                if items:
                    summary_parts.append(f"Created widgets: {len(items)}")
            except Exception:
                pass
        
        # Use cached queries section
        queries_section = self._warm_cache.get("queries")
        if queries_section:
            try:
                items = getattr(queries_section, 'items', []) or []
                if items:
                    summary_parts.append(f"Queries executed: {len(items)}")
            except Exception:
                pass
        
        # Include research context if provided
        if research_context:
            research_tools = list(research_context.keys())
            if research_tools:
                summary_parts.append(f"Research completed: {', '.join(research_tools)}")
        
        return "; ".join(summary_parts) if summary_parts else "No previous context"
    
    async def render(self, format_for_prompt: bool = True, include_metadata: bool = False) -> str:
        """
        Render context for prompt inclusion or debugging.
        
        Args:
            format_for_prompt: If True, format for LLM prompt inclusion (compact).
                             If False, format for debugging/inspection (detailed).
            include_metadata: Whether to include metadata in the output
            
        Returns:
            Formatted string representation of all context
        """
        context = await self.build_context()
        
        if format_for_prompt:
            return self._render_for_prompt(context, include_metadata)
        else:
            return self._render_for_debug(context, include_metadata)
    
    def _render_for_prompt(self, context: ContextSnapshot, include_metadata: bool) -> str:
        """Render context optimized for LLM prompt inclusion (compact format)."""
        parts = []
        
        if include_metadata:
            parts.append(f"<context_meta org={self.metadata.organization_id} report={self.metadata.report_id} loop={self.metadata.loop_index}/>")
        
        if context.schemas_excerpt:
            parts.append(f"<schemas>\n{context.schemas_excerpt}\n</schemas>")
        
        if context.messages_context:
            # Compact message format for prompts
            parts.append(f"<conversation>\n{context.messages_context[:2000]}...\n</conversation>")
        
        if context.widgets_context:
            parts.append(f"<widgets>\n{context.widgets_context}\n</widgets>")
        
        if context.instructions_context:
            parts.append(f"<instructions>\n{context.instructions_context}\n</instructions>")
        
        if context.research_context:
            research_summary = "; ".join([f"{k}: {v}" for k, v in context.research_context.items()])
            parts.append(f"<research>\n{research_summary}\n</research>")
        
        return "\n\n".join(parts)
    
    def _render_for_debug(self, context: ContextSnapshot, include_metadata: bool) -> str:
        """Render context for debugging/inspection (detailed format)."""
        parts = []
        
        if include_metadata:
            parts.append("=== CONTEXT METADATA ===")
            parts.append(f"Organization: {self.metadata.organization_id}")
            parts.append(f"Report: {self.metadata.report_id}")
            parts.append(f"User: {self.metadata.user_id}")
            parts.append(f"Loop: {self.metadata.loop_index}")
            parts.append(f"Research Steps: {self.metadata.research_step_count}")
            parts.append(f"Generated: {self.metadata.generation_time}")
            parts.append("")
        
        if context.schemas_excerpt:
            parts.append("=== SCHEMAS ===")
            parts.append(context.schemas_excerpt)
            parts.append("")
        
        if context.messages_context:
            parts.append("=== CONVERSATION HISTORY ===")
            parts.append(context.messages_context)
            parts.append("")
        
        if context.widgets_context:
            parts.append("=== WIDGETS ===")
            parts.append(context.widgets_context)
            parts.append("")
        
        if context.instructions_context:
            parts.append("=== INSTRUCTIONS ===")
            parts.append(context.instructions_context)
            parts.append("")
        
        # Add observation context if available
        observation_context = self.observation_builder.build_context(format_for_prompt=False)
        if observation_context:
            parts.append("=== OBSERVATION CONTEXT ===")
            parts.append(observation_context)
            parts.append("")
        
        if context.history_summary:
            parts.append("=== SUMMARY ===")
            parts.append(context.history_summary)
        
        return "\n".join(parts)