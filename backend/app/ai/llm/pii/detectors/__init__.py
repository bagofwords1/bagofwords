"""PII detectors (names, locations, organizations) and the registry that picks
the one this instance uses.

* ``pii.presidio_url`` set in bow-config → the Presidio analyzer service over
  HTTP (:mod:`.presidio_http`).
* otherwise, when the optional ``pii`` extra is installed → Presidio in this
  process (:mod:`.presidio_inproc`).
* otherwise → no detector; policies cannot enable detector rules.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import List, Optional, Sequence, Tuple

from .base import DetectedSpan, Detector, DetectorError
from .catalog import DETECTOR_ENTITIES, detector_entity

logger = logging.getLogger(__name__)

__all__ = [
    "DETECTOR_ENTITIES", "DetectedSpan", "Detector", "DetectorError",
    "detect_cached", "detector_entity", "detector_status", "get_detector", "set_detector_for_tests",
]

_lock = threading.Lock()
_detector: Optional[Detector] = None
_resolved = False

# The planner re-sends the whole conversation on every step, so the same tool
# result is scanned again and again. Spans are cached by content.
_CACHE_MAX = 2048
_cache: "OrderedDict[Tuple[str, str, str], List[DetectedSpan]]" = OrderedDict()
_cache_lock = threading.Lock()

# Text is analyzed in chunks so a very large tool result neither exceeds the
# NLP model's limits nor goes partly unscanned.
CHUNK_CHARS = 20_000


def _pii_config():
    from app.settings.config import settings
    return getattr(settings.bow_config, "pii", None)


def get_detector() -> Optional[Detector]:
    """The detector for this instance, or None. Resolved once."""
    global _detector, _resolved
    if _resolved:
        return _detector
    with _lock:
        if _resolved:
            return _detector
        cfg = _pii_config()
        url = getattr(cfg, "presidio_url", None)
        try:
            if url:
                from .presidio_http import PresidioHttpDetector
                _detector = PresidioHttpDetector(
                    url,
                    timeout=getattr(cfg, "presidio_timeout_seconds", 10.0),
                    language=getattr(cfg, "presidio_language", "en"),
                )
            else:
                from .presidio_inproc import PresidioInprocDetector, presidio_installed
                if presidio_installed():
                    _detector = PresidioInprocDetector(
                        engine=getattr(cfg, "presidio_engine", "spacy"),
                        model=getattr(cfg, "presidio_model", "en_core_web_sm"),
                        language=getattr(cfg, "presidio_language", "en"),
                    )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("PII detector could not be configured: %s", exc)
            _detector = None
        _resolved = True
        return _detector


def set_detector_for_tests(detector: Optional[Detector], *, resolved: bool = True) -> None:
    """Install (or clear, with ``resolved=False``) the detector. Tests only."""
    global _detector, _resolved
    with _lock:
        _detector = detector
        _resolved = resolved
    with _cache_lock:
        _cache.clear()


def detector_status() -> dict:
    """What the settings page needs: is a detector available, which one, and
    which entities it can find."""
    detector = get_detector()
    if detector is None:
        return {"available": False, "name": None, "entities": []}
    try:
        supported = set(detector.supported_entities())
    except Exception:
        supported = set()
    return {
        "available": True,
        "name": detector.name,
        "entities": [e for e in DETECTOR_ENTITIES if not supported or e["id"] in supported],
    }


def _chunks(text: str) -> List[Tuple[int, str]]:
    """``(offset, chunk)`` pieces of at most CHUNK_CHARS, cut at line ends
    (else at spaces) so no word is split."""
    if len(text) <= CHUNK_CHARS:
        return [(0, text)]
    out: List[Tuple[int, str]] = []
    pos = 0
    while pos < len(text):
        end = min(pos + CHUNK_CHARS, len(text))
        if end < len(text):
            cut = text.rfind("\n", pos + CHUNK_CHARS // 2, end)
            if cut == -1:
                cut = text.rfind(" ", pos + CHUNK_CHARS // 2, end)
            if cut != -1:
                end = cut + 1
        out.append((pos, text[pos:end]))
        pos = end
    return out


def detect_cached(detector: Detector, text: str, entities: Sequence[str]) -> List[DetectedSpan]:
    """``detector.detect`` over the whole text (chunked), cached by content.
    Raises DetectorError; failures are never cached."""
    if not text or not text.strip() or not entities:
        return []
    key = (detector.name, ",".join(entities), hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest())
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    spans: List[DetectedSpan] = []
    for offset, chunk in _chunks(text):
        for sp in detector.detect(chunk, entities):
            spans.append(DetectedSpan(sp.start + offset, sp.end + offset, sp.entity, sp.score))
    with _cache_lock:
        _cache[key] = spans
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return spans
