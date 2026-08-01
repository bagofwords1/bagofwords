"""Unit tests for the OneNote Graph client.

Every Graph call is stubbed, so these run offline and deterministically. The
live end-to-end behaviour (real tenant, real OAuth, real notebooks) is covered
separately by the e2e sandbox run.

What matters here is the stuff that is easy to get subtly wrong:
  * the hierarchy → path flattening (section groups, subpages, '/' in names)
  * the incremental `prior_catalog` skip, which is the whole scaling story
  * media extraction from page HTML
  * the no-identity guard (OneNote has no app-only mode at all)
"""
import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients._file_source_common import GlobScopeError
from app.data_sources.clients.graph_onenote_client import (
    GraphOneNoteClient,
    _safe_segment,
)


# --------------------------------------------------------------------- fixtures

NOTEBOOK = "Pcb - Shaygo"

SECTIONS = [
    {
        "id": "sec-smt",
        "displayName": "SMT - Troubleshooting",
        "parentNotebook": {"id": "nb1", "displayName": NOTEBOOK},
        "parentSectionGroup": None,
    },
    {
        "id": "sec-sw",
        "displayName": "Software",
        "parentNotebook": {"id": "nb1", "displayName": NOTEBOOK},
        "parentSectionGroup": {"id": "grp-it", "displayName": "IT/OT"},
    },
    {
        "id": "sec-trash",
        "displayName": "Deleted Pages",
        "parentNotebook": {"id": "nb1", "displayName": NOTEBOOK},
        "parentSectionGroup": {"id": "grp-bin", "displayName": "OneNote_RecycleBin"},
    },
]

SECTION_GROUPS = [
    {
        "id": "grp-it",
        "displayName": "IT/OT",
        "parentNotebook": {"id": "nb1", "displayName": NOTEBOOK},
        "parentSectionGroup": None,
    },
    {
        "id": "grp-bin",
        "displayName": "OneNote_RecycleBin",
        "parentNotebook": {"id": "nb1", "displayName": NOTEBOOK},
        "parentSectionGroup": None,
    },
]

PAGES = {
    "sec-smt": [
        {
            "id": "pg-line",
            "title": "LINE SMT 1-4",
            "level": 0,
            "order": 0,
            "createdDateTime": "2023-10-05T10:55:00Z",
            "lastModifiedDateTime": "2024-01-02T09:00:00Z",
            "links": {"oneNoteClientUrl": {"href": "onenote:///line"}},
        },
        {
            "id": "pg-sub",
            "title": "CpliceProGui detail",
            "level": 1,
            "order": 1,
            "createdDateTime": "2023-10-06T10:55:00Z",
            "lastModifiedDateTime": "2024-01-03T09:00:00Z",
            "links": {},
        },
        {
            # Quick note with no title — must still get a usable path.
            "id": "pg-untitled",
            "title": "",
            "level": 0,
            "order": 2,
            "createdDateTime": "2024-02-11T08:00:00Z",
            "lastModifiedDateTime": "2024-02-11T08:00:00Z",
            "links": {},
        },
    ],
    "sec-sw": [
        {
            "id": "pg-xcopy",
            "title": "מכבשים / copy scripts",
            "level": 0,
            "order": 0,
            "createdDateTime": "2023-05-01T10:00:00Z",
            "lastModifiedDateTime": "2025-06-01T10:00:00Z",
            "links": {"oneNoteWebUrl": {"href": "https://onenote.com/xcopy"}},
        },
    ],
    "sec-trash": [
        {"id": "pg-dead", "title": "deleted thing", "level": 0, "order": 0,
         "createdDateTime": "2020-01-01T00:00:00Z",
         "lastModifiedDateTime": "2020-01-01T00:00:00Z", "links": {}},
    ],
}

PAGE_HTML = {
    "pg-line": (
        "<html><head><title>LINE SMT 1-4</title></head><body>"
        "<p>Troubleshooting <b>CpliceProGui</b></p>"
        "<p>1.If CpliceProGui without connection to line need to on the pc from line 4</p>"
        "<img src='https://graph.microsoft.com/v1.0/me/onenote/resources/r1/$value' "
        "data-fullres-src='https://graph.microsoft.com/v1.0/me/onenote/resources/r1full/$value' "
        "data-fullres-src-type='image/png' />"
        "</body></html>"
    ),
    "pg-xcopy": (
        "<html><body><p>set \"SOURCE=D:\\dp\\ver\\etc\\data\\*.*\"</p>"
        "<p>xcopy \"%SOURCE%\" \"%DEST%\" /E /C /H /R /Y /Z /I</p>"
        "<p>חיפוש תגיות</p><p>Xcopy xcopy roobocopy</p>"
        "<iframe data-original-src='https://youtu.be/demo'></iframe>"
        "</body></html>"
    ),
    "pg-sub": "<html><body><p>sub page body</p></body></html>",
    "pg-untitled": "<html><body><p>quick note</p></body></html>",
}


def _make_client(**kwargs):
    """A client with a delegated token and every Graph call stubbed."""
    c = GraphOneNoteClient(access_token="user-token", **kwargs)
    calls = {"get": [], "bytes": []}

    def fake_get(path, **_):
        calls["get"].append(path)
        if "/sectionGroups" in path:
            return {"value": SECTION_GROUPS}
        if "/sections/" in path and "/pages" in path:
            sec = path.split("/sections/", 1)[1].split("/pages", 1)[0]
            return {"value": PAGES.get(sec, [])}
        if path.endswith("/sections") or "/sections?" in path:
            return {"value": SECTIONS}
        if "/notebooks" in path:
            return {"value": [{"id": "nb1", "displayName": NOTEBOOK}]}
        if path.startswith("/me?"):
            return {"userPrincipalName": "demo1@example.com"}
        return {"value": []}

    def fake_get_bytes(path):
        calls["bytes"].append(path)
        for pid, body in PAGE_HTML.items():
            if f"/pages/{pid}/content" in path:
                return body.encode("utf-8")
        if "resources/" in path:
            # 1x1 PNG
            return (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
                b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
        return b""

    c._get = fake_get           # type: ignore[assignment]
    c._get_bytes = fake_get_bytes  # type: ignore[assignment]
    c._calls = calls            # type: ignore[attr-defined]
    return c


# ------------------------------------------------------------------ hierarchy

def test_paths_flatten_the_full_hierarchy():
    c = _make_client()
    by_id = {p["id"]: p for p in c.list_files()}

    # notebook / section / page
    assert by_id["pg-line"]["path"] == f"{NOTEBOOK}/SMT - Troubleshooting/LINE SMT 1-4"
    # notebook / section GROUP / section / page — and the '/' inside the group
    # name "IT/OT" must not have created an extra path segment.
    assert by_id["pg-xcopy"]["path"] == f"{NOTEBOOK}/IT-OT/Software/מכבשים - copy scripts"


def test_subpage_carries_its_parent_page_in_the_path():
    c = _make_client()
    by_id = {p["id"]: p for p in c.list_files()}
    assert by_id["pg-sub"]["path"] == (
        f"{NOTEBOOK}/SMT - Troubleshooting/LINE SMT 1-4/CpliceProGui detail"
    )


def test_untitled_page_gets_a_dated_fallback_title():
    c = _make_client()
    by_id = {p["id"]: p for p in c.list_files()}
    assert by_id["pg-untitled"]["name"] == "Untitled (2024-02-11)"


def test_recycle_bin_pages_are_not_indexed():
    c = _make_client()
    ids = {p["id"] for p in c.list_files()}
    assert "pg-dead" not in ids


def test_pages_are_newest_first():
    c = _make_client()
    mods = [p["modified_at"] for p in c.list_files()]
    assert mods == sorted(mods, reverse=True)


def test_safe_segment_neutralizes_separators():
    assert _safe_segment("IT/OT") == "IT-OT"
    assert _safe_segment("../secrets") == "..-secrets"
    assert _safe_segment("  ") == "Untitled"


def test_web_url_is_carried_through():
    c = _make_client()
    by_id = {p["id"]: p for p in c.list_files()}
    assert by_id["pg-line"]["web_url"] == "onenote:///line"
    assert by_id["pg-xcopy"]["web_url"] == "https://onenote.com/xcopy"


# --------------------------------------------------------------------- search

def test_search_matches_title_and_section_path():
    c = _make_client()
    # Matches by SECTION name even though no page is titled "Troubleshooting".
    hits = {h["id"] for h in c.search_files("smt troubleshooting")}
    assert "pg-line" in hits
    # Hebrew title matching works (local matching, not Graph search).
    assert {h["id"] for h in c.search_files("מכבשים")} == {"pg-xcopy"}


def test_search_requires_every_term():
    c = _make_client()
    assert c.search_files("smt nonexistentword") == []


# ---------------------------------------------------------------------- grep

def test_grep_matches_text_inside_pages():
    c = _make_client()
    out = c.grep_files("xcopy", is_regex=False, ignore_case=True)
    assert out["total_matches"] >= 2
    paths = {m["path"] for m in out["matches"]}
    assert any("copy scripts" in p for p in paths)


def test_grep_finds_hebrew_tag_lines_in_page_bodies():
    c = _make_client()
    out = c.grep_files("חיפוש תגיות", is_regex=False)
    assert out["total_matches"] == 1


def test_grep_scoped_by_name_pattern():
    c = _make_client()
    out = c.grep_files("CpliceProGui", is_regex=False, name_pattern=f"{NOTEBOOK}/Software/**")
    assert out["total_matches"] == 0
    out2 = c.grep_files("CpliceProGui", is_regex=False)
    assert out2["total_matches"] >= 1


# ----------------------------------------------------------------------- read

def test_read_returns_text_images_and_link():
    c = _make_client()
    out = c.read_file("pg-line")
    assert out["__note_page__"] is True
    assert "CpliceProGui" in out["text"]
    assert "Link: onenote:///line" in out["text"]
    assert f"Page: {NOTEBOOK}/SMT - Troubleshooting/LINE SMT 1-4" in out["text"]
    # The embedded image was fetched, and named so the renderer can dispatch
    # on its extension (Graph resource ids carry none).
    assert len(out["images"]) == 1
    data, name = out["images"][0]
    assert data.startswith(b"\x89PNG")
    assert name.endswith(".png")


def test_read_prefers_the_full_resolution_image_source():
    c = _make_client()
    c.read_file("pg-line")
    assert any("r1full" in u for u in c._calls["bytes"])


def test_unreadable_video_is_reported_not_dropped():
    c = _make_client()
    out = c.read_file("pg-xcopy")
    assert "not readable" in out["text"]
    assert "youtu.be/demo" in out["text"]


def test_read_enforces_glob_scope():
    # Note the glob spans the section GROUP segment ("IT-OT") too — the scope is
    # matched against the full flattened path, not just section/page.
    c = _make_client(include_globs=f"{NOTEBOOK}/IT-OT/Software/**")
    # In scope.
    assert c.read_file("pg-xcopy")["__note_page__"] is True
    # Out of scope — denied before any content fetch.
    with pytest.raises(GlobScopeError):
        c.read_file("pg-line")


def test_glob_scope_filters_the_catalog_too():
    c = _make_client(include_globs="**/Software/**")
    assert {p["id"] for p in c.list_files()} == {"pg-xcopy"}


# ------------------------------------------------------------------- catalog

def test_content_tier_caches_page_text_and_keywords():
    c = _make_client()
    tables = c.get_schemas()
    row = next(t for t in tables if t.name.endswith("copy scripts"))
    meta = row.metadata_json["onenote"]
    assert meta["indexed"] is True
    assert "xcopy" in meta["text"]
    assert meta["keywords"]
    assert meta["page_id"] == "pg-xcopy"
    assert meta["web_url"] == "https://onenote.com/xcopy"


def test_metadata_tier_skips_body_fetches_entirely():
    c = _make_client(index_mode="metadata")
    tables = c.get_schemas()
    assert tables
    assert not any("/content" in b for b in c._calls["bytes"])
    assert "text" not in tables[0].metadata_json["onenote"]


def test_index_mode_none_returns_no_catalog():
    c = _make_client(index_mode="none")
    assert c.get_schemas() == []


def test_prior_catalog_skips_unchanged_pages():
    """The incremental path — this is what makes the Nth user's crawl of a
    shared notebook cost listings only, and what makes a scheduled reindex
    cheap. Without it every crawl re-fetches every page body."""
    first = _make_client()
    tables = first.get_schemas()
    fetches_cold = len([b for b in first._calls["bytes"] if "/content" in b])
    assert fetches_cold == 4  # every non-deleted page

    prior = {t.name: t.metadata_json for t in tables}
    second = _make_client()
    second.get_schemas(prior_catalog=prior)
    fetches_warm = len([b for b in second._calls["bytes"] if "/content" in b])
    assert fetches_warm == 0, "unchanged pages must not be re-fetched"


def test_prior_catalog_refetches_only_the_changed_page():
    first = _make_client()
    tables = first.get_schemas()
    prior = {t.name: t.metadata_json for t in tables}
    # Simulate one page edited since the last run.
    changed = f"{NOTEBOOK}/IT-OT/Software/מכבשים - copy scripts"
    prior[changed]["onenote"]["modified_at"] = "1999-01-01T00:00:00Z"

    second = _make_client()
    second.get_schemas(prior_catalog=prior)
    fetched = [b for b in second._calls["bytes"] if "/content" in b]
    assert len(fetched) == 1
    assert "pg-xcopy" in fetched[0]


def test_prior_tables_shape_also_skips_refetches():
    """The per-user sign-in sync passes `prior_tables` (metadata wrapped one
    level deeper), and that is the ONLY indexing path a delegated-only source
    ever takes — so the skip has to understand that shape too, or every
    sign-in re-fetches every page body."""
    first = _make_client()
    tables = first.get_schemas()
    prior_tables = {
        t.name: {"columns": [], "pks": [], "fks": [], "metadata_json": t.metadata_json}
        for t in tables
    }
    second = _make_client()
    second.get_schemas(prior_tables=prior_tables)
    assert len([b for b in second._calls["bytes"] if "/content" in b]) == 0


def test_catalog_is_capped_and_keeps_newest():
    c = _make_client(max_catalog_objects=1)
    tables = c.get_schemas()
    assert len(tables) == 1
    # Newest page wins the cap.
    assert tables[0].name.endswith("copy scripts")


# ------------------------------------------------------- delegated-only guards

def test_no_user_token_yields_empty_catalog_not_an_error():
    """OneNote has NO app-only mode (Microsoft retired it in March 2025), so an
    admin-side crawl before anyone signs in must degrade quietly."""
    c = GraphOneNoteClient(tenant_id="t", client_id="c", client_secret="s")
    assert c.get_schemas() == []
    assert c.list_files() == []


def test_test_connection_without_user_token_only_checks_credentials():
    c = GraphOneNoteClient(tenant_id="t", client_id="c", client_secret="s")
    c._token = lambda: "app-token"  # type: ignore[assignment]
    res = c.test_connection()
    assert res["success"] is True
    assert "sign in with Microsoft" in res["message"]


def test_test_connection_with_user_token_probes_onenote():
    c = _make_client()
    res = c.test_connection()
    assert res["success"] is True
    assert "demo1@example.com" in res["message"]
    assert NOTEBOOK in res["message"]


# ------------------------------------------------------------------ vocabulary

def test_client_declares_note_capabilities_not_file_ones():
    c = _make_client()
    assert c.capabilities == {
        Capability.LIST_NOTES, Capability.READ_NOTE,
        Capability.SEARCH_NOTES, Capability.GREP_NOTES,
    }
    assert Capability.READ_FILE not in c.capabilities


def test_note_tools_subclass_the_file_tools():
    from app.ai.tools.implementations.grep_files import GrepFilesTool
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.ai.tools.implementations.note_tools import (
        GrepNotesTool, ListNotesTool, ReadNoteTool, SearchNotesTool,
    )
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool

    assert issubclass(ListNotesTool, ListFilesTool)
    assert issubclass(ReadNoteTool, ReadFileTool)
    assert issubclass(SearchNotesTool, SearchFilesTool)
    assert issubclass(GrepNotesTool, GrepFilesTool)

    assert ListNotesTool().metadata.name == "list_notes"
    assert ReadNoteTool().metadata.name == "read_note"
    assert SearchNotesTool().metadata.name == "search_notes"
    assert GrepNotesTool().metadata.name == "grep_notes"
    assert GrepNotesTool._required_capability == Capability.GREP_NOTES
    # The base file tool must keep its own capability after the refactor.
    assert GrepFilesTool._required_capability == Capability.GREP_FILES


def test_registry_entry_is_oauth_only_and_shared():
    from app.schemas.data_source_registry import get_entry
    e = get_entry("onenote")
    # No service_principal variant: app-only auth for OneNote is retired.
    assert list(e.credentials_auth.by_auth.keys()) == ["oauth"]
    assert e.credentials_auth.default == "oauth"
    # Shared (not per_user) so a team notebook is indexed once and stays
    # eligible for scheduled reindex.
    assert e.catalog_ownership == "shared"
    assert e.data_shape == "files"
    assert e.catalog_nouns == ("page", "pages")


def test_onenote_is_registered_as_a_file_source_everywhere():
    """Regression: `onenote` missing from these allow-lists made `list_notes`
    succeed (it resolves the client by capability) while `read_note`,
    `search_notes` and `grep_notes` all failed with the misleading
    "no file source is attached to this agent" — caught only by driving a real
    agent turn, since every client-level unit test passed.
    """
    from app.ai.tools.implementations._file_tool_common import FILE_SOURCE_TYPES
    from app.ai.tools.implementations.connection_catalog_common import (
        FILE_SOURCE_TYPES as CATALOG_FILE_TYPES,
    )
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder

    assert "onenote" in FILE_SOURCE_TYPES
    assert "onenote" in CATALOG_FILE_TYPES
    assert "onenote" in SchemaContextBuilder._FILE_SOURCE_TYPES
    assert "onenote" in SchemaContextBuilder._NATIVE_SEARCH_TYPES


def test_oauth_scopes_request_notes_permissions():
    from app.services.connection_oauth_service import get_oauth_params

    class _Conn:
        id = "c1"
        type = "onenote"

        def decrypt_credentials(self):
            return {
                "tenant_id": "tid",
                "oauth_client_id": "cid",
                "oauth_client_secret": "sec",
            }

    params = get_oauth_params(_Conn())
    assert "Notes.Read" in params["scopes"]
    assert "Notes.Read.All" in params["scopes"]
    assert "offline_access" in params["scopes"]
    assert params["provider_name"] == "microsoft"
