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
    display_policy: default

* ``builtin_overrides`` / ``custom_overrides`` map a rule id to
  ``{enabled, action, replacement}``; a missing key keeps the rule's default
  (enabled, the policy's ``mode``, the rule's own token).
* ``exempt`` is built in: it has no rules, is never stored under ``policies``,
  and is only ever a provider assignment.
* ``redact_context`` (default False) also redacts instructions, schemas and the
  other platform context (see :mod:`.context`); by default only the prompt and
  the data are redacted.

Before named policies the config was one rule set: ``mode``,
``builtin_overrides``, per-rule ``enabled``/``action``/``replacement`` on each
custom rule, and (BOW-152) ``exempt_provider_ids``. :func:`normalize` turns that
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
from .redactor import VALID_MODES, PiiRedactor, _resolve_rules

DEFAULT_POLICY = "default"
EXEMPT_POLICY = "exempt"
RESERVED_POLICY_IDS = (DEFAULT_POLICY, EXEMPT_POLICY)
POLICY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
MAX_POLICIES = 20
RULE_OPTION_KEYS = ("enabled", "action", "replacement")
BUILTIN_RULE_IDS = frozenset(spec["id"] for spec in BUILTIN_PII_RULES)


def custom_rule_id(raw: Dict[str, Any]) -> str:
    """A custom rule's id, derived exactly as the pre-policy resolver did."""
    return str(raw.get("id") or raw.get("name") or "custom")


def _clean_options(raw: Any) -> Dict[str, Any]:
    """Keep only the per-rule option keys, with sane types."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    if raw.get("action") in ("replace", "block"):
        out["action"] = raw["action"]
    if isinstance(raw.get("replacement"), str) and raw["replacement"]:
        out["replacement"] = raw["replacement"]
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
        # Single rule set: it becomes the Default policy, unchanged.
        policies[DEFAULT_POLICY] = _clean_policy(
            {
                "name": "Default",
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
    display = raw.get("display_policy")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "custom_rules": custom_rules,
        "policies": policies,
        "provider_policies": provider_policies,
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


def compile_policy(cfg: Dict[str, Any], policy_id: str) -> Optional[PiiRedactor]:
    """The redactor for one policy, or None when it has no active rule."""
    if policy_id == EXEMPT_POLICY or policy_id not in cfg["policies"]:
        return None
    pol = cfg["policies"][policy_id]
    rule_set = policy_as_rule_set(cfg, policy_id)
    rules = _resolve_rules(rule_set, pol["mode"])
    if not rules:
        return None
    return PiiRedactor(
        mode=pol["mode"],
        rules=rules,
        policy_id=policy_id,
        policy_name=pol["name"],
        redact_context=pol["redact_context"],
    )


@dataclass
class CompiledPiiConfig:
    """An org's compiled policies and provider assignments."""

    enabled: bool
    policies: Dict[str, Optional[PiiRedactor]] = field(default_factory=dict)
    provider_policies: Dict[str, str] = field(default_factory=dict)
    display_policy: str = DEFAULT_POLICY

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
    if "policies" not in update and "provider_policies" not in update:
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
            for field_name in ("builtin_overrides", "custom_overrides"):
                overrides = pol.get(field_name) or {}
                if not isinstance(overrides, dict):
                    raise PolicyValidationError(f"Invalid {field_name} for PII policy {pid!r}.")
                for opts in overrides.values():
                    if not isinstance(opts, dict):
                        raise PolicyValidationError(f"Invalid rule options in PII policy {pid!r}.")
                    if opts.get("action") not in (None, "replace", "block"):
                        raise PolicyValidationError(f"Invalid rule action in PII policy {pid!r}.")
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
    return entries
