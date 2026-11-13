"""MCP Apps host rules (app/services/mcp_apps.py).

Contract:
  * a tool's UI binding is read from ``_meta.ui`` (and the deprecated flat
    ``_meta["ui/resourceUri"]``); only ``ui://`` URIs count;
  * visibility defaults to model + app; an app-only tool is hidden from the
    model, a model-only tool cannot be called by an app;
  * every declared CSP domain is validated — anything that could break out of
    its directive (quotes, ``;``, paths, other schemes) is dropped — and an
    app with no declarations gets no network at all;
  * a resource's own ``_meta.ui`` wins over its listing entry;
  * only a ``text/html;profile=mcp-app`` document is accepted as an app.
"""
from __future__ import annotations

import base64

import pytest

from app.services import mcp_apps as apps


@pytest.mark.parametrize("meta, expected_uri", [
    ({"ui": {"resourceUri": "ui://srv/app"}}, "ui://srv/app"),
    ({"ui/resourceUri": "ui://srv/legacy"}, "ui://srv/legacy"),
    ({"ui": {"resourceUri": "https://evil.example/app"}}, None),
    ({"ui": {}}, None),
    ({}, None),
    (None, None),
])
def test_tool_ui_binding_reads_ui_scheme_only(meta, expected_uri):
    ui = apps.parse_tool_ui_meta(meta) or {}
    assert ui.get("resourceUri") == expected_uri


@pytest.mark.parametrize("visibility, model, app", [
    (None, True, True),                 # spec default: both
    (["model", "app"], True, True),
    (["app"], False, True),             # app-only helper
    (["model"], True, False),
    ([], False, False),                 # explicit "nobody"
    (["app", "bogus", "app"], False, True),
])
def test_visibility_rules(visibility, model, app):
    meta = {"ui": {"resourceUri": "ui://s/a"}}
    if visibility is not None:
        meta["ui"]["visibility"] = visibility
    stored = {"annotations": {}, "ui": apps.parse_tool_ui_meta(meta)}
    assert apps.model_visible(stored) is model
    assert apps.app_callable(stored) is app


def test_tools_without_ui_metadata_stay_visible_to_everyone():
    for stored in (None, {}, {"annotations": {"readOnlyHint": True}}):
        assert apps.model_visible(stored)
        assert apps.app_callable(stored)


@pytest.mark.parametrize("bad", [
    "https://a.example; script-src *",
    "https://a.example 'unsafe-eval'",
    "javascript:alert(1)",
    "https://a.example/path",
    "data:",
    "*",
    "https://",
    "ftp://files.example",
])
def test_csp_drops_domains_that_could_widen_the_policy(bad):
    declared = {k: [bad] for k in ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")}
    # A rejected declaration changes nothing: the app keeps the default policy.
    assert apps.build_app_csp(declared) == apps.build_app_csp(None)


def test_csp_maps_each_declared_domain_to_its_directives():
    csp = apps.build_app_csp({
        "connectDomains": ["https://API.example.com", "wss://live.example.com:8443"],
        "resourceDomains": ["https://*.cdn.example"],
        "frameDomains": ["https://www.youtube.com"],
        "baseUriDomains": ["https://base.example"],
    })
    d = {part.split(" ", 1)[0]: part for part in (p.strip() for p in csp.split(";"))}
    assert "https://api.example.com" in d["connect-src"]
    assert "wss://live.example.com:8443" in d["connect-src"]
    for directive in ("script-src", "style-src", "img-src", "font-src", "media-src"):
        assert "https://*.cdn.example" in d[directive]
    assert "https://*.cdn.example" not in d["connect-src"]
    assert d["frame-src"] == "frame-src https://www.youtube.com"
    assert d["base-uri"] == "base-uri https://base.example"
    assert d["object-src"] == "object-src 'none'"


def test_csp_default_is_the_spec_restrictive_policy():
    d = {p.strip().split(" ", 1)[0]: p.strip() for p in apps.build_app_csp(None).split(";")}
    assert d["default-src"] == "default-src 'none'"
    assert d["connect-src"] == "connect-src 'none'"
    assert d["frame-src"] == "frame-src 'none'"
    assert d["object-src"] == "object-src 'none'"


def test_resource_meta_prefers_the_content_item_over_the_listing():
    item = {"ui": {"csp": {"connectDomains": ["https://item.example"]}, "prefersBorder": False}}
    listing = {"ui": {"csp": {"connectDomains": ["https://list.example"], "resourceDomains": ["https://cdn.example"]},
                      "prefersBorder": True, "permissions": {"camera": {}, "teleport": {}}}}
    ui = apps.parse_resource_ui_meta(item, listing)
    assert ui["csp"]["connectDomains"] == ["https://item.example"]
    # A key only the listing declares still applies (fallback per key).
    assert ui["csp"]["resourceDomains"] == ["https://cdn.example"]
    assert ui["prefersBorder"] is False
    assert ui["permissions"] == ["camera"]  # unknown permissions are ignored


def test_permissions_map_to_iframe_allow_features():
    assert apps.permissions_allow_attr(["camera", "clipboardWrite"]) == "camera; clipboard-write"
    assert apps.permissions_allow_attr(["root"]) == ""


@pytest.mark.parametrize("mime", ["text/html;profile=mcp-app", "text/html; profile=mcp-app"])
def test_app_document_accepted_as_text_or_blob(mime):
    html = "<!doctype html><p>hi</p>"
    text_item = {"type": "text", "text": html, "mime_type": mime, "meta": {"ui": {}}}
    assert apps.html_from_contents([text_item], "ui://a")[0] == html
    blob_item = {"type": "binary", "blob_b64": base64.b64encode(html.encode()).decode(), "mime_type": mime}
    assert apps.html_from_contents([blob_item], "ui://a")[0] == html


@pytest.mark.parametrize("contents", [
    [{"type": "text", "text": "<p>x</p>", "mime_type": "text/html"}],          # not an app profile
    [{"type": "text", "text": "{}", "mime_type": "application/json"}],
    [{"type": "text", "text": "   ", "mime_type": "text/html;profile=mcp-app"}],
    [],
])
def test_non_app_documents_are_refused(contents):
    with pytest.raises(apps.McpAppResourceError):
        apps.html_from_contents(contents, "ui://a")


def test_oversized_app_document_is_refused():
    big = "x" * (apps.MAX_UI_HTML_BYTES + 1)
    with pytest.raises(apps.McpAppResourceError):
        apps.html_from_contents([{"type": "text", "text": big, "mime_type": apps.MCP_APP_MIME_TYPE}], "ui://a")
