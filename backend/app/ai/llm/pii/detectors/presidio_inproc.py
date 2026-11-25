"""Presidio in this process (optional ``pii`` extra).

Two engines:

* ``spacy`` — a spaCy NER model (``presidio_model``, e.g. ``en_core_web_sm`` /
  ``en_core_web_lg``). Fast on CPU, English.
* ``gliner`` — a GLiNER model (``presidio_model``, e.g.
  ``urchade/gliner_multi_pii-v1``; needs the ``gliner`` package). Slower, more
  accurate on names, multilingual (the only option here that reads Hebrew).

The model loads on first use, under a lock, so starting the backend costs
nothing and each worker loads it at most once. Next to the NER recognizer, the
pattern recognizers listed in :mod:`.catalog` are loaded (credit cards,
IBANs, national IDs, passports…); they need no model and run with either
engine.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from .base import DetectedSpan, DetectorError
from .catalog import NER_ENTITY_IDS, RECOGNIZER_ENTITIES

logger = logging.getLogger(__name__)

_SPACY_LABELS = {
    "PER": "PERSON", "PERSON": "PERSON",
    "LOC": "LOCATION", "GPE": "LOCATION", "FAC": "LOCATION",
    "ORG": "ORGANIZATION",
    "NORP": "NRP",
}
# GLiNER is prompted with plain-language labels.
_GLINER_LABELS = {
    "person": "PERSON",
    "location": "LOCATION",
    "organization": "ORGANIZATION",
    "nationality or religious or political group": "NRP",
}


def presidio_installed() -> bool:
    return importlib.util.find_spec("presidio_analyzer") is not None


_pattern_classes: Optional[List[Tuple[Dict[str, str], type]]] = None


def _pattern_recognizers() -> List[Tuple[Dict[str, str], type]]:
    """``(catalog entry, recognizer class)`` for each pattern entity the
    installed Presidio has. A version without one of them just lacks that
    entity."""
    global _pattern_classes
    if _pattern_classes is None:
        try:
            from presidio_analyzer import predefined_recognizers as pr
        except Exception:
            return []
        found = []
        for spec in RECOGNIZER_ENTITIES:
            cls = getattr(pr, spec["recognizer"], None)
            if cls is not None:
                found.append((spec, cls))
        _pattern_classes = found
    return _pattern_classes


def _whole_word_context():
    """Presidio's context enhancer, matching context words as whole words.
    Its default substring mode lets short context words hit inside ordinary
    ones ("us" in "customer" raises a 9-digit number to a US passport, "lic"
    in "duplicate" a code to a driver's license)."""
    from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer

    try:
        return LemmaContextAwareEnhancer(context_matching_mode="whole_word")
    except TypeError:  # pragma: no cover - a Presidio without the option
        return None


class PresidioInprocDetector:
    def __init__(self, *, engine: str = "spacy", model: str = "en_core_web_sm", language: str = "en"):
        self.engine = engine if engine in ("spacy", "gliner") else "spacy"
        self.model = model
        self.language = language or "en"
        self.name = f"presidio-inproc:{self.engine}:{self.model}"
        self._analyzer = None
        self._load_error: Optional[str] = None
        self._lock = threading.Lock()

    def supported_entities(self) -> List[str]:
        return NER_ENTITY_IDS + [e["id"] for e, _ in _pattern_recognizers()]

    def _load(self):
        if self._analyzer is not None:
            return self._analyzer
        with self._lock:
            if self._analyzer is not None:
                return self._analyzer
            if self._load_error is not None:
                # Do not retry a broken install on every call.
                raise DetectorError(self._load_error)
            try:
                self._analyzer = self._build()
            except Exception as exc:
                self._load_error = f"PII detector model could not be loaded ({self.engine}:{self.model}): {exc}"
                logger.error(self._load_error)
                raise DetectorError(self._load_error) from exc
            return self._analyzer

    def _build(self):
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        entities = list(NER_ENTITY_IDS)
        if self.engine == "gliner":
            # GLiNER does the NER; spaCy only tokenizes, so a blank pipeline is enough.
            import spacy
            from presidio_analyzer.nlp_engine import SpacyNlpEngine
            from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

            class _BlankSpacyNlpEngine(SpacyNlpEngine):
                def load(self_inner) -> None:  # noqa: N805
                    self_inner.nlp = {self.language: spacy.blank("xx" if self.language != "en" else "en")}

            nlp_engine = _BlankSpacyNlpEngine(models=[{"lang_code": self.language, "model_name": "blank"}])
            nlp_engine.load()
            registry = RecognizerRegistry(supported_languages=[self.language])
            registry.add_recognizer(GLiNERRecognizer(
                model_name=self.model,
                entity_mapping=dict(_GLINER_LABELS),
                supported_language=self.language,
                flat_ner=True,
                multi_label=False,
                threshold=0.3,
                map_location="cpu",
            ))
        else:
            from presidio_analyzer.predefined_recognizers import SpacyRecognizer

            nlp_engine = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": self.language, "model_name": self.model}],
                "ner_model_configuration": {
                    "labels_to_ignore": [],
                    "model_to_presidio_entity_mapping": dict(_SPACY_LABELS),
                    "low_confidence_score_multiplier": 0.4,
                    "low_score_entity_names": [],
                },
            }).create_engine()
            registry = RecognizerRegistry(supported_languages=[self.language])
            registry.add_recognizer(SpacyRecognizer(supported_language=self.language, supported_entities=entities))
        for spec, cls in _pattern_recognizers():
            try:
                registry.add_recognizer(cls(supported_language=self.language))
            except Exception as exc:  # pragma: no cover - a recognizer changed upstream
                logger.warning("PII detector: skipping %s (%s)", spec["recognizer"], exc)
        return AnalyzerEngine(
            nlp_engine=nlp_engine, registry=registry, supported_languages=[self.language],
            context_aware_enhancer=_whole_word_context(),
        )

    def detect(self, text: str, entities: Sequence[str], language: Optional[str] = None) -> List[DetectedSpan]:
        analyzer = self._load()
        try:
            results = analyzer.analyze(text=text, language=self.language, entities=list(entities))
        except Exception as exc:
            raise DetectorError(f"PII detector failed: {exc}") from exc
        return [DetectedSpan(r.start, r.end, r.entity_type, float(r.score)) for r in results]
