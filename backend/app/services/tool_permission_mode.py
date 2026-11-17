"""Run-level permission mode for connection tools and remote shell.

A report carries a permission mode the user picks in the prompt box:

- ``ask`` — today's behaviour: the per-tool settings apply unchanged.
- ``auto`` — fixed rules first (clearly read-only → allow), then a judge
  (the org's decision model, else the small default model) decides allow /
  ask the user / refuse. No model → behaves like ``ask``, never allow.
- ``allow_all`` — tools left on ``ask`` / ``auto`` run without asking. Needs
  the org switch, applies only to runs a person is watching.

The mode is a layer applied on top of ``ToolPolicyResolution.effective``,
after the disabled check, the built-in blocklist (``tool_blocklist``) and any
``deny`` (admin or the user's own): it can only loosen ``ask`` / ``auto``.

Org config lives in ``OrganizationSettings.config['tool_permission_mode']``;
an agent may cap the mode with ``DataSource.max_permission_mode``.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Optional

MODE_ASK = "ask"
MODE_AUTO = "auto"
MODE_ALLOW_ALL = "allow_all"
MODES = (MODE_ASK, MODE_AUTO, MODE_ALLOW_ALL)
# Agents may cap at these (a cap of allow_all would be no cap).
AGENT_CAPS = (MODE_ASK, MODE_AUTO)

_RANK = {MODE_ASK: 0, MODE_AUTO: 1, MODE_ALLOW_ALL: 2}

UNATTENDED_REFUSE = "refuse"
UNATTENDED_SETTINGS = "settings"
UNATTENDED_ALLOW = "allow"
UNATTENDED_OPTIONS = (UNATTENDED_REFUSE, UNATTENDED_SETTINGS, UNATTENDED_ALLOW)

# Judge outcomes
VERDICT_ALLOW = "allow"
VERDICT_ASK = "ask"
VERDICT_REFUSE = "refuse"

# Who decided (audit ``decided_by``)
BY_POLICY = "policy"
BY_READ_ONLY = "rule:read_only"
BY_BLOCKLIST = "rule:blocklist"
BY_DECISION_MODEL = "judge:decision_model"
BY_SMALL_MODEL = "judge:small_model"
BY_NO_JUDGE = "judge:unavailable"
BY_USER = "user"
BY_REMEMBERED_REPORT = "remembered:report"
BY_ALLOW_ALL = "mode:allow_all"
BY_UNATTENDED = "unattended_setting"

SETTING_KEY = "tool_permission_mode"
# Below this, Auto would allow calls the judge thinks are more likely unsafe
# than safe — that is Allow all without its switch.
MIN_ALLOW_THRESHOLD = 0.5
DEFAULTS = {
    "default_mode": MODE_ASK,
    "allow_all_enabled": False,
    "unattended": UNATTENDED_REFUSE,
    "judge_decision_model_id": None,
    "allow_threshold": 0.85,
    "refuse_threshold": 0.3,
}


def normalize_mode(value: Any) -> Optional[str]:
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    v = v.replace("-", "_").replace(" ", "_")
    if v == "allowall":
        v = MODE_ALLOW_ALL
    return v if v in MODES else None


@dataclass(frozen=True)
class PermissionSettings:
    default_mode: str = MODE_ASK
    allow_all_enabled: bool = False
    unattended: str = UNATTENDED_REFUSE
    judge_decision_model_id: Optional[str] = None
    allow_threshold: float = 0.85
    refuse_threshold: float = 0.3


def _clamp01(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return min(max(f, 0.0), 1.0)


def _raw_value(org_settings: Any) -> dict:
    raw = None
    try:
        cfg = org_settings.get_config(SETTING_KEY) if org_settings is not None else None
        raw = getattr(cfg, "value", cfg)
        if isinstance(raw, dict) and "value" in raw and isinstance(raw.get("value"), dict):
            raw = raw["value"]
    except Exception:
        raw = None
    return raw if isinstance(raw, dict) else {}


def normalize_settings(raw: Any) -> dict:
    """Clean a stored or incoming setting dict; unknown keys dropped."""
    raw = raw if isinstance(raw, dict) else {}
    allow_all_enabled = raw.get("allow_all_enabled") is True
    mode = normalize_mode(raw.get("default_mode")) or DEFAULTS["default_mode"]
    if mode == MODE_ALLOW_ALL:
        # Allow all is a per-report choice that resets on each new report;
        # it is never the default.
        mode = MODE_ASK
    unattended = raw.get("unattended") if raw.get("unattended") in UNATTENDED_OPTIONS else DEFAULTS["unattended"]
    allow_t = _clamp01(raw.get("allow_threshold", DEFAULTS["allow_threshold"]), DEFAULTS["allow_threshold"])
    refuse_t = _clamp01(raw.get("refuse_threshold", DEFAULTS["refuse_threshold"]), DEFAULTS["refuse_threshold"])
    if refuse_t > allow_t or allow_t < MIN_ALLOW_THRESHOLD:
        refuse_t, allow_t = DEFAULTS["refuse_threshold"], DEFAULTS["allow_threshold"]
    model_id = raw.get("judge_decision_model_id")
    return {
        "default_mode": mode,
        "allow_all_enabled": allow_all_enabled,
        "unattended": unattended,
        "judge_decision_model_id": str(model_id) if isinstance(model_id, str) and model_id else None,
        "allow_threshold": allow_t,
        "refuse_threshold": refuse_t,
    }


def permission_settings(org_settings: Any) -> PermissionSettings:
    return PermissionSettings(**normalize_settings(_raw_value(org_settings)))


def effective_mode(
    report_mode: Any,
    settings: PermissionSettings,
    *,
    agent_caps: Iterable[Any] = (),
    allow_all_permitted: bool = False,
) -> str:
    """The mode a run's tool calls use.

    Report choice (else the org default), then capped: Allow all needs the org
    switch and ``allow_all_permitted`` (a watched run by the report's owner),
    else it acts as Ask; every run agent's cap applies (strictest wins).
    """
    mode = normalize_mode(report_mode) or settings.default_mode
    if mode == MODE_ALLOW_ALL and (not settings.allow_all_enabled or not allow_all_permitted):
        mode = MODE_ASK
    for cap in agent_caps or ():
        c = normalize_mode(cap)
        if c is not None and _RANK[c] < _RANK[mode]:
            mode = c
    return mode


def verdict_from_probability(p_safe: Optional[float], settings: PermissionSettings) -> str:
    """Three-way decision from a decision model's P(safe to run)."""
    if p_safe is None:
        return VERDICT_ASK
    p = _clamp01(p_safe, -1.0)
    if p < 0:
        return VERDICT_ASK
    if p >= settings.allow_threshold:
        return VERDICT_ALLOW
    if p <= settings.refuse_threshold:
        return VERDICT_REFUSE
    return VERDICT_ASK


def verdict_from_small_model(approve: bool, confidence: float, settings: PermissionSettings) -> str:
    """Three-way decision from the small-model judge's yes/no + confidence.

    Only a confident answer either way is acted on; anything in between (or an
    unparseable / failed judge, which reports confidence 0) asks the user.
    """
    c = _clamp01(confidence, 0.0)
    if c < settings.allow_threshold:
        return VERDICT_ASK
    return VERDICT_ALLOW if approve else VERDICT_REFUSE


# ── fixed "clearly read-only" rule for remote shell ─────────────────────────

# Commands that only report metadata. Commands that print file contents
# (cat, grep, tail, Get-Content…) are left to the judge: they can't change
# anything, but they can read a key or a .env file into the conversation.
# Commands that can also set something (date, hostname, ipconfig) are left out.
# (ps is left out: `ps e` / `ps eww` print other processes' environment.)
_READ_ONLY_POSIX = {
    "ls", "pwd", "whoami", "id", "uname", "uptime", "df", "du", "free",
    "wc", "stat", "file", "which", "nproc", "lscpu", "lsblk", "groups",
}
_READ_ONLY_WINDOWS = {
    "dir", "whoami", "get-childitem", "get-process", "get-location",
    "get-service", "get-item", "get-volume", "get-computerinfo",
}
# Any shell syntax beyond "program args" (separators, redirects, expansion,
# subshells, globs that could expand to options) disqualifies the shortcut.
_SHELL_META = set(";&|<>`$(){}\n\r*?[]!\\")


def is_read_only_shell_command(command: Any, *, windows: bool = False) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    meta = _SHELL_META - {"\\"} if windows else _SHELL_META  # Windows paths use backslash
    if any(c in meta for c in command):
        return False
    words = command.split()
    prog = words[0].lower() if windows else words[0]
    allowed = _READ_ONLY_WINDOWS if windows else _READ_ONLY_POSIX
    if prog not in allowed:
        return False
    # PowerShell's Env: drive lists environment variables (secrets).
    if windows and "env:" in command.lower():
        return False
    return True


async def validate_settings_update(db, organization, stored: Any, incoming: Any) -> dict:
    """Merge an admin update over the stored setting and normalize it.

    Raises HTTPException(400) for a bad value instead of silently dropping it,
    so the settings page shows why a save failed.
    """
    from fastapi import HTTPException

    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="tool_permission_mode must be an object.")
    unknown = set(incoming) - set(DEFAULTS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown tool_permission_mode keys: {', '.join(sorted(unknown))}.")
    merged = {**DEFAULTS, **(stored if isinstance(stored, dict) else {}), **incoming}
    if "default_mode" in incoming and normalize_mode(incoming["default_mode"]) is None:
        raise HTTPException(status_code=400, detail="default_mode must be auto, ask or allow_all.")
    if "unattended" in incoming and incoming["unattended"] not in UNATTENDED_OPTIONS:
        raise HTTPException(status_code=400, detail="unattended must be refuse, settings or allow.")
    if "allow_all_enabled" in incoming and not isinstance(incoming["allow_all_enabled"], bool):
        raise HTTPException(status_code=400, detail="allow_all_enabled must be true or false.")
    if normalize_mode(incoming.get("default_mode")) == MODE_ALLOW_ALL:
        raise HTTPException(status_code=400, detail="Allow all can't be the default mode; it is picked per report.")
    for k in ("allow_threshold", "refuse_threshold"):
        if k in incoming:
            v = incoming[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= float(v) <= 1:
                raise HTTPException(status_code=400, detail=f"{k} must be a number between 0 and 1.")
    if float(merged["allow_threshold"]) < MIN_ALLOW_THRESHOLD:
        raise HTTPException(status_code=400, detail=f"allow_threshold must be at least {MIN_ALLOW_THRESHOLD}.")
    if float(merged["refuse_threshold"]) > float(merged["allow_threshold"]):
        raise HTTPException(status_code=400, detail="refuse_threshold must not be above allow_threshold.")
    # Only a newly chosen model is checked, so a model disabled later can't
    # stop an admin from changing anything else (the judge then falls back).
    model_id = incoming.get("judge_decision_model_id")
    if model_id:
        from app.ai.model_router import _load_decision_model
        if await _load_decision_model(db, organization, model_id) is None:
            raise HTTPException(status_code=400, detail="Choose an enabled decision model for the Auto judge.")
    return normalize_settings(merged)
