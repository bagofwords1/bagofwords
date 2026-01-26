from dataclasses import dataclass, field
from typing import Literal


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

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

