"""Regression tests for validation-vs-host runtime parity and related fixes.

Field failure: a stale frontend/.output build next to a current public/libs
made headless validation load an old artifact-globals.js without useParams —
correct artifact code phantom-failed with "useParams is not defined" and
burned the entire repair loop.
"""

import json
import re
import time

import pytest


def test_find_libs_dir_prefers_freshest_globals(tmp_path, monkeypatch):
    import app.services.artifact_libs as al

    stale = tmp_path / "output" / "libs"
    fresh = tmp_path / "public" / "libs"
    for d in (stale, fresh):
        d.mkdir(parents=True)
        (d / "anyfile.js").write_text("// filler")
    (stale / "artifact-globals.js").write_text("// OLD globals")
    time.sleep(0.02)
    (fresh / "artifact-globals.js").write_text("// NEW globals with useParams")

    # Stale dir listed FIRST (the old code returned the first match)
    monkeypatch.setattr(al, "_CANDIDATE_DIRS", [stale, fresh])
    assert al._find_libs_dir() == fresh

    # And freshness wins in either order
    monkeypatch.setattr(al, "_CANDIDATE_DIRS", [fresh, stale])
    assert al._find_libs_dir() == fresh


def test_read_lib_cache_invalidates_on_mtime(tmp_path):
    from app.services.artifact_libs import _read_lib

    libs = tmp_path
    f = libs / "artifact-globals.js"
    f.write_text("v1")
    assert _read_lib(libs, "artifact-globals.js") == "v1"
    time.sleep(0.02)
    f.write_text("v2")
    assert _read_lib(libs, "artifact-globals.js") == "v2"


def test_thumbnail_html_stubs_params_runtime(monkeypatch):
    """Validation HTML must define useParams/useParamOptions/useCurrentUser
    fallbacks so a stale globals bundle can never phantom-fail correct code."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    import app.ai.tools.implementations.create_artifact as ca
    monkeypatch.setattr(ca, "get_inline_scripts", lambda mode: "")
    html = CreateArtifactTool()._build_thumbnail_html(
        {"report": {}, "visualizations": [], "current_user": None},
        "<script></script>",
        mode="page",
    )
    assert "typeof window.useParams !== 'function'" in html
    assert "typeof window.useParamOptions !== 'function'" in html
    assert "typeof window.useCurrentUser !== 'function'" in html


@pytest.mark.asyncio
async def test_oauth_refresh_handles_form_encoded_and_error_200(monkeypatch):
    from app.services import connection_oauth_service as cos

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/x-www-form-urlencoded"}

        def __init__(self, text):
            self.text = text

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, text):
            self._text = text

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            assert k["headers"]["Accept"] == "application/json"
            return FakeResp(self._text)

    params = {"client_id": "c", "token_url": "https://x/token"}
    monkeypatch.setattr(cos, "_apply_client_auth", lambda p, d: None)

    # GitHub-style form-encoded success body
    monkeypatch.setattr(cos.httpx, "AsyncClient", lambda: FakeClient(
        "access_token=tok123&token_type=bearer&expires_in=100"))
    tokens = await cos.refresh_access_token(params, "rt")
    assert tokens["access_token"] == "tok123"

    # 200-with-error payload must raise, not return a bogus token
    monkeypatch.setattr(cos.httpx, "AsyncClient", lambda: FakeClient(
        "error=bad_refresh_token&error_description=The+refresh+token+is+invalid"))
    with pytest.raises(ValueError, match="refresh token"):
        await cos.refresh_access_token(params, "rt")

    # HTML garbage must raise a descriptive error, not a JSON traceback
    monkeypatch.setattr(cos.httpx, "AsyncClient", lambda: FakeClient("<html>nope</html>"))
    with pytest.raises(ValueError, match="unrecognized body"):
        await cos.refresh_access_token(params, "rt")
