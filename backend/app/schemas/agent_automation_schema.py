"""Agent self-learning automation policy.

Each agent (a ``DataSource``) carries a small **decision tree** that the
agent-page "Self Learning" modal exposes verbatim:

    1. Auto-approve suggestions?
         yes -> a new suggestion is promoted to main immediately, no evals.
         no  -> 2.
    2. Auto-run eval?
         yes -> the suggestion is evaluated against a *candidate build*
                (main + the suggested hunks), then 3.
         no  -> the suggestion just lands in the Review feed for a human.
    3. Auto-approve on success?
         yes -> promote automatically when the evals pass.
         no  -> leave the (passing) candidate pending for a human.

    (advanced) Auto-fix on failure?
         yes -> when evals fail, run the train -> re-eval loop to try to fix.
         no  -> leave the suggestion in Review marked "eval failed"
                (main is untouched, so there is nothing to revert).

The policy is stored per-agent on ``DataSource.automation_settings`` (a partial
dict is fine — missing keys fall back to the built-in defaults). The resolved
policy is what the orchestrator (``AgentReliabilityService``) reads, exclusively
through :meth:`AgentAutomationPolicy.stage` / the ``auto_approve_suggestions``
flag, so the rest of the loop is unchanged by this surface.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


# Autonomy levels for an internal pipeline stage. Kept because the orchestrator
# still reasons in these terms; the Self-Learning tree is *projected* onto them
# by :meth:`AgentAutomationPolicy.stage`.
AUTONOMY_OFF = "off"
AUTONOMY_SUGGEST = "suggest"
AUTONOMY_AUTO = "auto"
AUTONOMY_LEVELS = (AUTONOMY_OFF, AUTONOMY_SUGGEST, AUTONOMY_AUTO)

# What to do when training can't make the evals pass within ``max_iterations``.
ON_FAILURE_NONE = "none"            # leave the agent as-is, just record the run
ON_FAILURE_TRAINING = "training"   # flag it; keep serving to everyone (last-good build)
ON_FAILURE_DEVELOPMENT = "development"  # pull from regular users; only agent admins see it
ON_FAILURE_ACTIONS = (ON_FAILURE_NONE, ON_FAILURE_TRAINING, ON_FAILURE_DEVELOPMENT)


class AgentAutomationPolicy(BaseModel):
    """Resolved (effective) self-learning policy for one agent.

    The four booleans below are the entire user-facing surface (the modal). The
    orchestrator never reads them directly except ``auto_approve_suggestions``;
    every other consumer goes through :meth:`stage`, which projects the tree onto
    the legacy ``off | suggest | auto`` stage vocabulary.
    """

    # === The decision tree (the modal) ===
    auto_approve_suggestions: bool = False   # promote new suggestions without evals
    auto_run_eval: bool = False              # run evals on the candidate build
    auto_approve_on_success: bool = False    # promote automatically when evals pass
    auto_fix_on_failure: bool = False        # advanced: train -> re-eval on failure

    # Outcome when the train -> re-eval loop gives up.
    on_repeated_failure: str = ON_FAILURE_TRAINING

    # Bound on the train -> re-eval loop. The single most important cost guard.
    max_iterations: int = Field(default=3, ge=1, le=10)

    @field_validator("on_repeated_failure")
    @classmethod
    def _valid_on_failure(cls, v: str) -> str:
        if v not in ON_FAILURE_ACTIONS:
            raise ValueError(f"on_repeated_failure must be one of {ON_FAILURE_ACTIONS}, got {v!r}")
        return v

    @property
    def enabled(self) -> bool:
        """Derived master switch: the loop does something only if the agent
        either auto-approves suggestions or auto-runs evals on them."""
        return bool(self.auto_approve_suggestions or self.auto_run_eval)

    def stage(self, name: str) -> str:
        """Project the decision tree onto a legacy pipeline stage.

        Returns ``off`` for every stage when the policy is effectively disabled
        so callers don't have to special-case the kill switch everywhere.
        """
        if not self.enabled:
            return AUTONOMY_OFF
        if name in ("eval_on_change", "eval_on_table_change", "eval_on_global_change"):
            return AUTONOMY_AUTO if self.auto_run_eval else AUTONOMY_OFF
        if name == "train_on_failure":
            return AUTONOMY_AUTO if self.auto_fix_on_failure else AUTONOMY_OFF
        if name == "approve_instructions":
            # When evals run, promote on green only if the agent opted in;
            # otherwise stop at a human-reviewable (passing) candidate.
            return AUTONOMY_AUTO if self.auto_approve_on_success else AUTONOMY_SUGGEST
        # auto_promote_evals and anything else: not part of the tree.
        return AUTONOMY_OFF


# The hard-coded fallback used when an agent has configured nothing. Conservative
# on purpose: nothing runs, every suggestion goes to Review for a human.
DEFAULT_POLICY = AgentAutomationPolicy()


def resolve_policy(
    org_defaults: Optional[Dict[str, Any]],
    agent_override: Optional[Dict[str, Any]],
) -> AgentAutomationPolicy:
    """Merge org defaults over the built-in defaults, then the per-agent
    override on top. Unknown / partial dicts are tolerated — only recognized
    keys take effect, invalid values fall back to the lower-precedence layer.

    Org defaults remain supported for forward-compat, but there is currently no
    org-level UI: per-agent overrides are the only configured surface.
    """
    merged: Dict[str, Any] = DEFAULT_POLICY.model_dump()

    for layer in (org_defaults, agent_override):
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if key in merged and value is not None:
                merged[key] = value

    try:
        return AgentAutomationPolicy(**merged)
    except Exception:
        # A bad stored value shouldn't brick the whole agent — fall back to the
        # safe default rather than raising into a request/trigger path.
        return AgentAutomationPolicy()
