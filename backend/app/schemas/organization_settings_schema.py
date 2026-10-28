from pydantic import BaseModel, validator, Field
from typing import Dict, Any, Optional, Union, List
import json
from datetime import datetime
from enum import Enum

class FeatureState(str, Enum):
    """Explicit states for features"""
    ENABLED = "enabled"
    DISABLED = "disabled"
    LOCKED = "locked"

class FeatureConfig(BaseModel):
    # enabled: bool = True  # Keep for backward compatibility - REMOVED
    value: Optional[Any] = None
    name: str
    description: str
    is_lab: bool = False
    editable: bool = True
    state: FeatureState = FeatureState.ENABLED # Default state

    @validator('value', pre=True, always=True)
    def set_default_value_if_none(cls, v, values):
        """Set default value based on state if value is None"""
        if v is None:
            # Default value to True if state is ENABLED, False otherwise
            return values.get('state', FeatureState.ENABLED) == FeatureState.ENABLED
        return v

    @validator('state', pre=True, always=True)
    def set_state_from_value(cls, v, values):
        """Set state based on value field if state is not provided or applicable"""
        # If state is already set (e.g., to LOCKED), respect it.
        if v is not None and v != FeatureState.ENABLED and v != FeatureState.DISABLED:
            return v

        # Determine state from value if value is boolean
        value = values.get('value')
        if isinstance(value, bool):
            return FeatureState.ENABLED if value else FeatureState.DISABLED
        # Fallback to ENABLED if value isn't boolean and state isn't set
        return v or FeatureState.ENABLED


    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        """Ensure state reflects value unless explicitly different (e.g., LOCKED)"""
        d = super().dict(*args, **kwargs)
        # Ensure state is consistent with boolean value if not LOCKED
        if isinstance(self.value, bool) and self.state != FeatureState.LOCKED:
             d['state'] = FeatureState.ENABLED if self.value else FeatureState.DISABLED
        # Ensure value is consistent with state if value is boolean
        if isinstance(self.value, bool):
             d['value'] = (self.state == FeatureState.ENABLED)
        return d

    class Config:
        validate_assignment = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureConfig":
        """Create a FeatureConfig from a dictionary, with proper defaults."""
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return self.model_dump()

    def merge(self, other: Union[Dict[str, Any], "FeatureConfig"]) -> "FeatureConfig":
        """Merge with another FeatureConfig or dict, preserving existing values."""
        if isinstance(other, dict):
            other_dict = other
        else:
            other_dict = other.to_dict()

        current = self.to_dict()
        current.update(other_dict)
        return FeatureConfig(**current)

    # @validator('value') # Keep this if specific validation rules are needed later
    # def validate_value(cls, v, values):
    #     """Validate that value is appropriate for the feature."""
    #     # Add any specific validation rules here
    #     return v

class PiiRule(BaseModel):
    """A single PII detection rule.

    One logical rule (e.g. "Phone number") carries *multiple* regex patterns so
    the many real-world shapes of the same entity (US, international, dotted,
    spaced) can be matched under one enable switch and one replacement token.
    A match is any pattern hitting. ``builtin`` rules ship in code — the org
    config only stores an override (enable/replacement) for them keyed by ``id``,
    never their pattern definitions.
    """
    id: str
    name: str
    patterns: List[str] = []
    replacement: str = "[REDACTED]"
    enabled: bool = True
    builtin: bool = False
    # Per-rule action. None => inherit the workspace default (``mode``).
    # "replace" swaps matches with ``replacement``; "block" refuses the whole
    # request if this rule matches (block always wins over replace).
    action: Optional[str] = None

    @validator('action', pre=True, always=True)
    def validate_action(cls, v):
        if v in (None, "replace", "block"):
            return v
        return None


class PiiProtectionConfig(BaseModel):
    """Org-level configuration for redacting PII from prompts before they are
    sent to any LLM provider. Enterprise-gated (``pii_protection`` feature) —
    when the instance is unlicensed the redactor is a no-op regardless of this
    config, so nothing here can turn the feature on in a community build.

    Only overrides + custom rules live here; the built-in rule catalogue lives
    in ``app.ai.llm.pii.builtin_rules`` so patterns can be improved in code
    without a migration.
    """
    enabled: bool = False  # master switch (still requires an enterprise license)
    mode: str = "replace"  # "replace" (swap with token) | "block" (refuse the call)
    # Per-builtin overrides keyed by rule id -> {"enabled": bool, "replacement": str}
    builtin_overrides: Dict[str, Dict[str, Any]] = {}
    # Fully user-defined rules
    custom_rules: List[PiiRule] = []

    @validator('mode', pre=True, always=True)
    def validate_mode(cls, v):
        if v not in ("replace", "block"):
            return "replace"
        return v


# Microsoft Graph /me fields that are readable with the default-granted
# delegated ``User.Read`` scope (no admin consent required). All are readable
# on the signed-in user's own profile via ``GET /me?$select=...``. The
# ``employee*`` fields are worker-record attributes but are NOT permission-gated
# — the only Entra "employee" field that needs elevated access is
# ``employeeLeaveDateTime`` (``User-LifeCycleInfo.Read.All`` + an admin role),
# which is deliberately excluded from this allowlist.
ENTRA_PROFILE_SYNC_ALLOWED_FIELDS = [
    "jobTitle",
    "department",
    "companyName",
    "officeLocation",
    "employeeId",
    "employeeType",
    "employeeHireDate",
    "employeeOrgData",  # nested: division + costCenter
    "mobilePhone",
    "city",
    "state",
    "country",
    "usageLocation",
    "preferredLanguage",
]

# Sensible default subset synced when the feature is first enabled.
ENTRA_PROFILE_SYNC_DEFAULT_FIELDS = [
    "jobTitle",
    "department",
    "companyName",
    "officeLocation",
]


class EntraProfileSyncConfig(BaseModel):
    """Per-org toggle for syncing Microsoft Entra ID profile / job info.

    When enabled, the signed-in user's Graph ``/me`` profile (job title,
    department, etc.) is fetched on login and stored for AI context. Uses the
    delegated ``User.Read`` scope, which Entra grants by default — no admin
    consent required. Configured on the Identity Providers settings page rather
    than in bow-config, so it is opt-in per organization.
    """
    enabled: bool = False
    fields: List[str] = ENTRA_PROFILE_SYNC_DEFAULT_FIELDS


# Google profile fields readable with the ``openid profile email`` scopes
# already requested at login — no extra consent. ``displayName``, ``locale``
# and ``hostedDomain`` come from the OAuth2 userinfo endpoint; the job-info
# fields (``jobTitle``, ``department``, ``organization``, ``location``) come
# from the People API (``people/me?personFields=organizations,locations``) and
# carry the Workspace admin-set directory profile when present. The People API
# must be enabled on the OAuth client's GCP project — when it isn't, those
# fields simply come back unset instead of failing the sync.
GOOGLE_PROFILE_SYNC_ALLOWED_FIELDS = [
    "displayName",
    "jobTitle",
    "department",
    "organization",
    "location",
    "locale",
    "hostedDomain",
]

# Sensible default subset synced when the feature is first enabled.
GOOGLE_PROFILE_SYNC_DEFAULT_FIELDS = [
    "displayName",
    "jobTitle",
    "department",
    "organization",
]


class GoogleProfileSyncConfig(BaseModel):
    """Per-org toggle for syncing Google profile / job info.

    When enabled, the signed-in user's Google profile (name, and — for
    Workspace accounts — directory job title, department, etc.) is fetched on
    login and stored for AI context. Uses only the ``openid profile email``
    scopes the Google login already requests. Configured on the Identity
    Providers settings page rather than in bow-config, so it is opt-in per
    organization.
    """
    enabled: bool = False
    fields: List[str] = GOOGLE_PROFILE_SYNC_DEFAULT_FIELDS


class OrganizationSettingsConfig(BaseModel):
    # General (workspace) settings
    class GeneralConfig(BaseModel):
        ai_analyst_name: str = "AI Analyst"
        bow_credit: bool = True
        # Icon storage fields (disk/object storage)
        icon_key: Optional[str] = None
        icon_url: Optional[str] = None

        @validator('ai_analyst_name')
        def validate_ai_name(cls, v: str) -> str:
            name = (v or "").strip()
            if len(name) == 0:
                raise ValueError("AI analyst name cannot be empty")
            if len(name) > 50:
                raise ValueError("AI analyst name must be 50 characters or less")
            return name

    general: GeneralConfig = GeneralConfig()

    # Locale override for this org. When None, the system default from
    # bow_config.i18n.default_locale applies. Validated against
    # bow_config.i18n.enabled_locales at the service layer (not here, to
    # avoid coupling the schema to runtime config).
    locale: Optional[str] = None

    # IANA timezone for this org (e.g. "America/New_York"). When None, UTC is
    # used. Governs how wall-clock schedules (e.g. the reindex daily time) map
    # onto the UTC timeline and how timestamps are presented — storage stays UTC.
    # Validated against zoneinfo at the service layer.
    timezone: Optional[str] = None

    # First day of the work week, governing how the AI interprets "this week" /
    # "last week". One of "sunday" | "monday" | "saturday", or None/"auto" to
    # derive from ``locale`` (Hebrew/Arabic -> Sunday, otherwise Monday/ISO).
    # Validated at the service layer. Presentation only; storage stays UTC.
    week_start: Optional[str] = None

    # Signup policy (domain allowlist). Gate: full_admin_access.
    class SignupPolicy(BaseModel):
        enabled: bool = False
        allowed_domains: List[str] = []
        auto_invite_role: str = "member"

    signup_policy: SignupPolicy = SignupPolicy()

    # Entra ID profile / job-info sync. Per-org opt-in, configured on the
    # Identity Providers settings page (not bow-config). When enabled, the
    # signed-in user's Graph /me profile is fetched on login and stored for AI
    # context. Gate: manage_identity_providers.
    entra_profile_sync: EntraProfileSyncConfig = EntraProfileSyncConfig()

    # Google profile / job-info sync. Same shape and gate as the Entra sync
    # above, fed from Google userinfo + the People API instead of MS Graph.
    google_profile_sync: GoogleProfileSyncConfig = GoogleProfileSyncConfig()

    # PII protection for outbound LLM prompts. Enterprise-gated (see
    # PiiProtectionConfig). Stored as a nested block (like signup_policy) rather
    # than a FeatureConfig so it gets its own settings page instead of the
    # auto-rendered AI-settings list.
    pii_protection: PiiProtectionConfig = PiiProtectionConfig()

    # How long (in hours) a Teams 1:1 / WhatsApp conversation keeps reusing the
    # same report before the next message starts a fresh one. Stored as plain
    # ints (not FeatureConfig) so they surface on the Channels settings page
    # rather than the auto-rendered AI-settings list. Range-validated (1-720)
    # in OrganizationSettingsService.update_settings.
    teams_session_max_age_hours: int = 120
    whatsapp_session_max_age_hours: int = 24
    google_chat_session_max_age_hours: int = 120

    # Whether the bot announces "I've started a new conversation report" (with a
    # link) when a message on an external channel creates a fresh report. Plain
    # per-platform bools (not FeatureConfig) so they surface in each channel's
    # integration modal on the Channels settings page. WhatsApp has no flag: it
    # never sends the announcement (see ExternalPlatformManager). The explicit
    # "new" command confirmation is always sent regardless — it is the only
    # feedback that command gets.
    slack_announce_new_report: bool = True
    teams_announce_new_report: bool = True
    google_chat_announce_new_report: bool = True
    email_announce_new_report: bool = True

    # Org-wide default automation policy for agent reliability (the
    # self-learning loop). A plain dict matching AgentAutomationPolicy; agents
    # inherit this and may override per-agent via
    # DataSource.automation_settings. Empty/partial is fine — unset keys fall
    # back to the built-in conservative defaults (master switch off). Gate:
    # full_admin_access / manage settings.
    agent_automation_defaults: Dict[str, Any] = {}

    # Update defaults to use 'value' instead of 'enabled'
    allow_llm_see_data: FeatureConfig = FeatureConfig(value=True, name="Allow LLM to see data", description="Enable LLM to see data as part of the analysis and user queries", is_lab=False, editable=True)
    enable_artifact_resources: FeatureConfig = FeatureConfig(value=False, name="Artifact resources", description="Allow artifacts to use persistent records, private files and approved AI operations. Existing sharing and resource permissions still apply.", is_lab=False, editable=True)
    enable_artifact_verification: FeatureConfig = FeatureConfig(value=True, name="Verify data apps", description="Let the agent check interactive page artifacts with browser tools. Requires Allow LLM to see data.", is_lab=True, editable=True)
    enable_training_mode: FeatureConfig = FeatureConfig(value=True, name="Training Mode", description="Enable training mode for admins to work with the agent to build documentation, instructions, semantics and guidelines ", is_lab=False, editable=True)
    enable_demo_data_generation: FeatureConfig = FeatureConfig(value=False, name="Demo data generation", description="In training mode, let admins who can manage connections ask the agent to design and generate a realistic mock SQLite dataset for any domain (finance, HR, sales, logs, machine data...), then create a connection and suggested agents on top. Data is generated by the small default model.", is_lab=True, editable=True)
    # Voice input in the web composer. Transcription runs in the user's
    # browser (the Web Speech API), so no audio reaches this server; the
    # browser vendor's speech service may receive it. Frontend-only gate:
    # when off, the microphone button is hidden.
    enable_voice_input: FeatureConfig = FeatureConfig(value=True, name="Voice input", description="Show a microphone button in the chat composer so users can dictate a prompt. Transcription runs in the user's browser (Chrome, Edge and Safari); audio never reaches this server, but the browser vendor's speech service may process it.", is_lab=False, editable=True)
    enable_file_upload: FeatureConfig = FeatureConfig(value=True, name="Allow file upload", description="Allow users to upload spreadsheets and documents (xls/pdf) and push their content to the LLM", is_lab=False, editable=True)
    enable_llm_judgement: FeatureConfig = FeatureConfig(value=True, name="Enable LLM Judge", description="Enable LLM to judge the quality of the analysis and user queries", is_lab=False, editable=True)
    suggest_instructions: FeatureConfig = FeatureConfig(value=True, name="Autogenerate instructions", description="Automatically generate instructions following clarifications provided by the user", is_lab=False, editable=True)
    enable_follow_ups: FeatureConfig = FeatureConfig(value=True, name="Follow-up suggestions", description="After each answer in the web app, suggest a few follow-up questions the user can ask next. Generated on the small/default model.", is_lab=False, editable=True)
    auto_suggest_evals: FeatureConfig = FeatureConfig(value=True, name="Auto-suggest evals", description="When a manage-evals user upvotes a response that produced data, the knowledge harness drafts an eval test case (judge + tool calls) into the org's drafts suite for review.", is_lab=False, editable=True)
    # validate_code: FeatureConfig = FeatureConfig(value=True, name="Validate code", description="Validate the code generated by the LLM", is_lab=False, editable=True)
    limit_row_count: FeatureConfig = FeatureConfig(value=1000, name="Limit row count", description="Limit the number of rows that can be displayed in tables and data previews. Set to 0 for no limit.", is_lab=False, editable=True)

    @validator('limit_row_count', pre=False, always=True)
    def validate_limit_row_count(cls, v):
        """Set state to DISABLED when value is 0 or less (no limit)."""
        if v.value is not None and isinstance(v.value, (int, float)) and v.value <= 0:
            v.state = FeatureState.DISABLED
        return v
    ai_tool_concurrency: FeatureConfig = FeatureConfig(value=4, name="Parallel tool calls", description="How many tool calls from one AI plan step may run at the same time (e.g. create_data / inspect_data across different agents). Set to 1 to run them one after another; up to 8. Calls against the same agent always run one at a time.", is_lab=True, editable=True)
    # ── Sub-agents (delegated agent runs with wave fan-in; docs/design/sub-agents.md) ──
    enable_sub_agents: FeatureConfig = FeatureConfig(value=True, name="Sub-agents", description="Let the agent delegate independent sub-questions (for example one hypothesis each in a root-cause analysis) to bounded background sub-agents that investigate on their own and report back as typed records in one fan-in turn. When off, the delegate tool is hidden.", is_lab=True, editable=True)
    sub_agent_posture: FeatureConfig = FeatureConfig(value="proactive", name="Sub-agent posture", description="'proactive': the agent delegates on its own when a request splits into three or more independent sub-questions (a 'why did X drop' with several candidate causes, a fan-out across many sources). 'explicit': the agent delegates only when the user or an instruction asks for it.", is_lab=True, editable=True)
    sub_agent_max_steps: FeatureConfig = FeatureConfig(value=40, name="Sub-agent max steps", description="Planner steps one sub-agent may take before it is forced to finish with a partial record. Clamped to 5-200.", is_lab=False, editable=True)
    sub_agent_max_tokens: FeatureConfig = FeatureConfig(value=400000, name="Sub-agent max tokens", description="LLM tokens (prompt plus completion, summed over its steps) one sub-agent may spend before it is told to finish; one more step is allowed for the record, then it is stopped with a partial record. Steps bound time, this bounds cost. Clamped to 20,000-5,000,000.", is_lab=False, editable=True)
    sub_agent_deadline_minutes: FeatureConfig = FeatureConfig(value=15, name="Sub-agent deadline (minutes)", description="Wall-clock limit for a wave of sub-agents. When it fires, still-running sub-agents are stopped and recorded as timed out, and the fan-in turn runs with the records that exist. Clamped to 2-60.", is_lab=False, editable=True)
    sub_agent_concurrency: FeatureConfig = FeatureConfig(value=3, name="Concurrent sub-agents", description="How many sub-agents of one report may run at the same time. Sub-agents beyond the cap wait inside their wave. Clamped to 1-6.", is_lab=True, editable=True)
    sub_agent_max_per_wave: FeatureConfig = FeatureConfig(value=5, name="Max sub-agents per wave", description="How many delegate calls the agent may launch from one planning step. Extra calls are refused with a note so the agent can plan a second wave. Clamped to 1-10.", is_lab=True, editable=True)
    sub_agent_model: FeatureConfig = FeatureConfig(value="auto", name="Sub-agent model", description="Model sub-agents plan on. 'auto' = the same model as the main conversation (the report's model when one is set); 'small' forces the small default model; or a model id from the LLM page. A per-call override from the agent is still validated against the user's model access.", is_lab=False, editable=True)
    sub_agent_allow_model_override: FeatureConfig = FeatureConfig(value=True, name="Allow sub-agent model override", description="Let the agent pick a different model for one sub-agent when the user asked for it or an instruction names one. The pick is validated against the models the asking user may use.", is_lab=True, editable=True)
    sub_agent_allow_create_data: FeatureConfig = FeatureConfig(value=False, name="Sub-agents may create queries", description="Let sub-agents create tracked queries (create_data) whose ids come back as evidence the agent can chart and cite. Off = research-only sub-agents (describe, inspect, read, notes).", is_lab=True, editable=True)
    agent_max_steps: FeatureConfig = FeatureConfig(value=100, name="Max agent steps", description="Maximum number of planner steps (decisions/tool calls) the agent may take in a single request before it stops. Applies to both regular and training mode. Clamped to 1-500.", is_lab=False, editable=True)
    agent_loop_retries: FeatureConfig = FeatureConfig(value=2, name="Agent loop retries", description="How many times the agent may recover from an unexpected error during a run and retry from its latest context, instead of failing the whole run. When retries are exhausted and LLM fallback is enabled, the run switches to the next model in the fallback order before giving up. Clamped to 0-10.", is_lab=False, editable=True)
    limit_code_retries: FeatureConfig = FeatureConfig(value=2, name="Limit code retries", description="How many attempts the LLM gets to generate working code for a data request (initial attempt plus retries on failure). Clamped to 1-10.", is_lab=False, editable=True)
    query_timeout_seconds: FeatureConfig = FeatureConfig(value=180, name="Query timeout (seconds)", description="Default per-query wall-clock timeout when the agent runs SQL via create_data / inspect_data. A connection's config can override this with its own 'query_timeout_seconds' value.", is_lab=False, editable=True)
    max_concurrent_queries_per_connection: FeatureConfig = FeatureConfig(value=4, name="Concurrent queries per connection", description="How many agent queries may run against one connection at the same time, per replica. A burst above this waits for a slot rather than failing. Lower it for fragile on-prem sources (Oracle, SQL Server) that cannot take parallel scans. A connection's config can override this with its own 'max_concurrent_queries' value.", is_lab=False, editable=True)
    top_k_schema: FeatureConfig = FeatureConfig(value=10, name="Top K schema", description="The number of schema to sample from the data source in the Agent", is_lab=False, editable=True) # Assuming value is int here
    top_k_metadata_resources: FeatureConfig = FeatureConfig(value=10, name="Top K metadata resources", description="The number of metadata resources to sample from the data source in the Agent", is_lab=False, editable=True) # Assuming value is int here
    agent_roster_top_k: FeatureConfig = FeatureConfig(value=10, name="Agent roster top K", description="When a report has many agents, how many appear as full roster lines in the AI context (ranked by the asker's recent usage; the rest are listed by name only in a more_agents tail). Clamped to 1-100.", is_lab=False, editable=True)
    # Off = no new "anyone with the link" shares (dashboards or conversations),
    # and existing public links only open for members of this organization.
    # Stored visibility is left alone, so turning it back on restores the links.
    # Enforced in app/services/public_sharing_policy.py.
    allow_public_sharing: FeatureConfig = FeatureConfig(value=True, name="Public link sharing", description="Allow dashboards and conversations to be shared with anyone who has the link, including people outside the organization. When off, links that are already public only open for signed-in members of this organization; turning it back on restores them.", is_lab=False, editable=True)
    allow_forks: FeatureConfig = FeatureConfig(value=True, name="Allow Forks", description="Allow users to fork published reports into their own workspace", is_lab=False, editable=True)
    mcp_enabled: FeatureConfig = FeatureConfig(value=True, name="MCP", description="Enable Model Context Protocol (MCP) endpoint for integration with AI assistants like Cursor, Claude, or others", is_lab=False, editable=True)
    enable_mcp_tools: FeatureConfig = FeatureConfig(value=True, name="MCP & Custom API Tools", description="Allow connecting external MCP servers and custom API endpoints to data sources as tool providers", is_lab=True, editable=True)
    tools_max: FeatureConfig = FeatureConfig(value=50, name="Tools max", description="How many external MCP / custom API tools are given to the agent as its own tools in one run. Past this number, the most used tools are kept and the rest stay reachable through tool search, so a very large catalog cannot fill every request with tool definitions.", is_lab=True, editable=True)
    mcp_create_data_preview_rows: FeatureConfig = FeatureConfig(value=1000, name="MCP data preview rows", description="How many result rows create_data returns to an external MCP client (Claude, Cursor, etc.) in its response. The full result is always saved in the report; this only decides how much the client's model receives inline. A client may ask for fewer rows, never more. Also bounded by 'Limit row count'. Clamped to 1-10000.", is_lab=False, editable=True)
    enable_web_fetch: FeatureConfig = FeatureConfig(value=False, name="Web Fetch", description="Allow the agent to fetch the contents of public HTTP and HTTPS URLs. Only text-like responses are returned and large bodies are truncated.", is_lab=False, editable=True)
    enable_load_step: FeatureConfig = FeatureConfig(value=False, name="Reuse prior steps (load_step)", description="Let generated code reuse a prior step's results in this report via load_step instead of re-querying. When off, load_step is neither advertised to the agent nor available at runtime.", is_lab=False, editable=True)
    enable_ml_training: FeatureConfig = FeatureConfig(value=True, name="Machine learning (scikit-learn)", description="Let generated code train scikit-learn models (classification, regression, clustering, feature importance) on query results and uploaded files. Training runs in-process on the server; turn off to keep generated code to pandas/numpy analysis only.", is_lab=False, editable=True)
    ml_training_row_limit: FeatureConfig = FeatureConfig(value=50000, name="ML training row limit", description="Maximum number of rows generated code should train a model on. Larger sources are sampled down to this size before fitting. Minimum 1000.", is_lab=False, editable=True)
    enable_custom_queries: FeatureConfig = FeatureConfig(value=False, name="Custom tables (beta)", description="Let connection admins define a query (SQL, or DAX on Power BI) that BOW materializes to an encrypted local copy on a schedule, so agents answer from the cached result instead of querying the source on every turn. Beta: off by default.", is_lab=True, editable=True)
    enable_agent_notes: FeatureConfig = FeatureConfig(value=True, name="Agent Notes", description="Let the agent keep per-report working notes (a scratchpad) it writes and reads while answering — plans, findings, and todos. Notes are shown in the report but are not shared knowledge. When off, the note tools are hidden and notes are not injected into context.", is_lab=True, editable=True)
    # Agent check-ins (lab): after a chat turn, the agent may quietly plan ONE
    # follow-up in the same report; a judge decides at fire time whether to run
    # it, and the run notifies only on explicit criteria. See
    # app/services/checkin_service.py and docs/feedback-loops/agent-checkins.md.
    enable_agent_checkins: FeatureConfig = FeatureConfig(value=True, name="Agent check-ins", description="Let the agent follow up on its own after a conversation when something is worth revisiting (e.g. after a data refresh). Follow-ups run as the user in the same report and notify them only when something meaningful changed.", is_lab=True, editable=True)
    checkins_max_per_user_per_week: FeatureConfig = FeatureConfig(value=2, name="Check-ins per user per week", description="Maximum follow-up runs a single user can receive in 7 days.", is_lab=True, editable=True)
    checkins_max_runs_per_org_per_day: FeatureConfig = FeatureConfig(value=50, name="Check-in runs per day", description="Upper bound on follow-up runs across the organization per day (cost cap).", is_lab=True, editable=True)
    enable_user_memory: FeatureConfig = FeatureConfig(value=True, name="User memory", description="Let the agent remember facts about each user across sessions — their work, projects, deadlines and events, what they follow, their own shorthand. Rules for how to answer stay in instructions. Each user can see, edit and delete their memory. It is kept up to date overnight.", is_lab=False, editable=True)
    # Overnight learning (lab). enable_agent_dreaming is the ceiling for the
    # agent dream: when off, nothing runs whatever the per-agent
    # `nightly_learning` value is. The user dream has no switch of its own:
    # it only writes memory and check-ins, so it follows enable_user_memory,
    # enable_agent_checkins and each user's check-in opt-out. See
    # app/services/dreams/ and docs/design/overnight-learning.md.
    enable_agent_dreaming: FeatureConfig = FeatureConfig(value=True, name="Agent overnight learning", description="Each night, agents review their pending suggestions, feedback and usage and propose one consolidated improvement per agent. What happens to it follows each agent's Self-Learning setting.", is_lab=True, editable=True)
    ai_suggestion_expiry_days: FeatureConfig = FeatureConfig(value=30, name="Expire unreviewed AI suggestions after (days)", description="AI-suggested instructions still pending after this many days are closed as expired (not rejected) by the nightly agent learning. Expired suggestions still count as evidence if the same pattern comes back. 0 keeps them open forever.", is_lab=True, editable=True)
    max_instructions_in_context: FeatureConfig = FeatureConfig(value=50, name="Max instructions in context", description="Maximum number of instructions to include in AI context. 'Always' instructions are loaded first, then 'smart' instructions fill remaining slots.", is_lab=False, editable=True)
    smart_instructions_on_demand: FeatureConfig = FeatureConfig(value=False, name="Agent chooses smart instructions", description="The agent reads only the smart instructions it needs, instead of loading every match.", is_lab=False, editable=True)
    allow_report_webhooks: FeatureConfig = FeatureConfig(value=True, name="Report Webhooks", description="Allow external systems (GitHub, Jira, generic services) to send events to reports via inbound webhooks. Master switch for the whole feature.", is_lab=False, editable=True)
    max_webhooks: FeatureConfig = FeatureConfig(value=20, name="Max webhooks", description="Maximum number of active inbound webhooks per organization.", is_lab=False, editable=True)
    webhook_rate_limit_per_min: FeatureConfig = FeatureConfig(value=60, name="Webhook rate limit (per minute)", description="Maximum inbound webhook deliveries accepted per minute per organization. Excess deliveries are rejected with 429.", is_lab=False, editable=True)
    step_retention_days: FeatureConfig = FeatureConfig(value=14, name="Widget Data Retention Days", description="Number of days to retain widgets data before purging.", is_lab=False, editable=True)
    enable_excel_addin: FeatureConfig = FeatureConfig(value=True, name="Excel Add-in", description="Enable the built-in Excel Add-in so users can sideload the manifest directly from this instance", is_lab=False, editable=True)
    model_routing: FeatureConfig = FeatureConfig(value=False, name="Auto model router", description="Enterprise. When a user doesn't pick a specific model, start each request on the small model and let the agent escalate to a stronger one only when the task needs it. Add per-model routing guidance on the LLM page to steer the choice. Off by default; requires an enterprise license to enable.", is_lab=True, editable=True)
    llm_fallback: FeatureConfig = FeatureConfig(value=False, name="LLM fallback", description="Enterprise. When the active model fails with a rate limit, provider overload, or network error, automatically retry the request on the next model in the fallback order (configured on the LLM page) for the rest of the run. The substitution is always disclosed in the chat. Off by default; requires an enterprise license to enable.", is_lab=True, editable=True)
    # Ordered LLMModel db ids tried top-to-bottom on failure. Managed via
    # POST /llm/fallback_order (EE-gated); stored as a bare list, not a
    # FeatureConfig, mirroring the plain-int settings.
    llm_fallback_order: list = []

    ai_features: Dict[str, FeatureConfig] = {
        # Update defaults to use 'value' instead of 'enabled'
        "planner": FeatureConfig(value=True, name="Planner", description="Orchestrates analysis by breaking down user requests into actionable steps", is_lab=False, editable=False),
        "coder": FeatureConfig(value=True, name="Coder", description="Translates data models into executable Python code for data processing", is_lab=False, editable=False),
        "validator": FeatureConfig(value=True, name="Validator", description="Validates code safety and integrity and its data model compatibility", is_lab=False, editable=True),
        "dashboard_designer": FeatureConfig(value=True, name="Dashboard Designer", description="Creates layout and organization of dashboard elements", is_lab=False),
        "analyze_data": FeatureConfig(value=False, name="Analyze Data", description="Provides natural language responses to user questions about their data", is_lab=False, editable=False),
        "code_reviewer": FeatureConfig(value=False, name="Code Reviewer", description="Allow users to get feedback on their code", is_lab=False), # Changed enabled=True to value=False based on previous value
        "search_context": FeatureConfig(value=True, name="Search Context", description="Allow users to search through metadata, context, and data models", is_lab=False),
    }


class OrganizationSettingsBase(BaseModel):
    organization_id: str
    config: OrganizationSettingsConfig

class OrganizationSettingsCreate(OrganizationSettingsBase):
    pass

class OrganizationSettingsUpdate(BaseModel):
    config: Optional[Dict[str, Any]] = None

class OrganizationSettingsSchema(OrganizationSettingsBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SignupPolicySchema(BaseModel):
    """Read/write shape for the per-org signup policy."""
    enabled: bool = False
    allowed_domains: List[str] = []
    auto_invite_role: str = "member"


class OrgSmtpSchema(BaseModel):
    """Read shape for the org's SMTP server (the password is never returned)."""
    enabled: bool = False
    host: Optional[str] = None
    port: int = 587
    security: str = "starttls"  # "starttls" | "ssl" | "none"
    username: Optional[str] = None
    password_set: bool = False
    from_address: Optional[str] = None
    from_name: Optional[str] = None
    # Advanced TLS: when False, skip certificate verification (self-signed /
    # internal CA relays). Mirrors bow-config's global SMTP ``validate_certs``.
    validate_certs: bool = True
    # Which transport system mail is *actually* leaving through right now:
    # "org_smtp" (this server), "global" (bow-config), or "none". Surfaced so an
    # admin can see at a glance that their relay is or isn't in use, rather than
    # discovering months later that invites went out via bow-config.
    active_source: str = "none"
    # Whether a global bow-config SMTP exists to fall back to when this server
    # is disabled — i.e. whether turning it off means "use the global relay" or
    # "stop sending system mail".
    global_configured: bool = False


class OrgSmtpTestRequest(BaseModel):
    """Optional body for the SMTP test. ``to`` defaults to the caller's address.

    Only the caller's own address is accepted — this endpoint sends real mail
    through the organization's relay, so an arbitrary recipient would turn it
    into a spam relay for anyone holding ``manage_settings``.
    """
    to: Optional[str] = None


class OrgSmtpTestResult(BaseModel):
    """Outcome of a real test send, including which transport carried it."""
    success: bool
    # "org_smtp" | "global" | "none" — the transport the message actually used.
    source: Optional[str] = None
    # Which step of the SMTP conversation failed, so the page can name the fix.
    stage: Optional[str] = None
    recipient: Optional[str] = None
    from_address: Optional[str] = None
    error: Optional[str] = None
    # Flat string kept for older clients.
    smtp: Optional[str] = None


class OrgSmtpUpdate(BaseModel):
    """Write shape; ``password`` is only sent when (re)setting it.

    Username/password are optional — relays that accept unauthenticated mail
    from trusted hosts (mirroring bow-config's ``use_credentials=False``) just
    leave them blank and BOW skips SMTP AUTH.
    """
    enabled: bool = False
    host: Optional[str] = None
    port: int = 587
    security: str = "starttls"
    username: Optional[str] = None
    password: Optional[str] = None
    from_address: Optional[str] = None
    from_name: Optional[str] = None
    validate_certs: bool = True

    @validator("host", "username", "from_address", "from_name")
    def _no_header_breaks(cls, v):
        # These end up in SMTP commands / message headers; a CR or LF there is
        # header injection (and crashed the test send with a traceback).
        if v is not None and ("\r" in v or "\n" in v):
            raise ValueError("must not contain line breaks")
        return v
