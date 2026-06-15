"""Agent reliability automation policy.

A single, consistent autonomy dial (``off`` | ``suggest`` | ``auto``) applied at
each stage of the self-learning loop, plus a couple of scalar knobs. The policy
lives in two places and is *resolved* by merging them:

* **Org defaults** — an org-wide policy stored on
  ``OrganizationSettings.config['agent_automation_defaults']``. A cautious org
  configures this once.
* **Per-agent override** — a (possibly partial) policy stored on
  ``DataSource.automation_settings``. Only the keys present here override the
  org default; everything else inherits.

The resolved policy is what the orchestrator (``AgentReliabilityService``) reads.

Autonomy semantics per stage:
    off      — stage never runs.
    suggest  — stage runs but stops at a human-reviewable artifact
               (draft eval / draft instruction build / report-only eval run).
    auto     — stage runs end to end without a human in the loop.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


# Autonomy levels for a pipeline stage.
AUTONOMY_OFF = "off"
AUTONOMY_SUGGEST = "suggest"
AUTONOMY_AUTO = "auto"
AUTONOMY_LEVELS = (AUTONOMY_OFF, AUTONOMY_SUGGEST, AUTONOMY_AUTO)

# What to do when training can't make the evals pass within ``max_iterations``.
ON_FAILURE_NONE = "none"            # leave the agent as-is, just record the run
ON_FAILURE_UNDER_REVIEW = "under_review"  # flag it; keep serving last-good build
ON_FAILURE_DISABLE = "disable"     # take the agent offline (publish_status=disabled)
ON_FAILURE_ACTIONS = (ON_FAILURE_NONE, ON_FAILURE_UNDER_REVIEW, ON_FAILURE_DISABLE)


class AgentAutomationPolicy(BaseModel):
    """Resolved (effective) automation policy for one agent.

    Also used as the org-default shape and as the (partial) per-agent override
    shape — for overrides, callers pass only the keys they want to change and
    merge via :func:`resolve_policy`.
    """

    # Master switch. When False the orchestrator treats every trigger as ``off``
    # regardless of the per-stage dials — a single, obvious kill switch.
    enabled: bool = False

    # Triggers: when should we (re)measure the agent?
    eval_on_change: str = AUTONOMY_SUGGEST          # agent's own instructions changed
    eval_on_table_change: str = AUTONOMY_SUGGEST    # a table was activated / columns changed
    eval_on_global_change: str = AUTONOMY_SUGGEST   # a global instruction build was promoted

    # Remediation: when evals fail, write/fix instructions.
    train_on_failure: str = AUTONOMY_SUGGEST

    # Promotion: push a candidate (passing) instruction build live.
    approve_instructions: str = AUTONOMY_SUGGEST

    # Eval curation: promote a thumbs-up auto-drafted eval from draft -> active.
    auto_promote_evals: str = AUTONOMY_OFF

    # Outcome when the loop gives up.
    on_repeated_failure: str = ON_FAILURE_UNDER_REVIEW

    # Bound on the train -> re-eval loop. The single most important cost guard.
    max_iterations: int = Field(default=3, ge=1, le=10)

    @field_validator(
        "eval_on_change",
        "eval_on_table_change",
        "eval_on_global_change",
        "train_on_failure",
        "approve_instructions",
        "auto_promote_evals",
    )
    @classmethod
    def _valid_autonomy(cls, v: str) -> str:
        if v not in AUTONOMY_LEVELS:
            raise ValueError(f"autonomy must be one of {AUTONOMY_LEVELS}, got {v!r}")
        return v

    @field_validator("on_repeated_failure")
    @classmethod
    def _valid_on_failure(cls, v: str) -> str:
        if v not in ON_FAILURE_ACTIONS:
            raise ValueError(f"on_repeated_failure must be one of {ON_FAILURE_ACTIONS}, got {v!r}")
        return v

    def stage(self, name: str) -> str:
        """Effective autonomy for a stage, honoring the master switch.

        Returns ``off`` for every stage when ``enabled`` is False so callers
        don't have to special-case the kill switch everywhere.
        """
        if not self.enabled:
            return AUTONOMY_OFF
        return getattr(self, name, AUTONOMY_OFF)


# The hard-coded fallback used when an org has configured nothing. Conservative
# on purpose: master switch off, nothing auto-promotes.
DEFAULT_POLICY = AgentAutomationPolicy()


def resolve_policy(
    org_defaults: Optional[Dict[str, Any]],
    agent_override: Optional[Dict[str, Any]],
) -> AgentAutomationPolicy:
    """Merge org defaults over the built-in defaults, then the per-agent
    override on top. Unknown / partial dicts are tolerated — only recognized
    keys take effect, invalid values fall back to the lower-precedence layer.
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
