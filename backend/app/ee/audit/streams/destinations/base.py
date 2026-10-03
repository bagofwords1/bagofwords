# Audit stream destination contract
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

OK = "ok"
RETRYABLE = "retryable"  # transient: back off, stay active
INVALID = "invalid"      # credentials/config rejected: stop until fixed
FATAL = "fatal"          # non-retryable delivery error: stop until fixed

_MAX_ERROR_LEN = 500


@dataclass
class SendResult:
    kind: str
    error: Optional[str] = None
    status: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.kind == OK

    @classmethod
    def success(cls, status: Optional[int] = None) -> "SendResult":
        return cls(OK, None, status)


def short_error(text: Any) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s[:_MAX_ERROR_LEN]


def classify_http(status: int, body: str = "", *, invalid=(401, 403), retryable_extra=()) -> SendResult:
    """Default HTTP status classification shared by the HTTP-family senders."""
    if 200 <= status < 300:
        return SendResult.success(status)
    err = short_error(f"HTTP {status}: {body}")
    if status in invalid:
        return SendResult(INVALID, err, status)
    if status in (408, 425, 429) or status >= 500 or status in retryable_extra:
        return SendResult(RETRYABLE, err, status)
    return SendResult(FATAL, err, status)


@dataclass
class FieldSpec:
    """One form field. ``secret`` fields are stored encrypted and never returned."""
    key: str
    kind: str = "text"  # text | url | number | bool | select | textarea | headers
    required: bool = False
    secret: bool = False
    default: Any = None
    options: List[str] = field(default_factory=list)
    advanced: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "kind": self.kind, "required": self.required, "secret": self.secret,
            "default": self.default, "options": self.options, "advanced": self.advanced,
        }


class Destination:
    """Sends batches of envelope-v1 dicts to one external intake."""

    type: str = ""
    max_batch: int = 500
    fields: List[FieldSpec] = []

    def __init__(self, config: Dict[str, Any], secrets: Dict[str, Any]):
        self.config = dict(config or {})
        self.secrets = dict(secrets or {})

    def cfg(self, key: str, default: Any = None) -> Any:
        v = self.config.get(key)
        if v in (None, ""):
            for f in self.fields:
                if f.key == key and f.default is not None:
                    return f.default
            return default
        return v

    async def send(self, events: List[dict]) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:
        return None
