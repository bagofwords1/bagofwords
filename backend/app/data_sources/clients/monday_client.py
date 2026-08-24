"""monday.com data source client.

Talks to the monday.com GraphQL API (https://api.monday.com/v2). Boards are
exposed as tables: each board column becomes a schema column, and
`execute_query` returns board items as DataFrame rows via `items_page` with
cursor pagination. There is no SQL endpoint; queries are JSON specs (see
`system_prompt`) that map 1:1 to an `items_page` call with `query_params`.

Auth is a single token in the Authorization header — either the connection's
service API token (`api_token`) or a per-user delegated OAuth token
(`access_token`); the API treats them identically, so no auth-mode branching.

Filter quirk verified live: status/dropdown rules match on label INDICES, not
label text ("Done" matches nothing; its index does). The client translates
label text in `compare_value` to indices using the board's column settings so
generated queries can filter by the human-readable label.
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter

logger = logging.getLogger(__name__)

API_URL = "https://api.monday.com/v2"
# Multi-level boards first became queryable in 2025-10. Pin the oldest current
# stable version that includes them, and still request both hierarchy types
# explicitly so discovery does not depend on monday's version-specific default.
API_VERSION = "2026-04"

MAX_ROWS = 10_000        # hard cap per execute_query
PAGE_SIZE = 500          # items_page maximum page size
DEFAULT_LIMIT = 100      # when the query spec omits `limit`
BOARDS_PAGE = 50         # boards per page during schema discovery
WORKSPACES_PAGE = 50     # workspaces per page during the per-workspace sweep
MAX_BOARDS = 1_000       # discovery cap — beyond this, scope with the config
MAX_WORKSPACES = 1_000   # sanity cap for the workspace sweep
RETRIES = 6              # per-request retries on 429/complexity/5xx

# Board fields fetched during discovery. `type` distinguishes real boards from
# auto-managed subitem shadow boards (sub_items_board) — filtering by name
# prefix ("Subitems of ") wrongly drops real boards monday creates with that
# name (e.g. "Expand subitems into a new board") and misses renamed/localized
# shadow boards.
_BOARD_FIELDS = (
    "id name description board_kind hierarchy_type items_count type "
    "workspace { id name } columns { id title type settings_str }"
)

# monday column type → pandas-ish dtype for prompt schemas.
_TYPE_MAP = {
    "numbers": "float",
    "rating": "int",
    "checkbox": "bool",
    "date": "date",
    "creation_log": "datetime",
    "last_updated": "datetime",
    "timeline": "str",
    "status": "str",
    "dropdown": "str",
    "people": "str",
    "text": "str",
    "long_text": "str",
    "email": "str",
    "phone": "str",
    "link": "str",
    "board_relation": "str",
    "mirror": "str",
    "formula": "str",
    "item_id": "int",
    "hour": "str",
    "week": "str",
    "world_clock": "str",
    "country": "str",
    "location": "str",
    "tags": "str",
    "color_picker": "str",
    "file": "str",
    "vote": "int",
    "name": "str",
}

# Column types whose settings carry an index→label map that rules filter by.
_LABELED_TYPES = {"status", "dropdown"}

# Column types that link to items and expose `linked_item_ids` — each gets a
# companion "<Column> (item_ids)" DataFrame column for precise id-based joins
# (display names are ambiguous: duplicates exist and multi-link cells are
# comma-joined).
_LINKED_ID_TYPES = {"board_relation", "dependency"}
_ITEM_IDS_SUFFIX = " (item_ids)"


class MondayClient(DataSourceClient):

    def __init__(
        self,
        api_token: Optional[str] = None,
        access_token: Optional[str] = None,
        workspaces: Optional[str] = None,
        boards: Optional[str] = None,
    ):
        self.api_token = api_token
        # Per-user delegated OAuth token (from user_connection_credentials);
        # construct_client passes whichever token fields the signature accepts.
        self.access_token = access_token
        self.workspaces = [w.strip() for w in workspaces.split(",") if w.strip()] if workspaces else None
        self.boards = [b.strip() for b in boards.split(",") if b.strip()] if boards else None
        # board catalog cache: list of raw board dicts from the API, and
        # whether it was built with the per-workspace sweep (deep discovery)
        self._boards_cache: Optional[List[dict]] = None
        self._deep_done = False

    # ── transport ───────────────────────────────────────────────────────────

    def _token(self) -> str:
        token = self.api_token or self.access_token
        if not token:
            raise RuntimeError(
                "monday.com client has no credentials: provide an API token or sign in with OAuth."
            )
        return token

    def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        """POST one GraphQL request, absorbing 429s and complexity throttles.

        monday's 429 responses are HTML (not JSON) with a Retry-After header;
        complexity exhaustion arrives as a JSON error containing "reset in N
        seconds". Both are transient — wait and retry rather than fail the
        whole indexing run or query.
        """
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        last_error = "unknown error"
        for attempt in range(RETRIES):
            response = requests.post(
                API_URL,
                json=payload,
                headers={
                    "Authorization": self._token(),
                    "Content-Type": "application/json",
                    "API-Version": API_VERSION,
                },
                timeout=120,
            )
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "30"))
                time.sleep(min(wait, 60))
                continue
            if response.status_code == 401:
                if self.access_token and not self.api_token:
                    raise RuntimeError(
                        "monday.com authentication failed (401): the OAuth token was "
                        "rejected (revoked or the app was uninstalled) — sign in again."
                    )
                raise RuntimeError("monday.com authentication failed (401): check the API token.")
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                time.sleep(2 ** attempt)
                continue
            try:
                body = response.json()
            except ValueError:
                raise RuntimeError(
                    f"monday.com API returned a non-JSON response (HTTP {response.status_code})."
                )
            errors = body.get("errors") or []
            if errors:
                text = json.dumps(errors)
                match = re.search(r"reset in (\d+) seconds", text)
                if match or "ComplexityException" in text or "COMPLEXITY" in text:
                    time.sleep(int(match.group(1)) + 1 if match else 30)
                    continue
                messages = "; ".join(str(e.get("message", e)) for e in errors)
                raise RuntimeError(f"monday.com API error: {messages}")
            return body.get("data") or {}
        raise RuntimeError(f"monday.com API kept throttling/failing ({last_error}); try again later.")

    # ── connection ──────────────────────────────────────────────────────────

    def test_connection(self):
        try:
            try:
                data = self._gql("query { me { name email } account { name } boards (limit: 1) { id } }")
                me = data.get("me") or {}
                account = data.get("account") or {}
                return {
                    "success": True,
                    "message": (
                        f"Connected to monday.com as {me.get('name')} ({me.get('email')}) "
                        f"on account '{account.get('name')}'"
                    ),
                }
            except RuntimeError as e:
                # OAuth tokens minted before me:read was requested (or apps that
                # never enabled it) fail ONLY the Query.me identity probe —
                # boards remain fully queryable, so degrade instead of failing.
                if "Query.me" not in str(e) and "missing required scopes" not in str(e):
                    raise
                data = self._gql("query { account { name } boards (limit: 1) { id } }")
                account = data.get("account") or {}
                return {
                    "success": True,
                    "message": f"Connected to monday.com on account '{account.get('name')}'",
                }
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── board catalog ───────────────────────────────────────────────────────

    def _fetch_workspaces(self) -> List[dict]:
        """All active workspaces visible to the token (member or not).

        Best-effort: discovery must survive a token that cannot list
        workspaces (e.g. an OAuth token minted before workspaces:read was
        requested) — the global board crawl still runs without the sweep.
        """
        workspaces: List[dict] = []
        page = 1
        try:
            while len(workspaces) < MAX_WORKSPACES:
                data = self._gql(
                    """
                    query ($limit: Int!, $page: Int!) {
                      workspaces (limit: $limit, page: $page, state: active,
                                  membership_kind: all) {
                        id name kind
                      }
                    }
                    """,
                    {"limit": WORKSPACES_PAGE, "page": page},
                )
                batch = [w for w in (data.get("workspaces") or []) if w]
                if not batch:
                    break
                workspaces.extend(batch)
                page += 1
        except RuntimeError as e:
            logger.warning("monday discovery: workspace listing failed (%s) — "
                           "continuing with the global board crawl only.", e)
        return workspaces

    def _fetch_boards(self, deep: bool = False) -> List[dict]:
        """All active boards visible to the token, with columns.

        Discovery deliberately combines two passes:

        1. A global `boards (limit, page)` crawl. Pagination stops only on an
           EMPTY page — monday does not guarantee non-final pages are full, and
           breaking on a short page silently truncates every board after it.
        2. With `deep=True`, a per-workspace id sweep
           (`boards (workspace_ids: [...])`), the strategy monday's own MCP
           server uses. The global listing is known to omit boards that
           direct/workspace-scoped queries return (observed with shareable
           boards and cross-product workspaces); any board the sweep finds
           that the crawl missed is recovered via `boards (ids:)`.

        Schema discovery always runs deep. Query-time board resolution starts
        shallow (one crawl) and escalates to deep only when the board isn't
        found, so per-query latency doesn't grow with the workspace count.
        """
        if self._boards_cache is not None and (self._deep_done or not deep):
            return self._boards_cache

        boards_by_id: Dict[str, dict] = {}
        page_sizes: List[int] = []
        truncated = False
        page = 1
        while True:
            data = self._gql(
                f"""
                query ($limit: Int!, $page: Int!) {{
                  boards (limit: $limit, page: $page, state: active, order_by: created_at,
                          hierarchy_types: [classic, multi_level]) {{
                    {_BOARD_FIELDS}
                  }}
                }}
                """,
                {"limit": BOARDS_PAGE, "page": page},
            )
            batch = [b for b in (data.get("boards") or []) if b]
            page_sizes.append(len(batch))
            if not batch:
                break
            for board in batch:
                boards_by_id.setdefault(str(board["id"]), board)
            if len(boards_by_id) >= MAX_BOARDS:
                truncated = True
                break
            page += 1
        # page_sizes ends [..., last_non_empty, 0] on a full crawl — a short
        # LAST non-empty page is normal; short pages before it are not.
        if any(0 < size < BOARDS_PAGE for size in page_sizes[:-2]):
            logger.info("monday discovery: global crawl returned short non-final "
                        "pages %s — continued to the empty page.", page_sizes)

        # Per-workspace sweep: cheap id-only pages, then recover full payloads
        # for boards the global crawl did not return.
        missing_ids: List[str] = []
        for workspace in (self._fetch_workspaces() if deep else []):
            page = 1
            while True:
                data = self._gql(
                    """
                    query ($limit: Int!, $page: Int!, $ws: [ID]) {
                      boards (limit: $limit, page: $page, state: active,
                              hierarchy_types: [classic, multi_level], workspace_ids: $ws) {
                        id
                      }
                    }
                    """,
                    {"limit": BOARDS_PAGE, "page": page, "ws": [workspace["id"]]},
                )
                batch = [b for b in (data.get("boards") or []) if b]
                if not batch:
                    break
                for board in batch:
                    board_id = str(board["id"])
                    if board_id not in boards_by_id and board_id not in missing_ids:
                        missing_ids.append(board_id)
                page += 1
        for start in range(0, len(missing_ids), BOARDS_PAGE):
            chunk = missing_ids[start:start + BOARDS_PAGE]
            data = self._gql(
                f"query ($ids: [ID!]) {{ boards (ids: $ids, state: active) {{ {_BOARD_FIELDS} }} }}",
                {"ids": chunk},
            )
            for board in (data.get("boards") or []):
                if board:
                    boards_by_id.setdefault(str(board["id"]), board)
        if missing_ids:
            logger.warning("monday discovery: %d board(s) were missing from the global "
                           "listing and recovered via the workspace sweep: %s",
                           len(missing_ids), missing_ids[:20])

        boards = list(boards_by_id.values())

        # Subitem boards are auto-managed shadows of their parent board. Filter
        # by API type; fall back to the name prefix only when `type` is absent.
        def _is_subitem_board(board: dict) -> bool:
            board_type = board.get("type")
            if board_type is not None:
                return board_type == "sub_items_board"
            return (board.get("name") or "").startswith("Subitems of ")

        boards = [b for b in boards if not _is_subitem_board(b)]

        if truncated or len(boards) > MAX_BOARDS:
            boards = boards[:MAX_BOARDS]
            logger.warning("monday discovery: board catalog truncated at the %d-board "
                           "cap — scope the connection with the workspaces/boards "
                           "config to index the rest.", MAX_BOARDS)

        if self.workspaces:
            wanted = {w.lower() for w in self.workspaces}
            # Boards in the legacy Main workspace come back with workspace: null
            # (documented) — no id or name can match them, so let the aliases
            # "main workspace" / "main" select them explicitly.
            main_wanted = bool(wanted & {"main workspace", "main"})
            boards = [
                b for b in boards
                if (
                    (b.get("workspace") is None and main_wanted)
                    or str((b.get("workspace") or {}).get("id")) in wanted
                    or ((b.get("workspace") or {}).get("name") or "").lower() in wanted
                )
            ]
        if self.boards:
            wanted = {x.lower() for x in self.boards}
            boards = [
                b for b in boards
                if str(b.get("id")) in wanted or (b.get("name") or "").lower() in wanted
            ]
        logger.info("monday discovery: %d board(s) in the catalog "
                    "(global crawl pages %s, %d recovered via workspace sweep%s).",
                    len(boards), page_sizes, len(missing_ids),
                    "" if deep else "; sweep skipped (shallow)")
        self._boards_cache = boards
        self._deep_done = deep
        return boards

    def _table_names(self, boards: List[dict]) -> Dict[str, dict]:
        """Stable table name per board: the board name, disambiguated with the
        board id when several boards share one name."""
        by_name: Dict[str, List[dict]] = {}
        for board in boards:
            by_name.setdefault(board.get("name") or f"board {board['id']}", []).append(board)
        names: Dict[str, dict] = {}
        for name, group in by_name.items():
            if len(group) == 1:
                names[name] = group[0]
            else:
                for board in group:
                    names[f"{name} [{board['id']}]"] = board
        return names

    def _resolve_board(self, ref) -> dict:
        """Accept a board id (int/str), exact board name, or disambiguated
        'name [id]' and return the raw board dict.

        Starts from the shallow catalog; a miss escalates once to deep
        discovery (per-workspace sweep) before failing, so boards the global
        listing omits are still queryable."""
        try:
            return self._resolve_board_in(self._fetch_boards(), ref)
        except ValueError:
            if self._deep_done:
                raise
            self._boards_cache = None
            return self._resolve_board_in(self._fetch_boards(deep=True), ref)

    def _resolve_board_in(self, boards: List[dict], ref) -> dict:
        text = str(ref).strip()
        for board in boards:
            if str(board["id"]) == text:
                return board
        match = re.match(r"^(.*) \[(\d+)\]$", text)
        if match:
            for board in boards:
                if str(board["id"]) == match.group(2):
                    return board
        lowered = text.lower()
        named = [b for b in boards if (b.get("name") or "").lower() == lowered]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            options = ", ".join(f"{b['name']} [{b['id']}]" for b in named)
            raise ValueError(
                f"Board name {text!r} is ambiguous — use the board id or one of: {options}"
            )
        raise ValueError(f"Board {text!r} not found (by id or name) among {len(boards)} visible boards.")

    # ── schema discovery ────────────────────────────────────────────────────

    @staticmethod
    def _column_labels(column: dict) -> Optional[Dict[str, str]]:
        """index → label for status/dropdown columns, from settings_str."""
        if column.get("type") not in _LABELED_TYPES:
            return None
        try:
            settings = json.loads(column.get("settings_str") or "{}")
        except ValueError:
            return None
        if column["type"] == "status":
            labels = settings.get("labels") or {}
            return {str(k): str(v) for k, v in labels.items()} or None
        labels = (settings.get("settings") or settings).get("labels") or []
        if isinstance(labels, list):
            return {str(l.get("id")): str(l.get("name")) for l in labels if l.get("name")} or None
        return None

    def _build_table(self, name: str, board: dict, board_names_by_id: Dict[str, str]) -> Table:
        columns = [
            TableColumn(name="item_id", dtype="int", description="monday item id"),
            TableColumn(name="name", dtype="str", description="item name"),
            TableColumn(name="group", dtype="str", description="board group the item belongs to"),
        ]
        fks: List[ForeignKey] = []
        seen = {"item_id", "name", "group"}
        for column in board.get("columns") or []:
            if column.get("type") == "name":
                continue  # the built-in first column IS the item name above
            title = column.get("title") or column.get("id")
            col_name = title if title not in seen else f"{title} ({column['id']})"
            seen.add(col_name)
            dtype = _TYPE_MAP.get(column.get("type"), "str")
            parts = [f"type: {column.get('type')}", f"id: {column.get('id')}"]
            labels = self._column_labels(column)
            if labels:
                parts.append("labels: " + ", ".join(sorted(labels.values())))
            table_column = TableColumn(name=col_name, dtype=dtype, description="; ".join(parts))
            columns.append(table_column)

            if column.get("type") in _LINKED_ID_TYPES:
                # Companion ids column: the joinable counterpart of the
                # display-name column (query results carry both).
                ids_column = TableColumn(
                    name=f"{col_name}{_ITEM_IDS_SUFFIX}",
                    dtype="list[int]",
                    description=f"item ids of the items linked in {col_name!r}; "
                                "explode and merge on the linked board's item_id",
                )
                seen.add(ids_column.name)
                columns.append(ids_column)

            if column.get("type") == "board_relation":
                try:
                    settings = json.loads(column.get("settings_str") or "{}")
                    for linked_id in settings.get("boardIds") or []:
                        linked_name = board_names_by_id.get(str(linked_id))
                        if linked_name:
                            fks.append(ForeignKey(
                                column=ids_column,
                                references_name=linked_name,
                                references_column=TableColumn(name="item_id", dtype="int"),
                            ))
                except ValueError:
                    pass

        workspace = (board.get("workspace") or {}).get("name")
        description_bits = [bit for bit in [
            board.get("description"),
            f"workspace: {workspace}" if workspace else None,
            f"{board.get('items_count')} items" if board.get("items_count") is not None else None,
        ] if bit]
        return Table(
            name=name,
            columns=columns,
            pks=[TableColumn(name="item_id", dtype="int")],
            fks=fks,
            description="; ".join(description_bits) or None,
            metadata_json={
                "board_id": str(board["id"]),
                "workspace": workspace,
                "hierarchy_type": board.get("hierarchy_type") or "classic",
            },
        )

    def get_schemas(self, progress_callback=None) -> List[Table]:
        boards = self._fetch_boards(deep=True)
        names = self._table_names(boards)
        board_names_by_id = {str(b["id"]): n for n, b in names.items()}
        tables: List[Table] = []
        total = len(names)
        for i, (name, board) in enumerate(names.items()):
            tables.append(self._build_table(name, board, board_names_by_id))
            if progress_callback and (i % 25 == 0 or i == total - 1):
                try:
                    progress_callback(current=i + 1, total=total, message=f"Indexed {i + 1}/{total} boards")
                except TypeError:
                    pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        board = self._resolve_board(table_name)
        boards = self._fetch_boards()  # cached by the resolve above
        names = self._table_names(boards)
        board_names_by_id = {str(b["id"]): n for n, b in names.items()}
        name = board_names_by_id.get(str(board["id"]), board.get("name") or str(board["id"]))
        return self._build_table(name, board, board_names_by_id)

    # ── querying ────────────────────────────────────────────────────────────

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a monday.com query spec and return a DataFrame.

        `query` is a JSON string:
            {"board": "Sales Pipeline Q3",          (required: board name or id)
             "columns": ["Status", "Amount"],       (optional: titles or ids)
             "rules": [{"column_id": "Status",
                        "compare_value": ["Done"],
                        "operator": "any_of"}],     (optional items_page rules)
             "operator": "and",                     (optional: and | or)
             "order_by": {"column_id": "Due Date",
                          "direction": "desc"},     (optional)
             "limit": 500}                          (optional, default 100)
        """
        spec = self._parse_spec(query)
        board = self._resolve_board(spec["board"])
        board_columns = [c for c in (board.get("columns") or []) if c.get("type") != "name"]
        by_key = self._columns_by_key(board_columns)

        selected = self._selected_columns(spec.get("columns"), board_columns, by_key)
        query_params = self._build_query_params(spec, by_key)
        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)

        column_ids = [c["id"] for c in selected]
        items = self._fetch_items(board["id"], column_ids, query_params, limit)
        return self._items_to_df(items, selected)

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = query
        else:
            try:
                spec = json.loads(query)
            except (TypeError, ValueError):
                raise ValueError(
                    "monday.com query must be a JSON object like "
                    '{"board": "Sales Pipeline", "limit": 100} — got: ' + str(query)[:200]
                )
        if not isinstance(spec, dict) or not spec.get("board"):
            raise ValueError('monday.com query spec must include a "board" key (board name or id).')
        return spec

    @staticmethod
    def _columns_by_key(board_columns: List[dict]) -> Dict[str, dict]:
        """id and lowercase-title lookup for a board's columns."""
        by_key: Dict[str, dict] = {}
        for column in board_columns:
            by_key[column["id"]] = column
            title = (column.get("title") or "").lower()
            # first column wins on duplicate titles; ids stay unambiguous
            by_key.setdefault(title, column)
        return by_key

    # Always-present DataFrame columns. The published schema lists them on every
    # board table, so generated specs legitimately request them — accept and
    # skip them instead of failing (they are returned regardless).
    _BASE_COLUMNS = {"item_id", "name", "group"}

    def _selected_columns(self, requested, board_columns: List[dict], by_key: Dict[str, dict]) -> List[dict]:
        if not requested:
            return board_columns
        selected, missing = [], []
        for ref in requested:
            if str(ref).lower() in self._BASE_COLUMNS:
                continue
            column = by_key.get(str(ref)) or by_key.get(str(ref).lower())
            if column is None and str(ref).lower().endswith(_ITEM_IDS_SUFFIX):
                # The schema publishes "<Column> (item_ids)" companions for
                # linked columns — selecting one selects its parent column
                # (the companion is emitted alongside it).
                base = str(ref)[:-len(_ITEM_IDS_SUFFIX)]
                column = by_key.get(base) or by_key.get(base.lower())
            if column is None:
                missing.append(str(ref))
            elif column not in selected:
                selected.append(column)
        if missing:
            available = ", ".join(f"{c['title']} ({c['id']})" for c in board_columns)
            raise ValueError(
                f"Unknown column(s) {missing} — available columns: {available} "
                "(item_id, name and group are always included and need not be requested)"
            )
        return selected

    def _build_query_params(self, spec: dict, by_key: Dict[str, dict]) -> Optional[dict]:
        rules = spec.get("rules")
        order_by = spec.get("order_by")
        if not rules and not order_by:
            return None
        query_params: Dict[str, Any] = {}
        if rules:
            resolved_rules = []
            for rule in rules:
                rule = dict(rule)
                ref = str(rule.get("column_id", ""))
                column = by_key.get(ref) or by_key.get(ref.lower())
                if column is not None:
                    rule["column_id"] = column["id"]
                    rule["compare_value"] = self._translate_labels(column, rule.get("compare_value"))
                resolved_rules.append(rule)
            query_params["rules"] = resolved_rules
            query_params["operator"] = spec.get("operator") or "and"
        if order_by:
            entries = order_by if isinstance(order_by, list) else [order_by]
            resolved_order = []
            for entry in entries:
                entry = dict(entry)
                ref = str(entry.get("column_id", ""))
                column = by_key.get(ref) or by_key.get(ref.lower())
                if column is not None:
                    entry["column_id"] = column["id"]
                resolved_order.append(entry)
            query_params["order_by"] = resolved_order
        return query_params

    def _translate_labels(self, column: dict, compare_value):
        """Rules on status/dropdown columns match label INDICES (verified live:
        text values match nothing) — translate label text to index."""
        labels = self._column_labels(column)
        if not labels or not isinstance(compare_value, list):
            return compare_value
        by_text = {v.lower(): k for k, v in labels.items()}
        translated = []
        for value in compare_value:
            if isinstance(value, str) and value.lower() in by_text:
                index = by_text[value.lower()]
                translated.append(int(index) if index.lstrip("-").isdigit() else index)
            else:
                translated.append(value)
        return translated

    # Relation-family column values (mirror, connect-boards, dependency,
    # subitems) return an EMPTY `text` — their display string lives on the
    # type-specific `display_value` field (verified live; monday's own SDK and
    # Airbyte's connector request the same fragments). Without these, every
    # such column reads as None in query results.
    _COLUMN_VALUE_FRAGMENTS = (
        " ... on MirrorValue { display_value }"
        " ... on BoardRelationValue { display_value linked_item_ids }"
        " ... on DependencyValue { display_value linked_item_ids }"
        " ... on SubtasksValue { display_value }"
    )

    def _fetch_items(self, board_id, column_ids: List[str], query_params: Optional[dict], limit: int) -> List[dict]:
        items: List[dict] = []
        item_fields = (
            "id name group { title } column_values (ids: $cols) { id text value type"
            + self._COLUMN_VALUE_FRAGMENTS + " }"
            if column_ids else "id name group { title }"
        )
        first_page = min(PAGE_SIZE, limit)
        variables: Dict[str, Any] = {"bid": [str(board_id)], "limit": first_page}
        if column_ids:
            variables["cols"] = column_ids
        params_arg = ""
        if query_params:
            variables["qp"] = query_params
            params_arg = ", query_params: $qp"
        cols_var = ", $cols: [String!]" if column_ids else ""
        qp_var = ", $qp: ItemsQuery" if query_params else ""
        data = self._gql(
            f"query ($bid: [ID!], $limit: Int!{cols_var}{qp_var}) {{"
            f" boards (ids: $bid) {{ items_page (limit: $limit{params_arg}) {{"
            f" cursor items {{ {item_fields} }} }} }} }}",
            variables,
        )
        boards = data.get("boards") or []
        if not boards:
            raise RuntimeError(f"Board {board_id} not accessible with these credentials.")
        page = boards[0]["items_page"]
        items.extend(page["items"])
        cursor = page.get("cursor")

        while cursor and len(items) < limit:
            page_size = min(PAGE_SIZE, limit - len(items))
            next_vars: Dict[str, Any] = {"cursor": cursor, "limit": page_size}
            if column_ids:
                next_vars["cols"] = column_ids
            data = self._gql(
                f"query ($cursor: String!, $limit: Int!{cols_var}) {{"
                f" next_items_page (cursor: $cursor, limit: $limit) {{"
                f" cursor items {{ {item_fields} }} }} }}",
                next_vars,
            )
            page = data.get("next_items_page") or {}
            items.extend(page.get("items") or [])
            cursor = page.get("cursor")
        return items[:limit]

    def _items_to_df(self, items: List[dict], selected: List[dict]) -> pd.DataFrame:
        # DataFrame column name per board column: title, disambiguated like the
        # schema. Linked columns (connect-boards, dependency) additionally get
        # a "<Column> (item_ids)" companion carrying the linked item ids, so
        # joins run on ids instead of ambiguous display names.
        names: Dict[str, str] = {}
        id_companions: Dict[str, str] = {}
        seen = {"item_id", "name", "group"}
        for column in selected:
            title = column.get("title") or column["id"]
            name = title if title not in seen else f"{title} ({column['id']})"
            seen.add(name)
            names[column["id"]] = name
            if column.get("type") in _LINKED_ID_TYPES:
                companion = f"{name}{_ITEM_IDS_SUFFIX}"
                seen.add(companion)
                id_companions[column["id"]] = companion

        rows = []
        for item in items:
            row: Dict[str, Any] = {
                "item_id": int(item["id"]),
                "name": item.get("name"),
                "group": (item.get("group") or {}).get("title"),
            }
            for value in item.get("column_values") or []:
                target = names.get(value.get("id"))
                if target is None:
                    continue
                row[target] = self._cell_value(value)
                companion = id_companions.get(value.get("id"))
                if companion is not None:
                    row[companion] = [int(i) for i in value.get("linked_item_ids") or []]
            rows.append(row)

        # Deterministic shape regardless of returned values: base columns +
        # every selected column (each linked column followed by its ids
        # companion), in board order (a column that is empty on all returned
        # items would otherwise vanish from the frame).
        expected = ["item_id", "name", "group"]
        for column_id, name in names.items():
            expected.append(name)
            if column_id in id_companions:
                expected.append(id_companions[column_id])
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=expected)
        return frame.reindex(columns=expected)

    @staticmethod
    def _cell_value(value: dict):
        """One typed scalar per cell. `text` is monday's display string; typed
        columns parse it (or the raw JSON `value`) into a real dtype.
        Relation-family columns carry their display string in `display_value`
        instead of `text` (which comes back empty) — backfill it."""
        kind = value.get("type")
        text = value.get("text")
        if text in (None, "") and value.get("display_value"):
            text = value["display_value"]
        if kind == "numbers":
            if text in (None, ""):
                return None
            try:
                return float(text)
            except ValueError:
                return text
        if kind == "rating":
            if text in (None, ""):
                return None
            try:
                return int(float(text))
            except ValueError:
                return text
        if kind == "checkbox":
            try:
                raw = json.loads(value.get("value") or "null") or {}
                return str(raw.get("checked")).lower() == "true"
            except ValueError:
                return False
        return text if text != "" else None

    # ── prompts ─────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        text = """
        ## monday.com Integration
        Query monday.com boards via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"board": "Sales Pipeline Q3",
         "columns": ["Status", "Amount", "Due Date"],
         "rules": [{"column_id": "Status", "compare_value": ["Done", "In Progress"], "operator": "any_of"}],
         "operator": "and",
         "order_by": {"column_id": "Due Date", "direction": "desc"},
         "limit": 500}
        ```

        - `board` (required): board name (as shown in the schema) or numeric board id.
        - `columns` (optional): column titles or ids to return; omit for all columns.
        - `rules` (optional): items_page filter rules, combined with `operator`
          ("and"/"or"). Useful operators: `any_of` / `not_any_of` (status,
          dropdown, people), `contains_text` / `not_contains_text` (text and
          item names via column_id "name"), `is_empty` / `is_not_empty`.
          For status/dropdown you can pass label text ("Done") — it is
          translated to the label index automatically.
        - `order_by` (optional): {"column_id": ..., "direction": "asc"|"desc"}.
        - `limit` (optional): max rows, default 100, cap 10000.

        The DataFrame has `item_id`, `name` (item name), `group`, then one column
        per board column, named by column TITLE. Types: numbers → float,
        rating → int, checkbox → bool, everything else (status, people, date,
        timeline, dropdown, …) → display TEXT strings; empty cells → None.
        Status columns hold label text ("Done"), date columns "YYYY-MM-DD"
        strings (parse with pd.to_datetime), timeline "YYYY-MM-DD - YYYY-MM-DD".
        Connect-boards / mirror / dependency / subitems columns hold the
        comma-separated display names of the linked items. Connect-boards and
        dependency columns ALSO come with a companion column
        "<Column> (item_ids)" holding the linked monday item ids as a list —
        always join on it, never on display names (names duplicate).

        Date/number comparisons in `rules` are unreliable in the monday API —
        fetch the rows (set `limit` high enough) and filter/aggregate in pandas
        instead. Do NOT try to join boards in the API; fetch each board and
        merge in pandas: explode the "<Column> (item_ids)" companion and merge
        it against the linked board's `item_id` (the schema's foreign keys show
        which board each connect-boards column links to). Mirror columns are
        display text only and cannot be joined on.

        Examples:
        ```python
        df = client.execute_query('{"board": "Engineering Bug Tracker", "columns": ["Status", "Priority", "Due Date"], "rules": [{"column_id": "Status", "compare_value": ["Done"], "operator": "not_any_of"}], "limit": 1000}')
        df = client.execute_query('{"board": "Sales Pipeline Q3", "limit": 2000}')
        df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")  # numbers columns are already float; only needed for text columns
        # cross-board join via a connect-boards column:
        deals = client.execute_query('{"board": "Sales Pipeline Q3", "limit": 2000}')
        accounts = client.execute_query('{"board": "Accounts", "limit": 2000}')
        joined = (deals.explode("Account (item_ids)")
                       .merge(accounts, left_on="Account (item_ids)", right_on="item_id",
                              how="left", suffixes=("", " (Account)")))
        ```
        """
        return text

    @property
    def description(self):
        text = "monday.com client — query boards as tables (items, statuses, dates, people, numbers) via the GraphQL API."
        return text + "\n\n" + self.system_prompt()
