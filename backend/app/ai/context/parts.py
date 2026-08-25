"""Typed conversation parts — the unit the agent transcript is built from.

Today the planner re-serializes prior tool results into one rebuilt user
message (`<past_observations>` / `<last_observation>`). These types are the
replacement: an append-only list of typed parts that renders to native
provider messages through a single seam.

Three ideas carry the design:

* **Two views of a result.** ``content`` is model-visible and budgeted;
  ``metadata`` is UI / audit only and never reaches the model or the
  compaction summarizer.
* **A decay ladder rather than one compaction step.** ``content`` (fresh) →
  ``digest`` (the tool's own compact form, computed once at execution time) →
  a summary part → dropped-but-referenceable. Decaying a part is a field
  swap, not an LLM call.
* **Provider-opaque state is carried, not invented.** ``signature`` +
  ``provider_name`` travel with the call/thinking that produced them and are
  replayed only to the provider that issued them — a mid-run fallback to a
  different provider must drop them rather than forward them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional, Union


class Outcome(str, Enum):
    """How a tool call ended.

    A field, not a heuristic: today this is re-derived by sniffing observation
    dicts (`_observation_failed`). ``denied`` and ``interrupted`` are real
    states the current shape cannot express — a rejected confirmation and a
    call cancelled mid-flight both look like "no result".
    """

    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    INTERRUPTED = "interrupted"
    OUTCOME_UNKNOWN = "outcome_unknown"


class Tier(int, Enum):
    """Position on the decay ladder. Lower is more detailed."""

    FULL = 0      # content, verbatim
    DIGEST = 1    # the tool's own compact form
    SUMMARY = 2   # folded into a summary part
    DROPPED = 3   # id only; re-fetchable via read_query / read_artifact / read_file


@dataclass
class TextPart:
    text: str
    kind: Literal["text"] = "text"


@dataclass
class ThinkingPart:
    """Provider-native reasoning. ``signature`` is opaque and provider-bound."""

    text: str
    signature: Optional[str] = None
    provider_name: Optional[str] = None
    kind: Literal["thinking"] = "thinking"


@dataclass
class ToolCallPart:
    id: str
    tool_name: str
    args: dict = field(default_factory=dict)
    # Gemini 3 returns a thought_signature on every function call and rejects
    # the call on replay without one; Anthropic and OpenAI have their own
    # opaque state. Carried here, replayed only to the issuing provider.
    signature: Optional[str] = None
    provider_name: Optional[str] = None
    kind: Literal["tool_call"] = "tool_call"


@dataclass
class ToolResultPart:
    call_id: str
    tool_name: str
    outcome: Outcome = Outcome.SUCCESS
    content: str = ""
    digest: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    tokens: int = 0
    tier: Tier = Tier.FULL
    images: list = field(default_factory=list)
    kind: Literal["tool_result"] = "tool_result"

    @property
    def is_error(self) -> bool:
        return self.outcome is not Outcome.SUCCESS

    def model_text(self) -> str:
        """The text the model should see at this part's current tier.

        DIGEST falls back to content when a tool declared no digest, so decay
        never blanks a result that has nothing shorter to say.

        A dropped result must still say *that it succeeded and was elided*.
        Returning an empty body made models read the gap as a failure — live
        probes answered "no row count returned (file may not exist or error
        occurred)" for a call that had in fact succeeded. Elision must never be
        mistakable for failure.
        """
        if self.tier is Tier.FULL:
            body = self.content
        elif self.tier is Tier.DIGEST:
            body = self.digest or self.content
        elif self.tier is Tier.SUMMARY:
            body = self.digest or self._elided()
        else:
            body = self.digest or self._elided()
        if self.outcome is Outcome.SUCCESS or not body:
            return body
        # Providers without a native error channel need the failure legible in
        # the text itself, or a retry loop cannot tell success from failure.
        return f"[{self.outcome.value}] {body}"

    def _elided(self) -> str:
        """Breadcrumb for a result whose body has aged out of context.

        States the outcome explicitly and names the tool, so the model knows the
        call ran, knows it worked, and knows how to get the data back rather
        than re-running the step blindly.
        """
        return (
            f"[{self.tool_name} completed successfully; its output has been elided "
            f"from context to save space. Re-run it or read the stored result "
            f"if you need the values again.]"
        )


Part = Union[TextPart, ThinkingPart, ToolCallPart, ToolResultPart]


@dataclass
class Turn:
    """One role-homogeneous group of parts.

    Turns exist so a decay boundary can be chosen without splitting a tool call
    from its result — the invariant every compaction step must preserve.
    """

    role: Literal["user", "assistant"]
    parts: list = field(default_factory=list)

    def tool_call_ids(self) -> set:
        return {p.id for p in self.parts if isinstance(p, ToolCallPart)}

    def tool_result_ids(self) -> set:
        return {p.call_id for p in self.parts if isinstance(p, ToolResultPart)}

    def tokens(self) -> int:
        total = 0
        for p in self.parts:
            if isinstance(p, ToolResultPart):
                total += p.tokens
            elif isinstance(p, (TextPart, ThinkingPart)):
                total += max(len(p.text) // 4, 0)
        return total


def estimate_tokens(text: str) -> int:
    """Cheap local size estimate, used only for budget decisions.

    Deliberately not the billing number: provider-reported usage stays the
    source of truth for cost and quota. This exists because the ladder has to
    know what a part costs *before* the call, which no provider can tell us.
    """
    return len(text) // 4 if text else 0
