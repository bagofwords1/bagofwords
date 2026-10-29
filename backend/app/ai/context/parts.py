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

import base64
import binascii
import json
import math
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
    # A native tool (submit_<list>, mcp__…) executes as its gateway
    # (submit_list, execute_mcp), and ``tool_name``/``args`` record the
    # gateway call. The model only knows the native tool, so replay renders
    # the call as it was issued — a gateway name the model never saw (and is
    # not in its catalog) reads as a call that did not happen, and it calls
    # again (BOW-103).
    replay_name: Optional[str] = None
    replay_args: Optional[dict] = None
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
    # How many of this result's images were taken out of context (retention
    # window, image cap, or budget decay). Non-zero makes model_text() say so,
    # so the model reads the gap as "elided, re-read if needed", not as a
    # failed read.
    images_released: int = 0
    # Cached sum of image_tokens() over ``images``; reset whenever the list
    # changes. The budget is recomputed on every planner call.
    _image_cost: Optional[int] = field(default=None, repr=False)
    kind: Literal["tool_result"] = "tool_result"

    @property
    def is_error(self) -> bool:
        return self.outcome is not Outcome.SUCCESS

    def image_tokens(self) -> int:
        """Estimated prompt cost of the images this part still carries."""
        if not self.images:
            return 0
        if self._image_cost is None:
            self._image_cost = sum(image_tokens(img) for img in self.images)
        return self._image_cost

    def release_images(self, keep: int = 0) -> int:
        """Take all but the first ``keep`` images out of context, for good.

        The part's text gains a note (see model_text) and its token count is
        recomputed. Returns how many images were released.
        """
        keep = max(int(keep), 0)
        if len(self.images) <= keep:
            return 0
        released = len(self.images) - keep
        # Reassign rather than slice in place: per-request copies of a part
        # (copy.copy) share the list object with the live transcript.
        self.images = list(self.images[:keep])
        self.images_released += released
        self._image_cost = None
        if isinstance(self.metadata, dict) and "images" in self.metadata:
            # The metadata view holds the same base64. It never reaches the
            # model, but keeping it would pin the bytes for the whole run.
            self.metadata = {k: v for k, v in self.metadata.items() if k != "images"}
            self.metadata["images_released"] = self.images_released
        self.tokens = estimate_tokens(self.model_text())
        return released

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
        if self.images_released and body:
            body = f"{body}\n{self._images_released_note()}"
        if self.outcome is Outcome.SUCCESS or not body:
            return body
        # Providers without a native error channel need the failure legible in
        # the text itself, or a retry loop cannot tell success from failure.
        return f"[{self.outcome.value}] {body}"

    def _images_released_note(self) -> str:
        """Says the pictures were removed on purpose, and how to get them back.

        Without it a read whose pages vanished looks like a read that returned
        nothing, and the model either reports a failure or guesses.
        """
        if self.tool_name == "read_file":
            how = "call read_file again with the same file_id and page_range"
        else:
            how = f"call {self.tool_name} again with the same arguments"
        return (
            f"[{self.images_released} page image(s) from this result were removed "
            f"from context to save space; the call itself completed. To look at "
            f"them again, {how}.]"
        )

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
                total += p.tokens + p.image_tokens()
            elif isinstance(p, (TextPart, ThinkingPart)):
                total += estimate_tokens(p.text)
            elif isinstance(p, ToolCallPart):
                # The arguments are replayed to the provider on every later
                # iteration — create_data / edit_artifact code routinely runs
                # to tens of thousands of characters. Leaving them out made the
                # budget blind to the largest parts of a long run.
                total += estimate_tokens(json.dumps(p.args, default=str)) if p.args else 0
        return total


def estimate_tokens(text: str) -> int:
    """Cheap local size estimate, used only for budget decisions.

    Deliberately not the billing number: provider-reported usage stays the
    source of truth for cost and quota. This exists because the ladder has to
    know what a part costs *before* the call, which no provider can tell us.

    Script-aware, because ``len(text) // 4`` is an English ratio. Measured with
    tiktoken on a Hebrew contract paragraph (188 Hebrew + 77 ASCII chars):
    cl100k_base 253 tokens, o200k_base 104, against 66 from chars/4 — a Hebrew
    run was undercounted 1.6-4x, so the ladder decayed far too late. Arabic,
    Cyrillic and CJK measure in the same band (0.4-1.4 tokens per char).
    Hence:

    * ASCII: 4 chars per token, unchanged (English prose measures ~4.0).
    * each non-ASCII char: 1 token. Matches cl100k-class tokenizers, ~2x high
      for o200k. Over-estimating only decays a little early; under-estimating
      is what overflows the window.
    * each ``\\uXXXX`` escape: +2 tokens on top of its 6 ASCII chars. Tool
      results are ``json.dumps``'d with the default ``ensure_ascii=True``, so
      Hebrew values reach the model as escapes; they measured ~3.7 tokens
      apiece on both encodings, which chars/4 alone counts as 1.5.

    C-speed builtins only (``isascii`` / ``encode`` / ``count``): this runs
    over the whole transcript on every planner call.
    """
    if not text:
        return 0
    n = len(text)
    ascii_chars = n if text.isascii() else len(text.encode("ascii", "ignore"))
    escapes = text.count("\\u")
    return ascii_chars // 4 + (n - ascii_chars) + 2 * escapes


# -- images --------------------------------------------------------------

# Vision cost per the Anthropic docs: one token per 28x28 px patch, after the
# provider's downscale to a 1568 px long edge (rendered pages are already
# capped there by image_utils.VISION_MAX_EDGE_PX). A portrait page at 1568 px
# is ~2,400 tokens. OpenAI's tile pricing is cheaper for the same image, so
# this errs high on purpose.
IMAGE_PATCH_PX = 28
IMAGE_MAX_EDGE_PX = 1568
# When an image's size cannot be read (URL source, unknown format): assume the
# largest image a provider accepts unscaled, ceil(1568/28)^2 = 3,136.
IMAGE_TOKENS_FALLBACK = math.ceil(IMAGE_MAX_EDGE_PX / IMAGE_PATCH_PX) ** 2

# Enough base64 to reach a JPEG's SOF header past typical EXIF/ICC segments.
_HEADER_B64_CHARS = 64 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _image_b64(img: Any) -> Optional[str]:
    if not isinstance(img, dict):
        return None
    src = img.get("source") if isinstance(img.get("source"), dict) else img
    if src.get("type") == "url" or src.get("source_type") == "url":
        return None
    data = src.get("data")
    return data if isinstance(data, str) and data else None


def image_dimensions(img: Any) -> Optional[tuple]:
    """(width, height) from a base64 PNG/JPEG header, without decoding the
    image. None when the source or format is not one we can read."""
    data = _image_b64(img)
    if not data:
        return None
    chunk = data[:_HEADER_B64_CHARS]
    chunk = chunk[: len(chunk) - (len(chunk) % 4)]
    try:
        head = base64.b64decode(chunk)
    except (binascii.Error, ValueError):
        return None
    if head[:8] == _PNG_MAGIC and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                return None
            marker = head[i + 1]
            if marker == 0xFF:  # fill byte
                i += 1
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(head[i + 5:i + 7], "big")
                w = int.from_bytes(head[i + 7:i + 9], "big")
                return (w, h) if w and h else None
            i += 2 + int.from_bytes(head[i + 2:i + 4], "big")
    return None


def image_tokens(img: Any) -> int:
    """Estimated prompt tokens for one image (see IMAGE_PATCH_PX)."""
    dims = image_dimensions(img)
    if not dims:
        return IMAGE_TOKENS_FALLBACK
    w, h = dims
    long_edge = max(w, h)
    if long_edge > IMAGE_MAX_EDGE_PX:
        scale = IMAGE_MAX_EDGE_PX / long_edge
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
    return math.ceil(w / IMAGE_PATCH_PX) * math.ceil(h / IMAGE_PATCH_PX)
