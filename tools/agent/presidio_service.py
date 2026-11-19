#!/usr/bin/env python3
"""A Presidio analyzer service with the official container's REST API
(``POST /analyze``, ``GET /health``), for local loops and as a reference for
serving a GLiNER model.

BoW talks to it through ``pii.presidio_url`` (BOW_PII_PRESIDIO_URL). In
production use the official image for spaCy models:

    docker run -p 5002:3000 mcr.microsoft.com/presidio-analyzer

This script exists because (a) the sandbox has no Docker, and (b) GLiNER —
the multilingual option, and the only one here that reads Hebrew — needs
packages that conflict with the backend's pinned ones, so it must run in its
own environment. Run it from a venv that has:

    pip install presidio-analyzer fastapi uvicorn          # + a spaCy model
    pip install gliner sentencepiece                        # for --engine gliner

    python tools/agent/presidio_service.py --engine spacy  --model en_core_web_lg --port 5002
    python tools/agent/presidio_service.py --engine gliner --model urchade/gliner_multi_pii-v1 --port 5003

Only NER entities are served (PERSON, LOCATION, ORGANIZATION, NRP): BoW's
built-in pattern rules cover the rest.
"""
import argparse

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

ENTITIES = ["PERSON", "LOCATION", "ORGANIZATION", "NRP"]
SPACY_LABELS = {"PER": "PERSON", "PERSON": "PERSON", "LOC": "LOCATION", "GPE": "LOCATION",
                "FAC": "LOCATION", "ORG": "ORGANIZATION", "NORP": "NRP"}
GLINER_LABELS = {"person": "PERSON", "location": "LOCATION", "organization": "ORGANIZATION",
                 "nationality or religious or political group": "NRP"}


def build_analyzer(engine: str, model: str, language: str = "en"):
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider, SpacyNlpEngine

    registry = RecognizerRegistry(supported_languages=[language])
    if engine == "gliner":
        import spacy
        from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

        class BlankNlp(SpacyNlpEngine):
            def load(self):
                self.nlp = {language: spacy.blank("en" if language == "en" else "xx")}

        nlp = BlankNlp(models=[{"lang_code": language, "model_name": "blank"}])
        nlp.load()
        registry.add_recognizer(GLiNERRecognizer(
            model_name=model, entity_mapping=dict(GLINER_LABELS), supported_language=language,
            flat_ner=True, multi_label=False, threshold=0.3, map_location="cpu"))
    else:
        from presidio_analyzer.predefined_recognizers import SpacyRecognizer
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": language, "model_name": model}],
            "ner_model_configuration": {"labels_to_ignore": [], "model_to_presidio_entity_mapping": dict(SPACY_LABELS),
                                        "low_confidence_score_multiplier": 0.4, "low_score_entity_names": []},
        }).create_engine()
        registry.add_recognizer(SpacyRecognizer(supported_language=language, supported_entities=ENTITIES))
    return AnalyzerEngine(nlp_engine=nlp, registry=registry, supported_languages=[language])


class AnalyzeRequest(BaseModel):
    text: str
    language: str = "en"
    entities: list[str] | None = None
    score_threshold: float | None = None


def create_app(engine: str, model: str) -> FastAPI:
    app = FastAPI()
    analyzer = build_analyzer(engine, model)

    @app.get("/health")
    def health():
        return "Presidio Analyzer service is up"

    @app.post("/analyze")
    def analyze(req: AnalyzeRequest):
        results = analyzer.analyze(text=req.text, language="en", entities=req.entities or ENTITIES,
                                   score_threshold=req.score_threshold)
        return [{"entity_type": r.entity_type, "start": r.start, "end": r.end, "score": float(r.score)} for r in results]

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["spacy", "gliner"], default="spacy")
    ap.add_argument("--model", default="en_core_web_lg")
    ap.add_argument("--port", type=int, default=5002)
    a = ap.parse_args()
    uvicorn.run(create_app(a.engine, a.model), host="127.0.0.1", port=a.port, log_level="warning")
