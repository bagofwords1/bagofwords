"""The pieces of the document PDF export that do not need a browser.

A document is the one artifact mode with no server-side renderer: its body is
markdown plus {{viz:...}} placeholders that only the frontend's DocViewer knows
how to draw. So the export loads the app's own standalone paper page in headless
Chromium and injects the data. These tests cover the parts of that path that are
pure logic — where the renderer looks for the page, what geometry it lays the
document out at, and which embedded files it has to inline — because the render
itself needs a browser and a running frontend.
"""

import pytest

from app.services.report_pdf_service import (
    _CSS_PX_PER_IN,
    _DOC_MARGIN_IN,
    _FILE_PLACEHOLDER_RE,
    _PAPER_IN,
    _doc_page_geometry,
    _render_origin,
)


class TestRenderOrigin:
    """Where the headless browser looks for the app's own pages."""

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("BOW_RENDER_ORIGIN", "http://frontend.internal:3000/")
        assert _render_origin() == "http://frontend.internal:3000"

    def test_listen_address_becomes_a_reachable_one(self, monkeypatch):
        """`0.0.0.0` is an address to listen on, not one to connect to — and it
        is the shipped default for base_url."""
        monkeypatch.delenv("BOW_RENDER_ORIGIN", raising=False)
        _set_base_url(monkeypatch, "http://0.0.0.0:3000")
        assert _render_origin() == "http://127.0.0.1:3000"

    def test_configured_public_url_is_used_as_is(self, monkeypatch):
        monkeypatch.delenv("BOW_RENDER_ORIGIN", raising=False)
        _set_base_url(monkeypatch, "https://bow.example.com")
        assert _render_origin() == "https://bow.example.com"

    def test_path_and_query_are_dropped(self, monkeypatch):
        """Only the origin matters; the renderer appends its own path."""
        monkeypatch.delenv("BOW_RENDER_ORIGIN", raising=False)
        _set_base_url(monkeypatch, "https://bow.example.com/app?x=1")
        assert _render_origin() == "https://bow.example.com"

    def test_missing_config_still_yields_something_loadable(self, monkeypatch):
        monkeypatch.delenv("BOW_RENDER_ORIGIN", raising=False)
        _set_base_url(monkeypatch, None)
        assert _render_origin() == "http://127.0.0.1:3000"


def _set_base_url(monkeypatch, value):
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config, "base_url", value, raising=False)


def test_doc_layout_matches_the_printable_area_of_the_sheet():
    """The page is laid out at the width it prints at: a chart measures itself
    once, at init, so a mismatch here is a chart that overhangs the margin."""
    width, height = _doc_page_geometry()
    paper_w, paper_h = _PAPER_IN

    assert width == round((paper_w - 2 * _DOC_MARGIN_IN) * _CSS_PX_PER_IN)
    assert height == round((paper_h - 2 * _DOC_MARGIN_IN) * _CSS_PX_PER_IN)
    # Portrait, and a text measure a document can actually be read at.
    assert width < height
    assert 550 < width < 700


@pytest.mark.parametrize(
    "markdown, expected",
    [
        ("{{file:0f8e1c2b-1111-2222-3333-444455556666}}",
         ["0f8e1c2b-1111-2222-3333-444455556666"]),
        # A caption after the pipe is part of the placeholder, not the id.
        ("{{file:0f8e1c2b-1111-2222-3333-444455556666|Revenue by region}}",
         ["0f8e1c2b-1111-2222-3333-444455556666"]),
        ("{{ file: 0f8e1c2b-1111-2222-3333-444455556666 }}",
         ["0f8e1c2b-1111-2222-3333-444455556666"]),
        # A visualization placeholder is a different thing entirely.
        ("{{viz:0f8e1c2b-1111-2222-3333-444455556666}}", []),
        ("no placeholders here", []),
    ],
)
def test_embedded_file_ids_are_found_in_the_markdown(markdown, expected):
    """A doc records its images only as placeholders in the body — unlike a
    dashboard, which lists them in content['files']. Miss them and every image
    prints as "image unavailable", because the renderer has no session to fetch
    them with."""
    assert _FILE_PLACEHOLDER_RE.findall(markdown) == expected
