"""PII redaction engine applied to prompts before they leave for an LLM.

Design notes
------------
* A rule carries several regex patterns; a match is any pattern hitting. A
  rule may also require a checksum or nearby context words, and a custom rule
  may list exact terms (see :mod:`.checks`).
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
from .checks import terms_pattern, wrap
from .context import has_markers, split_segments, strip_markers

logger = logging.getLogger(__name__)

# Guard against pathological inputs. Redaction runs on every prompt; a hard cap
# keeps a single enormous prompt from turning into a regex hot-loop. Prompts
# longer than this are redacted up to the cap and the tail is passed through
# (the tail is almost always data the model has already been shown elsewhere).
MAX_SCAN_CHARS = 2_000_000

VALID_MODES = ("replace", "block")
# What one rule does with a match. ``mode`` (a policy's default) stays
# replace | block; ``mask`` and ``pseudonymize`` are chosen per rule.
VALID_ACTIONS = ("replace", "mask", "block", "pseudonymize")


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


class PiiDetectorUnavailableError(PiiPromptBlockedError):
    """The policy needs a PII detector that could not run, and says to refuse
    the request rather than send text nobody checked for names."""

    def __init__(self, reason: str, *, policy_id: Optional[str] = None, policy_name: Optional[str] = None):
        self.reason = reason
        super().__init__([], policy_id=policy_id, policy_name=policy_name)

    def _message(self) -> str:
        target = f" to {self.model}" if self.model else ""
        return f"PII protection blocked this request{target}: the PII detector is unavailable."


@dataclass
class CompiledRule:
    id: str
    name: str
    replacement: str
    patterns: List[re.Pattern]
    action: str = "replace"  # one of VALID_ACTIONS
    label: str = ""          # token label for ``pseudonymize`` (e.g. EMAIL)


@dataclass
class DetectorRule:
    """An entity found by a detector (Presidio), with one policy's options."""
    id: str            # "presidio.PERSON"
    name: str
    entity: str        # "PERSON"
    replacement: str
    action: str = "replace"
    min_score: float = 0.7
    label: str = ""


@dataclass
class RedactionResult:
    text: str
    # [{"id", "name", "count", "action"}] — never any raw matched value.
    matches: List[Dict[str, Any]] = field(default_factory=list)
    # The detector could not run and the policy chose to continue with the
    # pattern rules only (``on_detector_error: regex_only``).
    detector_failed: bool = False

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


def token_label(raw: str) -> str:
    """Token label from a rule id or name: ``us_ssn`` → ``US_SSN``."""
    label = re.sub(r"[^A-Za-z0-9]+", "_", raw or "").strip("_").upper()
    if not label or not label[0].isalpha():
        label = f"PII_{label}".strip("_")
    return label[:24].strip("_") or "PII"


def mask_value(value: str) -> str:
    """``4111 1111 1111 1234`` → ``**** **** **** 1234``: letters and digits
    become ``*`` except the last four (all of them when there are four or
    fewer); separators stay, so the shape remains recognizable."""
    positions = [i for i, ch in enumerate(value) if ch.isalnum()]
    keep = set(positions[-4:]) if len(positions) > 4 else set()
    return "".join("*" if (ch.isalnum() and i not in keep) else ch for i, ch in enumerate(value))


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Whether [a_start, a_end) touches the inside of [b_start, b_end). An
    empty match counts only strictly inside the other interval."""
    if a_start == a_end:
        return b_start < a_start < b_end
    return a_start < b_end and a_end > b_start


class PiiRedactor:
    """Compiled, reusable redactor for one PII policy."""

    def __init__(
        self,
        mode: str,
        rules: List[CompiledRule],
        *,
        detector_rules: Optional[List[DetectorRule]] = None,
        on_detector_error: str = "block",
        policy_id: str = "default",
        policy_name: str = "Default",
        redact_context: bool = False,
    ):
        self.mode = mode if mode in VALID_MODES else "replace"
        self.rules = rules
        self.detector_rules = list(detector_rules or [])
        self.on_detector_error = on_detector_error if on_detector_error in ("block", "regex_only") else "block"
        self.policy_id = policy_id
        self.policy_name = policy_name
        # False (default): regions marked with app.ai.llm.pii.context.pii_context
        # — instructions, schemas, platform context — are left as they are, and
        # only the prompt and the data are redacted.
        self.redact_context = redact_context
        # True for a policy merged from several (see policy.merge_policies):
        # overlapping pattern matches are resolved on the ORIGINAL text, the
        # strongest action winning, instead of rule after rule on edited text.
        # Otherwise an agent's mask on a built-in rule would rewrite a value
        # before the provider's stronger custom rule could match it, and adding
        # an agent would reveal more. Single policies keep the historical order.
        self.resolve_overlaps = False

    @property
    def active(self) -> bool:
        return bool(self.rules or self.detector_rules)

    @property
    def uses_detector(self) -> bool:
        """Whether scanning calls a detector (slower; keep it off the event loop)."""
        return bool(self.detector_rules)

    @property
    def uses_vault(self) -> bool:
        """Whether any rule produces reversible tokens."""
        return any(r.action == "pseudonymize" for r in self.rules) or any(
            r.action == "pseudonymize" for r in self.detector_rules
        )

    def scan(self, text: str, vault: Any = None) -> RedactionResult:
        """Redact ``text`` and strip its context markers.

        Regions marked as context are passed through unless the policy also
        redacts context (pattern rules only: a detector never runs on context,
        where a name-like column or table would be mangled). Everything else is
        scanned with :meth:`_scan_plain`. Matches are summed per rule.

        ``vault`` (app.ai.llm.pii.vault.PiiVault) supplies the reversible
        tokens for ``pseudonymize`` rules; without one, tokens are still
        produced but nothing can restore them."""
        if not text or not self.active:
            return RedactionResult(text=strip_markers(text), matches=[])
        if not has_markers(text):
            return self._scan_plain(text, vault, detect=True)
        out: List[str] = []
        totals: Dict[Tuple[str, str], Dict[str, Any]] = {}
        detector_failed = False
        for is_context, segment in split_segments(text):
            if is_context and not self.redact_context:
                out.append(segment)
                continue
            result = self._scan_plain(segment, vault, detect=not is_context)
            out.append(result.text)
            detector_failed = detector_failed or result.detector_failed
            for m in result.matches:
                key = (m["id"], m["action"])
                if key in totals:
                    totals[key]["count"] += m["count"]
                else:
                    totals[key] = dict(m)
        return RedactionResult(text="".join(out), matches=list(totals.values()), detector_failed=detector_failed)

    # -- the engine -----------------------------------------------------------

    def _detect(self, text: str) -> List[Tuple[int, int, DetectorRule, float]]:
        """Detector spans for this policy's entities, above each rule's
        ``min_score``. Raises DetectorError."""
        from .detectors import DetectorError, detect_cached, get_detector

        detector = get_detector()
        if detector is None:
            raise DetectorError("no PII detector is available on this instance")
        by_entity = {r.entity: r for r in self.detector_rules}
        spans = detect_cached(detector, text, sorted(by_entity))
        out = []
        for sp in spans:
            rule = by_entity.get(sp.entity)
            if rule is None or sp.score < rule.min_score or sp.end <= sp.start:
                continue
            out.append((sp.start, sp.end, rule, sp.score))
        return out

    def _replacement_for(self, action: str, replacement: str, label: str, value: str, vault: Any) -> str:
        if action == "mask":
            return mask_value(value)
        if action == "pseudonymize":
            return vault.token_for(label, value)
        return replacement

    def _scan_plain(self, text: str, vault: Any = None, *, detect: bool = True) -> RedactionResult:
        """Detect + redact one piece of text.

        1. **block** rules are checked against the *original* text (no
           mutation), so a replace rule can never mask a value a block rule
           should catch.
        2. Detector spans (names, locations…) are found on the original text.
        3. Pattern rules substitute one after another, in rule order — exactly
           the historical behavior — while the positions of the detector spans
           and of reversible tokens are kept up to date. A pattern rule wins
           over a detector span it overlaps; nothing matches inside a
           reversible token.
        4. The surviving detector spans are applied (longest first, then by
           score).

        Only reports block hits (each match carries its ``action``); the caller
        (:meth:`apply` / the v2 message walker) decides when to raise."""
        if not text or not self.active:
            return RedactionResult(text=text, matches=[])
        if vault is None and self.uses_vault:
            from .vault import PiiVault
            vault = PiiVault.ephemeral()

        head = text[:MAX_SCAN_CHARS]
        tail = text[MAX_SCAN_CHARS:]
        matches: List[Dict[str, Any]] = []
        detector_failed = False

        # 1. block detection (pattern rules) on the untouched text.
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

        # 2. detector spans on the untouched text.
        pending: List[Tuple[int, int, DetectorRule, float]] = []
        if detect and self.detector_rules:
            from .detectors import DetectorError
            try:
                spans = self._detect(head)
            except DetectorError as exc:
                if self.on_detector_error == "block":
                    raise PiiDetectorUnavailableError(
                        str(exc), policy_id=self.policy_id, policy_name=self.policy_name
                    ) from exc
                logger.warning("PII detector unavailable; continuing with pattern rules only: %s", exc)
                detector_failed = True
                spans = []
            block_counts: Dict[str, int] = {}
            for start, end, rule, score in spans:
                if rule.action == "block":
                    block_counts[rule.id] = block_counts.get(rule.id, 0) + 1
                else:
                    pending.append((start, end, rule, score))
            for rule in self.detector_rules:
                if block_counts.get(rule.id):
                    matches.append({"id": rule.id, "name": rule.name, "count": block_counts[rule.id], "action": "block"})

        # 3. pattern rules, one after another, tracking positions.
        protected: List[Tuple[int, int]] = []  # reversible tokens, current coordinates
        if self.resolve_overlaps:
            head, protected, pending, pattern_matches = self._apply_patterns_resolved(head, pending, vault)
            matches.extend(pattern_matches)
        for rule in ([] if self.resolve_overlaps else self.rules):
            if rule.action == "block":
                continue
            count = 0
            for pattern in rule.patterns:
                try:
                    edits: List[Tuple[int, int, str]] = []
                    for m in pattern.finditer(head):
                        s, e = m.span()
                        if any(_overlaps(s, e, a, b) for a, b in protected):
                            continue
                        if rule.action == "replace":
                            rep = m.expand(rule.replacement)
                        else:
                            rep = self._replacement_for(rule.action, rule.replacement, rule.label, m.group(0), vault)
                        edits.append((s, e, rep))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("PII rule %s: match error, skipping pattern: %s", rule.id, exc)
                    continue
                if not edits:
                    continue
                head, protected, pending = _apply_edits(
                    head, edits, protected, pending, protect_new=(rule.action == "pseudonymize")
                )
                count += len(edits)
            if count:
                matches.append({"id": rule.id, "name": rule.name, "count": count, "action": rule.action})

        # 4. detector spans that survived: longest first, then by score.
        if pending:
            accepted: List[Tuple[int, int, DetectorRule, float]] = []
            for span in sorted(pending, key=lambda x: (-(x[1] - x[0]), -x[3], x[0])):
                if any(span[0] < b and span[1] > a for a, b, _, _ in accepted):
                    continue
                accepted.append(span)
            counts: Dict[str, int] = {}
            for start, end, rule, _score in sorted(accepted, key=lambda x: -x[0]):
                rep = self._replacement_for(rule.action, rule.replacement, rule.label, head[start:end], vault)
                head = head[:start] + rep + head[end:]
                counts[rule.id] = counts.get(rule.id, 0) + 1
            for rule in self.detector_rules:
                if counts.get(rule.id):
                    matches.append({"id": rule.id, "name": rule.name, "count": counts[rule.id], "action": rule.action})

        return RedactionResult(text=head + tail, matches=matches, detector_failed=detector_failed)

    def _apply_patterns_resolved(self, head: str, pending: list, vault: Any):
        """Steps 3 and 4 for merged policies: every non-block pattern match and
        every detector span is taken on the original text, overlaps are
        resolved strongest action first (block > replace > pseudonymize >
        mask), then the longest match, then the higher detector score, then
        rule order, and the survivors are applied in one pass."""
        strength = {"mask": 1, "pseudonymize": 2, "replace": 3, "block": 4}
        # start, end, rule index, rule, regex match (None for a detector span), score
        found: List[Tuple[int, int, int, Any, Any, float]] = []
        for idx, rule in enumerate(self.rules):
            if rule.action == "block":
                continue
            for pattern in rule.patterns:
                try:
                    for m in pattern.finditer(head):
                        s, e = m.span()
                        if e > s:
                            found.append((s, e, idx, rule, m, 1.0))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("PII rule %s: match error, skipping pattern: %s", rule.id, exc)
        base = len(self.rules)
        for k, (start, end, drule, score) in enumerate(pending):
            found.append((start, end, base + k, drule, None, score))
        accepted: List[Tuple[int, int, int, Any, Any, float]] = []
        for cand in sorted(found, key=lambda f: (-strength.get(f[3].action, 0), -(f[1] - f[0]), -f[5], f[2], f[0])):
            if any(_overlaps(cand[0], cand[1], a[0], a[1]) for a in accepted):
                continue
            accepted.append(cand)
        accepted.sort(key=lambda f: f[0])
        edits: List[Tuple[int, int, str]] = []
        protect_flags: List[bool] = []
        counts: Dict[str, int] = {}
        for s, e, _idx, rule, m, _score in accepted:
            if m is None:  # detector span
                rep = self._replacement_for(rule.action, rule.replacement, rule.label, head[s:e], vault)
            elif rule.action == "replace":
                rep = m.expand(rule.replacement)
            else:
                rep = self._replacement_for(rule.action, rule.replacement, rule.label, m.group(0), vault)
            edits.append((s, e, rep))
            protect_flags.append(rule.action == "pseudonymize")
            counts[rule.id] = counts.get(rule.id, 0) + 1
        protected: List[Tuple[int, int]] = []
        if edits:
            # Every detector span was either applied here or overlapped one
            # that was: none are left for step 4.
            head, protected, _ = _apply_edits(head, edits, protected, [], protect_new=protect_flags)
        out = [{"id": r.id, "name": r.name, "count": counts[r.id], "action": r.action}
               for r in [*self.rules, *self.detector_rules] if counts.get(r.id)]
        return head, protected, [], out

    def redact_display(self, text: str) -> str:
        """Mask PII for *display* in the UI. Unlike ``scan``/``apply`` (which
        enforce the LLM boundary and honor each rule's action), this replaces
        every pattern rule's matches with its token — including block-action
        rules — so the rendered value never shows raw PII regardless of action.

        Two kinds of rule do not mask the display:

        * ``pseudonymize`` rules: their point is that only the model is denied
          the value; people who can open the report see it.
        * detector rules (names, locations…): they guard what is sent to the
          model. Running a language model over every table cell on every page
          load is not something display masking does.

        Returns the text unchanged when nothing matches."""
        if not text or not self.rules:
            return text
        head = text[:MAX_SCAN_CHARS]
        tail = text[MAX_SCAN_CHARS:]
        for rule in self.rules:
            if rule.action == "pseudonymize":
                continue
            for pattern in rule.patterns:
                try:
                    if rule.action == "mask":
                        head = pattern.sub(lambda m: mask_value(m.group(0)), head)
                    else:
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

    def apply(self, prompt: str, vault: Any = None) -> Tuple[str, RedactionResult]:
        """Return the prompt to send + a result summary.

        Raises PiiPromptBlockedError if any *block*-action rule matched
        (nothing is sent). Otherwise returns the prompt with *replace*-action
        rules applied.
        """
        result = self.scan(prompt, vault)
        if result.blocked_rules:
            raise PiiPromptBlockedError(
                result.blocked_rules, policy_id=self.policy_id, policy_name=self.policy_name
            )
        return result.text, result


def _apply_edits(
    text: str,
    edits: List[Tuple[int, int, str]],
    protected: List[Tuple[int, int]],
    pending: list,
    *,
    protect_new,
):
    """Apply non-overlapping, left-to-right ``(start, end, replacement)`` edits
    and move the tracked positions with them: ``protected`` intervals shift,
    ``pending`` detector spans shift or are dropped when an edit overlaps them.
    With ``protect_new`` the replacements become protected intervals; a list
    of booleans (one per edit) protects only the flagged ones."""
    out: List[str] = []
    pos = 0
    new_protected: List[Tuple[int, int]] = []
    shift = 0
    shifts: List[Tuple[int, int]] = []  # (edit_end_in_old_coords, cumulative_shift_after_it)
    for i, (s, e, rep) in enumerate(edits):
        out.append(text[pos:s])
        out.append(rep)
        if (protect_new[i] if isinstance(protect_new, list) else protect_new):
            new_start = s + shift
            new_protected.append((new_start, new_start + len(rep)))
        shift += len(rep) - (e - s)
        shifts.append((e, shift))
        pos = e
    out.append(text[pos:])

    def shift_at(position: int) -> int:
        total = 0
        for edit_end, cumulative in shifts:
            if edit_end <= position:
                total = cumulative
            else:
                break
        return total

    moved_protected = [(a + shift_at(a), b + shift_at(a)) for a, b in protected]
    moved_pending = []
    for start, end, rule, score in pending:
        if any(_overlaps(s, e, start, end) or (s <= start and e >= end and e > s) for s, e, _ in edits):
            continue
        d = shift_at(start)
        moved_pending.append((start + d, end + d, rule, score))
    return "".join(out), moved_protected + new_protected, moved_pending


def _resolve_action(raw_action: Any, default_mode: str) -> str:
    """A rule's effective action: its own choice, else the policy default."""
    return raw_action if raw_action in VALID_ACTIONS else default_mode


def _resolve_rules(pii_config: Dict[str, Any], default_mode: str) -> List[CompiledRule]:
    """Merge built-in rules (with per-org overrides) and custom rules into a
    compiled, enabled ruleset. Each rule's action is its own override, falling
    back to the workspace default ``mode``."""
    overrides = pii_config.get("builtin_overrides") or {}
    rules: List[CompiledRule] = []

    # Built-ins (code-defined patterns, org-overridable enable/replacement/action)
    for spec in BUILTIN_PII_RULES:
        ov = overrides.get(spec["id"], {}) if isinstance(overrides, dict) else {}
        enabled = ov.get("enabled", spec.get("enabled_by_default", True))
        if not enabled:
            continue
        replacement = ov.get("replacement") or spec["replacement"]
        action = _resolve_action(ov.get("action"), default_mode)
        compiled = wrap(
            _compile_patterns(spec["patterns"], rule_id=spec["id"]),
            validator=spec.get("checksum"), context=spec.get("context") or [],
        )
        if compiled:
            rules.append(CompiledRule(spec["id"], spec["name"], replacement, compiled, action, token_label(spec["id"])))

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
        patterns = list(raw.get("patterns") or [])
        terms = terms_pattern(raw.get("terms") or [])
        if terms:
            patterns.append(terms)
        compiled = wrap(
            _compile_patterns(patterns, rule_id=rid),
            validator=raw.get("checksum"), context=raw.get("context") or [],
        )
        if compiled:
            rules.append(CompiledRule(rid, name, replacement, compiled, action, token_label(name)))

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
