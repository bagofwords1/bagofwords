"""Splunk data source client.

Talks to the Splunk REST API on the management port (`https://<host>:8089`).
There is no SQL endpoint; queries are **SPL** (Search Processing Language)
strings run as oneshot search jobs.

Schema discovery mirrors the **Zabbix** connector's curated-catalog + best-
effort-enrichment discipline, adapted to Splunk's schema-on-read model:

  1. **Tables = `index::sourcetype`.** Enumerated with ONE cheap search
     (`| tstats count where index=* by index, sourcetype`) that reads the
     tsidx *metadata*, not raw events. Cost is O(1) in searches regardless of
     how many sourcetypes exist — the property that keeps the 12h reindex from
     "taking forever".
  2. **Columns = sampled fields, capped.** Splunk has no free field catalog,
     so fields cost a real search. We sample fields for only the **top-K
     sourcetypes by volume** (`… | head N | fieldsummary`), bounded by a time
     window + head cap, cached, and best-effort (a sample failure degrades that
     one table to thin — it never fails discovery). Sourcetypes beyond the cap
     stay **thin** (no columns); the agent discovers their fields on demand via
     a `… | head 5` sample, per `system_prompt()`. An unknown field in Splunk
     is not an error — it silently matches nothing — so the thin-tail path is
     safe, just one extra peek.

Auth is Splunk-native:
  - `token`    → an authentication token sent as `Authorization: Bearer <token>`
                 (Settings → Tokens; works on Splunk Cloud and 8.x+).
  - `userpass` → HTTP basic against the management port (older on-prem installs).
"""
import json
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter


MAX_ROWS = 50_000              # hard cap per execute_query
DEFAULT_LIMIT = 1_000         # when the query spec omits `limit`
HTTP_TIMEOUT = 180            # seconds per REST request (searches can be slow)
SAMPLE_EVENTS = 500          # events sampled per sourcetype for field discovery
DEFAULT_MAX_SAMPLED = 50     # top-K sourcetypes that get field sampling
CATALOG_SEARCH = "| tstats count where index=* by index, sourcetype"


class SplunkClient(DataSourceClient):

    def __init__(
        self,
        host: str,
        port: int = 8089,
        api_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        discovery_window_days: int = 7,
        max_sampled_sourcetypes: int = DEFAULT_MAX_SAMPLED,
    ):
        # Accept a bare host, host:port, or a full URL. Normalize to the
        # management-port base URL (https by default).
        h = (host or "").strip().rstrip("/")
        if h.startswith("http://") or h.startswith("https://"):
            self.base_url = h
        else:
            self.base_url = f"https://{h}:{port}"
        self.host = host
        self.port = port
        self.api_token = api_token or None
        self.username = username or None
        self.password = password or None
        self.verify_ssl = verify_ssl
        self.discovery_window_days = int(discovery_window_days or 7)
        self.max_sampled_sourcetypes = int(max_sampled_sourcetypes or DEFAULT_MAX_SAMPLED)

    @property
    def description(self):
        text = ("Splunk client — investigate machine data (logs/events) across "
                "indexes and sourcetypes with SPL (search, stats, timechart).")
        return text + "\n\n" + self.system_prompt()

    # ── transport ─────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        return {}

    def _auth(self):
        if self.api_token:
            return None
        if self.username:
            return (self.username, self.password or "")
        return None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        params.setdefault("output_mode", "json")
        try:
            resp = requests.get(
                f"{self.base_url}{path}", params=params,
                headers=self._headers(), auth=self._auth(),
                verify=self.verify_ssl, timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"Splunk TLS error: {e}. Set verify_ssl=false for self-signed certs.")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Splunk connection failed to {self.base_url}: {e}")
        return self._parse(resp, path)

    def _parse(self, resp: requests.Response, path: str) -> Any:
        if resp.status_code == 401:
            raise RuntimeError("Splunk authentication failed (401): check the token or username/password.")
        if resp.status_code == 402 or resp.status_code == 403:
            raise RuntimeError(f"Splunk access denied ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"Splunk HTTP error ({resp.status_code}) on {path}: {resp.text[:400]}")
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"Splunk returned non-JSON response on {path}: {resp.text[:300]}")

    # ── search ────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_spl(spl: str) -> str:
        """Splunk's search endpoint requires a leading `search` for bare
        searches; generating/transforming commands begin with `|`."""
        s = (spl or "").strip()
        if not s:
            raise ValueError("Empty SPL search.")
        if s.startswith("|") or s.lower().startswith("search "):
            return s
        return f"search {s}"

    def _run_search(self, spl: str, *, earliest: Optional[str] = None,
                    latest: Optional[str] = None, count: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """Run a oneshot search job and return result rows (list of dicts).

        `exec_mode=oneshot` blocks and returns results in one call — no job
        polling. `count` caps rows server-side.
        """
        params = {
            "search": self._normalize_spl(spl),
            "exec_mode": "oneshot",
            "output_mode": "json",
            "count": int(count),
        }
        if earliest is not None:
            params["earliest_time"] = earliest
        if latest is not None:
            params["latest_time"] = latest
        try:
            resp = requests.post(
                f"{self.base_url}/services/search/jobs",
                data=params, headers=self._headers(), auth=self._auth(),
                verify=self.verify_ssl, timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"Splunk TLS error: {e}. Set verify_ssl=false for self-signed certs.")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Splunk search request failed: {e}")
        body = self._parse(resp, "/services/search/jobs")
        # Surface search-time messages (e.g. bad SPL) as errors.
        for msg in (body.get("messages") or []):
            if msg.get("type", "").upper() in ("ERROR", "FATAL"):
                raise RuntimeError(f"Splunk search error: {msg.get('text')}")
        return body.get("results") or []

    # ── connection ──────────────────────────────────────────────────────────

    def test_connection(self):
        try:
            info = self._get("/services/server/info")
            entries = info.get("entry") or []
            version = "?"
            if entries:
                version = (entries[0].get("content") or {}).get("version", "?")
            return {"success": True, "message": f"Connected to Splunk {version}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── schema discovery ──────────────────────────────────────────────────────

    def _window(self) -> str:
        return f"-{self.discovery_window_days}d"

    def _catalog_pairs(self) -> List[Dict[str, Any]]:
        """Cheap `index::sourcetype` enumeration via tstats (tsidx metadata).

        Falls back to a metadata search if tstats is unavailable (rare).
        """
        try:
            rows = self._run_search(CATALOG_SEARCH, earliest=self._window(),
                                    latest="now", count=MAX_ROWS)
            if rows:
                return rows
        except Exception as e:
            print(f"Splunk tstats catalog failed, falling back to metadata: {e}")
        # Fallback: metadata gives sourcetypes but not the index pairing.
        try:
            rows = self._run_search("| metadata type=sourcetypes index=*",
                                    earliest=self._window(), latest="now", count=MAX_ROWS)
            return [{"index": "*", "sourcetype": r.get("sourcetype"),
                     "count": r.get("totalCount")} for r in rows if r.get("sourcetype")]
        except Exception as e:
            print(f"Splunk metadata catalog failed: {e}")
            return []

    def _sample_fields(self, index: str, sourcetype: str) -> List[TableColumn]:
        """Sample fields for one sourcetype via a bounded fieldsummary search.

        Best-effort: any failure returns [] (the table stays thin). Bounded by
        the discovery window + a head cap so it can't scan all-time.
        """
        idx = "*" if index in (None, "", "*") else index
        spl = (f'search index={idx} sourcetype="{sourcetype}" '
               f'| head {SAMPLE_EVENTS} | fieldsummary maxvals=0')
        try:
            rows = self._run_search(spl, earliest=self._window(), latest="now", count=1000)
        except Exception as e:
            print(f"Splunk field sample failed for {index}::{sourcetype}: {e}")
            return []
        columns: List[TableColumn] = []
        for r in rows:
            field = r.get("field")
            if not field or field.startswith("_") and field not in ("_time", "_raw"):
                # Skip most internal fields; keep _time and _raw (useful).
                if field not in ("_time", "_raw"):
                    continue
            try:
                numeric = int(float(r.get("numeric_count") or 0))
                total = int(float(r.get("count") or 0))
            except (TypeError, ValueError):
                numeric, total = 0, 0
            dtype = "float" if (total and numeric >= total * 0.9) else "str"
            columns.append(TableColumn(name=field, dtype=dtype))
        return columns

    def get_schemas(self, progress_callback=None) -> List[Table]:
        """Discover `index::sourcetype` tables (cheap) and sample fields for
        the top-K sourcetypes by volume (capped)."""
        pairs = self._catalog_pairs()
        # Rank by event count so the sampling budget goes to the sourcetypes
        # people actually query.
        def _cnt(p):
            try:
                return int(float(p.get("count") or 0))
            except (TypeError, ValueError):
                return 0
        pairs = sorted(pairs, key=_cnt, reverse=True)

        total = len(pairs)
        if progress_callback:
            try:
                progress_callback("schema", "splunk catalog", 0, total)
            except Exception:
                pass

        tables: List[Table] = []
        for i, p in enumerate(pairs):
            index = p.get("index") or "*"
            sourcetype = p.get("sourcetype")
            if not sourcetype:
                continue
            name = f"{index}::{sourcetype}"
            count = _cnt(p)
            columns: List[TableColumn] = []
            sampled = i < self.max_sampled_sourcetypes
            if sampled:
                columns = self._sample_fields(index, sourcetype)
            if columns:
                desc = (f"Splunk events: index='{index}', sourcetype='{sourcetype}' "
                        f"(~{count:,} events, fields sampled from last "
                        f"{self.discovery_window_days}d).")
            else:
                desc = (f"Splunk events: index='{index}', sourcetype='{sourcetype}' "
                        f"(~{count:,} events). Schema-on-read: fields NOT pre-sampled "
                        f"(cost cap) — the data IS present. You MUST discover fields "
                        f"yourself, do NOT ask the user: run `search index={index} "
                        f"sourcetype=\"{sourcetype}\" | head 1000 | fieldsummary`, read the "
                        f"field names, then query.")
            tables.append(Table(
                name=name, description=desc, columns=columns, pks=[], fks=[],
                metadata_json={"index": index, "sourcetype": sourcetype,
                               "event_count": count, "fields_sampled": bool(columns)},
            ))
            if progress_callback:
                try:
                    progress_callback("schema", name, i + 1, total)
                except Exception:
                    pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Fields for a single `index::sourcetype` table (samples on demand —
        this is how the thin-tail tables fill in their columns)."""
        if "::" in table_name:
            index, sourcetype = table_name.split("::", 1)
        else:
            index, sourcetype = "*", table_name
        columns = self._sample_fields(index, sourcetype)
        desc = f"Splunk events: index='{index}', sourcetype='{sourcetype}'."
        return Table(name=table_name, description=desc, columns=columns, pks=[], fks=[],
                     metadata_json={"index": index, "sourcetype": sourcetype})

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ── querying ──────────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute an SPL search and return a DataFrame.

        `query` is either a bare SPL string, or a JSON envelope:
            {"spl": "search index=web status>=500 | stats count by host",
             "earliest": "-24h", "latest": "now", "limit": 1000}

        `earliest`/`latest` default to the connection's discovery window and
        `now` when omitted; `limit` caps rows (default 1000, hard cap 50k).
        """
        spl, earliest, latest, limit = self._parse_spec(query)
        rows = self._run_search(spl, earliest=earliest, latest=latest, count=limit)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    def _parse_spec(self, query):
        earliest: Optional[str] = self._window()
        latest: Optional[str] = "now"
        limit = DEFAULT_LIMIT
        spl = None
        if isinstance(query, dict):
            spec = query
        else:
            s = (query or "").strip()
            # A JSON envelope, or a bare SPL string.
            if s.startswith("{"):
                try:
                    spec = json.loads(s)
                except json.JSONDecodeError:
                    spec = {"spl": s}
            else:
                spec = {"spl": s}
        spl = spec.get("spl") or spec.get("search") or spec.get("query")
        if not spl:
            raise ValueError(
                'Splunk query must be an SPL string or a JSON envelope like '
                '{"spl": "search index=web | stats count by host", "earliest": "-24h", "limit": 1000}.'
            )
        if spec.get("earliest") is not None:
            earliest = spec["earliest"]
        if spec.get("latest") is not None:
            latest = spec["latest"]
        if spec.get("limit") is not None:
            try:
                limit = min(int(spec["limit"]), MAX_ROWS)
            except (TypeError, ValueError):
                pass
        return spl, earliest, latest, limit

    # ── prompts ───────────────────────────────────────────────────────────────

    def system_prompt(self):
        text = """
        ## Splunk Integration
        Query Splunk via `execute_query(query)` using SPL (Search Processing
        Language). `query` is either a bare SPL string or a JSON envelope:

        ```json
        {"spl": "search index=web sourcetype=access_combined status>=500 | stats count by host",
         "earliest": "-24h", "latest": "now", "limit": 1000}
        ```

        - Tables are named `index::sourcetype`. Query them with
          `index=<index> sourcetype="<sourcetype>"` in SPL.
        - `earliest`/`latest`: time bounds (e.g. "-24h", "-7d@d", "now",
          epoch). ALWAYS bound a search by time — an unbounded search is slow.
          Defaults are applied if omitted.
        - `limit`: max rows (default 1000).

        IMPORTANT specifics:
        - Splunk is SCHEMA-ON-READ. A table showing NO columns does NOT mean it
          is empty or misconfigured — it means the fields were not pre-sampled
          during indexing (a deliberate cost cap). The data is there. You MUST
          discover the fields YOURSELF by running:
          `search index=<idx> sourcetype="<st>" | head 1000 | fieldsummary`
          (this surfaces auto-extracted JSON/KV fields that a bare `| head` may
          not). Read the field names from that result, THEN write your real
          query. Do NOT ask the user what the fields are, do NOT skip to a
          different table, and do NOT assume the table is empty — just run the
          discovery search and proceed. A field that does not exist is not an
          error; it silently matches nothing, so confirm names via discovery.
        - `_time` is the event time; `_raw` is the raw event text.
        - Use transforming commands to aggregate: `| stats count by host`,
          `| timechart span=1h count`, `| top limit=10 status`.
        - Search across multiple indexes/sourcetypes with `index=*` or
          `(index=web OR index=app)`.

        Examples:
        ```python
        # Discover fields for a thin (un-sampled) sourcetype first
        df = client.execute_query('search index=app sourcetype="log4j" | head 5')
        # Error events per host, last 24h
        df = client.execute_query('{"spl": "search index=* (level=ERROR OR log_level=error) | stats count by host", "earliest": "-24h"}')
        # HTTP 5xx over time
        df = client.execute_query('{"spl": "search index=web sourcetype=access_combined status>=500 | timechart span=1h count", "earliest": "-7d"}')
        ```
        """
        return text


# Alias so dynamic naming ("Splunk" → "SplunkClient") and the explicit
# client_path both resolve to the same class.
SplunkClient = SplunkClient
