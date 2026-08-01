"""Note-named agent tools for OneNote notebooks.

The OneNote client exposes ``list_files`` / ``read_file`` / ``search_files`` /
``grep_files`` methods that return page-shaped results. Exposing the *file*
tools on a notebook has the same failure mode mail had — the planner reasons
about "files" and picks the wrong verb — so a notebook gets its own vocabulary:
``list_notes`` / ``read_note`` / ``search_notes`` / ``grep_notes``.

These are thin subclasses of the file tools: identical resolution, live-fetch
and session-file logic, changing only the planner-facing name/description and
the capability the resolved connection must expose. Because the OneNote client
advertises only the note capabilities, a notebook agent sees ONLY these tools;
a drive agent still sees the file tools; a mixed agent sees both, each scoped
to its own connection.

``grep_notes`` has no SharePoint/Drive counterpart on purpose: a page is a few
KB of HTML, so sweeping page bodies is affordable where sweeping a document
library's contents would not be — and it is the only way to match text INSIDE
a page, since Graph's page search does not reach work/school notebooks.
"""
from __future__ import annotations

from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.file_tools import (
    GrepFilesInput,
    GrepFilesOutput,
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    SearchFilesInput,
    SearchFilesOutput,
)
from app.data_sources.clients.base import Capability

from .grep_files import GrepFilesTool
from .list_files import ListFilesTool
from .read_file import ReadFileTool
from .search_files import SearchFilesTool


class ListNotesTool(ListFilesTool):
    """List pages in a OneNote connection."""

    _required_capability = Capability.LIST_NOTES
    _item_noun = "note"
    _start_title = "Listing notes"
    _operation_name = "list_notes"
    _empty_hint_action = "search_notes"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_notes",
            description=(
                "List pages in a Microsoft OneNote connection, most recently "
                "modified first. Each page carries its full "
                "'Notebook/Section/Page' path, so the section a page lives in "
                "is visible without a separate call. Pass `folder_id` to list "
                "one section only (section ids come from a page's listing). "
                "Use `search_notes` to find pages by title, `grep_notes` to "
                "match text inside pages, and `read_note` to open one. "
                "`connection_id` is the OneNote connection (id or name)."
            ),
            category="research",
            input_schema=ListFilesInput.model_json_schema(),
            output_schema=ListFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["onenote", "notes", "notebook", "pages", "list"],
            requires_capability="list_notes",
        )


class ReadNoteTool(ReadFileTool):
    """Read one OneNote page — text plus its embedded images."""

    _required_capability = Capability.READ_NOTE
    _start_noun = "note"
    _operation_name = "read_note"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_note",
            description=(
                "Read a full OneNote page and attach it to the conversation. "
                "Pass the page id (from `list_notes`, `search_notes` or "
                "`grep_notes`) as `file_id` and the OneNote connection as "
                "`connection_id`. Returns the page's path, a `Link:` line with "
                "its OneNote URL, and the body as plain text — plus any images "
                "embedded in the page as viewable images, and the text of any "
                "files attached to it. Cite the `Link:` URL when pointing the "
                "user at a page, never the opaque id. USE THIS — not read_file "
                "— to open a page surfaced by the note tools."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=90,
            tags=["onenote", "notes", "notebook", "page", "read"],
            requires_capability="read_note",
        )


class SearchNotesTool(SearchFilesTool):
    """Find OneNote pages by title or notebook/section path."""

    _required_capability = Capability.SEARCH_NOTES
    _item_noun = "note"
    _start_noun = "notes"
    _operation_name = "search_notes"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_notes",
            description=(
                "Find OneNote pages whose TITLE or notebook/section path match "
                "the given words (all words must appear; case-insensitive, and "
                "non-Latin scripts such as Hebrew work). Returns matching pages "
                "with their id and full path. Because section names often carry "
                "the topic ('SMT - Troubleshooting'), this finds pages even "
                "when the page title itself is uninformative. To match text "
                "INSIDE pages rather than in their titles, use `grep_notes`. "
                "Open the best hit with `read_note`."
            ),
            category="research",
            input_schema=SearchFilesInput.model_json_schema(),
            output_schema=SearchFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["onenote", "notes", "notebook", "page", "search"],
            requires_capability="search_notes",
        )


class GrepNotesTool(GrepFilesTool):
    """Regex sweep over the TEXT of OneNote pages."""

    _required_capability = Capability.GREP_NOTES
    _operation_name = "grep_notes"
    _item_plural = "notes"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="grep_notes",
            description=(
                "Search the TEXT INSIDE OneNote pages with a regex or literal "
                "pattern, returning matching lines with their page and optional "
                "context lines. This is the tool for finding a command, error "
                "string, hostname, tool name or keyword written in the body of "
                "a note — including tag words people add to pages so they can "
                "find them later. Use `name_pattern` (glob over "
                "'Notebook/Section/Page' paths, e.g. 'Team Notebook/**') or "
                "`folder_id` (a section) to narrow the sweep. Prefer this over "
                "`search_notes` when the user quotes a phrase or names "
                "something that would appear in a page's content rather than "
                "its title."
            ),
            category="research",
            input_schema=GrepFilesInput.model_json_schema(),
            output_schema=GrepFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=120,
            tags=["onenote", "notes", "notebook", "grep", "search", "content"],
            requires_capability="grep_notes",
        )
