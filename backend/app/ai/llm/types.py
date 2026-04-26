from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Prompt caching (Anthropic native, OpenAI/Azure automatic, Bedrock-Claude).
    # cache_read_tokens: tokens served from cache (billed at provider's reduced rate).
    # cache_creation_tokens: tokens written to cache on this call (Anthropic charges
    # 1.25x normal input for these). Both are subsets of prompt_tokens conceptually,
    # though providers report them differently — see per-client _extract_usage.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


@dataclass
class LLMResponse:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass
class ImageInput:
    """Represents an image input for vision-capable models."""
    data: str  # base64-encoded image data or URL
    media_type: str = "image/png"  # MIME type: image/png, image/jpeg, image/gif, image/webp
    source_type: Literal["base64", "url"] = "base64"


# ---------------------------------------------------------------------------
# v2 streaming surface: native tool_use support
# Used by LLMClient.inference_stream_v2(). Provider-agnostic event vocabulary
# that each client maps to from its SDK's stream events.
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """A tool the model can call. Mapped to provider-specific tool format by client."""
    name: str
    description: str
    input_schema: dict  # JSON Schema for the tool's arguments


@dataclass
class ToolUseBlock:
    """A completed tool call emitted by the model."""
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    """The result of executing a tool, sent back to the model on the next turn."""
    tool_use_id: str
    content: str  # Stringified tool result (JSON-stringified or plain text)
    is_error: bool = False


@dataclass
class Message:
    """A message in the conversation history.

    `content` may be a plain string (text-only) or a list of typed blocks for
    mixed text/image/tool_use/tool_result content. Blocks are dicts with a
    'type' field; the client translates them to provider-native shapes.
    """
    role: Literal["user", "assistant"]
    content: Union[str, list[dict]]


# --- Streaming events ------------------------------------------------------


@dataclass
class TextDeltaEvent:
    type: Literal["text_delta"] = "text_delta"
    text: str = ""


@dataclass
class ToolUseStartEvent:
    """Emitted when the model begins a tool call. id+name known, args streaming."""
    id: str = ""
    name: str = ""
    type: Literal["tool_use_start"] = "tool_use_start"


@dataclass
class ToolUseInputDeltaEvent:
    """A fragment of the tool's argument JSON (string, not yet parsed)."""
    id: str = ""
    partial_json: str = ""
    type: Literal["tool_use_input_delta"] = "tool_use_input_delta"


@dataclass
class ToolUseCompleteEvent:
    """The tool call is fully assembled; input is parsed JSON dict."""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    type: Literal["tool_use_complete"] = "tool_use_complete"


@dataclass
class MessageStopEvent:
    """End of the message. stop_reason normalized across providers."""
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"] = "other"
    type: Literal["message_stop"] = "message_stop"


@dataclass
class UsageEvent:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    type: Literal["usage"] = "usage"


@dataclass
class ReasoningStartEvent:
    """Provider has begun emitting a reasoning / thinking block."""
    type: Literal["reasoning_start"] = "reasoning_start"


@dataclass
class ReasoningDeltaEvent:
    """A fragment of reasoning / thinking text from the provider's
    extended-thinking stream. Anthropic emits these as `thinking_delta`
    blocks; OpenAI Responses API as `response.reasoning_summary_text.delta`;
    Gemini as `Part(thought=true)` chunks.
    """
    text: str
    type: Literal["reasoning_delta"] = "reasoning_delta"


@dataclass
class ReasoningCompleteEvent:
    """Reasoning block finished. `signature` is provider-specific (Anthropic
    returns a cryptographic signature for verification + multi-turn replay)."""
    text: str
    signature: Optional[str] = None
    type: Literal["reasoning_complete"] = "reasoning_complete"


LLMStreamEvent = Union[
    TextDeltaEvent,
    ToolUseStartEvent,
    ToolUseInputDeltaEvent,
    ToolUseCompleteEvent,
    MessageStopEvent,
    UsageEvent,
    ReasoningStartEvent,
    ReasoningDeltaEvent,
    ReasoningCompleteEvent,
]

