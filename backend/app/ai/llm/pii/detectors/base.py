"""Detector interface: something that finds PII spans in a piece of text.

Regex rules are matched inside the engine (``redactor.py``), where their
historical one-rule-after-another semantics live. A detector is for PII a
pattern cannot find — names, locations, organizations — and returns spans in
the coordinates of the text it was given.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence


@dataclass(frozen=True)
class DetectedSpan:
    start: int
    end: int
    entity: str   # e.g. "PERSON"
    score: float  # detector confidence, 0..1


class DetectorError(RuntimeError):
    """The detector could not analyze the text (model missing, service down,
    timeout). The policy decides what happens to the request."""


class Detector(Protocol):
    #: Short name for logs / traces / audit ("presidio-inproc", "presidio-http").
    name: str

    def supported_entities(self) -> List[str]:
        """Entity types this detector can find."""

    def detect(self, text: str, entities: Sequence[str], language: Optional[str] = None) -> List[DetectedSpan]:
        """Spans of ``entities`` in ``text``. Raises :class:`DetectorError`."""
