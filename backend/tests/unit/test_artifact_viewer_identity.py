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


def test_no_baked_specifics_rules_present():
    """The anti-hardcoding contract: names/emails must never be baked into
    generated code, even when they arrive via the report title."""
    import inspect
    from app.ai.tools.implementations._sandbox_context import build_identity_context

    assert "NEVER hardcode a specific person's name" in SANDBOX_RUNTIME_PROMPT
    src = inspect.getsource(build_identity_context)
    assert "NO BAKED SPECIFICS" in src
    # Making a name "dynamic" means binding, not deleting
    assert "removing it entirely does not satisfy" in src


def test_prompt_documents_rules_of_hooks():
    """Hooks after an early return are silent in the baked-srcdoc runtime but
    crash in postMessage runtimes — the contract must forbid the pattern."""
    assert "RULES OF HOOKS" in SANDBOX_RUNTIME_PROMPT
    assert "BEFORE any early return" in SANDBOX_RUNTIME_PROMPT


def test_titles_are_viewer_agnostic_by_contract():
    """Report and artifact titles must never carry the requester's name —
    the possessive report title was how names leaked back into artifacts."""
    import inspect
    from app.ai.tools.schemas.create_artifact import CreateArtifactInput
    from app.ai.agents.reporter.reporter import Reporter

    desc = CreateArtifactInput.model_fields["title"].description or ""
    assert "viewer-agnostic" in desc
    src = inspect.getsource(Reporter.generate_report_title)
    assert "never the person requesting it" in src


def test_planner_briefs_forbid_resolved_personalization():
    """Whoever writes artifact code must be told not to substitute the
    requester's name (the 'exactly: Yochay's Album Catalog' plan) and must
    understand the anonymous preview, so working personalization is never
    'repaired' by hardcoding.

    The brief-carrying surface differs per path: create_artifact still takes a
    free-text `prompt`, the legacy (MCP) editor still takes `edit_prompt`, and
    the planner-authored path has no brief field at all — its guidance rides in
    the authoring reference that ships in the planner system prompt.
    """
    from app.ai.tools.schemas.create_artifact import CreateArtifactInput
    from app.ai.tools.schemas.edit_artifact_legacy import LegacyEditArtifactInput
    from app.ai.agents.planner.artifact_authoring import (
        build_artifact_authoring_reference,
    )

    create_desc = CreateArtifactInput.model_fields["prompt"].description or ""
    edit_desc = LegacyEditArtifactInput.model_fields["edit_prompt"].description or ""
    for desc in (create_desc, edit_desc):
        assert "RUNTIME BINDING" in desc
        assert "ANONYMOUS" in desc

    # The planner authors the code itself now, so the same contract must reach
    # it without any brief field in the loop.
    reference = build_artifact_authoring_reference()
    assert "ANONYMOUS" in reference
    assert "hardcoding" in reference


def test_preview_notes_attached_wherever_a_screenshot_is():
    """Every tool that hands the model a render screenshot must label it, on
    both axes the model has misread: the preview is ANONYMOUS (missing names
    are expected) and it is STATIC (closed dropdowns are expected).

    The mechanical edit_artifact is deliberately exempt — it attaches no image
    (there is no inner LLM to reflect on one), so it has nothing to label.
    """
    import inspect
    from app.ai.tools.implementations._sandbox_context import (
        ANON_PREVIEW_NOTE,
        STATIC_PREVIEW_NOTE,
    )
    from app.ai.tools.implementations import create_artifact as ca
    from app.ai.tools.implementations import read_artifact as ra
    from app.ai.tools.implementations import edit_artifact_legacy as eal
    from app.ai.tools.implementations import edit_artifact as ea

    assert "EXPECTED behavior" in ANON_PREVIEW_NOTE
    assert "EXPECTED" in STATIC_PREVIEW_NOTE
    # The static note must name the closed-by-default surfaces and forbid
    # "fixing" them — that misdiagnosis burned a turn's whole edit budget.
    for token in ("multi-select", "dropdown", "CLOSED", "NEVER"):
        assert token in STATIC_PREVIEW_NOTE

    for module in (ca, ra, eal):
        src = inspect.getsource(module)
        assert "ANON_PREVIEW_NOTE" in src, f"{module.__name__} attaches an unlabeled screenshot"
        assert "STATIC_PREVIEW_NOTE" in src, f"{module.__name__} attaches an unlabeled screenshot"

    # Exemption is real, not an oversight: assert it attaches no image at all.
    assert '"images"' not in inspect.getsource(ea)


def test_screenshot_limits_documented_in_authoring_reference():
    """Observations get compacted out of long conversations; the caveat must
    also live in the always-present system-prompt reference."""
    from app.ai.agents.planner.artifact_authoring import (
        build_artifact_authoring_reference,
    )

    reference = build_artifact_authoring_reference()
    assert "STATIC capture" in reference
    for token in ("dropdown", "CLOSED", "read its wiring in the CODE", "read the CODE"):
        if token in reference:
            break
    else:
        raise AssertionError("reference must redirect interaction checks to the code")


def test_render_repair_attempt_budget():
    """Edits must not give up after a couple of repair rounds."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    assert CreateArtifactTool.MAX_RENDER_REPAIR_ATTEMPTS == 5


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
