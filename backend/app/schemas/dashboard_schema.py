"""Schemas of the dashboard routes (/api/d and /api/artifacts, phase 4)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.notification_schema import NotificationSubscriber, NotificationType, NotifyRequest

Visibility = Literal["none", "shared", "internal", "public"]


class PublicGeneralSettings(BaseModel):
    ai_analyst_name: str = "AI Analyst"
    bow_credit: bool = True
    icon_url: str | None = None


class DashboardForkEligibility(BaseModel):
    can_fork: bool
    reason: str | None = None


class DashboardConnectionKind(BaseModel):
    type: str | None = None
    auth_policy: str | None = None


class DashboardSourceKind(BaseModel):
    connections: list[DashboardConnectionKind] = []


class PublishedDashboardSchema(BaseModel):
    """What a /d page needs about the dashboard: nothing of its settings
    beyond what it shows (who it is shared with, schedules and chat sources
    are for the owner only)."""
    id: str
    slug: str | None = None
    title: str | None = None
    mode: str
    release_id: str
    published_at: datetime | None = None
    last_run_at: datetime | None = None
    theme_name: str | None = None
    theme_overrides: dict | None = None
    include_data_tab: bool
    chat_enabled: bool
    general: PublicGeneralSettings | None = None
    # What /r's page reads besides the dashboard (phase 8): the reader's
    # standing, Fork, refresh-on-view, and the kinds of connection it runs
    # on (the sign-in gate's icon) -- never their names or ids.
    is_owner: bool = False
    # Whether the release's stored rows are this reader's own run (its
    # publisher, else whoever the last refresh ran as). Owning the dashboard
    # no longer says so: another editor may have published over a personal
    # connection, and the owner then runs it for themselves like any reader.
    data_is_mine: bool = False
    fork_eligibility: DashboardForkEligibility
    refresh_on_view: bool
    data_sources: list[DashboardSourceKind] = []
    # This reader's exports cannot hold the shared snapshot: a page or doc
    # holds their own run's rows (and needs one), a deck is refused.
    exports_own_rows: bool = False
    # What this reader may do with the dashboard (collab01):
    # dashboard_access.capabilities -- the page shows only those buttons.
    capabilities: list[str] = []


class DraftDashboardSchema(BaseModel):
    id: str
    slug: str | None = None
    title: str | None = None
    mode: str
    report_id: str | None = None
    version_id: str | None = None
    published: bool
    theme_name: str | None = None
    theme_overrides: dict | None = None
    general: PublicGeneralSettings | None = None  # the page's credit and analyst name (phase 8)
    # An editor (owner, project) -- not the organization's admin, who reads it.
    can_edit: bool = False
    # The owner, whose rows are the shared ones; anyone else runs it for
    # their own (POST /api/r/{report}/run, as on main's /r).
    is_owner: bool = False
    # collab01: what this reader may do, the version shown (its number, its
    # conversation's author), the reader's own editing conversation if any,
    # and the dashboard's drafts.
    capabilities: list[str] = []
    version_number: int | None = None
    version_author: "DashboardPerson | None" = None
    version_is_mine: bool = False
    my_report_id: str | None = None
    draft: "DashboardDraftSchema | None" = None
    drafts: list["DashboardDraftSchema"] = []


class DashboardSharingSchema(BaseModel):
    # str, not Visibility: reports the stored value even if it is not one
    # the API writes, rather than failing the owner's read.
    visibility: str
    published: bool
    user_ids: list[str]
    group_ids: list[str]
    # Editors (collab01): granted 'edit', whatever the visibility.
    editor_user_ids: list[str] = []
    editor_group_ids: list[str] = []
    owner_id: str | None = None
    project_id: str | None = None


class DashboardSharingUpdate(BaseModel):
    """None leaves that kind of grant as it is. View grants count only while
    the visibility is 'shared' (leaving it clears them); edit grants stand
    whatever the visibility, and before the first publish too. Someone in
    both lists is an editor."""
    visibility: Visibility
    user_ids: list[str] | None = None
    group_ids: list[str] | None = None
    editor_user_ids: list[str] | None = None
    editor_group_ids: list[str] | None = None


class DashboardSettingsSchema(BaseModel):
    shared_run_identity: Literal["viewer", "creator"]
    include_data_tab: bool
    chat_enabled: bool
    chat_data_source_ids: list[str] | None = None
    chat_model_id: str | None = None
    cron_schedule: str | None = None
    refresh_on_view: bool = True
    notification_subscribers: list[dict] | None = None
    slug: str | None = None  # the name in its link (phase 8)
    notes: str | None = None  # the dashboard's README (collab01)


class DashboardSettingsUpdate(BaseModel):
    """None leaves a setting as it is. chat_data_source_ids ["*"] and
    chat_model_id "" go back to inheriting from the conversation."""
    shared_run_identity: Literal["viewer", "creator"] | None = None
    include_data_tab: bool | None = None
    chat_enabled: bool | None = None
    chat_data_source_ids: list[str] | None = None
    chat_model_id: str | None = None
    # The schedule (phase 6). cron_schedule "" unschedules (and clears the
    # subscribers); refresh_on_view is separate and stays.
    cron_schedule: str | None = None
    refresh_on_view: bool | None = None
    notification_subscribers: list[NotificationSubscriber] | None = None
    # The name in its link (phase 8); "" removes it.
    slug: str | None = Field(default=None, max_length=255)
    # The dashboard's notes (collab01); "" clears them.
    notes: str | None = Field(default=None, max_length=20000)


class DashboardReleaseSchema(BaseModel):
    id: str
    status: Literal["publishing", "ready", "failed"]
    error: str | None = None
    source_version_id: str | None = None
    # The version's number and name, as the toolbar says it ("Live: v5").
    source_version_number: int | None = None
    source_version_name: str | None = None
    published_by: str | None = None
    started_at: datetime | None = None
    published_at: datetime | None = None
    # Copies left unrun at Publish, waiting for a viewer's input (a context
    # app), and their labels: the owner is told which charts start empty.
    steps_needing_input: int = 0
    steps_needing_input_labels: list[str] = []


class DashboardPublishStatusSchema(BaseModel):
    """The live release, the latest attempt (the live one, or a publish
    running or failed since), and whether the draft's newest completed
    version is not the one that is live."""
    published: DashboardReleaseSchema | None = None
    latest: DashboardReleaseSchema | None = None
    has_unpublished_changes: bool
    # The newest completed version (what Publish would make live).
    newest_version_id: str | None = None
    newest_version_number: int | None = None
    last_run: "DashboardRunSchema | None" = None


class DashboardRunSchema(BaseModel):
    """How the last refresh of the live release went (phase 6): `at` is the
    last success; status and error describe the last attempt."""
    at: datetime | None = None
    status: Literal["success", "failed"] | None = None
    error: str | None = None


class DashboardForkRequest(BaseModel):
    title: str | None = None


class DashboardPublishRequest(BaseModel):
    """None publishes the newest completed version. `force`: publish even
    though the live release is not part of this version's history (someone
    published since this draft started; collab01)."""
    version_id: str | None = None
    force: bool = False


DashboardPublishStatusSchema.model_rebuild()


class DashboardNotifyRequest(NotifyRequest):
    """Telling people a dashboard was shared with them (phase 8). Always a
    dashboard share; the link is the server's (share_url is ignored)."""
    type: NotificationType = NotificationType.SHARE_DASHBOARD


class DashboardListOwner(BaseModel):
    id: str
    name: str | None = None


class DashboardListItem(BaseModel):
    """One card of the dashboards page (phase 8). report_id only for its
    editors (the card's pencil)."""
    id: str
    slug: str | None = None
    title: str | None = None
    mode: str
    published: bool
    is_owner: bool
    # 'owner' | 'edit' | 'view' (collab01)
    role: str = "view"
    report_id: str | None = None
    owner: DashboardListOwner | None = None
    thumbnail_url: str | None = None
    updated_at: datetime | None = None


class DashboardListMeta(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool
    has_prev: bool


class DashboardListResponse(BaseModel):
    dashboards: list[DashboardListItem]
    meta: DashboardListMeta


# ── collaboration (collab01) ─────────────────────────────────────────────

class DashboardPerson(BaseModel):
    id: str
    name: str | None = None


class DashboardDraftSchema(BaseModel):
    id: str | None = None  # None: the dashboard's implicit draft
    name: str
    slug: str | None = None
    status: str = "open"
    head_version_id: str | None = None
    head_version_number: int | None = None
    updated_at: datetime | None = None
    created_by: DashboardPerson | None = None


class DashboardDraftCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # The version to start from; None: the newest of the implicit draft.
    from_version_id: str | None = None


class DashboardJoinRequest(BaseModel):
    """Open the dashboard in a conversation of the caller's own: theirs if
    they have one, else a new one copied from `draft` (a draft's slug; None:
    the implicit one) or, with nothing to copy, the live release."""
    draft: str | None = None


class DashboardJoinResponse(BaseModel):
    report_id: str
    created: bool
    # Every chart is running again with the caller's credentials.
    rerunning: bool = False


class DashboardBringRequest(BaseModel):
    """Bring the dashboard into a conversation of the caller's: a copy of
    `version_id` (any version of it, from any conversation), else of the
    live version, else of the newest."""
    report_id: str
    version_id: str | None = None


class DashboardBringResponse(BaseModel):
    report_id: str
    version_id: str | None = None
    version: int | None = None
    rerunning: bool = False
    # Nothing was copied: the conversation already builds on what was asked.
    unchanged: bool = False


class DashboardOpenStateVersion(BaseModel):
    version_id: str | None = None
    version: int | None = None
    by: DashboardPerson | None = None
    at: datetime | None = None


class DashboardOpenStateSchema(BaseModel):
    live: DashboardOpenStateVersion | None = None
    newest_here: DashboardOpenStateVersion | None = None
    behind_live: bool = False
    unpublished_here: bool = False


class DashboardConversationSchema(BaseModel):
    report_id: str
    user: DashboardPerson | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    versions: int = 0
    draft: str | None = None
    # Only for people who can open that conversation themselves.
    can_open: bool = False


class DashboardHistoryRelease(BaseModel):
    id: str
    status: str
    live: bool
    version_id: str | None = None
    version_number: int | None = None
    published_by: DashboardPerson | None = None
    published_at: datetime | None = None
    error: str | None = None


class DashboardHistoryVersion(BaseModel):
    """One row of the Versions list: who made it, its name, whether it is
    live, when it was last published and how many times (each publish runs
    the queries again), and the kept release Restore goes back to."""
    id: str
    number: int
    name: str | None = None
    author: DashboardPerson | None = None
    created_at: datetime | None = None
    report_id: str
    draft: str | None = None
    published: bool = False
    live: bool = False
    published_at: datetime | None = None
    publish_count: int = 0
    restore_release_id: str | None = None


class DashboardVersionNameUpdate(BaseModel):
    """"" removes the name."""
    name: str = Field(max_length=120)


class DashboardHistorySchema(BaseModel):
    conversations: list[DashboardConversationSchema]
    releases: list[DashboardHistoryRelease]
    versions: list[DashboardHistoryVersion]
    drafts: list[DashboardDraftSchema]


class DashboardRefreshRunSchema(BaseModel):
    """One refresh of the dashboard (refhist01). trigger: schedule | view |
    manual. status: running | success | partial | failed | skipped |
    interrupted (still running long after it started)."""
    id: str
    trigger: str
    status: str
    skip_reason: str | None = None
    # published (the release readers see) | draft (a conversation's) | None
    kind: str | None = None
    # None when the run did not count its queries (skipped, a published run
    # that failed).
    steps_total: int | None = None
    steps_succeeded: int | None = None
    steps_failed: int | None = None
    # The query or connection the first failure was in, and the reason.
    error_source: str | None = None
    error: str | None = None
    run_as: DashboardPerson | None = None
    triggered_by: DashboardPerson | None = None
    started_at: datetime
    finished_at: datetime | None = None


class DashboardRefreshRunsSchema(BaseModel):
    runs: list[DashboardRefreshRunSchema]
    has_more: bool = False
    # Pass as `before` for the next page.
    next_before: str | None = None


class DashboardRollbackRequest(BaseModel):
    release_id: str


class DashboardTransferRequest(BaseModel):
    user_id: str


class DashboardElementChange(BaseModel):
    key: str
    title: str | None = None
    changed_by: DashboardPerson | None = None
    changed_at: datetime | None = None
    version_number: int | None = None
    # vs the live release: added | changed | unchanged; removed ones are listed apart
    vs_live: str = "unchanged"


class DashboardChangesSchema(BaseModel):
    version_id: str | None = None
    elements: list[DashboardElementChange]
    removed_since_live: list[DashboardElementChange] = []
    live_version_id: str | None = None
    # The live release is not part of this version's history: someone
    # published since it started.
    published_since: "DashboardPublishedSince | None" = None


class DashboardPublishedSince(BaseModel):
    by: DashboardPerson | None = None
    at: datetime | None = None
    release_id: str | None = None
    live_version_id: str | None = None
    live_version: int | None = None
    # A Restore made the live release live again; this version still builds
    # on the later one it rolled back.
    restored: bool = False
    rolled_back_version: int | None = None


class DashboardPresenceUpdate(BaseModel):
    report_id: str | None = None


class DashboardPresenceEntry(BaseModel):
    user: DashboardPerson
    report_id: str | None = None
    state: str
    element_keys: list[str] = []
    seen_at: datetime | None = None
    is_me: bool = False


class DashboardPresenceSchema(BaseModel):
    entries: list[DashboardPresenceEntry]


class DashboardConversationContext(BaseModel):
    """What a conversation editing a dashboard shows on top: the card
    "Continued from ...", and the summary the agent got."""
    artifact_id: str
    title: str | None = None
    slug: str | None = None
    # page / doc / slides: the card says dashboard, document or presentation.
    mode: str | None = None
    continued_from: DashboardPerson | None = None
    continued_from_report_id: str | None = None
    can_open_previous: bool = False
    started_at: datetime | None = None
    base_version_number: int | None = None
    summary: dict | None = None
    rerunning: bool = False
    # A separate (named) draft this conversation works in; None for the main one.
    draft: dict | None = None


DashboardChangesSchema.model_rebuild()
DraftDashboardSchema.model_rebuild()
