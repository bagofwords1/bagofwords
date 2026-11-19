"""Fake decision-model server for tests (an httpx transport, not a socket).

Serves both wire formats the app speaks:

- OpenAI Decisions: ``POST …/decisions`` with ``input`` + a ``questions`` array,
  answering an ``answers`` array in question order.
- System One: ``POST …/systemone`` with ``state`` + a ``questions`` map,
  answering an ``answers`` map keyed by question name.

Every request is recorded (path, headers, JSON body) so tests can assert what
actually went over the wire. Answers are scripted per question type:

    server = FakeDecisionServer()
    server.pick("model-b", confidence=0.91)    # choice questions pick this key
    server.fail(503)                           # or: every call errors
    server.delay(2.0)                          # or: every call stalls

Install it with the ``fake_decisions`` fixture in tests/e2e/test_decision_models.py
(it patches ``app.ai.llm.decisions.TRANSPORT_OVERRIDE``).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import httpx


class FakeDecisionServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._pick: Optional[str] = None
        self._confidence: float = 0.9
        self._status: Optional[int] = None
        self._delay: float = 0.0
        self._refuse: bool = False

    # ── scripting ────────────────────────────────────────────────────────
    def pick(self, value: str, confidence: float = 0.9) -> "FakeDecisionServer":
        self._pick, self._confidence = value, confidence
        return self

    def fail(self, status: int = 503) -> "FakeDecisionServer":
        self._status = status
        return self

    def delay(self, seconds: float) -> "FakeDecisionServer":
        self._delay = seconds
        return self

    def refuse(self) -> "FakeDecisionServer":
        self._refuse = True
        return self

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self._handle)

    # ── answering ────────────────────────────────────────────────────────
    def _choice_probs(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        top = self._pick if self._pick in keys else keys[0]
        rest = [k for k in keys if k != top]
        probs = {top: round(self._confidence, 4)}
        share = round((1 - self._confidence) / len(rest), 4) if rest else 0.0
        for k in rest:
            probs[k] = share
        return probs

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({
            "path": request.url.path,
            "url": str(request.url),
            "headers": dict(request.headers),
            "json": body,
        })
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._status:
            return httpx.Response(self._status, json={"error": {"message": "scripted failure"}})
        if request.url.path.endswith("/systemone"):
            return httpx.Response(200, json=self._systemone(body))
        if request.url.path.endswith("/decisions"):
            return httpx.Response(200, json=self._openai(body))
        return httpx.Response(404, json={"error": {"message": "unknown path"}})

    def _openai(self, body: dict) -> dict:
        answers = []
        for q in body.get("questions") or []:
            name, qtype = q.get("name"), q.get("type")
            if self._refuse:
                answers.append({"type": "refusal", "name": name})
            elif qtype == "predicate":
                answers.append({"type": "predicate", "name": name, "probability": self._confidence})
            elif qtype == "choice":
                keys = [str(c.get("value")) for c in q.get("choices") or []]
                probs = self._choice_probs(keys)
                top = max(probs, key=probs.get) if probs else None
                answers.append({
                    "type": "choice", "name": name, "choice": top,
                    "probabilities": [{"value": k, "probability": p} for k, p in probs.items()],
                    "confidence": self._confidence,
                })
            elif qtype == "score":
                levels = q.get("levels") or []
                answers.append({
                    "type": "score", "name": name, "score": 0.0,
                    "probabilities": [
                        {"value": i, "label": lv.get("label"), "probability": 1.0 if i == 0 else 0.0}
                        for i, lv in enumerate(levels)
                    ],
                    "confidence": self._confidence,
                })
        return {"model": body.get("model"), "answers": answers,
                "usage": {"input_tokens": 120, "output_tokens": 0, "total_tokens": 120}}

    def _systemone(self, body: dict) -> dict:
        answers: dict[str, Any] = {}
        for name, q in (body.get("questions") or {}).items():
            qtype = q.get("type")
            if self._refuse:
                continue
            if qtype == "noul":
                answers[name] = {"type": "noul", "noul": self._confidence}
            elif qtype == "choice":
                probs = self._choice_probs(list((q.get("criteria") or {}).keys()))
                top = max(probs, key=probs.get) if probs else None
                answers[name] = {"type": "choice", "choice": top,
                                 "confidence": self._confidence, "probabilities": probs}
            elif qtype == "score":
                crit = q.get("criteria") or []
                answers[name] = {
                    "type": "score", "score": 0.0, "confidence": self._confidence,
                    "legend": {str(i): c for i, c in enumerate(crit)},
                    "probabilities": {str(i): (1.0 if i == 0 else 0.0) for i in range(len(crit))},
                }
        return {"model": body.get("model"), "answers": answers,
                "usage": {"input_tokens": 110, "output_tokens": 5}}
