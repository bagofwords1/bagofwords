"""Agent self-learning automation policy.

Each agent (a ``DataSource``) carries a single **mode** that the agent-page
"Self Learning" modal exposes as one dropdown:

    off          — new suggestions wait in the Review feed; nothing runs.
    auto_approve — promote new suggestions immediately, without evals.
    eval_review  — evaluate each suggestion on a candidate build; a passing
                   candidate waits for a human to approve.
    eval_auto    — evaluate each suggestion and promote it automatically when
                   the evals pass (failures stay in Review, or kick the fix loop
                   when ``auto_fix_on_failure``).

The policy is stored per-agent on ``DataSource.automation_settings``. The
resolved policy is what the orchestrator (``AgentReliabilityService``) reads,
through :meth:`stage` and the ``mode`` / derived flags.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


# Autonomy levels for an internal pipeline stage. The Self-Learning mode is
# *projected* onto these by :meth:`AgentAutomationPolicy.stage`.
AUTONOMY_OFF = "off"
AUTONOMY_SUGGEST = "suggest"
AUTONOMY_AUTO = "auto"

# Self-learning modes (the dropdown).
MODE_OFF = "off"
MODE_AUTO_APPROVE = "auto_approve"
MODE_EVAL_REVIEW = "eval_review"
MODE_EVAL_AUTO = "eval_auto"
MODE_VALUES = (MODE_OFF, MODE_AUTO_APPROVE, MODE_EVAL_REVIEW, MODE_EVAL_AUTO)

# What to do when training can't make the evals pass within ``max_iterations``.
ON_FAILURE_NONE = "none"            # leave the agent as-is, just record the run
ON_FAILURE_TRAINING = "training"   # flag it; keep serving to everyone (last-good build)
ON_FAILURE_DEVELOPMENT = "development"  # pull from regular users; only agent admins see it
ON_FAILURE_ACTIONS = (ON_FAILURE_NONE, ON_FAILURE_TRAINING, ON_FAILURE_DEVELOPMENT)


class AgentAutomationPolicy(BaseModel):
    """Resolved (effective) self-learning policy for one agent."""

    # The single dropdown.
    mode: str = MODE_OFF

    # Advanced — only meaningful for ``eval_auto``.
    auto_fix_on_failure: bool = False        # train -> re-eval on failure
    on_repeated_failure: str = ON_FAILURE_TRAINING
    max_iterations: int = Field(default=3, ge=1, le=10)

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in MODE_VALUES:
            raise ValueError(f"mode must be one of {MODE_VALUES}, got {v!r}")
        return v

    @field_validator("on_repeated_failure")
    @classmethod
    def _valid_on_failure(cls, v: str) -> str:
        if v not in ON_FAILURE_ACTIONS:
            raise ValueError(f"on_repeated_failure must be one of {ON_FAILURE_ACTIONS}, got {v!r}")
        return v

    # ----- derived flags (read by the orchestrator) -------------------------
    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    @property
    def auto_approve_suggestions(self) -> bool:
        """Promote a new suggestion without running evals."""
        return self.mode == MODE_AUTO_APPROVE

    @property
    def auto_run_eval(self) -> bool:
        """Run evals on the suggestion's candidate build."""
        return self.mode in (MODE_EVAL_REVIEW, MODE_EVAL_AUTO)

    @property
    def auto_approve_on_pass(self) -> bool:
        """Promote automatically when the evals pass."""
        return self.mode == MODE_EVAL_AUTO

    def stage(self, name: str) -> str:
        """Project the mode onto a legacy pipeline stage. Returns ``off`` for
        every stage when the policy is disabled."""
        if not self.enabled:
            return AUTONOMY_OFF
        if name in ("eval_on_change", "eval_on_table_change", "eval_on_global_change"):
            return AUTONOMY_AUTO if self.auto_run_eval else AUTONOMY_OFF
        if name == "train_on_failure":
            return AUTONOMY_AUTO if (self.mode == MODE_EVAL_AUTO and self.auto_fix_on_failure) else AUTONOMY_OFF
        if name == "approve_instructions":
            return AUTONOMY_AUTO if self.mode == MODE_EVAL_AUTO else AUTONOMY_SUGGEST
        return AUTONOMY_OFF


# The hard-coded fallback used when an agent has configured nothing.
DEFAULT_POLICY = AgentAutomationPolicy()


def _normalize_legacy(layer: Dict[str, Any]) -> Dict[str, Any]:
    """Map a pre-``mode`` stored override (the old boolean tree) onto ``mode``
    so existing per-agent settings keep working after the schema change."""
    if not isinstance(layer, dict) or "mode" in layer:
        return layer
    legacy_keys = ("auto_approve_suggestions", "auto_run_eval", "auto_approve_on_success")
    if not any(k in layer for k in legacy_keys):
        return layer
    out = dict(layer)
    if layer.get("auto_approve_suggestions"):
        out["mode"] = MODE_AUTO_APPROVE
    elif layer.get("auto_run_eval"):
        out["mode"] = MODE_EVAL_AUTO if layer.get("auto_approve_on_success", True) else MODE_EVAL_REVIEW
    else:
        out["mode"] = MODE_OFF
    return out


def resolve_policy(
    org_defaults: Optional[Dict[str, Any]],
    agent_override: Optional[Dict[str, Any]],
) -> AgentAutomationPolicy:
    """Merge org defaults over the built-in defaults, then the per-agent
    override on top. Tolerates partial / legacy dicts."""
    merged: Dict[str, Any] = DEFAULT_POLICY.model_dump()

    for layer in (org_defaults, agent_override):
        if not isinstance(layer, dict):
            continue
        layer = _normalize_legacy(layer)
        for key, value in layer.items():
            if key in merged and value is not None:
                merged[key] = value

    try:
        return AgentAutomationPolicy(**merged)
    except Exception:
        return AgentAutomationPolicy()
