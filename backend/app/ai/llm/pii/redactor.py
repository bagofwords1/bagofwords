"""PII redaction engine applied to prompts before they leave for an LLM.

Design notes
------------
* A rule carries several regex patterns; a match is any pattern hitting.
* ``replace`` mode swaps every match with the rule's replacement token.
* ``block`` mode refuses the call (raises :class:`PiiPromptBlockedError`) if any
  rule matches — nothing is sent to the provider.
* The engine never records raw matched values. The audit summary carries only
  the rule id/name and a hit count, so redaction telemetry can't itself leak PII.
* Runtime robustness: a rule whose pattern throws at match time is skipped (and
  logged) rather than crashing the LLM call. Patterns are validated at save time
  (see :func:`validate_pattern`) so this should be rare.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .builtin_rules import BUILTIN_PII_RULES

logger = logging.getLogger(__name__)

# Guard against pathological inputs. Redaction runs on every prompt; a hard cap
# keeps a single enormous prompt from turning into a regex hot-loop. Prompts
# longer than this are redacted up to the cap and the tail is passed through
# (the tail is almost always data the model has already been shown elsewhere).
MAX_SCAN_CHARS = 2_000_000

VALID_MODES = ("replace", "block")


class PiiPromptBlockedError(Exception):
    """Raised in ``block`` mode when a prompt contains PII and must not be sent."""

    def __init__(self, rule_names: List[str]):
        self.rule_names = rule_names
        joined = ", ".join(rule_names) if rule_names else "PII"
        super().__init__(
            f"Prompt blocked by PII protection: detected {joined}."
        )


@dataclass
class CompiledRule:
    id: str
    name: str
    replacement: str
    patterns: List[re.Pattern]
    action: str = "replace"  # "replace" | "block"


@dataclass
class RedactionResult:
    text: str
    # [{"id", "name", "count", "action"}] — never any raw matched value.
    matches: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.matches)

    @property
    def blocked_rules(self) -> List[str]:
        """Names of matched rules whose action is ``block``."""
        return [m["name"] for m in self.matches if m.get("action") == "block"]


def validate_pattern(pattern: str) -> Optional[str]:
    """Return an error string if ``pattern`` is not a usable regex, else None."""
    if not isinstance(pattern, str) or pattern == "":
        return "pattern must be a non-empty string"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"invalid regex: {exc}"
    return None


def _compile_patterns(patterns: List[str], *, rule_id: str) -> List[re.Pattern]:
    compiled: List[re.Pattern] = []
    for pat in patterns or []:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            # Skip the bad pattern but keep the rest of the rule working.
            logger.warning("PII rule %s: skipping invalid pattern %r: %s", rule_id, pat, exc)
    return compiled


class PiiRedactor:
    """Compiled, reusable redactor for one organization's ruleset."""

    def __init__(self, mode: str, rules: List[CompiledRule]):
        self.mode = mode if mode in VALID_MODES else "replace"
        self.rules = rules

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def scan(self, text: str) -> RedactionResult:
        """Detect + redact in two phases so per-rule actions compose correctly:

        1. **block** rules are checked against the *original* text (no mutation),
           so a replace rule can never mask a value a block rule should catch.
        2. **replace** rules then substitute their token.

        This only reports block hits (each match carries its ``action``); the
        caller (:meth:`apply` / the v2 message walker) decides when to raise."""
        if not text or not self.rules:
            return RedactionResult(text=text, matches=[])

        head = text[:MAX_SCAN_CHARS]
        tail = text[MAX_SCAN_CHARS:]
        matches: List[Dict[str, Any]] = []

        # Phase 1: block detection on the untouched text.
        for rule in self.rules:
            if rule.action != "block":
                continue
            count = 0
            for pattern in rule.patterns:
                try:
                    count += len(pattern.findall(head))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("PII rule %s: match error, skipping pattern: %s", rule.id, exc)
            if count:
                matches.append({"id": rule.id, "name": rule.name, "count": count, "action": "block"})

        # Phase 2: replace rules mutate the text.
        for rule in self.rules:
            if rule.action == "block":
                continue
            count = 0
            for pattern in rule.patterns:
                try:
                    head, n = pattern.subn(rule.replacement, head)
                    count += n
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("PII rule %s: match error, skipping pattern: %s", rule.id, exc)
            if count:
                matches.append({"id": rule.id, "name": rule.name, "count": count, "action": "replace"})

        return RedactionResult(text=head + tail, matches=matches)

    def apply(self, prompt: str) -> Tuple[str, RedactionResult]:
        """Return the prompt to send + a result summary.

        Raises PiiPromptBlockedError if any *block*-action rule matched
        (nothing is sent). Otherwise returns the prompt with *replace*-action
        rules applied.
        """
        result = self.scan(prompt)
        if result.blocked_rules:
            raise PiiPromptBlockedError(result.blocked_rules)
        if not result.redacted:
            return prompt, result
        return result.text, result


def _resolve_action(raw_action: Any, default_mode: str) -> str:
    """A rule's effective action: its own choice, else the workspace default."""
    return raw_action if raw_action in ("replace", "block") else default_mode


def _resolve_rules(pii_config: Dict[str, Any], default_mode: str) -> List[CompiledRule]:
    """Merge built-in rules (with per-org overrides) and custom rules into a
    compiled, enabled ruleset. Each rule's action is its own override, falling
    back to the workspace default ``mode``."""
    overrides = pii_config.get("builtin_overrides") or {}
    rules: List[CompiledRule] = []

    # Built-ins (code-defined patterns, org-overridable enable/replacement/action)
    for spec in BUILTIN_PII_RULES:
        ov = overrides.get(spec["id"], {}) if isinstance(overrides, dict) else {}
        enabled = ov.get("enabled", True)
        if not enabled:
            continue
        replacement = ov.get("replacement") or spec["replacement"]
        action = _resolve_action(ov.get("action"), default_mode)
        compiled = _compile_patterns(spec["patterns"], rule_id=spec["id"])
        if compiled:
            rules.append(CompiledRule(spec["id"], spec["name"], replacement, compiled, action))

    # Custom (fully user-defined)
    for raw in pii_config.get("custom_rules") or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue
        rid = str(raw.get("id") or raw.get("name") or "custom")
        name = str(raw.get("name") or rid)
        replacement = raw.get("replacement") or "[REDACTED]"
        action = _resolve_action(raw.get("action"), default_mode)
        compiled = _compile_patterns(raw.get("patterns") or [], rule_id=rid)
        if compiled:
            rules.append(CompiledRule(rid, name, replacement, compiled, action))

    return rules


def build_redactor(pii_config: Optional[Dict[str, Any]]) -> Optional[PiiRedactor]:
    """Build a redactor from an org's ``pii_protection`` config dict.

    Returns None when protection is disabled or no rule is active, so callers
    can cheaply skip the whole path. Enterprise licensing is enforced by the
    caller (the redactor is only built for licensed instances).
    """
    if not pii_config or not isinstance(pii_config, dict):
        return None
    if not pii_config.get("enabled"):
        return None
    mode = pii_config.get("mode", "replace")
    if mode not in VALID_MODES:
        mode = "replace"
    rules = _resolve_rules(pii_config, mode)
    if not rules:
        return None
    return PiiRedactor(mode=mode, rules=rules)
