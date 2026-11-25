"""Named PII policies: one shared rule list, per-policy options, a policy per
LLM provider.

Stored shape (``OrganizationSettings.config["pii_protection"]``)::

    enabled: bool
    custom_rules:        # shared list: patterns are defined once
      - {id, name, patterns, replacement}          # replacement = default token
    policies:
      default: {name, mode, redact_context, builtin_overrides, custom_overrides}
      <slug>:  {...}
    provider_policies: {<llm_provider_id>: <policy_id> | "exempt"}
    agent_policies: {<data_source_id>: <policy_id> | ":none"}
    default_agent_policy: ":none" | <policy_id>
    display_policy: default

* ``builtin_overrides`` / ``custom_overrides`` map a rule id to
  ``{enabled, action, replacement}``; a missing key keeps the rule's default
  (enabled, the policy's ``mode``, the rule's own token).
* ``exempt`` is built in: it has no rules, is never stored under ``policies``,
  and is only ever a provider assignment.
* ``agent_policies`` / ``default_agent_policy`` (see
  ``docs/design/pii-agent-policies.md``): an agent's policy applies to every
  LLM call the agent takes part in, merged with the provider's policy so the
  strictest option for each rule wins (:func:`merge_policies`). An agent with
  no assignment uses ``default_agent_policy``; ``none`` means no agent policy.
* ``redact_context`` (default False) also redacts instructions, schemas and the
  other platform context (see :mod:`.context`); by default only the prompt and
  the data are redacted.

Before named policies the config was one rule set: ``mode``,
``builtin_overrides``, per-rule ``enabled``/``action``/``replacement`` on each
custom rule, and (provider exemptions) ``exempt_provider_ids``. :func:`normalize` turns that
into a ``default`` policy plus ``exempt`` assignments, and each policy compiles
through the same resolver as before, so an org that never touches policies gets
exactly the redaction it had.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .builtin_rules import BUILTIN_PII_RULES
from .checks import MAX_CONTEXT_WORDS, MAX_TERM_CHARS, MAX_TERMS, VALIDATORS
from .detectors.catalog import DETECTOR_ENTITIES, DETECTOR_PREFIX, default_min_score, detector_entity
from .redactor import VALID_ACTIONS, VALID_MODES, DetectorRule, PiiRedactor, _resolve_rules, token_label

DEFAULT_POLICY = "default"
EXEMPT_POLICY = "exempt"
# Agent assignment meaning "no agent policy" (only the provider's applies).
# The leading colon keeps it apart from every policy id (POLICY_ID_RE starts
# with a letter or digit), so an existing policy named "none" stays valid.
NO_AGENT_POLICY = ":none"
RESERVED_POLICY_IDS = (DEFAULT_POLICY, EXEMPT_POLICY)
# Action strength for merging: a merged rule takes the strongest action any
# input policy gives it. mask leaks part of the value, pseudonymize leaks
# equality between values, replace leaks nothing, block sends nothing.
ACTION_STRENGTH = {"mask": 1, "pseudonymize": 2, "replace": 3, "block": 4}
POLICY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
MAX_POLICIES = 20
RULE_OPTION_KEYS = ("enabled", "action", "replacement")
BUILTIN_RULE_IDS = frozenset(spec["id"] for spec in BUILTIN_PII_RULES)
# Built-in rules added later ship off, so an existing policy never starts
# redacting more without an admin choosing it.
BUILTIN_ON_BY_DEFAULT = {spec["id"]: spec.get("enabled_by_default", True) for spec in BUILTIN_PII_RULES}
DETECTOR_ENTITY_IDS = frozenset(e["id"] for e in DETECTOR_ENTITIES)
DETECTOR_ERROR_MODES = ("block", "regex_only")


def custom_rule_id(raw: Dict[str, Any]) -> str:
    """A custom rule's id, derived exactly as the pre-policy resolver did."""
    return str(raw.get("id") or raw.get("name") or "custom")


def _clean_words(raw: Any, limit: int, max_chars: int) -> List[str]:
    """A list of distinct, non-empty strings, in order, within the limits."""
    out: List[str] = []
    for w in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(w, str) and w.strip() and len(w.strip()) <= max_chars and w.strip() not in out:
            out.append(w.strip())
    return out[:limit]


def _clean_options(raw: Any) -> Dict[str, Any]:
    """Keep only the per-rule option keys, with sane types."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    if raw.get("action") in VALID_ACTIONS:
        out["action"] = raw["action"]
    if isinstance(raw.get("replacement"), str) and raw["replacement"]:
        out["replacement"] = raw["replacement"]
    return out


def _clean_detector_options(raw: Any) -> Dict[str, Any]:
    """Options for one detector entity: the rule options plus ``min_score``."""
    out = _clean_options(raw)
    score = raw.get("min_score") if isinstance(raw, dict) else None
    if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= float(score) <= 1:
        out["min_score"] = round(float(score), 2)
    return out


def _clean_detector_map(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for entity, opts in raw.items():
        cleaned = _clean_detector_options(opts)
        if cleaned and str(entity) in DETECTOR_ENTITY_IDS:
            out[str(entity)] = cleaned
    return out


def _clean_override_map(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for rid, opts in raw.items():
        cleaned = _clean_options(opts)
        if cleaned:
            out[str(rid)] = cleaned
    return out


def _clean_policy(raw: Any, *, fallback_name: str) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode")
    name = raw.get("name")
    return {
        "name": name.strip() if isinstance(name, str) and name.strip() else fallback_name,
        "mode": mode if mode in VALID_MODES else "replace",
        "redact_context": bool(raw.get("redact_context", False)),
        "builtin_overrides": _clean_override_map(raw.get("builtin_overrides")),
        "custom_overrides": _clean_override_map(raw.get("custom_overrides")),
        # Detector entities (names, locations…) are OFF unless turned on here.
        "detector_overrides": _clean_detector_map(raw.get("detector_overrides")),
        # When the detector cannot run: refuse the request, or send it with the
        # pattern rules only.
        "on_detector_error": raw.get("on_detector_error") if raw.get("on_detector_error") in DETECTOR_ERROR_MODES else "block",
    }


def is_v2(raw: Any) -> bool:
    """A config written with named policies (it has a ``policies`` map)."""
    return isinstance(raw, dict) and isinstance(raw.get("policies"), dict) and bool(raw.get("policies"))


def normalize(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Canonical policy config from any stored shape. Lenient: unknown or
    malformed pieces are dropped, never raised (this also runs on read)."""
    raw = raw if isinstance(raw, dict) else {}

    custom_rules: List[Dict[str, Any]] = []
    legacy_custom_overrides: Dict[str, Dict[str, Any]] = {}
    seen_ids = set()
    for r in raw.get("custom_rules") or []:
        if not isinstance(r, dict):
            continue
        rid = custom_rule_id(r)
        if rid in seen_ids:
            # Two rules sharing an id both stayed active before; never drop one
            # (that would redact less). The later one gets a distinct id.
            n = 2
            while f"{rid}-{n}" in seen_ids:
                n += 1
            rid = f"{rid}-{n}"
        seen_ids.add(rid)
        patterns = [p for p in (r.get("patterns") or []) if isinstance(p, str) and p]
        custom_rules.append({
            "id": rid,
            "name": str(r.get("name") or rid),
            "patterns": patterns,
            "replacement": r.get("replacement") if isinstance(r.get("replacement"), str) and r.get("replacement") else "[REDACTED]",
            # Optional conditions (see checks.py): exact terms, a checksum,
            # context words that must appear near a match.
            "terms": _clean_words(r.get("terms"), MAX_TERMS, MAX_TERM_CHARS),
            "checksum": r.get("checksum") if r.get("checksum") in VALIDATORS else None,
            "context": _clean_words(r.get("context"), MAX_CONTEXT_WORDS, MAX_TERM_CHARS),
        })
        opts = {k: r[k] for k in ("enabled", "action") if k in r}
        cleaned = _clean_options(opts)
        if cleaned:
            legacy_custom_overrides[rid] = cleaned

    policies: Dict[str, Dict[str, Any]] = {}
    provider_policies: Dict[str, str] = {}
    if is_v2(raw):
        for pid, pol in raw["policies"].items():
            pid = str(pid)
            if pid == EXEMPT_POLICY or not POLICY_ID_RE.match(pid):
                continue
            policies[pid] = _clean_policy(pol, fallback_name=pid)
        pp = raw.get("provider_policies")
        if isinstance(pp, dict):
            for prov, pol in pp.items():
                if isinstance(prov, str) and prov and isinstance(pol, str) and pol:
                    provider_policies[prov] = pol
    else:
        # Single rule set: it becomes the Default policy, unchanged. That
        # includes its coverage: before policies, an enabled rule set redacted
        # the whole prompt — instructions and schemas too — so it keeps doing
        # so after the upgrade (``redact_context``) until an admin turns that
        # off. Only policies created from here on default to prompt + data.
        policies[DEFAULT_POLICY] = _clean_policy(
            {
                "name": "Default",
                "redact_context": bool(raw.get("enabled", False)),
                "mode": raw.get("mode"),
                "builtin_overrides": raw.get("builtin_overrides"),
                "custom_overrides": legacy_custom_overrides,
            },
            fallback_name="Default",
        )
        exempt = raw.get("exempt_provider_ids")
        if isinstance(exempt, (list, tuple)):
            for prov in exempt:
                if isinstance(prov, str) and prov.strip():
                    provider_policies[prov.strip()] = EXEMPT_POLICY

    if DEFAULT_POLICY not in policies:
        policies[DEFAULT_POLICY] = _clean_policy({"name": "Default"}, fallback_name="Default")
    # Default first, then the rest in stored order.
    policies = {DEFAULT_POLICY: policies[DEFAULT_POLICY], **{k: v for k, v in policies.items() if k != DEFAULT_POLICY}}
    # Options for rules that do not exist (a deleted custom rule, a typo) are dropped.
    custom_ids = {r["id"] for r in custom_rules}
    for pol in policies.values():
        pol["builtin_overrides"] = {k: v for k, v in pol["builtin_overrides"].items() if k in BUILTIN_RULE_IDS}
        pol["custom_overrides"] = {k: v for k, v in pol["custom_overrides"].items() if k in custom_ids}

    # Assignments to a policy that no longer exists fall back to Default.
    provider_policies = {
        prov: pol for prov, pol in provider_policies.items()
        if pol == EXEMPT_POLICY or (pol in policies and pol != DEFAULT_POLICY)
    }
    # Agent assignments: a policy (never Exempt: agents only tighten) or
    # ":none". An assignment to a policy that no longer exists is dropped, so
    # the agent inherits the default agent policy again.
    agent_policies: Dict[str, str] = {}
    ap = raw.get("agent_policies")
    if isinstance(ap, dict):
        for agent_id, pid in ap.items():
            if not isinstance(agent_id, str) or not agent_id.strip() or not isinstance(pid, str):
                continue
            if pid == NO_AGENT_POLICY or pid in policies:
                agent_policies[agent_id.strip()] = pid
    default_agent = raw.get("default_agent_policy")
    display = raw.get("display_policy")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "custom_rules": custom_rules,
        "policies": policies,
        "provider_policies": provider_policies,
        "agent_policies": agent_policies,
        "default_agent_policy": default_agent if default_agent in policies else NO_AGENT_POLICY,
        "display_policy": display if display in policies else DEFAULT_POLICY,
    }


def legacy_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The pre-policy fields, mirrored from the Default policy, so a client
    that only knows the single rule set keeps reading the same values."""
    default = cfg["policies"][DEFAULT_POLICY]
    custom_rules = []
    for r in cfg["custom_rules"]:
        opts = default["custom_overrides"].get(r["id"], {})
        # ``replacement`` stays the shared token: a single-rule-set client
        # writes it back as the shared token, so this round-trips unchanged.
        custom_rules.append({
            **r,
            "enabled": opts.get("enabled", True),
            "action": opts.get("action"),
        })
    return {
        "mode": default["mode"],
        "builtin_overrides": copy.deepcopy(default["builtin_overrides"]),
        "custom_rules": custom_rules,
        "exempt_provider_ids": sorted(
            prov for prov, pol in cfg["provider_policies"].items() if pol == EXEMPT_POLICY
        ),
    }


def apply_update(existing: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a settings write into the canonical config.

    A write that carries ``policies`` is a policy-aware client: its fields
    replace the stored ones. Any other write is from a client that knows only
    the single rule set, and its fields land on the Default policy — so the
    pre-policy settings page and API keep working unchanged.
    """
    cfg = copy.deepcopy(normalize(existing))
    update = update if isinstance(update, dict) else {}

    if "enabled" in update:
        cfg["enabled"] = bool(update["enabled"])

    if "provider_policies" in update and isinstance(update["provider_policies"], dict):
        cfg["provider_policies"] = {
            str(prov): pol for prov, pol in update["provider_policies"].items()
            if isinstance(prov, str) and prov and isinstance(pol, str) and pol
        }
    if isinstance(update.get("display_policy"), str):
        cfg["display_policy"] = update["display_policy"]
    if "agent_policies" in update and isinstance(update["agent_policies"], dict):
        cfg["agent_policies"] = dict(update["agent_policies"])
    if isinstance(update.get("default_agent_policy"), str):
        cfg["default_agent_policy"] = update["default_agent_policy"]

    if is_v2(update):
        incoming = normalize({"custom_rules": update.get("custom_rules") or [], "policies": update["policies"]})
        cfg["policies"] = incoming["policies"]
        if "custom_rules" in update:
            cfg["custom_rules"] = incoming["custom_rules"]
        return normalize(cfg)

    default = cfg["policies"][DEFAULT_POLICY]
    if "mode" in update:
        default["mode"] = update["mode"] if update["mode"] in VALID_MODES else "replace"
    if "builtin_overrides" in update:
        default["builtin_overrides"] = _clean_override_map(update["builtin_overrides"])
    if "custom_rules" in update:
        legacy = normalize({"custom_rules": update["custom_rules"]})
        cfg["custom_rules"] = legacy["custom_rules"]
        default["custom_overrides"] = legacy["policies"][DEFAULT_POLICY]["custom_overrides"]
    if "exempt_provider_ids" in update:
        exempt = {
            p.strip() for p in (update.get("exempt_provider_ids") or [])
            if isinstance(p, str) and p.strip()
        }
        kept = {prov: pol for prov, pol in cfg["provider_policies"].items() if pol != EXEMPT_POLICY}
        cfg["provider_policies"] = {**kept, **{prov: EXEMPT_POLICY for prov in exempt}}
    return normalize(cfg)


def policy_as_rule_set(cfg: Dict[str, Any], policy_id: str) -> Dict[str, Any]:
    """One policy expressed as the single-rule-set dict the resolver takes."""
    pol = cfg["policies"][policy_id]
    custom_rules = []
    for r in cfg["custom_rules"]:
        opts = pol["custom_overrides"].get(r["id"], {})
        custom_rules.append({
            "id": r["id"],
            "name": r["name"],
            "patterns": r["patterns"],
            "terms": r.get("terms") or [],
            "checksum": r.get("checksum"),
            "context": r.get("context") or [],
            "replacement": opts.get("replacement") or r["replacement"],
            "enabled": opts.get("enabled", True),
            "action": opts.get("action"),
        })
    return {
        "enabled": True,
        "mode": pol["mode"],
        "builtin_overrides": pol["builtin_overrides"],
        "custom_rules": custom_rules,
    }


def _resolve_detector_rules(pol: Dict[str, Any]) -> List[DetectorRule]:
    """The detector entities this policy turned on, with their options."""
    rules: List[DetectorRule] = []
    for spec in DETECTOR_ENTITIES:
        opts = pol["detector_overrides"].get(spec["id"], {})
        if not opts.get("enabled", False):
            continue
        rules.append(DetectorRule(
            id=f"{DETECTOR_PREFIX}{spec['id']}",
            name=spec["name"],
            entity=spec["id"],
            replacement=opts.get("replacement") or spec["replacement"],
            action=opts.get("action") if opts.get("action") in VALID_ACTIONS else pol["mode"],
            min_score=opts.get("min_score", default_min_score(spec["id"])),
            label=token_label(spec["id"]),
        ))
    return rules


def compile_policy(cfg: Dict[str, Any], policy_id: str) -> Optional[PiiRedactor]:
    """The redactor for one policy, or None when it has no active rule."""
    if policy_id == EXEMPT_POLICY or policy_id not in cfg["policies"]:
        return None
    pol = cfg["policies"][policy_id]
    rule_set = policy_as_rule_set(cfg, policy_id)
    rules = _resolve_rules(rule_set, pol["mode"])
    detector_rules = _resolve_detector_rules(pol)
    if not rules and not detector_rules:
        return None
    return PiiRedactor(
        mode=pol["mode"],
        rules=rules,
        detector_rules=detector_rules,
        on_detector_error=pol["on_detector_error"],
        policy_id=policy_id,
        policy_name=pol["name"],
        redact_context=pol["redact_context"],
    )


def _rule_state(overrides: Dict[str, Any], rid: str, mode: str, *, enabled_by_default: bool) -> Optional[Dict[str, Any]]:
    """A rule's effective options in one policy, or None when it is off."""
    ov = overrides.get(rid, {})
    if not ov.get("enabled", enabled_by_default):
        return None
    action = ov.get("action") if ov.get("action") in VALID_ACTIONS else mode
    return {"action": action, "replacement": ov.get("replacement"), "min_score": ov.get("min_score")}


def _merge_rule(states: List[Optional[Dict[str, Any]]], *, detector: bool, default_score: float = 0.7) -> Dict[str, Any]:
    on = [st for st in states if st is not None]
    if not on:
        return {"enabled": False}
    # Strongest action; on a tie the first policy in merge order keeps its token.
    winner = max(on, key=lambda st: ACTION_STRENGTH.get(st["action"], 0))
    out: Dict[str, Any] = {"enabled": True, "action": winner["action"]}
    if winner.get("replacement"):
        out["replacement"] = winner["replacement"]
    if detector:
        out["min_score"] = min(
            st["min_score"] if st.get("min_score") is not None else default_score for st in on
        )
    return out


def merge_policies(cfg: Dict[str, Any], policy_ids: List[str]) -> Dict[str, Any]:
    """One policy at least as strict as each of ``policy_ids`` (in a
    normalized config). Per rule: on if any input turns it on, with the
    strongest action (block > replace > pseudonymize > mask); detectors take
    the lowest ``min_score``; ``mode``, ``on_detector_error`` and
    ``redact_context`` take their strictest value. Every rule's options are
    written out explicitly, so the result compiles like any stored policy."""
    pols = [cfg["policies"][pid] for pid in policy_ids]
    builtin = {
        rid: _merge_rule(
            [_rule_state(p["builtin_overrides"], rid, p["mode"], enabled_by_default=BUILTIN_ON_BY_DEFAULT[rid]) for p in pols],
            detector=False,
        )
        for rid in sorted(BUILTIN_RULE_IDS)
    }
    custom = {
        r["id"]: _merge_rule(
            [_rule_state(p["custom_overrides"], r["id"], p["mode"], enabled_by_default=True) for p in pols],
            detector=False,
        )
        for r in cfg["custom_rules"]
    }
    detectors = {}
    for eid in sorted(DETECTOR_ENTITY_IDS):
        merged = _merge_rule(
            [_rule_state(p["detector_overrides"], eid, p["mode"], enabled_by_default=False) for p in pols],
            detector=True, default_score=default_min_score(eid),
        )
        if merged["enabled"]:
            detectors[eid] = merged
    return {
        "name": " + ".join(p["name"] for p in pols),
        "mode": "block" if any(p["mode"] == "block" for p in pols) else "replace",
        "redact_context": any(p["redact_context"] for p in pols),
        "builtin_overrides": builtin,
        "custom_overrides": custom,
        "detector_overrides": detectors,
        "on_detector_error": "block" if any(p["on_detector_error"] == "block" for p in pols) else "regex_only",
    }


@dataclass
class CompiledPiiConfig:
    """An org's compiled policies and provider / agent assignments."""

    enabled: bool
    policies: Dict[str, Optional[PiiRedactor]] = field(default_factory=dict)
    provider_policies: Dict[str, str] = field(default_factory=dict)
    display_policy: str = DEFAULT_POLICY
    agent_policies: Dict[str, str] = field(default_factory=dict)
    default_agent_policy: str = NO_AGENT_POLICY
    # The normalized config, for merging policies on demand.
    cfg: Dict[str, Any] = field(default_factory=dict)
    # Merged redactors, by the tuple of policy ids they merge.
    _merged: Dict[tuple, Optional[PiiRedactor]] = field(default_factory=dict)

    def policy_id_for_agent(self, agent_id: str) -> str:
        """The agent's own assignment, else the default agent policy."""
        return self.agent_policies.get(str(agent_id), self.default_agent_policy)

    def agent_policy_ids(self, agent_ids: Optional[Any]) -> List[str]:
        """Policies the agents in scope add to a call, sorted. ``None`` is an
        unknown scope: every policy any agent could have (fail closed)."""
        if agent_ids is None:
            ids = set(self.agent_policies.values()) | {self.default_agent_policy}
        else:
            ids = {self.policy_id_for_agent(a) for a in agent_ids}
        return sorted(pid for pid in ids if pid != NO_AGENT_POLICY and pid in self.policies)

    def policy_ids_for_call(self, provider_id: Optional[str], agent_ids: Optional[Any]) -> List[str]:
        """Provider policy first (unless Exempt), then the agents' policies."""
        ids: List[str] = []
        prov = self.policy_id_for_provider(provider_id)
        if prov != EXEMPT_POLICY:
            ids.append(prov)
        for pid in self.agent_policy_ids(agent_ids):
            if pid not in ids:
                ids.append(pid)
        return ids

    def for_call(self, provider_id: Optional[str], agent_ids: Optional[Any]) -> Optional[PiiRedactor]:
        """The redactor for a prompt sent to ``provider_id`` on behalf of
        ``agent_ids`` (``None``: unknown). An agent policy applies even when
        the provider is Exempt; None = send as is."""
        if not self.enabled:
            return None
        # A policy with no active rule (compiled to None) adds nothing.
        ids = [pid for pid in self.policy_ids_for_call(provider_id, agent_ids) if self.policies.get(pid) is not None]
        if not ids:
            return None
        if len(ids) == 1:
            return self.policies.get(ids[0])
        key = tuple(ids)
        if key not in self._merged:
            merged_id = "+".join(ids)
            cfg = {**self.cfg, "policies": {**self.cfg["policies"], merged_id: merge_policies(self.cfg, ids)}}
            merged = compile_policy(cfg, merged_id)
            if merged is not None:
                merged.resolve_overlaps = True
            self._merged[key] = merged
        return self._merged[key]

    def policy_id_for_provider(self, provider_id: Optional[str]) -> str:
        pol = self.provider_policies.get(str(provider_id)) if provider_id else None
        if pol == EXEMPT_POLICY:
            return EXEMPT_POLICY
        return pol if pol in self.policies else DEFAULT_POLICY

    def for_provider(self, provider_id: Optional[str]) -> Optional[PiiRedactor]:
        """The redactor for prompts sent to ``provider_id``; None = send as is."""
        if not self.enabled:
            return None
        pol = self.policy_id_for_provider(provider_id)
        if pol == EXEMPT_POLICY:
            return None
        return self.policies.get(pol)

    def for_display(self) -> Optional[PiiRedactor]:
        """The redactor that masks what people see in the app."""
        if not self.enabled:
            return None
        return self.policies.get(self.display_policy) or self.policies.get(DEFAULT_POLICY)


def compile_config(raw: Optional[Dict[str, Any]]) -> CompiledPiiConfig:
    cfg = normalize(raw)
    if not cfg["enabled"]:
        return CompiledPiiConfig(enabled=False)
    return CompiledPiiConfig(
        enabled=True,
        policies={pid: compile_policy(cfg, pid) for pid in cfg["policies"]},
        provider_policies=dict(cfg["provider_policies"]),
        display_policy=cfg["display_policy"],
        agent_policies=dict(cfg["agent_policies"]),
        default_agent_policy=cfg["default_agent_policy"],
        cfg=cfg,
    )


MAX_POLICY_NAME = 60
MAX_REPLACEMENT = 100


class PolicyValidationError(ValueError):
    """A settings write that cannot be saved (reported to the client as 400)."""


def validate_update(update: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> None:
    """Reject a policy-aware write that :func:`normalize` would otherwise
    quietly repair. Reads stay lenient; writes say what is wrong.
    ``existing`` is the stored config, used to check provider assignments in a
    write that does not resend the policies."""
    if "exempt_provider_ids" in update:
        exempt = update.get("exempt_provider_ids")
        if exempt is not None and (
            not isinstance(exempt, (list, tuple))
            or any(not isinstance(x, str) or not x.strip() for x in exempt)
        ):
            raise PolicyValidationError("exempt_provider_ids must be a list of provider ids.")
    if not any(k in update for k in ("policies", "provider_policies", "agent_policies", "default_agent_policy")):
        return
    policies = update.get("policies")
    if "policies" in update:
        if not isinstance(policies, dict) or DEFAULT_POLICY not in policies:
            raise PolicyValidationError("PII policies must include the Default policy.")
        if len(policies) > MAX_POLICIES:
            raise PolicyValidationError(f"At most {MAX_POLICIES} PII policies are allowed.")
        names = set()
        for pid, pol in policies.items():
            if pid == EXEMPT_POLICY or not isinstance(pid, str) or not POLICY_ID_RE.match(pid):
                raise PolicyValidationError(f"Invalid PII policy id: {pid!r}.")
            if not isinstance(pol, dict):
                raise PolicyValidationError(f"Invalid PII policy: {pid!r}.")
            name = pol.get("name")
            if pid != DEFAULT_POLICY:
                if not isinstance(name, str) or not name.strip():
                    raise PolicyValidationError("Every PII policy needs a name.")
                if len(name.strip()) > MAX_POLICY_NAME:
                    raise PolicyValidationError(f"PII policy names are at most {MAX_POLICY_NAME} characters.")
            key = (name or pid).strip().lower() if isinstance(name, str) else pid
            if key in names or (pid != EXEMPT_POLICY and key == EXEMPT_POLICY):
                raise PolicyValidationError(f"Duplicate PII policy name: {name!r}.")
            names.add(key)
            if pol.get("mode") not in (None,) + VALID_MODES:
                raise PolicyValidationError(f"Invalid mode for PII policy {pid!r}.")
            if pol.get("on_detector_error") not in (None,) + DETECTOR_ERROR_MODES:
                raise PolicyValidationError(f"Invalid on_detector_error for PII policy {pid!r}.")
            for field_name in ("builtin_overrides", "custom_overrides", "detector_overrides"):
                overrides = pol.get(field_name) or {}
                if not isinstance(overrides, dict):
                    raise PolicyValidationError(f"Invalid {field_name} for PII policy {pid!r}.")
                for rule_id, opts in overrides.items():
                    if not isinstance(opts, dict):
                        raise PolicyValidationError(f"Invalid rule options in PII policy {pid!r}.")
                    if opts.get("action") not in (None,) + VALID_ACTIONS:
                        raise PolicyValidationError(f"Invalid rule action in PII policy {pid!r}.")
                    if field_name == "detector_overrides":
                        if rule_id not in DETECTOR_ENTITY_IDS:
                            raise PolicyValidationError(f"Unknown detector entity {rule_id!r} in PII policy {pid!r}.")
                        score = opts.get("min_score")
                        if score is not None and (
                            isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1
                        ):
                            raise PolicyValidationError(f"min_score must be between 0 and 1 in PII policy {pid!r}.")
                    rep = opts.get("replacement")
                    if rep is not None and (not isinstance(rep, str) or len(rep) > MAX_REPLACEMENT):
                        raise PolicyValidationError(f"Invalid replacement token in PII policy {pid!r}.")
    if "provider_policies" in update:
        pp = update.get("provider_policies")
        if not isinstance(pp, dict):
            raise PolicyValidationError("provider_policies must map provider ids to policy ids.")
        known = set(policies.keys()) if isinstance(policies, dict) else set(normalize(existing)["policies"])
        for prov, pol in pp.items():
            if not isinstance(prov, str) or not prov.strip() or not isinstance(pol, str):
                raise PolicyValidationError("provider_policies must map provider ids to policy ids.")
            if pol in (EXEMPT_POLICY, DEFAULT_POLICY):
                continue
            if pol not in known:
                raise PolicyValidationError(f"Unknown PII policy {pol!r} assigned to a provider.")
    known = set(policies.keys()) if isinstance(policies, dict) else set(normalize(existing)["policies"])
    if "agent_policies" in update:
        ap = update.get("agent_policies")
        if not isinstance(ap, dict):
            raise PolicyValidationError("agent_policies must map agent ids to policy ids.")
        for agent_id, pid in ap.items():
            if not isinstance(agent_id, str) or not agent_id.strip() or not isinstance(pid, str):
                raise PolicyValidationError("agent_policies must map agent ids to policy ids.")
            if pid == EXEMPT_POLICY:
                raise PolicyValidationError("An agent can't be assigned Exempt: agent policies only add redaction.")
            if pid != NO_AGENT_POLICY and pid not in known:
                raise PolicyValidationError(f"Unknown PII policy {pid!r} assigned to an agent.")
    if "default_agent_policy" in update:
        dap = update.get("default_agent_policy")
        if not isinstance(dap, str) or dap == EXEMPT_POLICY or (dap != NO_AGENT_POLICY and dap not in known):
            raise PolicyValidationError(f"default_agent_policy must be {NO_AGENT_POLICY!r} or an existing policy.")


def diff_for_audit(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Audit entries for a change between two canonical configs: one per policy
    created / updated / deleted, one for provider assignments. Never includes
    patterns or values — only ids, names and which settings changed."""
    entries: List[Dict[str, Any]] = []
    old_p, new_p = old["policies"], new["policies"]
    for pid in new_p:
        if pid not in old_p:
            entries.append({"action": "pii_protection.policy_created",
                            "details": {"policy_id": pid, "policy_name": new_p[pid]["name"]}})
        elif new_p[pid] != old_p[pid]:
            changed = sorted(k for k in new_p[pid] if new_p[pid].get(k) != old_p[pid].get(k))
            entries.append({"action": "pii_protection.policy_updated",
                            "details": {"policy_id": pid, "policy_name": new_p[pid]["name"], "changed": changed}})
    for pid in old_p:
        if pid not in new_p:
            entries.append({"action": "pii_protection.policy_deleted",
                            "details": {"policy_id": pid, "policy_name": old_p[pid]["name"]}})
    old_a, new_a = old["provider_policies"], new["provider_policies"]
    changes = [
        {"provider_id": prov,
         "from": old_a.get(prov, DEFAULT_POLICY),
         "to": new_a.get(prov, DEFAULT_POLICY)}
        for prov in sorted(set(old_a) | set(new_a))
        if old_a.get(prov, DEFAULT_POLICY) != new_a.get(prov, DEFAULT_POLICY)
    ]
    if changes:
        entries.append({"action": "pii_protection.provider_policies_updated", "details": {"changes": changes}})
    old_ag, new_ag = old.get("agent_policies") or {}, new.get("agent_policies") or {}
    agent_changes = [
        {"agent_id": agent_id,
         "from": old_ag.get(agent_id, "inherit"),
         "to": new_ag.get(agent_id, "inherit")}
        for agent_id in sorted(set(old_ag) | set(new_ag))
        if old_ag.get(agent_id) != new_ag.get(agent_id)
    ]
    if agent_changes:
        entries.append({"action": "pii_protection.agent_policies_updated", "details": {"changes": agent_changes}})
    old_d = old.get("default_agent_policy") or NO_AGENT_POLICY
    new_d = new.get("default_agent_policy") or NO_AGENT_POLICY
    if old_d != new_d:
        entries.append({"action": "pii_protection.default_agent_policy_updated", "details": {"from": old_d, "to": new_d}})
    return entries
