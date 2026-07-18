"""Session events — the silent ledger.

A *session event* is a ``role='event'`` :class:`Completion` row: something
happened in the report (a UI action, a state change) that the agent did **not**
run for, but should see on its next natural turn. It is the passive sibling of
``role='external'``:

  - ``role='external'`` — something happened AND the agent runs now (eval run
    finished, webhook arrived). Needs the full turn machinery: a hidden trigger
    prompt and a following ``role='system'`` reply.
  - ``role='event'``    — something happened and nobody runs. It just sits in
    the log and is read the next time the agent naturally runs, interleaved
    chronologically between the turns it falls between.

Three per-kind policy maps govern each event kind (keyed by ``message_type``):

  - :data:`EVENT_LLM_HIDDEN`  — kinds NOT rendered into the agent's message
    context (pure UI/audit noise for the model). Everything else is LLM-visible.
  - :data:`EVENT_UI_VISIBLE`  — kinds rendered as a strip in the report
    timeline. Everything else is hidden in the UI (default off).
  - :data:`EVENT_DURABLE`     — kinds that survive compaction: folded into the
    rolling summary (aggregated). Ephemeral kinds simply fall behind the
    watermark and vanish, because the state they describe already lives in
    another context section (current model, ``<files>`` index, schema scope…).

Policy lives in code, not columns, so flipping a kind's visibility is a
one-line edit rather than a data migration + backfill across existing rows.
"""

# The Completion.role value that marks a silent session event.
EVENT_ROLE = "event"

# ---------------------------------------------------------------------------
# Event kinds (stored in Completion.message_type)
# ---------------------------------------------------------------------------
# session / run
RUN_STOPPED = "run_stopped"
LLM_CHANGED = "llm_changed"
QUEUE_PROMPT_REMOVED = "queue_prompt_removed"
# feedback
FEEDBACK_GIVEN = "feedback_given"
FEEDBACK_CHANGED = "feedback_changed"
FEEDBACK_REMOVED = "feedback_removed"
# files
FILE_UPLOADED = "file_uploaded"
FILE_REMOVED = "file_removed"
# agent scope
AGENT_SCOPE_CHANGED = "agent_scope_changed"
MCP_POLICY_SAVED = "mcp_policy_saved"
# report / conversation
REPORT_SHARED = "report_shared"
REPORT_PUBLISHED = "report_published"
REPORT_UNPUBLISHED = "report_unpublished"
REPORT_RENAMED = "report_renamed"
REPORT_FORKED = "report_forked"
# artifacts / docs
ARTIFACT_SHARED = "artifact_shared"
ARTIFACT_UNSHARED = "artifact_unshared"
ARTIFACT_USER_EDITED = "artifact_user_edited"
ARTIFACT_SCHEDULE_SET = "artifact_schedule_set"
ARTIFACT_SCHEDULE_CHANGED = "artifact_schedule_changed"
ARTIFACT_SCHEDULE_REMOVED = "artifact_schedule_removed"
ARTIFACT_DATA_REFRESHED = "artifact_data_refreshed"
# knowledge / instructions
INSTRUCTION_ACCEPTED = "instruction_accepted"
INSTRUCTION_REJECTED = "instruction_rejected"
INSTRUCTION_CREATED = "instruction_created"
INSTRUCTION_EDITED = "instruction_edited"
INSTRUCTION_DELETED = "instruction_deleted"
BUILD_PUBLISHED = "build_published"
BUILD_REJECTED = "build_rejected"
# analytics-only
EXPORT_DOWNLOADED = "export_downloaded"


# ---------------------------------------------------------------------------
# Policy maps
# ---------------------------------------------------------------------------
# Kinds that render as a strip in the report timeline. Default: hidden.
EVENT_UI_VISIBLE = {
    RUN_STOPPED,
    FILE_UPLOADED,
    FILE_REMOVED,
    AGENT_SCOPE_CHANGED,
    REPORT_SHARED,
    REPORT_PUBLISHED,
    REPORT_UNPUBLISHED,
    ARTIFACT_SHARED,
    ARTIFACT_UNSHARED,
    ARTIFACT_SCHEDULE_SET,
    ARTIFACT_SCHEDULE_CHANGED,
    ARTIFACT_SCHEDULE_REMOVED,
}

# Kinds NOT rendered into the agent's message context. Default: LLM-visible, so
# only pure-audit noise is listed here.
EVENT_LLM_HIDDEN = {
    EXPORT_DOWNLOADED,
}

# Kinds that survive compaction (folded into the rolling summary, aggregated).
# Default: ephemeral. Only kinds with NO other home in context earn durability:
# feedback and instruction/build rejections are the sole channel that carries
# their signal, so they must not vanish when the window slides.
EVENT_DURABLE = {
    FEEDBACK_GIVEN,
    FEEDBACK_CHANGED,
    FEEDBACK_REMOVED,
    INSTRUCTION_REJECTED,
    BUILD_REJECTED,
}


def is_event_kind_llm_visible(kind: str) -> bool:
    return kind not in EVENT_LLM_HIDDEN


def is_event_kind_ui_visible(kind: str) -> bool:
    return kind in EVENT_UI_VISIBLE


def is_event_kind_durable(kind: str) -> bool:
    return kind in EVENT_DURABLE


def default_event_content(kind: str, meta: dict | None = None, actor: str = "user") -> str:
    """Human/LLM one-line fallback for an event kind.

    ``actor`` is the display name of the person who took the action (falls back
    to the generic "user") — in a shared report "Dana thumbed down…" is far less
    ambiguous than "user thumbed down…". Callers normally pass an explicit
    ``content`` string to ``SessionEventService.emit`` (it reads better and can
    be localized); this is the backstop used when they don't, and it keeps the
    ledger legible for kinds added before a bespoke string exists.
    """
    m = meta or {}
    who = actor or "user"

    if kind == RUN_STOPPED:
        return f"{who} stopped the run"
    if kind == LLM_CHANGED:
        frm, to = m.get("from"), m.get("to")
        if to and frm:
            return f"{who} switched model from {frm} to {to}"
        if to:
            return f"{who} switched model to {to}"
        return f"{who} switched the model"
    if kind == QUEUE_PROMPT_REMOVED:
        txt = str(m.get("text") or "").strip()
        return f'{who} removed a queued prompt ("{txt[:80]}")' if txt else f"{who} removed a queued prompt"

    if kind in (FEEDBACK_GIVEN, FEEDBACK_CHANGED):
        direction = m.get("direction")
        thumb = "👍" if direction == 1 else ("👎" if direction == -1 else "feedback")
        msg = str(m.get("message") or "").strip()
        verb = "changed feedback to" if kind == FEEDBACK_CHANGED else "gave"
        base = f"{who} {verb} {thumb} on the assistant's answer"
        return f'{base} ("{msg[:120]}")' if msg else base
    if kind == FEEDBACK_REMOVED:
        return f"{who} retracted their feedback on the assistant's answer"

    if kind == FILE_UPLOADED:
        name = m.get("filename") or m.get("file_id") or "a file"
        return f"{who} uploaded {name}"
    if kind == FILE_REMOVED:
        name = m.get("filename") or m.get("file_id") or "a file"
        return f"{who} removed {name}"

    if kind == AGENT_SCOPE_CHANGED:
        added = m.get("added") or []
        removed = m.get("removed") or []
        parts = []
        if added:
            parts.append("added " + ", ".join(str(x) for x in added[:5]))
        if removed:
            parts.append("removed " + ", ".join(str(x) for x in removed[:5]))
        detail = "; ".join(parts) if parts else "changed the agent's scope"
        return f"{who} changed the agent's scope — {detail}" if parts else f"{who} changed the agent's scope"
    if kind == MCP_POLICY_SAVED:
        tool = m.get("tool") or "a tool"
        decision = m.get("decision") or "a policy"
        return f'{who} saved "always {decision}" for {tool}'

    if kind == REPORT_SHARED:
        shared = m.get("shared_with") or []
        who_s = ", ".join(str(x) for x in shared[:5]) if isinstance(shared, list) else str(shared)
        return f"{who} shared this conversation with {who_s}" if who_s else f"{who} shared this conversation"
    if kind == REPORT_PUBLISHED:
        return f"{who} published this report"
    if kind == REPORT_UNPUBLISHED:
        return f"{who} unpublished this report"
    if kind == REPORT_RENAMED:
        return f'{who} renamed the report to "{m.get("to")}"' if m.get("to") else f"{who} renamed the report"
    if kind == REPORT_FORKED:
        return f"{who} forked this conversation"

    if kind == ARTIFACT_SHARED:
        title = m.get("title") or "an artifact"
        shared = m.get("shared_with") or []
        who_s = ", ".join(str(x) for x in shared[:5]) if isinstance(shared, list) else str(shared)
        return f'{who} shared "{title}"' + (f" with {who_s}" if who_s else "")
    if kind == ARTIFACT_UNSHARED:
        return f'{who} made "{m.get("title") or "an artifact"}" private'
    if kind == ARTIFACT_USER_EDITED:
        return f'{who} manually edited "{m.get("title") or "an artifact"}"'
    if kind == ARTIFACT_SCHEDULE_SET:
        return f'{who} scheduled a refresh for "{m.get("title") or "an artifact"}"'
    if kind == ARTIFACT_SCHEDULE_CHANGED:
        return f'{who} changed the refresh schedule for "{m.get("title") or "an artifact"}"'
    if kind == ARTIFACT_SCHEDULE_REMOVED:
        return f'{who} removed the refresh schedule for "{m.get("title") or "an artifact"}"'
    if kind == ARTIFACT_DATA_REFRESHED:
        return f'{who} refreshed the data for "{m.get("title") or "an artifact"}"'

    if kind == INSTRUCTION_ACCEPTED:
        title = m.get("title")
        return f'{who} accepted the suggested instruction "{title}"' if title else f"{who} accepted a suggested instruction"
    if kind == INSTRUCTION_REJECTED:
        title = m.get("title") or ""
        snippet = str(m.get("snippet") or "").strip()
        base = f'{who} rejected the suggested instruction "{title}" — do not re-suggest it'
        return f"{base} ({snippet[:120]})" if snippet else base
    if kind == INSTRUCTION_CREATED:
        return f'{who} created instruction "{m.get("title") or ""}"'
    if kind == INSTRUCTION_EDITED:
        return f'{who} edited instruction "{m.get("title") or ""}"'
    if kind == INSTRUCTION_DELETED:
        return f'{who} deleted instruction "{m.get("title") or ""}"'
    if kind == BUILD_PUBLISHED:
        n = m.get("count")
        return f"{who} published a knowledge build ({n} instructions)" if n else f"{who} published a knowledge build"
    if kind == BUILD_REJECTED:
        return f"{who} rejected a knowledge build"

    if kind == EXPORT_DOWNLOADED:
        return f"{who} exported the report as {m.get('format') or 'a file'}"

    # Unknown kind — degrade gracefully to the raw kind name.
    return kind.replace("_", " ")
