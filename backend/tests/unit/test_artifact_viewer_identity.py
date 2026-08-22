"""Viewer identity (ARTIFACT_DATA.current_user) contract tests.

The viewer object is injected per render by the host surfaces; these tests pin
the three server-side guarantees:
  1. The prompt contract teaches generated code about current_user and its
     null-guarding rules.
  2. ViewerContextSchema exposes exactly the intended fields — membership
     secrets (invite_token) and preferences (note, memory, defaults) must
     never reach the sandbox iframe.
  3. Server-side renderers (thumbnails) inject current_user=null so shared
     assets never bake in a viewer's identity and the null path is exercised.
"""

import json
import re

from app.ai.tools.implementations._sandbox_context import (
    SANDBOX_RUNTIME_PROMPT,
    SANDBOX_RUNTIME_OBSERVATION,
)
from app.routes.user_profile import (
    ViewerContextSchema,
    VIEWER_CONTEXT_MAX_GROUPS,
    _resolve_viewer_groups,
)
from app.services.thumbnail_service import ThumbnailService


def test_prompt_documents_current_user_contract():
    assert "current_user" in SANDBOX_RUNTIME_PROMPT
    assert "useCurrentUser()" in SANDBOX_RUNTIME_PROMPT
    # The null-guarding rule is the load-bearing part of the contract
    assert "MAY BE `null`" in SANDBOX_RUNTIME_PROMPT
    assert "profile_attributes" in SANDBOX_RUNTIME_PROMPT
    # Observation contract (read_artifact / planner) carries it too
    assert "current_user" in SANDBOX_RUNTIME_OBSERVATION
    assert "useCurrentUser()" in SANDBOX_RUNTIME_OBSERVATION


def test_viewer_context_schema_exposes_only_safe_fields():
    fields = set(ViewerContextSchema.model_fields.keys())
    assert fields == {"id", "name", "email", "image_url", "role", "profile_attributes", "groups"}
    # Regression guard: these must never be added — the object reaches
    # LLM-generated sandbox code in the browser.
    for forbidden in ("invite_token", "note", "memory", "default_llm_model_id",
                      "default_data_source_ids"):
        assert forbidden not in fields


def test_prompt_documents_groups_contract():
    assert "groups" in SANDBOX_RUNTIME_PROMPT
    assert "server-capped" in SANDBOX_RUNTIME_PROMPT


def test_viewer_groups_are_capped():
    """The SQL LIMIT is the cap; pin the constant so a payload can never carry
    an unbounded group list into every artifact render."""
    assert 1 <= VIEWER_CONTEXT_MAX_GROUPS <= 50
    import inspect
    src = inspect.getsource(_resolve_viewer_groups)
    assert "limit(VIEWER_CONTEXT_MAX_GROUPS)" in src


def test_identity_context_never_breaks_generation():
    """build_identity_context must degrade to '' on missing context or DB
    failure — identity flavor can never fail artifact creation."""
    import asyncio
    from app.ai.tools.implementations._sandbox_context import build_identity_context

    class ExplodingDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    class Obj:
        id = "u1"
        name = "X"
        email = "x@y.z"

    assert asyncio.run(build_identity_context(None, Obj(), Obj())) == ""
    assert asyncio.run(build_identity_context(ExplodingDB(), None, Obj())) == ""
    assert asyncio.run(build_identity_context(ExplodingDB(), Obj(), Obj())) == ""


def _extract_artifact_data(html: str) -> dict:
    m = re.search(r"window\.ARTIFACT_DATA\s*=\s*(\{.*?\});", html, re.S)
    assert m, "window.ARTIFACT_DATA not found in rendered HTML"
    return json.loads(m.group(1))


def test_thumbnail_html_renders_anonymous(monkeypatch):
    # Vendored libs may be absent in CI checkouts; the assertion is about the
    # injected data, not the script bundle.
    import app.services.thumbnail_service as ts
    monkeypatch.setattr(ts, "get_inline_scripts", lambda mode: "")
    html = ThumbnailService()._build_thumbnail_html(
        report_id="r1",
        report_title="T",
        report_theme=None,
        artifact_code="<script></script>",
        visualizations=[],
        mode="page",
    )
    data = _extract_artifact_data(html)
    assert "current_user" in data
    assert data["current_user"] is None
