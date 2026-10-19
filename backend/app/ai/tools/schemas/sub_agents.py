"""Schemas for the sub-agent tools: delegate, finish_subtask, read_sub_agent, stop_sub_agent."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _null_if_blank(v):
    # Weaker models send "" for an argument they meant to omit (B9: opencode
    # strips model: "" / sessionID: ""). Normalise once, here.
    if isinstance(v, str) and not v.strip():
        return None
    return v


class DelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(..., min_length=8, description=(
        "The sub-question or hypothesis, fully self-contained: the child knows NOTHING of this "
        "conversation. Say what to test, over which window, and what to report back "
        "(e.g. 'Did the 2026-09-10 premium price change reduce paid orders? Compare paid orders "
        "per day for the premium sku_group two weeks before vs after; report the delta and whether "
        "it can explain a 15% total drop.')."
    ))
    context: str = Field("", description=(
        "What you already know that the child needs: definitions you settled, the metric and its "
        "baseline, table/column names you verified, ids of prior queries to reuse. Repeat it per "
        "call — children do not share context."
    ))
    title: str = Field(..., min_length=3, max_length=80, description=(
        "3-6 words, the live status line shown to the user (e.g. 'Pricing change hypothesis')."
    ))
    profile: Literal["investigate", "validate"] = Field("investigate", description=(
        "'investigate' (default): research-only, small model, verdict + evidence ids. "
        "'validate': produces tracked queries as evidence, regular model, and can write rows into "
        "this conversation's lists with their submit_<list> tools — use it for per-document or "
        "per-source extraction fan-outs (one child per document, all saving into the same list)."
    ))
    agent_ids: Optional[List[str]] = Field(None, description=(
        "Narrow the child to these agent (data source) ids from <agents>/<available_agents>. "
        "Default: every agent this conversation has. One source per hypothesis keeps a child focused."
    ))
    model: Optional[str] = Field(None, description=(
        "Override the child's model as '<provider>/<model_id>' or a model id from the catalog. Leave "
        "unset unless the user asked for a model or an instruction names one; the default is right."
    ))
    max_steps: Optional[int] = Field(None, ge=3, description=(
        "Lower the child's planner-step budget below the organization ceiling. Leave unset normally."
    ))

    @field_validator("agent_ids", "model", "max_steps", "context", mode="before")
    @classmethod
    def _blank(cls, v):
        return _null_if_blank(v)


class DelegateOutput(BaseModel):
    sub_agent_id: Optional[str] = None
    wave_id: Optional[str] = None
    status: str = "running"
    title: str = ""
    profile: str = "investigate"
    agents: List[Dict[str, Any]] = []
    model: Optional[str] = None
    refused: Optional[str] = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(..., min_length=3, max_length=400)
    evidence_ref: Optional[str] = Field(None, max_length=120, description="An id from `evidence` this claim rests on.")
    magnitude: Optional[str] = Field(None, max_length=120, description="The number that matters, with its unit and window.")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["step", "query", "viz", "file", "note", "mcp", "table"]
    id: str = Field(..., min_length=1, max_length=200)
    label: str = Field("", max_length=200)


class FinishSubtaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["done", "partial", "blocked", "supported", "refuted", "inconclusive"] = Field(..., description=(
        "For a hypothesis: supported | refuted | inconclusive. For other goals: done | partial | blocked."
    ))
    confidence: float = Field(..., ge=0.0, le=1.0, description="0..1, how sure you are of the outcome.")
    summary: str = Field(..., min_length=10, max_length=600, description=(
        "One or two sentences the parent reads first: the answer and the single number that carries it."
    ))
    findings: List[Finding] = Field(default_factory=list, max_length=12)
    ruled_out: List[str] = Field(default_factory=list, max_length=12, description="Things you checked that are NOT the explanation, one line each.")
    evidence: List[Evidence] = Field(default_factory=list, max_length=20, description="Every id a later reader can verify: steps/queries/vizs you created, files, notes, tables.")
    caveats: List[str] = Field(default_factory=list, max_length=8, description="Data gaps, confounders, windows assumed.")
    unknown: Optional[str] = Field(None, max_length=300, description="If inconclusive/blocked: the one thing that would settle it.")
    report_note_id: Optional[str] = Field(None, description="Id of the note holding your full write-up (timeline, checks, numbers). Required when you kept a note.")

    @field_validator("unknown", "report_note_id", mode="before")
    @classmethod
    def _blank(cls, v):
        return _null_if_blank(v)


class ReadSubAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub_agent_id: str = Field(..., min_length=4)
    include_report: bool = Field(False, description="Also return the child's full write-up note.")


class StopSubAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub_agent_id: str = Field(..., min_length=4)
    reason: str = Field("", max_length=300)


class MessageSubAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    recipient: str = Field(..., min_length=4, max_length=36, description="Child id from delegate/<sub_agents>; a child must use 'parent'.")
    kind: Literal['direction', 'question', 'answer', 'finding']
    body: str = Field(..., min_length=1, max_length=2000, description="Concise update, question, or answer; include evidence ids when relevant.")
    reply_to: Optional[str] = Field(None, max_length=36, description="For an answer, the question's message_id.")
