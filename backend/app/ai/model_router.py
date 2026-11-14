"""Auto model routing — candidate resolution, tool schema, and the per-run
escalation controller.

The router only ever acts when the user picked no model (neither on the message
nor pinned on the report) and the org's ``model_routing`` setting is on. In that
case the planner starts on the small model and may call the ``route_model`` tool
to escalate to a stronger model for the rest of the run. Escalation is one-way
and sticky; the swap propagates to every subsequent tool call because
``runtime_ctx`` is rebuilt from the agent's live ``self.model`` each dispatch.

Design doc: docs/design/auto-model-routing.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

# The planner only ever sees a bounded set of routing targets, so the tool
# schema stays small and the model isn't asked to reason over a long menu.
MAX_ROUTING_CANDIDATES = 10


def get_routing_hint(model: Any) -> Optional[str]:
    """The admin-written routing guidance for a model, or None.

    Stored on ``LLMModel.config['routing_hint']`` (JSON, no migration needed).
    """
    cfg = getattr(model, "config", None)
    if not isinstance(cfg, dict):
        return None
    hint = cfg.get("routing_hint")
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    return None


async def resolve_routing_candidates(
    db: AsyncSession,
    organization: Any,
    user: Any,
    *,
    cap: int = MAX_ROUTING_CANDIDATES,
) -> List[LLMModel]:
    """Models the router may escalate to for this org+user.

    A model is a candidate when it is enabled, its provider is alive, the user
    can use it (EE access control), and — per product decision — it carries a
    non-empty routing hint. Only guided models are offered so the planner routes
    on the admin's intent, not on a bare model list. Ordered small-default →
    default → cheapest, then capped.
    """
    result = await db.execute(
        select(LLMModel)
        .join(LLMModel.provider)
        .filter(LLMModel.organization_id == organization.id)
        .filter(LLMModel.deleted_at == None)  # noqa: E711
        .filter(LLMModel.is_enabled == True)  # noqa: E712
        .filter(LLMModel.model_kind != "decision")
        .filter(LLMProvider.deleted_at == None)  # noqa: E711
        .filter(LLMProvider.is_enabled == True)  # noqa: E712
    )
    models = list(result.unique().scalars().all())

    # Only models with admin guidance are routing targets.
    guided = [m for m in models if get_routing_hint(m) is not None]

    # Access control (EE). Fail-open helper returns True when the feature is off.
    from app.core.permission_resolver import user_can_use_model

    eligible: List[LLMModel] = []
    for m in guided:
        if user is None:
            eligible.append(m)
            continue
        try:
            if await user_can_use_model(db, user.id, organization.id, m):
                eligible.append(m)
        except Exception:
            # Never let an access-check error remove a model the planner might
            # need; the server re-validates on apply anyway.
            eligible.append(m)

    def _rank(m: LLMModel):
        # small default first, then default, then cheapest input cost.
        small = 0 if getattr(m, "is_small_default", False) else 1
        default = 0 if getattr(m, "is_default", False) else 1
        cost = m.get_input_cost_rate() if hasattr(m, "get_input_cost_rate") else (
            getattr(m, "input_cost_per_million_tokens_usd", None) or 0.0
        )
        return (small, default, cost or 0.0)

    eligible.sort(key=_rank)
    return eligible[:cap]


def build_route_model_schema(
    candidates: List[LLMModel],
    current_model_id: Optional[str] = None,
) -> Dict[str, Any]:
    """JSON schema for the route_model tool with a per-request model enum.

    The enum values are provider model_ids; each option's guidance and cost are
    folded into the property description so the planner picks on the admin's
    intent. Constraining to an enum makes it impossible for the planner to name
    a model that isn't an eligible target.
    """
    values: List[str] = []
    lines: List[str] = []
    for m in candidates:
        mid = m.model_id
        values.append(mid)
        hint = get_routing_hint(m) or ""
        try:
            in_rate = m.get_input_cost_rate()
            out_rate = m.get_output_cost_rate()
            cost = f" (${in_rate:g}/${out_rate:g} per M in/out)"
        except Exception:
            cost = ""
        tags = []
        if getattr(m, "is_small_default", False):
            tags.append("small")
        if getattr(m, "is_default", False):
            tags.append("default")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        current = " (currently active)" if current_model_id and str(m.id) == str(current_model_id) else ""
        lines.append(f"- {mid}{tag_str}{cost}{current}: {hint}")

    desc = (
        "The model to run the rest of this task on. Choose the cheapest model "
        "whose guidance fits the task's difficulty. Options:\n" + "\n".join(lines)
    )
    return {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "enum": values,
                "description": desc,
            },
            "reason": {
                "type": "string",
                "description": "One short phrase on why this task needs this model (e.g. 'multi-source dashboard', 'simple lookup').",
            },
        },
        "required": ["model"],
        "additionalProperties": False,
    }


class RoutingController:
    """Per-run handle the route_model tool uses to escalate the planner model.

    Bound to a single AgentV2 run. Holds the eligible candidates (for the tool
    schema) and applies a validated escalation by calling back into the agent,
    which swaps ``self.model`` and rebuilds the planner's LLM so every later turn
    and tool call uses the new model.
    """

    def __init__(self, agent: Any, candidates: List[LLMModel]) -> None:
        self._agent = agent
        self.candidates = candidates
        self._by_model_id: Dict[str, LLMModel] = {m.model_id: m for m in candidates}
        self._by_db_id: Dict[str, LLMModel] = {str(m.id): m for m in candidates}
        self.escalated = False

    def has_candidates(self) -> bool:
        return bool(self.candidates)

    def _match(self, ref: str) -> Optional[LLMModel]:
        """Resolve a routing target by model_id, db id, then display name.

        Order matters: a custom model's display name is admin-editable and not
        unique, so it is only a last-resort alias. The tool schema offers
        model_ids (see build_route_model_schema), so the planner never has to
        rely on the name; renaming a model can't break routing.
        """
        if not ref:
            return None
        return (
            self._by_model_id.get(ref)
            or self._by_db_id.get(ref)
            or next((m for m in self.candidates if (m.name or "").lower() == ref.lower()), None)
        )

    async def apply(self, model_ref: str, reason: Optional[str]) -> Dict[str, Any]:
        """Validate + apply a routing request. Returns an observation dict.

        Never raises to the tool — an unknown/ineligible model returns an error
        observation and leaves the current model in place.
        """
        target = self._match(str(model_ref or ""))
        if target is None:
            return {
                "summary": f"Model '{model_ref}' is not an available routing target; keeping current model.",
                "routed": False,
                "error": {"code": "invalid_model", "message": "not in eligible routing set"},
            }

        current = getattr(self._agent, "model", None)
        if current is not None and str(getattr(current, "id", "")) == str(target.id):
            return {
                "summary": f"Already using {target.name}; no change.",
                "routed": False,
                "model": target.model_id,
            }

        prev_name = getattr(current, "name", None)
        self._agent._apply_routed_model(target)
        self.escalated = True
        logger.info(
            "[routing] escalated %s -> %s (reason=%s)",
            prev_name, target.name, (reason or "")[:120],
        )
        provider_type = getattr(getattr(target, "provider", None), "provider_type", None)
        return {
            "summary": f"Routed to {target.name} for the rest of this task.",
            "routed": True,
            "model": target.model_id,
            "model_name": target.name,
            "provider_type": provider_type,
            "from_model": prev_name,
            "reason": reason,
        }


def compute_routing_savings_usd(
    records: List[Any],
    baseline_rates_by_model_id: Dict[str, Dict[str, float]],
) -> float:
    """Sum of (baseline-priced tokens − actual cost) over routed usage records.

    ``baseline_rates_by_model_id`` maps an LLMModel.id to {'in': rate, 'out':
    rate} in USD per million tokens. Records whose baseline is missing or which
    ran on the baseline model itself contribute ~0. Net of escalation overhead.
    """
    total = 0.0
    for r in records:
        if not getattr(r, "routed", False):
            continue
        baseline_id = getattr(r, "baseline_model_id", None)
        rates = baseline_rates_by_model_id.get(str(baseline_id)) if baseline_id else None
        if not rates:
            continue
        in_tokens = (getattr(r, "prompt_tokens", 0) or 0) + (getattr(r, "cache_read_tokens", 0) or 0) + (getattr(r, "cache_creation_tokens", 0) or 0)
        out_tokens = getattr(r, "completion_tokens", 0) or 0
        # Rates can be None for custom/self-hosted models with no price feed.
        in_rate = rates.get("in") or 0.0
        out_rate = rates.get("out") or 0.0
        baseline_cost = (in_tokens / 1_000_000.0) * in_rate + (out_tokens / 1_000_000.0) * out_rate
        actual = float(getattr(r, "total_cost_usd", 0) or 0)
        total += baseline_cost - actual
    return total


# ── Upfront routing with a decision model ───────────────────────────────────
#
# Org setting ``model_routing_decision`` picks HOW the Auto router chooses the
# starting model:
#   small_model (default) — start on the small model; the planner may escalate
#                           via route_model (today's behavior, unchanged).
#   decision              — before the first planner turn, ask a decision model
#                           one ``choice`` question over the routing candidates
#                           (each described by its routing hint). A confident,
#                           non-small pick becomes the starting model; anything
#                           else (low confidence, error, timeout, disabled model)
#                           falls back to small_model. route_model stays
#                           available either way, so escalation still works.

ROUTING_METHODS = ("small_model", "decision")
DEFAULT_MIN_CONFIDENCE = 0.7
DECISION_USAGE_SCOPE = "model_routing.decision"
_MAX_STATE_CHARS = 6000


def routing_decision_settings(settings: Any) -> Dict[str, Any]:
    """Normalized ``model_routing_decision`` value for an org's settings."""
    raw = None
    try:
        cfg = settings.get_config("model_routing_decision") if settings is not None else None
        raw = getattr(cfg, "value", cfg)
    except Exception:
        raw = None
    raw = raw if isinstance(raw, dict) else {}
    method = raw.get("method") if raw.get("method") in ROUTING_METHODS else "small_model"
    try:
        min_conf = float(raw.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        min_conf = DEFAULT_MIN_CONFIDENCE
    return {
        "method": method,
        "decision_model_id": raw.get("decision_model_id") or None,
        "min_confidence": min(max(min_conf, 0.0), 1.0),
    }


async def _load_decision_model(db: AsyncSession, organization: Any, model_id: Optional[str]) -> Optional[LLMModel]:
    if not model_id:
        return None
    return (await db.execute(
        select(LLMModel)
        .join(LLMModel.provider)
        .filter(LLMModel.id == str(model_id))
        .filter(LLMModel.organization_id == organization.id)
        .filter(LLMModel.deleted_at == None)  # noqa: E711
        .filter(LLMModel.is_enabled == True)  # noqa: E712
        .filter(LLMModel.model_kind == "decision")
        .filter(LLMProvider.deleted_at == None)  # noqa: E711
        .filter(LLMProvider.is_enabled == True)  # noqa: E712
    )).unique().scalar_one_or_none()


async def validate_routing_decision_update(
    db: AsyncSession, organization: Any, stored: Optional[dict], incoming: Any,
) -> Dict[str, Any]:
    """Validate a ``model_routing_decision`` update (merged over ``stored``).
    Raises HTTPException(400/402) with a user-facing message."""
    from fastapi import HTTPException
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="Auto router method must be an object.")
    merged = {**(stored or {}), **incoming}
    method = merged.get("method") or "small_model"
    if method not in ROUTING_METHODS:
        raise HTTPException(status_code=400, detail=f"method must be one of {', '.join(ROUTING_METHODS)}.")
    try:
        min_conf = float(merged.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="min_confidence must be a number between 0 and 1.")
    if not (0.0 <= min_conf <= 1.0):
        raise HTTPException(status_code=400, detail="min_confidence must be between 0 and 1.")
    decision_model_id = merged.get("decision_model_id") or None
    if method == "decision":
        from app.ee.license import has_feature
        if not has_feature("model_routing"):
            raise HTTPException(status_code=402, detail="The Auto model router requires an enterprise license.")
        if await _load_decision_model(db, organization, decision_model_id) is None:
            raise HTTPException(
                status_code=400,
                detail="Choose an enabled decision model to route with a decision model.",
            )
    return {"method": method, "decision_model_id": decision_model_id, "min_confidence": min_conf}


@dataclass
class DecisionOutcome:
    """Result of the upfront routing decision. ``model`` is the starting model
    to switch to, or None to keep today's path (start small); ``reason`` says
    why when None."""
    model: Optional[LLMModel] = None
    reason: Optional[str] = None
    confidence: float = 0.0
    probabilities: List[Dict[str, Any]] = field(default_factory=list)
    decision_model: Optional[LLMModel] = None
    latency_ms: int = 0


def build_routing_question(candidates: List[LLMModel]) -> Dict[str, Any]:
    """One ``choice`` question whose options are the candidates' model ids,
    each described by the admin's routing hint (and small/default tags)."""
    choices = []
    for m in candidates:
        tags = []
        if getattr(m, "is_small_default", False):
            tags.append("small, cheapest")
        if getattr(m, "is_default", False):
            tags.append("default")
        hint = get_routing_hint(m) or ""
        desc = (f"[{', '.join(tags)}] " if tags else "") + hint
        choices.append({"value": m.model_id, "description": desc.strip() or None})
    return {
        "type": "choice",
        "name": "starting_model",
        "instructions": (
            "Which model should handle this request? Pick the cheapest model whose "
            "guidance fits the request's difficulty."
        ),
        "choices": choices,
    }


def _routing_state(prompt_text: str, context_text: Optional[str]) -> str:
    parts = [f"User request:\n{(prompt_text or '').strip()}"]
    if context_text and context_text.strip():
        parts.append(f"Recent conversation:\n{context_text.strip()}")
    state = "\n\n".join(parts)
    return state[:_MAX_STATE_CHARS]


async def decide_starting_model(
    db: AsyncSession,
    organization: Any,
    settings: Any,
    *,
    candidates: List[LLMModel],
    small_model: Optional[LLMModel],
    prompt_text: str,
    context_text: Optional[str] = None,
    usage_context: Any = None,
) -> DecisionOutcome:
    """Ask the configured decision model which candidate a run should start
    on. Never raises: any problem returns ``DecisionOutcome(model=None,
    reason=...)`` and the caller keeps today's routing."""
    cfg = routing_decision_settings(settings)
    if cfg["method"] != "decision":
        return DecisionOutcome(reason="method_small_model")
    if not candidates:
        return DecisionOutcome(reason="no_candidates")
    decision_model = await _load_decision_model(db, organization, cfg["decision_model_id"])
    if decision_model is None:
        return DecisionOutcome(reason="decision_model_unavailable")

    from app.ai.llm import LLM
    from app.ai.llm.decisions import top_choice
    from app.dependencies import async_session_maker

    question = build_routing_question(candidates)
    try:
        llm = LLM(decision_model, usage_session_maker=async_session_maker, usage_context=usage_context)
        result = await llm.decide(
            _routing_state(prompt_text, context_text), [question],
            usage_scope=DECISION_USAGE_SCOPE,
        )
    except Exception as exc:  # DecisionError, quota, network — never break the run
        logger.warning("[routing] decision model call failed: %s", exc)
        return DecisionOutcome(reason=f"decision_failed: {exc}"[:300], decision_model=decision_model)

    answer = result.by_name().get("starting_model") or {}
    if answer.get("type") != "choice":
        return DecisionOutcome(reason="no_answer", decision_model=decision_model, latency_ms=result.latency_ms)
    picked, confidence = top_choice(answer)
    probabilities = sorted(
        ({"value": str(p.get("value")), "probability": float(p.get("probability") or 0.0)}
         for p in answer.get("probabilities") or []),
        key=lambda p: -p["probability"],
    )
    base = dict(confidence=confidence, probabilities=probabilities,
                decision_model=decision_model, latency_ms=result.latency_ms)
    target = next((m for m in candidates if m.model_id == picked), None)
    if target is None:
        return DecisionOutcome(reason="picked_unknown_model", **base)
    if confidence < cfg["min_confidence"]:
        return DecisionOutcome(reason="low_confidence", **base)
    if small_model is not None and str(target.id) == str(small_model.id):
        return DecisionOutcome(reason="picked_small_model", **base)
    return DecisionOutcome(model=target, **base)
