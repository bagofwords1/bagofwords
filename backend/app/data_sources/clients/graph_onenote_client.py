"""Microsoft OneNote client over Microsoft Graph.

Backs the ``onenote`` data source type.

DELEGATED-ONLY. Microsoft retired app-only (application permission) tokens for
the OneNote Graph APIs on 2025-03-31 and has stated that no app-only model is
planned, citing OneNote's user-centric nature. Every call here therefore runs
with a signed-in user's delegated token, and the registry entry offers no
service-principal variant (unlike SharePoint, which offers both). Without a
user token this client degrades to an empty catalog rather than erroring, the
same guard the mail client uses — the real crawl happens once a user signs in.

SHAPE. A *page* is the readable unit — "a note". Notebooks, section groups and
sections are containers with no body of their own, so they are flattened into
the page's path::

    Notebook / Section Group / Section / [Parent Page] / Page

exactly as the drive client flattens folders into ``path``. That keeps the
whole existing file-source surface working unchanged — catalog rows, glob
scoping, and the grep sweep engine all operate on paths — while the agent is
offered note-named tools (``list_notes`` / ``read_note`` / ``search_notes`` /
``grep_notes``) via the NOTE capabilities.

INDEX vs READ. Unlike the drive client — where the ``content`` tier is a no-op
because extracting every Office file would be ruinous — the content tier is
real here: a page is a few KB of HTML and accounts hold hundreds, not tens of
thousands. Caching page text at index time is what makes keyword search work
at all, because Graph's page-level search is unreliable for work notebooks
(the ``search=`` parameter is consumer-only, and work notebooks surface in
Microsoft Search as opaque ``.one`` section blobs, not as individual pages).
``prior_catalog`` makes a reindex incremental: a page whose
``lastModifiedDateTime`` matches its previous catalog row reuses the stored
text instead of re-fetching it, so the Nth user to crawl a shared team
notebook pays for listings only — the mechanism that replaces the service
account we cannot have.

Reads are always LIVE (never served from the index), and carry the page's
embedded media: ``<img>`` resources are fetched as bytes and handed to the
tool layer's vision path, ``<object>`` attachments are extracted through the
usual document pipeline, and embedded video — which nothing can read — is
reported explicitly with the page's OneNote link instead of silently dropped.
"""
from __future__ import annotations

import html as _html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.ai.prompt_formatters import Table
from app.data_sources.clients.base import Capability
from app.data_sources.clients._document_text import (
    doc_text_is_usable,
    extract_document_text_from_bytes,
)
from app.data_sources.clients._file_source_common import (
    GlobScopeError,
    INDEX_CONTENT,
    INDEX_NONE,
    path_matches_globs,
)
from app.data_sources.clients._grep_common import run_grep_sweep
from app.data_sources.clients._keywords import extract_keywords
from app.data_sources.clients.graph_drive_client import GraphDriveClient
from app.data_sources.clients.mail_common import strip_html
from app.data_sources.clients.progress import ProgressCallback, make_reporter

logger = logging.getLogger(__name__)

# Graph serves at most 100 pages per request on the section→pages endpoint.
PAGE_LIST_SIZE = 100

# Images fetched per page read. A page with a screenshot wall would otherwise
# turn one read into dozens of Graph round-trips and blow the model's image
# budget; the tool layer caps what it renders anyway.
MAX_PAGE_IMAGES = 8

# Bytes accepted for a single embedded resource. Guards against a 100 MB video
# poster or a raw camera image being pulled into a read.
MAX_RESOURCE_BYTES = 12 * 1024 * 1024

# Characters of extracted page text cached per catalog row. Pages are small,
# but a pasted log file is not.
MAX_INDEXED_TEXT_CHARS = 40_000

# OneNote's recycle bin surfaces as a real section group. Indexing deleted
# pages makes the catalog actively misleading.
RECYCLE_BIN_NAMES = {"OneNote_RecycleBin", "OneNote_DeletedPages"}

_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_OBJECT_RE = re.compile(r"<object\b[^>]*>", re.IGNORECASE)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z0-9_:-]+)\s*=\s*["']([^"']*)["']""")

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/webp": "webp",
}


def _attrs(tag: str) -> Dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in _ATTR_RE.finditer(tag or "")}


def _safe_segment(name: Optional[str]) -> str:
    """A path segment that can't forge hierarchy.

    Notebook and section names are free text and routinely contain '/' ("IT/OT",
    "גיבוי מכונות + Salvador"). Left raw, one such name would split into two
    path segments and every glob written against the real shape would silently
    miss it — and a name like '../x' would address a different scope entirely.
    """
    seg = (name or "").strip().replace("/", "-").replace("\\", "-")
    seg = seg.replace("\n", " ").replace("\r", " ").strip()
    return seg or "Untitled"


class GraphOneNoteClient(GraphDriveClient):
    """OneNote over Microsoft Graph, shaped as a file source.

    Declares the NOTE capabilities (not the file ones) so the planner is
    offered note-named tools. The underlying methods keep their file-tool names
    (``list_files`` / ``read_file`` / ``search_files`` / ``grep_files``) since
    the note tools delegate straight to them; only the vocabulary changes.
    """

    capabilities = {
        Capability.LIST_NOTES,
        Capability.READ_NOTE,
        Capability.SEARCH_NOTES,
        Capability.GREP_NOTES,
    }

    def __init__(self, *args, notebook_source: Optional[str] = None,
                 group_id: Optional[str] = None, **kwargs):
        kwargs.setdefault("mode", "onenote")
        # OneNote defaults to the CONTENT tier (the drive client defaults to
        # metadata). Page bodies are the only thing that makes keyword search
        # work here, and they are cheap enough to cache.
        if not kwargs.get("index_mode"):
            kwargs["index_mode"] = INDEX_CONTENT
        super().__init__(*args, **kwargs)
        # Where the notebooks live. Personal notebooks sit in the user's
        # OneDrive; TEAM notebooks sit in a SharePoint site or a Microsoft 365
        # group and are reachable even for users who have no OneDrive at all
        # (a licence without an Office plan provisions no personal site, and
        # `/me/onenote` then fails outright). Since a shared team notebook is
        # the common deployment for this connector, both are supported.
        src = (notebook_source or "").strip().lower()
        if src not in ("me", "site", "group"):
            src = "site" if (self.site_url or "").strip() else ("group" if group_id else "me")
        self.notebook_source = src
        self.group_id = (group_id or "").strip() or None
        # Hierarchy walk memo — one crawl per client instance. list/search/grep
        # in the same request then cost nothing extra.
        self._pages_cache: Optional[List[dict]] = None
        # page_id -> extracted text, memoized so a grep sweep and a subsequent
        # read don't re-fetch the same page.
        self._text_memo: Dict[str, str] = {}

    @property
    def description(self) -> str:
        scope = ""
        if self.include_globs:
            scope = " / scope " + ", ".join(self.include_globs)
        return "OneNote notebooks" + scope

    @property
    def is_document_based(self) -> bool:
        return True

    # ------------------------------------------------------------------ paths

    def _base(self) -> str:
        """Graph root for every call, per the configured notebook source.

        Every variant is still DELEGATED — app-only tokens are not supported by
        the OneNote API at all, so this switches WHOSE notebooks are read, never
        how we authenticate.
        """
        if self.notebook_source == "site":
            return f"/sites/{self._resolve_site_id()}/onenote"
        if self.notebook_source == "group":
            if not self.group_id:
                raise ValueError("group_id is required when notebook_source is 'group'")
            return f"/groups/{self.group_id}/onenote"
        return "/me/onenote"

    def _has_user_token(self) -> bool:
        return bool(getattr(self, "_user_token_provided", True))

    @property
    def catalog_identity_available(self) -> bool:
        """OneNote can only be crawled with a delegated user token.

        Without one every listing here returns empty — not because the
        notebooks are gone, but because there is nothing to ask Graph *as*.
        Surfacing that lets the indexer treat the empty result as "no
        information" instead of pruning the shared catalog.
        """
        return self._has_user_token()

    # ------------------------------------------------------------- hierarchy

    def _get_all(self, path: str) -> List[dict]:
        """GET a Graph collection, following @odata.nextLink."""
        out: List[dict] = []
        url = path
        while url:
            data = self._get(url)
            out.extend(data.get("value") or [])
            url = data.get("@odata.nextLink")
        return out

    def _section_groups(self) -> Dict[str, dict]:
        """All section groups, keyed by id, with their parent ids resolved.

        Section groups nest arbitrarily and ``$expand`` only resolves one level,
        so the chain is assembled client-side from a single flat fetch rather
        than walked level by level.
        """
        try:
            raw = self._get_all(
                f"{self._base()}/sectionGroups"
                "?$select=id,displayName"
                "&$expand=parentNotebook($select=id,displayName),"
                "parentSectionGroup($select=id,displayName)"
            )
        except Exception as e:
            logger.info("onenote: sectionGroups fetch failed (%s) — flat hierarchy", str(e)[:200])
            return {}
        groups: Dict[str, dict] = {}
        for g in raw:
            groups[g.get("id")] = {
                "id": g.get("id"),
                "name": _safe_segment(g.get("displayName")),
                "notebook": _safe_segment(
                    ((g.get("parentNotebook") or {}).get("displayName"))
                ),
                "parent_group_id": (g.get("parentSectionGroup") or {}).get("id"),
            }
        return groups

    def _group_chain(self, groups: Dict[str, dict], group_id: Optional[str]) -> List[str]:
        """Section-group names from outermost to innermost."""
        chain: List[str] = []
        seen = set()
        gid = group_id
        while gid and gid in groups and gid not in seen:
            seen.add(gid)
            g = groups[gid]
            chain.append(g["name"])
            gid = g.get("parent_group_id")
        chain.reverse()
        return chain

    def _sections(self) -> List[dict]:
        """Every section the signed-in user can see, with a resolved path prefix."""
        groups = self._section_groups()
        try:
            raw = self._get_all(
                f"{self._base()}/sections"
                "?$select=id,displayName"
                "&$expand=parentNotebook($select=id,displayName),"
                "parentSectionGroup($select=id,displayName)"
            )
        except Exception:
            # $expand is the fragile part; without it we still get sections and
            # fall back to a notebook-less path rather than failing the crawl.
            raw = self._get_all(f"{self._base()}/sections?$select=id,displayName")
        sections: List[dict] = []
        for s in raw:
            notebook = _safe_segment((s.get("parentNotebook") or {}).get("displayName"))
            group_id = (s.get("parentSectionGroup") or {}).get("id")
            chain = self._group_chain(groups, group_id)
            if any(c in RECYCLE_BIN_NAMES for c in chain):
                continue
            name = _safe_segment(s.get("displayName"))
            if name in RECYCLE_BIN_NAMES:
                continue
            sections.append({
                "id": s.get("id"),
                "name": name,
                "notebook": notebook,
                "prefix": "/".join([notebook] + chain + [name]),
            })
        return sections

    def _section_pages(self, section: dict) -> List[dict]:
        """Pages of one section, ordered as OneNote shows them."""
        sid = section["id"]
        select = "id,title,level,order,createdDateTime,lastModifiedDateTime,links"
        try:
            raw = self._get_all(
                f"{self._base()}/sections/{sid}/pages"
                f"?$select={select}&$top={PAGE_LIST_SIZE}"
            )
        except Exception:
            # Older/odd tenants reject the $select list; the unfiltered listing
            # carries the same fields, just heavier.
            try:
                raw = self._get_all(
                    f"{self._base()}/sections/{sid}/pages?$top={PAGE_LIST_SIZE}"
                )
            except Exception as e:
                logger.info(
                    "onenote: skipping unreadable section %r — %s",
                    section.get("prefix"), str(e)[:200],
                )
                return []
        # Sort by OneNote's own in-section order so subpage parents resolve.
        raw.sort(key=lambda p: (p.get("order") if p.get("order") is not None else 0))

        items: List[dict] = []
        # Titles of the nearest ancestor at each level, for subpage paths.
        ancestors: Dict[int, str] = {}
        for p in raw:
            level = int(p.get("level") or 0)
            title = (p.get("title") or "").strip()
            if not title:
                # Quick notes default to no title; a blank path segment would
                # be both unsearchable and ambiguous between pages.
                stamp = (p.get("createdDateTime") or "")[:10]
                title = f"Untitled ({stamp})" if stamp else "Untitled"
            title = _safe_segment(title)
            ancestors[level] = title
            for deeper in [k for k in ancestors if k > level]:
                ancestors.pop(deeper, None)
            parents = [ancestors[k] for k in sorted(ancestors) if k < level]
            path = "/".join([section["prefix"]] + parents + [title])
            links = p.get("links") or {}
            web_url = (
                (links.get("oneNoteClientUrl") or {}).get("href")
                or (links.get("oneNoteWebUrl") or {}).get("href")
            )
            items.append({
                "id": p.get("id"),
                "name": title,
                "path": path,
                "mime_type": "text/html",
                "modified_at": p.get("lastModifiedDateTime"),
                "created_at": p.get("createdDateTime"),
                "web_url": web_url,
                "section_id": section["id"],
                "notebook": section["notebook"],
                "level": level,
            })
        return items

    def _walk(self, progress_callback: Optional[ProgressCallback] = None) -> List[dict]:
        """Every page in scope, as flat path-carrying items. Memoized."""
        if self._pages_cache is not None:
            return self._pages_cache
        if not self._has_user_token():
            # Admin-save / credential test: OneNote has no app-only mode, so
            # there is nothing to enumerate until a user signs in.
            self._pages_cache = []
            return []

        started = time.perf_counter()
        reporter = make_reporter(progress_callback) if progress_callback else None
        sections = self._sections()
        if reporter is not None:
            reporter.phase("listing sections", total=len(sections))

        pages: List[dict] = []
        # Graph is round-trip bound (~300-600ms per call) and a notebook is
        # section-heavy, so a serial walk spends its whole wall-clock waiting.
        # Concurrency stays modest — the OneNote endpoints throttle harder than
        # the drive ones.
        if sections:
            with ThreadPoolExecutor(max_workers=self.walk_concurrency) as pool:
                for got in pool.map(self._section_pages, sections):
                    pages.extend(got)
                    if reporter is not None:
                        reporter.tick(f"{len(pages)} page(s)")

        # Scope enforcement + stable ordering. Newest first so a truncated
        # catalog keeps the pages anyone is plausibly asking about.
        pages = [p for p in pages if path_matches_globs(p["path"], self.include_globs)]
        pages.sort(key=lambda p: (p.get("modified_at") or ""), reverse=True)
        logger.info(
            "onenote: walked %d section(s) → %d page(s) in %.1fs",
            len(sections), len(pages), time.perf_counter() - started,
        )
        self._pages_cache = pages
        return pages

    def _by_id(self, page_id: str) -> Optional[dict]:
        for p in self._walk():
            if p["id"] == page_id:
                return p
        return None

    def _enforce_page_scope(self, page_id: str) -> Optional[dict]:
        """Resolve a page id and check it against the connection's globs.

        Returns the walked item when known. An id we cannot resolve is still
        allowed through when no glob scope is configured (the walk may be
        stale, and Graph re-checks the user's own ACL anyway), but is denied
        outright when a scope exists — an unresolvable id must never be able
        to bypass the boundary.
        """
        item = self._by_id(page_id)
        if item is None:
            if self.include_globs:
                raise GlobScopeError(
                    f"Page {page_id} is not within this connection's configured scope."
                )
            return None
        if not path_matches_globs(item["path"], self.include_globs):
            raise GlobScopeError(
                f"Page '{item['path']}' is outside this connection's configured scope."
            )
        return item

    # ------------------------------------------------------------------ read

    def _page_html(self, page_id: str) -> str:
        raw = self._get_bytes(f"{self._base()}/pages/{page_id}/content")
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw or "")

    def page_text(self, page_id: str) -> str:
        """Extracted plain text for one page, memoized per client instance."""
        if page_id in self._text_memo:
            return self._text_memo[page_id]
        try:
            text = strip_html(self._page_html(page_id))
        except Exception as e:
            logger.info("onenote: page %s text fetch failed — %s", page_id, str(e)[:200])
            text = ""
        self._text_memo[page_id] = text
        return text

    def _resource_bytes(self, url: str) -> Optional[bytes]:
        try:
            data = self._get_bytes(url)
        except Exception as e:
            logger.info("onenote: resource fetch failed — %s", str(e)[:200])
            return None
        if not data or len(data) > MAX_RESOURCE_BYTES:
            return None
        return data

    def _page_media(self, html_body: str) -> Tuple[List[tuple], List[str], List[dict]]:
        """Pull embedded media out of a page's HTML.

        Returns ``(images, notes, attachments)`` where images are
        ``(bytes, filename)`` ready for the tool layer's renderer, notes are
        human-readable lines about media that cannot be read, and attachments
        are embedded files with their extracted text.
        """
        images: List[tuple] = []
        notes: List[str] = []
        attachments: List[dict] = []

        for i, tag in enumerate(_IMG_RE.findall(html_body or "")):
            if len(images) >= MAX_PAGE_IMAGES:
                notes.append(
                    f"[page has more than {MAX_PAGE_IMAGES} images — only the "
                    f"first {MAX_PAGE_IMAGES} were fetched]"
                )
                break
            a = _attrs(tag)
            src = a.get("data-fullres-src") or a.get("src")
            if not src:
                continue
            mime = (a.get("data-fullres-src-type") or a.get("data-src-type") or "").lower()
            data = self._resource_bytes(src)
            if not data:
                continue
            # The renderer dispatches on EXTENSION and Graph resource ids carry
            # none, so a synthetic name is what makes the image renderable.
            ext = _MIME_EXT.get(mime, "png")
            images.append((data, f"onenote-image-{i + 1}.{ext}"))

        for tag in _OBJECT_RE.findall(html_body or ""):
            a = _attrs(tag)
            src = a.get("data")
            name = a.get("data-attachment") or "attachment"
            if not src:
                continue
            data = self._resource_bytes(src)
            if not data:
                notes.append(f"[embedded attachment '{name}' could not be fetched]")
                continue
            try:
                text = extract_document_text_from_bytes(name, data)
            except Exception:
                text = None
            if text and doc_text_is_usable(text):
                attachments.append({"name": name, "text": text})
            else:
                notes.append(
                    f"[embedded attachment '{name}' is not readable as text — "
                    f"open it in OneNote]"
                )

        for tag in _IFRAME_RE.findall(html_body or ""):
            a = _attrs(tag)
            src = a.get("data-original-src") or a.get("src") or ""
            if src:
                notes.append(
                    f"[embedded video//media on this page is not readable: {src}]"
                )

        return images, notes, attachments

    def read_file(self, file_id: str, **_) -> Any:
        """Read one page LIVE: text plus whatever media it embeds.

        Returns the note-page protocol dict consumed by the read tool — text
        alone would drop the images, and bytes alone would drop the text, so
        the page is returned as both.
        """
        item = self._enforce_page_scope(file_id)
        try:
            body = self._page_html(file_id)
        except Exception as e:
            raise ValueError(f"OneNote page read failed: {e}") from e

        text = strip_html(body)
        self._text_memo[file_id] = text
        images, notes, attachments = self._page_media(body)

        header_bits = []
        if item:
            header_bits.append(f"Page: {item['path']}")
            if item.get("modified_at"):
                header_bits.append(f"Modified: {item['modified_at']}")
            # The OneNote link is what a person can actually click; the opaque
            # page id is useless outside a tool call. It rides in the rendered
            # TEXT because that is what reaches the model and the digest.
            if item.get("web_url"):
                header_bits.append(f"Link: {item['web_url']}")
        header = "\n".join(header_bits)

        parts = [p for p in [header, text] if p]
        for att in attachments:
            parts.append(f"\n--- embedded attachment: {att['name']} ---\n{att['text']}")
        if notes:
            parts.append("\n" + "\n".join(notes))

        return {
            "__note_page__": True,
            "text": "\n\n".join(parts).strip(),
            "images": images,
            "path": item["path"] if item else None,
        }

    def read_raw_bytes(self, file_id: str) -> bytes:
        self._enforce_page_scope(file_id)
        return self._page_html(file_id).encode("utf-8")

    def file_version(self, file_id: str) -> Optional[str]:
        item = self._by_id(file_id)
        return item.get("modified_at") if item else None

    # ------------------------------------------------------------ list/search

    def list_files(
        self,
        folder_id: Optional[str] = None,
        recursive: Optional[bool] = None,
        limit: Optional[int] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> List[dict]:
        """Pages in scope, newest first.

        ``folder_id`` is a SECTION id — the section is the navigable container
        even though it is never a catalog row of its own.
        """
        pages = self._walk(progress_callback=progress_callback)
        if folder_id:
            pages = [p for p in pages if p.get("section_id") == folder_id]
        if limit:
            pages = pages[:limit]
        return [self._public(p) for p in pages]

    @staticmethod
    def _public(p: dict) -> dict:
        """The item shape the tool layer consumes (no internal bookkeeping)."""
        return {
            "id": p["id"],
            "name": p["name"],
            "path": p["path"],
            "mime_type": p.get("mime_type") or "text/html",
            "modified_at": p.get("modified_at"),
            "web_url": p.get("web_url"),
            "size": None,
        }

    def search_files(self, query: str, **_) -> List[dict]:
        """Find pages by title or path.

        Matched locally against the walked hierarchy rather than through
        Graph's page search, which is consumer-notebook-only and does not
        reach work/school notebooks at all. Local matching is also what makes
        non-Latin titles (Hebrew, mixed RTL) work reliably.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = [t for t in q.split() if t]
        hits = []
        for p in self._walk():
            hay = p["path"].lower()
            if all(t in hay for t in terms):
                hits.append(self._public(p))
        return hits

    # ------------------------------------------------------------------ grep

    def grep_files(
        self,
        pattern: str,
        *,
        is_regex: bool = True,
        ignore_case: bool = False,
        file_ids: Optional[List[str]] = None,
        folder_id: Optional[str] = None,
        name_pattern: Optional[str] = None,
        recursive: bool = True,
        before: int = 0,
        after: int = 0,
        max_matches: int = 100,
        max_matches_per_file: int = 50,
        max_files: int = 500,
        cursor: Optional[str] = None,
        **_,
    ) -> dict:
        """Line-level regex sweep over page text.

        Page bodies are fetched (concurrently, then memoized) rather than read
        from the catalog: the client has no handle on the stored rows, and a
        page is small enough that a bounded concurrent prefetch is far cheaper
        than the sweep engine's serial reads would be.
        """
        pages = self._walk()
        if folder_id:
            pages = [p for p in pages if p.get("section_id") == folder_id]
        if file_ids:
            wanted = set(file_ids)
            pages = [p for p in pages if p["id"] in wanted]
        if name_pattern:
            pat = name_pattern.strip()
            pages = [
                p for p in pages
                if path_matches_globs(p["path"], [pat])
                or path_matches_globs(p["name"], [pat])
            ]

        candidates = [
            {"id": p["id"], "path": p["path"], "size": None}
            for p in pages[:max_files]
        ]

        # Warm the memo in parallel; the sweep itself then never blocks on IO.
        todo = [c["id"] for c in candidates if c["id"] not in self._text_memo]
        if todo:
            with ThreadPoolExecutor(max_workers=self.walk_concurrency) as pool:
                list(pool.map(self.page_text, todo))

        def _read(cand: dict) -> bytes:
            return (self._text_memo.get(cand["id"]) or "").encode("utf-8")

        return run_grep_sweep(
            candidates=candidates,
            read_bytes=_read,
            pattern=pattern,
            is_regex=is_regex,
            ignore_case=ignore_case,
            before=before,
            after=after,
            max_matches=max_matches,
            max_matches_per_file=max_matches_per_file,
            max_files=max_files,
            scope_key=f"onenote:{folder_id or ''}:{name_pattern or ''}",
            cursor=cursor,
        )

    # --------------------------------------------------------------- catalog

    @staticmethod
    def _prior_meta(prior_catalog: Optional[dict], name: str) -> dict:
        """The previous run's OneNote metadata for one page path.

        Accepts both catalog shapes the indexer hands out: `prior_catalog`
        (`{name: metadata_json}`, from the shared-catalog refresh) and
        `prior_tables` (`{name: {columns, pks, fks, metadata_json}}`, from the
        per-user sign-in sync). Supporting both matters because the per-user
        path is the ONE that actually runs for a delegated-only source — if the
        skip only understood `prior_catalog`, every sign-in would re-fetch every
        page body and the incremental win would never materialize in practice.
        """
        entry = (prior_catalog or {}).get(name)
        if not isinstance(entry, dict):
            return {}
        if "metadata_json" in entry and isinstance(entry.get("metadata_json"), dict):
            entry = entry["metadata_json"]
        if isinstance(entry, dict):
            return entry.get("onenote") or entry
        return {}

    def get_schemas(
        self,
        progress_callback: Optional[ProgressCallback] = None,
        prior_catalog: Optional[dict] = None,
        prior_tables: Optional[dict] = None,
    ) -> List[Table]:
        """One catalog row per page, per the connection's index tier.

        ``none`` caches nothing (the tool layer lists/reads live). ``metadata``
        stores the path + Graph ids. ``content`` additionally caches the page's
        extracted text and keywords, which is what lets the platform match a
        page by a word in its BODY — the in-page tag lines people write
        precisely so search can find them.

        ``prior_catalog`` makes the content tier incremental: a page whose
        ``lastModifiedDateTime`` is unchanged reuses its stored text instead of
        re-fetching, so a scheduled reindex — or the second user to crawl the
        same shared notebook — pays for listings only.
        """
        prior_catalog = prior_catalog or prior_tables
        if self.index_mode == INDEX_NONE:
            return []
        if not self._has_user_token():
            # No delegated identity yet. Returning [] must be treated as "no
            # information", never as an authoritative empty catalog — see the
            # pruning guard in ConnectionService.
            return []

        reporter = make_reporter(progress_callback)
        pages = self._walk(progress_callback=progress_callback)
        truncated = len(pages) > self.max_catalog_objects
        if truncated:
            # Newest-first ordering from the walk means truncation keeps the
            # pages most likely to be asked about.
            pages = pages[: self.max_catalog_objects]
            logger.warning(
                "onenote: catalog capped at %d page(s) (max_catalog_objects); "
                "narrow the scope with include_globs to index more.",
                self.max_catalog_objects,
            )

        reporter.phase("indexing pages", total=len(pages))
        want_content = self.index_mode == INDEX_CONTENT

        # Fetch bodies for CHANGED pages only, concurrently. The skip check is
        # what collapses an N-user crawl of a shared notebook to listings.
        to_fetch: List[dict] = []
        if want_content:
            for p in pages:
                prior = self._prior_meta(prior_catalog, p["path"])
                if (
                    prior.get("indexed")
                    and prior.get("modified_at")
                    and prior.get("modified_at") == p.get("modified_at")
                ):
                    cached = prior.get("text")
                    if cached is not None:
                        self._text_memo[p["id"]] = cached
                        continue
                to_fetch.append(p)
            if to_fetch:
                with ThreadPoolExecutor(max_workers=self.walk_concurrency) as pool:
                    list(pool.map(lambda x: self.page_text(x["id"]), to_fetch))
            logger.info(
                "onenote: content index — %d page(s) fetched, %d reused from prior catalog",
                len(to_fetch), len(pages) - len(to_fetch),
            )

        tables: List[Table] = []
        for p in pages:
            reporter.tick(p["path"])
            meta: Dict[str, Any] = {
                "page_id": p["id"],
                "section_id": p.get("section_id"),
                "notebook": p.get("notebook"),
                "modified_at": p.get("modified_at"),
                "web_url": p.get("web_url"),
                "level": p.get("level"),
            }
            description = f"OneNote page '{p['name']}' in {p.get('notebook') or 'OneNote'}."
            if want_content:
                text = (self._text_memo.get(p["id"]) or "")[:MAX_INDEXED_TEXT_CHARS]
                meta["text"] = text
                meta["keywords"] = extract_keywords(text, p["name"])
                meta["indexed"] = True
                snippet = " ".join(text.split())[:200]
                if snippet:
                    description += f" {snippet}"
            tables.append(Table(
                name=p["path"],
                description=description,
                columns=[],
                pks=[],
                fks=[],
                metadata_json={"onenote": meta},
            ))
        return tables

    def get_schema(self, table_name: str) -> Optional[Table]:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        return None

    # ------------------------------------------------------------------ test

    def test_connection(self) -> dict:
        """Verify the connection is usable, without enumerating anything.

        Admin-only (no user token yet): OneNote has no app-only mode at all, so
        the most that can be proven is that the app credentials mint a token.
        The notebooks themselves become reachable once a user signs in.
        """
        if not self._has_user_token():
            try:
                self._token()
            except Exception as e:
                return {"success": False, "message": str(e)}
            return {
                "success": True,
                "message": (
                    "App credentials verified. OneNote requires each user to "
                    "sign in with Microsoft — notebooks become available after "
                    "that (Microsoft does not support app-only access to OneNote)."
                ),
            }

        try:
            me = self._get("/me?$select=userPrincipalName,displayName")
            who = me.get("userPrincipalName") or me.get("displayName") or "Microsoft account"
        except Exception as e:
            return {"success": False, "message": str(e)}

        # `/me` only proves the token maps to a directory user. Probe OneNote
        # itself so a missing licence or an unconsented scope surfaces here,
        # where it is actionable, rather than at query time.
        try:
            data = self._get(f"{self._base()}/notebooks?$select=id,displayName&$top=5")
        except Exception as e:
            detail = str(e)
            # 40001 on /me/onenote almost always means the account has no
            # personal notebook STORE — OneNote personal notebooks live in the
            # user's OneDrive, and an account without an Office/Microsoft 365
            # plan never gets one provisioned. The raw Graph text for this is
            # "The request does not contain a valid authentication token",
            # which sends people to debug their perfectly good token for hours.
            if "40001" in detail and self.notebook_source == "me":
                return {
                    "success": False,
                    "message": (
                        f"Signed in as {who}, but this account has no personal "
                        "OneNote store — its licence provisions no OneDrive, "
                        "where personal notebooks live. (Microsoft reports this "
                        "as a misleading 'invalid authentication token'.) Assign "
                        "a Microsoft 365 licence, or point this connection at a "
                        "SharePoint site / Microsoft 365 group notebook instead, "
                        "which needs no personal OneDrive."
                    ),
                }
            if "40007" in detail or "insufficient" in detail.lower() or "403" in detail:
                return {
                    "success": False,
                    "message": (
                        f"Signed in as {who}, but OneNote is not readable: {detail}. "
                        "Check that Notes.Read (and Notes.Read.All for shared "
                        "notebooks) are consented for this app, and that this "
                        "user is a member of the site/group."
                    ),
                }
            return {"success": False, "message": f"Signed in as {who}, but OneNote is unreadable: {detail}"}

        books = [n.get("displayName") for n in (data.get("value") or [])]
        if not books:
            return {
                "success": True,
                "message": f"Connected as {who} — no OneNote notebooks are visible to this account yet.",
            }
        return {
            "success": True,
            "message": f"Connected as {who} — {len(books)} notebook(s): " + ", ".join(
                b for b in books if b
            ),
        }


class OnenoteClient(GraphOneNoteClient):
    """Registry alias (``client_path`` resolves ``<Type>Client``)."""
    pass
