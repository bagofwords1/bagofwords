from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

_HOUR_MS = 3_600_000
# Header values end up in log lines: printable ASCII only (no CR/LF injection).
_HEADER_VALUE = r"^[\x20-\x7E]*$"


class StreamDeliveryReport(BaseModel):
    """What the browser measured about one live stream it received.

    ``holdback_ms`` is the longest time a token event was held back between
    the server writing it and the browser reading it (see
    ``app.streaming.delivery``). Header values are what the browser saw on the
    response, so they describe the last hop.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["kickoff", "watch"]
    holdback_ms: int = Field(ge=0, le=_HOUR_MS)
    server_span_ms: int = Field(ge=0, le=_HOUR_MS)
    token_events: int = Field(ge=0, le=10_000_000)
    network_reads: int = Field(ge=0, le=10_000_000)
    content_encoding: Optional[str] = Field(default=None, max_length=64, pattern=_HEADER_VALUE)
    via: Optional[str] = Field(default=None, max_length=256, pattern=_HEADER_VALUE)


class StreamDeliveryVerdict(BaseModel):
    buffered: bool
