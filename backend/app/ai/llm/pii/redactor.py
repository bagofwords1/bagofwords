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
from .context import has_markers, split_segments, strip_markers

logger = logging.getLogger(__name__)

# Guard against pathological inputs. Redaction runs on every prompt; a hard cap
# keeps a single enormous prompt from turning into a regex hot-loop. Prompts
# longer than this are redacted up to the cap and the tail is passed through
# (the tail is almost always data the model has already been shown elsewhere).
MAX_SCAN_CHARS = 2_000_000

VALID_MODES = ("replace", "block")


class PiiPromptBlockedError(Exception):
    """Raised in ``block`` mode when a prompt contains PII and must not be sent.

    Carries the matched rule names (never the values) and, once the LLM
    chokepoint has seen it, the model and policy, so the user is told which
    request was refused and why."""

    def __init__(
        self,
        rule_names: List[str],
        *,
        model: Optional[str] = None,
        policy_id: Optional[str] = None,
        policy_name: Optional[str] = None,
    ):
        self.rule_names = rule_names
        self.model = model
        self.policy_id = policy_id
        self.policy_name = policy_name
        super().__init__(self._message())

    def _message(self) -> str:
        joined = ", ".join(self.rule_names) if self.rule_names else "PII"
        target = f" to {self.model}" if self.model else ""
        return f"PII protection blocked this request{target} (rule: {joined})."

    def attach(self, *, model: Optional[str], policy_id: Optional[str], policy_name: Optional[str]) -> "PiiPromptBlockedError":
        self.model = model
        self.policy_id = policy_id
        self.policy_name = policy_name
        self.args = (self._message(),)
        return self


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
    """Compiled, reusable redactor for one PII policy."""

    def __init__(
        self,
        mode: str,
        rules: List[CompiledRule],
        *,
        policy_id: str = "default",
        policy_name: str = "Default",
        redact_context: bool = False,
    ):
        self.mode = mode if mode in VALID_MODES else "replace"
        self.rules = rules
        self.policy_id = policy_id
        self.policy_name = policy_name
        # False (default): regions marked with app.ai.llm.pii.context.pii_context
        # — instructions, schemas, platform context — are left as they are, and
        # only the prompt and the data are redacted.
        self.redact_context = redact_context

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def scan(self, text: str) -> RedactionResult:
        """Redact ``text`` and strip its context markers.

        Regions marked as context are passed through unless the policy also
        redacts context; everything else is scanned with :meth:`_scan_plain`.
        Matches are summed per rule across regions."""
        if not text or not self.rules:
            return RedactionResult(text=strip_markers(text), matches=[])
        if self.redact_context or not has_markers(text):
            return self._scan_plain(strip_markers(text))
        out: List[str] = []
        totals: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for is_context, segment in split_segments(text):
            if is_context:
                out.append(segment)
                continue
            result = self._scan_plain(segment)
            out.append(result.text)
            for m in result.matches:
                key = (m["id"], m["action"])
                if key in totals:
                    totals[key]["count"] += m["count"]
                else:
                    totals[key] = dict(m)
        return RedactionResult(text="".join(out), matches=list(totals.values()))

    def _scan_plain(self, text: str) -> RedactionResult:
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

    def redact_display(self, text: str) -> str:
        """Mask PII for *display* in the UI. Unlike ``scan``/``apply`` (which
        enforce the LLM boundary and honor block vs replace), this always
        replaces every rule's matches with its token — including block-action
        rules — so the rendered value never shows raw PII regardless of action.
        Returns the text unchanged when nothing matches."""
        if not text or not self.rules:
            return text
        head = text[:MAX_SCAN_CHARS]
        tail = text[MAX_SCAN_CHARS:]
        for rule in self.rules:
            for pattern in rule.patterns:
                try:
                    head = pattern.sub(rule.replacement, head)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("PII rule %s: display redaction error: %s", rule.id, exc)
        return head + tail

    def redact_deep(self, obj: Any) -> Any:
        """Recursively redact every string value in a nested dict/list for
        display — covers grid rows, ``info``/``stats`` column summaries, and
        tool-observation payloads (``result_json``) in one pass. Keys and
        non-string scalars are left untouched (column names like "Email" don't
        match PII patterns)."""
        if isinstance(obj, str):
            return self.redact_display(obj)
        if isinstance(obj, dict):
            return {k: self.redact_deep(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_deep(v) for v in obj]
        return obj

    def redact_grid(self, data: Any) -> Any:
        """Redact string cells in a widget-format grid ``{columns, rows, ...}``
        for display. Non-dict input and non-string cells pass through. The
        stored data is never mutated — callers pass a copy or the serialized
        view."""
        if not isinstance(data, dict):
            return data
        rows = data.get("rows")
        if not isinstance(rows, list) or not rows:
            return data
        new_rows = []
        for row in rows:
            if isinstance(row, dict):
                new_rows.append({
                    k: (self.redact_display(v) if isinstance(v, str) else v)
                    for k, v in row.items()
                })
            elif isinstance(row, list):
                new_rows.append([
                    (self.redact_display(v) if isinstance(v, str) else v) for v in row
                ])
            else:
                new_rows.append(row)
        return {**data, "rows": new_rows}

    def apply(self, prompt: str) -> Tuple[str, RedactionResult]:
        """Return the prompt to send + a result summary.

        Raises PiiPromptBlockedError if any *block*-action rule matched
        (nothing is sent). Otherwise returns the prompt with *replace*-action
        rules applied.
        """
        result = self.scan(prompt)
        if result.blocked_rules:
            raise PiiPromptBlockedError(
                result.blocked_rules, policy_id=self.policy_id, policy_name=self.policy_name
            )
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


def build_redactor(
    pii_config: Optional[Dict[str, Any]], policy_id: Optional[str] = None
) -> Optional[PiiRedactor]:
    """Build the redactor for one policy of an org's ``pii_protection`` config
    (the Default policy when ``policy_id`` is omitted).

    Returns None when protection is disabled, the policy is Exempt or unknown,
    or it has no active rule, so callers can cheaply skip the whole path.
    Enterprise licensing is enforced by the caller.
    """
    from .policy import DEFAULT_POLICY, compile_policy, normalize

    if not pii_config or not isinstance(pii_config, dict):
        return None
    cfg = normalize(pii_config)
    if not cfg["enabled"]:
        return None
    return compile_policy(cfg, policy_id or DEFAULT_POLICY)
