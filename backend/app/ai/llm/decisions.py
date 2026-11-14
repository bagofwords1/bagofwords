"""Decision models: typed questions in, calibrated probabilities out.

Two wire formats exist and neither is universal:

- **openai** — OpenAI Decisions, ``POST {base}/decisions``: ``input`` plus a
  ``questions`` *array* (``predicate`` / ``choice`` / ``score``, each with a
  ``name``); answers come back as an array in question order.
- **systemone** — TypeSafe Jev, Microsoft Foundry Decision-1, Perplexity
  pplx-decider, Cloudflare Clef, Ollama, vLLM, LiteLLM ``/v1/systemone``:
  ``state`` plus a ``questions`` *map* keyed by name (``noul`` / ``choice`` /
  ``score`` / ``box``); answers come back as a map.

The app speaks the OpenAI shape internally; :func:`build_request_body` and the
``answers_from_*`` helpers translate at the edge. :class:`DecisionClient` is
the HTTP call; :meth:`app.ai.llm.LLM.decide` wraps it with credentials, usage
recording and spend caps.

Per-model settings live in ``LLMModel.config`` (catalog defaults first, admin
overrides on top) — see :func:`decision_settings`. Decision APIs have no
temperature/top_p: nothing sampling-related is ever sent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

DECISION_FORMATS = ("openai", "systemone")
QUESTION_TYPES = ("predicate", "choice", "score", "box")
_TO_SYSTEMONE_TYPE = {"predicate": "noul", "choice": "choice", "score": "score", "box": "box"}

DEFAULT_TIMEOUT_MS = 1000
MIN_TIMEOUT_MS, MAX_TIMEOUT_MS = 100, 120_000
DEFAULT_MAX_QUESTIONS = 64
MAX_QUESTIONS_CAP = 1000

# Admin-editable decision settings and their validators (see validate_settings).
SETTING_KEYS = (
    "decision_format", "decision_path", "api_model_id", "max_questions",
    "question_types", "timeout_ms", "keep_alive", "prompt_caching",
)

# Tests swap this for tests/mocks/fake_decision_server.py's transport.
TRANSPORT_OVERRIDE: Optional[httpx.AsyncBaseTransport] = None


class DecisionError(Exception):
    """A decision request that could not be made or answered."""


@dataclass
class DecisionResult:
    answers: list[dict] = field(default_factory=list)  # internal (OpenAI) shape, one per question
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: Optional[str] = None

    def by_name(self) -> dict[str, dict]:
        return {a.get("name"): a for a in self.answers}


# ── settings ────────────────────────────────────────────────────────────────

def _catalog_entry(model_id: Optional[str]) -> dict:
    from app.models.llm_model import LLM_MODEL_DETAILS
    for d in LLM_MODEL_DETAILS:
        if d.get("model_id") == model_id and d.get("model_kind") == "decision":
            return d
    return {}


def _default_format(provider_type: Optional[str]) -> str:
    return "openai" if provider_type in ("openai", "azure") else "systemone"


def decision_settings(model: Any) -> dict:
    """Effective decision settings for a model: catalog defaults, then the
    admin's overrides in ``config``. Always returns every key."""
    cfg = dict(getattr(model, "config", None) or {})
    cat = _catalog_entry(getattr(model, "model_id", None))
    provider_type = getattr(getattr(model, "provider", None), "provider_type", None)

    def pick(key, default=None):
        if cfg.get(key) not in (None, ""):
            return cfg[key]
        if cat.get(key) not in (None, ""):
            return cat[key]
        return default

    fmt = pick("decision_format", _default_format(provider_type))
    return {
        "decision_format": fmt,
        "decision_path": pick("decision_path", "/decisions" if fmt == "openai" else "/systemone"),
        "api_model_id": pick("api_model_id", getattr(model, "model_id", None)),
        "max_questions": int(pick("max_questions", DEFAULT_MAX_QUESTIONS)),
        "question_types": list(pick("question_types", ["predicate", "choice", "score"])),
        "timeout_ms": int(pick("timeout_ms", DEFAULT_TIMEOUT_MS)),
        "keep_alive": pick("keep_alive"),
        "prompt_caching": bool(pick("prompt_caching", True)),
        "supports_images": bool(getattr(model, "supports_vision", False)),
    }


def validate_settings(update: dict) -> dict:
    """Validate an admin update of decision settings; returns the cleaned dict.
    Raises ValueError with a user-facing message."""
    out: dict = {}
    for key, val in (update or {}).items():
        if key not in SETTING_KEYS:
            raise ValueError(f"Unknown decision setting '{key}'.")
        if val is None or val == "":
            out[key] = None  # reset to the catalog/default value
            continue
        if key == "decision_format":
            if val not in DECISION_FORMATS:
                raise ValueError(f"decision_format must be one of {', '.join(DECISION_FORMATS)}.")
        elif key == "decision_path":
            if not isinstance(val, str) or not val.startswith("/") or "://" in val or ".." in val or len(val) > 200:
                raise ValueError("decision_path must be a path starting with '/' (e.g. /v1/systemone).")
        elif key == "api_model_id":
            if not isinstance(val, str) or len(val) > 300:
                raise ValueError("api_model_id must be a string.")
        elif key == "max_questions":
            if isinstance(val, bool) or not isinstance(val, int) or not (1 <= val <= MAX_QUESTIONS_CAP):
                raise ValueError(f"max_questions must be between 1 and {MAX_QUESTIONS_CAP}.")
        elif key == "question_types":
            if not isinstance(val, list) or not val or any(v not in QUESTION_TYPES for v in val):
                raise ValueError(f"question_types must be a non-empty list of {', '.join(QUESTION_TYPES)}.")
        elif key == "timeout_ms":
            if isinstance(val, bool) or not isinstance(val, int) or not (MIN_TIMEOUT_MS <= val <= MAX_TIMEOUT_MS):
                raise ValueError(f"timeout_ms must be between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}.")
        elif key == "keep_alive":
            if not isinstance(val, str) or len(val) > 20:
                raise ValueError("keep_alive must be a duration like '5m'.")
        elif key == "prompt_caching":
            if not isinstance(val, bool):
                raise ValueError("prompt_caching must be true or false.")
        out[key] = val
    return out


# ── questions & wire formats ────────────────────────────────────────────────

def validate_questions(questions: list[dict], *, max_questions: int, question_types) -> None:
    if not questions:
        raise DecisionError("At least one question is required.")
    if len(questions) > max_questions:
        raise DecisionError(f"{len(questions)} questions exceed this model's limit of {max_questions}.")
    names = set()
    for q in questions:
        if q.get("type") not in question_types:
            raise DecisionError(f"Question type '{q.get('type')}' is not supported by this model.")
        name = q.get("name")
        if not name or name in names:
            raise DecisionError("Every question needs a unique name.")
        names.add(name)


def _to_systemone_question(q: dict) -> dict:
    out: dict = {"type": _TO_SYSTEMONE_TYPE[q["type"]], "instructions": q.get("instructions", "")}
    if q["type"] == "choice":
        out["criteria"] = {str(c["value"]): c.get("description") for c in q.get("choices") or []}
    elif q["type"] == "score":
        out["criteria"] = [lv.get("description") or lv.get("label") for lv in q.get("levels") or []]
    return out


def build_request_body(fmt: str, api_model_id: str, input_value: Any, questions: list[dict],
                       *, keep_alive: Optional[str] = None) -> dict:
    if fmt == "openai":
        return {"model": api_model_id, "input": input_value, "questions": questions}
    body = {
        "model": api_model_id,
        "state": input_value,
        "questions": {q["name"]: _to_systemone_question(q) for q in questions},
    }
    if keep_alive:
        body["keep_alive"] = keep_alive
    return body


def answers_from_openai(raw: dict, questions: list[dict]) -> list[dict]:
    got = {a.get("name"): a for a in (raw.get("answers") or []) if isinstance(a, dict)}
    return [got.get(q["name"]) or {"type": "refusal", "name": q["name"]} for q in questions]


def answers_from_systemone(raw: dict, questions: list[dict]) -> list[dict]:
    got = raw.get("answers") or {}
    out = []
    for q in questions:
        name = q["name"]
        a = got.get(name) if isinstance(got, dict) else None
        if not isinstance(a, dict) or a.get("type") == "refusal":
            out.append({"type": "refusal", "name": name})
            continue
        t = a.get("type")
        if t == "noul":
            out.append({"type": "predicate", "name": name, "probability": a.get("noul")})
        elif t == "choice":
            probs = a.get("probabilities") or {}
            out.append({
                "type": "choice", "name": name, "choice": a.get("choice"),
                "confidence": a.get("confidence"),
                "probabilities": [{"value": k, "probability": v} for k, v in probs.items()],
            })
        elif t == "score":
            legend = a.get("legend") or {}
            probs = a.get("probabilities") or {}
            levels = q.get("levels") or []
            out.append({
                "type": "score", "name": name, "score": a.get("score"),
                "confidence": a.get("confidence"),
                "probabilities": [
                    {"value": int(k), "label": (levels[int(k)].get("label") if int(k) < len(levels) else legend.get(k)),
                     "probability": v}
                    for k, v in sorted(probs.items(), key=lambda kv: int(kv[0]))
                ],
            })
        elif t == "box":
            out.append({"type": "box", "name": name, "box": a.get("box"), "confidence": a.get("confidence")})
        else:
            out.append({"type": "refusal", "name": name})
    return out


def top_choice(answer: dict) -> tuple[Optional[str], float]:
    """The chosen value of a choice answer and its probability (0 when absent)."""
    if not answer or answer.get("type") != "choice":
        return None, 0.0
    choice = answer.get("choice")
    prob = None
    for p in answer.get("probabilities") or []:
        if str(p.get("value")) == str(choice):
            prob = p.get("probability")
    if prob is None:
        prob = answer.get("confidence")
    try:
        return (str(choice) if choice is not None else None), float(prob or 0.0)
    except (TypeError, ValueError):
        return (str(choice) if choice is not None else None), 0.0


# ── HTTP ────────────────────────────────────────────────────────────────────

class DecisionClient:
    """One provider endpoint. ``base_url`` is the API root including its
    version segment (``https://api.openai.com/v1``); the format's path is
    appended (``/decisions`` or ``/systemone``) unless overridden."""

    def __init__(self, *, base_url: str, api_key: Optional[str], settings: dict,
                 headers: Optional[dict] = None, verify_ssl: bool = True, pin_egress: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.settings = settings
        self.headers = dict(headers or {})
        self.verify_ssl = verify_ssl
        self.pin_egress = pin_egress

    def _client(self, timeout: float) -> httpx.AsyncClient:
        if TRANSPORT_OVERRIDE is not None:
            return httpx.AsyncClient(transport=TRANSPORT_OVERRIDE, timeout=timeout)
        from app.core.tls import shared_ssl_context
        verify = shared_ssl_context() if self.verify_ssl else False
        if self.pin_egress:
            # Admin-configured base_url: validated and pinned (app.core.egress).
            from app.core.egress import pinned_async_client, Policy
            return pinned_async_client(Policy.ADMIN, timeout=timeout, verify=verify)
        return httpx.AsyncClient(timeout=timeout, verify=verify)

    async def decide(self, input_value: Any, questions: list[dict]) -> DecisionResult:
        s = self.settings
        validate_questions(questions, max_questions=s["max_questions"], question_types=s["question_types"])
        fmt = s["decision_format"]
        body = build_request_body(fmt, s["api_model_id"], input_value, questions, keep_alive=s.get("keep_alive"))
        headers = {"Content-Type": "application/json", **self.headers}
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        url = self.base_url + s["decision_path"]
        timeout = s["timeout_ms"] / 1000.0
        started = time.monotonic()
        try:
            async with self._client(timeout) as http:
                resp = await http.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise DecisionError(f"Decision request timed out after {s['timeout_ms']} ms") from exc
        except httpx.HTTPError as exc:
            raise DecisionError(f"Decision request failed: {exc.__class__.__name__}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            msg = ""
            try:
                err = resp.json().get("error")
                msg = err.get("message") if isinstance(err, dict) else str(err or "")
            except Exception:
                msg = resp.text[:200]
            raise DecisionError(f"Decision provider returned {resp.status_code}: {msg}".strip())
        try:
            raw = resp.json()
        except ValueError as exc:
            raise DecisionError("Decision provider returned invalid JSON") from exc
        answers = answers_from_openai(raw, questions) if fmt == "openai" else answers_from_systemone(raw, questions)
        usage = raw.get("usage") or {}
        return DecisionResult(
            answers=answers,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
            model=raw.get("model"),
        )


def base_url_for(provider: Any) -> Optional[str]:
    """API root for a provider's decision endpoint, or None if unsupported."""
    cfg = getattr(provider, "additional_config", None) or {}
    ptype = getattr(provider, "provider_type", None)
    if ptype == "openai":
        return cfg.get("base_url") or "https://api.openai.com/v1"
    if ptype == "custom":
        return cfg.get("base_url")
    if ptype == "azure":
        endpoint = (cfg.get("endpoint_url") or "").rstrip("/")
        return endpoint or None
    return None


# Provider types a decision model may be configured on. Bedrock has no
# decision API (AWS ships Strands Decider as weights / a SageMaker endpoint);
# Anthropic, Google and Vertex have none either.
DECISION_PROVIDER_TYPES = ("openai", "azure", "custom")
