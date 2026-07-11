"""Zabbix data source client.

Talks to the Zabbix JSON-RPC 2.0 API (`<url>/api_jsonrpc.php`). There is no SQL
endpoint; queries are JSON specs (see `system_prompt`) that map 1:1 to a Zabbix
API method call (`host.get`, `item.get`, `problem.get`, `history.get`, …).

Schema discovery is a *fixed* catalog of virtual tables (hosts, items,
triggers, problems, events, history, trends, …) — Zabbix's data model is
stable, so the columns are declared in code (like the PostHog connector) rather
than introspected. The `items` table is optionally enriched from a live
`item.get` so the agent sees the value-types actually present.

Auth is Zabbix-native and version-dependent:
  - `token`    → an API token sent as `Authorization: Bearer <token>`
                 (Zabbix 5.4+/6.4+; the recommended path, incl. SSO orgs where
                 an SSO user still mints a personal API token in the UI).
  - `userpass` → `user.login` returns a session token passed in the request's
                 `auth` field (works on older on-prem installs / LDAP).
Zabbix's SAML/OIDC SSO governs the *frontend* only; the API never accepts an
external IdP token, so there is no on-behalf-of / delegated variant.
"""
import json
from contextlib import contextmanager
from typing import Generator, List, Optional

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter


MAX_ROWS = 50_000          # hard cap per execute_query
DEFAULT_LIMIT = 500        # when the query spec omits `limit`
ITEM_ENRICH_LIMIT = 2_000  # items sampled to enrich the `items` virtual table
HTTP_TIMEOUT = 120         # seconds per JSON-RPC request

# Zabbix item value_type → dtype for prompt schemas.
_VALUE_TYPE = {
    0: "float",   # numeric float
    1: "str",     # character
    2: "str",     # log
    3: "int",     # numeric unsigned
    4: "str",     # text
}

# Trigger/problem/event severity codes (0-5). Documented for the agent; results
# also carry the raw code so filtering stays exact.
SEVERITY_LABELS = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}


# ── virtual table catalog ─────────────────────────────────────────────────────
#
# Each virtual table maps to a Zabbix `*.get` method. `columns` is the curated
# set of useful output fields; `pk` is the id field; `fks` wire the graph so the
# planner understands host ⇄ item ⇄ trigger/history relationships. Timestamps
# named `clock` are Unix epoch seconds (documented in system_prompt).
_CATALOG = {
    "hosts": {
        "method": "host.get",
        "pk": "hostid",
        "columns": [
            ("hostid", "int"), ("host", "str"), ("name", "str"),
            ("status", "int"), ("available", "int"), ("description", "str"),
        ],
        "fks": [],
        "desc": "Monitored hosts. status 0=enabled,1=disabled. available 1=available,2=unavailable.",
    },
    "host_groups": {
        "method": "hostgroup.get",
        "pk": "groupid",
        "columns": [("groupid", "int"), ("name", "str")],
        "fks": [],
        "desc": "Host groups (logical groupings of hosts).",
    },
    "items": {
        "method": "item.get",
        "pk": "itemid",
        "columns": [
            ("itemid", "int"), ("hostid", "int"), ("name", "str"), ("key_", "str"),
            ("value_type", "int"), ("units", "str"), ("delay", "str"),
            ("lastvalue", "str"), ("lastclock", "int"), ("status", "int"),
        ],
        "fks": [("hostid", "hosts", "hostid")],
        "desc": "Metrics collected per host. value_type 0=float,1=char,3=unsigned int,2=log,4=text.",
    },
    "triggers": {
        "method": "trigger.get",
        "pk": "triggerid",
        "columns": [
            ("triggerid", "int"), ("description", "str"), ("priority", "int"),
            ("status", "int"), ("value", "int"), ("lastchange", "int"),
            ("comments", "str"),
        ],
        "fks": [],
        "desc": "Trigger (alert) definitions. priority=severity 0-5. value 0=OK,1=PROBLEM. lastchange is a clock.",
    },
    "problems": {
        "method": "problem.get",
        "pk": "eventid",
        "columns": [
            ("eventid", "int"), ("objectid", "int"), ("name", "str"),
            ("severity", "int"), ("clock", "int"), ("r_clock", "int"),
            ("acknowledged", "int"),
        ],
        "fks": [("objectid", "triggers", "triggerid")],
        "desc": "Currently-active problems. severity 0-5. clock=start (epoch). r_clock=resolution (0 if open).",
    },
    "events": {
        "method": "event.get",
        "pk": "eventid",
        "columns": [
            ("eventid", "int"), ("source", "int"), ("object", "int"),
            ("objectid", "int"), ("name", "str"), ("severity", "int"),
            ("value", "int"), ("clock", "int"), ("acknowledged", "int"),
        ],
        "fks": [("objectid", "triggers", "triggerid")],
        "desc": "Historical events (alert state changes). value 0=OK/recovery,1=PROBLEM. clock is epoch.",
    },
    "history": {
        "method": "history.get",
        "pk": None,
        "columns": [
            ("itemid", "int"), ("clock", "int"), ("value", "float"), ("ns", "int"),
        ],
        "fks": [("itemid", "items", "itemid")],
        "desc": ("Raw metric values (time series). REQUIRES itemids AND the correct "
                 "`history` value-type (0 float default, 3 unsigned int, 1 char, 2 log, 4 text) "
                 "matching the item's value_type. clock is epoch seconds."),
    },
    "trends": {
        "method": "trend.get",
        "pk": None,
        "columns": [
            ("itemid", "int"), ("clock", "int"),
            ("num", "int"), ("value_min", "float"),
            ("value_avg", "float"), ("value_max", "float"),
        ],
        "fks": [("itemid", "items", "itemid")],
        "desc": "Hourly aggregated numeric trends (min/avg/max). REQUIRES itemids. clock is the hour epoch.",
    },
}


class ZabbixClient(DataSourceClient):

    def __init__(
        self,
        url: str,
        api_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        history_window_days: int = 7,
    ):
        # Accept a bare host, a frontend URL, or the full endpoint. Normalize to
        # the JSON-RPC endpoint so both "https://zbx" and
        # "https://zbx/api_jsonrpc.php" work.
        base = (url or "").rstrip("/")
        if base.endswith("/api_jsonrpc.php"):
            self.endpoint = base
        else:
            self.endpoint = f"{base}/api_jsonrpc.php"
        self.api_token = api_token or None
        self.username = username or None
        self.password = password or None
        self.verify_ssl = verify_ssl
        self.history_window_days = int(history_window_days or 7)
        # Session token from user.login (userpass auth); lazily populated.
        self._session_token: Optional[str] = None

    @property
    def description(self):
        text = ("Zabbix client — query monitoring data (hosts, items, triggers, active "
                "problems, events, and metric history/trends) via the JSON-RPC API.")
        return text + "\n\n" + self.system_prompt()

    # ── connection ────────────────────────────────────────────────────────────

    @contextmanager
    def connect(self) -> Generator[requests.Session, None, None]:
        session = requests.Session()
        session.headers.update({"Content-Type": "application/json-rpc"})
        session.verify = self.verify_ssl
        # NB: the Bearer header is attached per-request in `_rpc`, not here —
        # Zabbix 7.0 rejects `apiinfo.version`/`user.login` if an Authorization
        # header is present, so unauthed calls must go out without it.
        try:
            yield session
        finally:
            session.close()

    def _rpc(self, session: requests.Session, method: str, params, *, auth: bool = True) -> object:
        """One JSON-RPC 2.0 call. Raises a readable RuntimeError on transport or
        API-level errors."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        headers = None
        # Token auth → Bearer header, but ONLY on authed calls (Zabbix 7.0
        # rejects apiinfo.version/user.login when the header is present).
        # Session auth (<6.4 style) → the token goes in the `auth` field.
        if auth:
            if self.api_token:
                headers = {"Authorization": f"Bearer {self.api_token}"}
            else:
                token = self._session_token or self._login(session)
                payload["auth"] = token

        try:
            resp = session.post(self.endpoint, data=json.dumps(payload),
                                headers=headers, timeout=HTTP_TIMEOUT)
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"Zabbix TLS error: {e}. Set verify_ssl=false for self-signed certs.")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Zabbix connection failed to {self.endpoint}: {e}")

        if resp.status_code == 404:
            raise RuntimeError(
                f"Zabbix endpoint not found (404) at {self.endpoint}: check the URL "
                "(some installs live under /zabbix/api_jsonrpc.php)."
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Zabbix HTTP error ({resp.status_code}): {resp.text[:400]}")

        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"Zabbix returned non-JSON response: {resp.text[:400]}")

        if "error" in body:
            err = body["error"]
            msg = f"{err.get('message', 'Error')}: {err.get('data', '')}".strip()
            raise RuntimeError(f"Zabbix API error ({err.get('code')}): {msg}")
        return body.get("result")

    def _login(self, session: requests.Session) -> str:
        if not (self.username and self.password):
            raise RuntimeError(
                "Zabbix requires either an API token or a username/password. "
                "None were provided."
            )
        # Zabbix 6.0+ uses `username`; older used `user`. Send `username` (the
        # supported name on all current versions).
        token = self._rpc(
            session, "user.login",
            {"username": self.username, "password": self.password},
            auth=False,
        )
        if not isinstance(token, str):
            raise RuntimeError("Zabbix user.login did not return a session token.")
        self._session_token = token
        return token

    def test_connection(self):
        try:
            with self.connect() as session:
                version = self._rpc(session, "apiinfo.version", [], auth=False)
                # One authed call proves the credentials + read access.
                self._rpc(session, "host.get", {"countOutput": True, "limit": 1})
                return {"success": True, "message": f"Connected to Zabbix API v{version}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── schema discovery ──────────────────────────────────────────────────────

    def get_schemas(self) -> List[Table]:
        tables = [self._build_table(name) for name in _CATALOG]
        # Best-effort enrichment: surface the value-types actually present so the
        # agent picks the right `history` type. Never fail discovery on this.
        try:
            with self.connect() as session:
                items = self._rpc(session, "item.get", {
                    "output": ["value_type", "units"],
                    "limit": ITEM_ENRICH_LIMIT,
                }) or []
            present = sorted({int(i.get("value_type", 0)) for i in items})
            if present:
                labels = ", ".join(f"{v}={_VALUE_TYPE.get(v, 'str')}" for v in present)
                for t in tables:
                    if t.name in ("history", "trends"):
                        t.description = (t.description or "") + f" Value-types present: {labels}."
        except Exception:
            pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name not in _CATALOG:
            raise ValueError(
                f"Unknown Zabbix table '{table_name}'. Available: {', '.join(_CATALOG)}."
            )
        return self._build_table(table_name)

    def _build_table(self, name: str) -> Table:
        spec = _CATALOG[name]
        columns = [TableColumn(name=c, dtype=d) for c, d in spec["columns"]]
        by_name = {c.name: c for c in columns}
        fks = [
            ForeignKey(
                column=by_name[col],
                references_name=ref_table,
                references_column=TableColumn(name=ref_col, dtype="int"),
            )
            for col, ref_table, ref_col in spec["fks"]
            if col in by_name
        ]
        pks = [by_name[spec["pk"]]] if spec.get("pk") and spec["pk"] in by_name else []
        return Table(name=name, description=spec["desc"], columns=columns, pks=pks, fks=fks)

    # ── querying ──────────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute a Zabbix query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"table": "problems",                          (required)
             "params": {"severity": [4, 5]},               (optional, merged into the get() call)
             "output": ["eventid", "name", "severity"],    (optional; else "extend")
             "limit": 100}                                 (optional, default 500)

        `table` maps to a Zabbix `*.get` method; `params` are passed through
        verbatim (filter/search/hostids/itemids/time_from/…). Escape hatch:
        pass `method` to call any Zabbix API method directly.
        """
        spec = self._parse_spec(query)
        method = spec.get("method")
        if not method:
            table = spec["table"]
            method = _CATALOG[table]["method"]

        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)
        params = dict(spec.get("params") or {})
        params.setdefault("output", spec.get("output") or "extend")
        # countOutput responses are scalars, not row lists — leave limit off then.
        if not params.get("countOutput"):
            params.setdefault("limit", limit)

        with self.connect() as session:
            result = self._rpc(session, method, params)

        if isinstance(result, list):
            df = pd.DataFrame(result)
        elif isinstance(result, dict):
            df = pd.DataFrame([result])
        else:
            df = pd.DataFrame({"result": [result]})
        return df

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = query
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    "Zabbix query must be a JSON object like "
                    '{"table": "problems", "params": {"severity": [4,5]}, "limit": 100} — got: '
                    f"{str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not (spec.get("table") or spec.get("method")):
            raise ValueError('Zabbix query spec must include a "table" (or "method") key.')
        if spec.get("table") and spec["table"] not in _CATALOG:
            raise ValueError(
                f"Unknown Zabbix table '{spec['table']}'. Available: {', '.join(_CATALOG)}. "
                'Or pass an explicit "method".'
            )
        return spec

    # ── prompts ───────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        text = """
        ## Zabbix Integration
        Query Zabbix via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "problems",
         "params": {"severity": [4, 5], "recent": true, "sortfield": "eventid", "sortorder": "DESC"},
         "output": ["eventid", "objectid", "name", "severity", "clock"],
         "limit": 100}
        ```

        - `table` (required): one of hosts, host_groups, items, triggers,
          problems, events, history, trends. Each maps to a Zabbix `*.get`
          method.
        - `params` (optional): passed straight to the Zabbix API method —
          `filter` (exact match), `search` (LIKE), `hostids`, `itemids`,
          `groupids`, `time_from`/`time_till` (epoch seconds), `sortfield`,
          `sortorder`, `selectHosts`, etc.
        - `output` (optional): fields to return; omit for all ("extend").
        - `limit` (optional): max rows, default 500.
        - `method` (escape hatch): call any Zabbix API method directly instead
          of a virtual table, e.g. {"method": "trigger.get", "params": {...}}.

        IMPORTANT specifics:
        - Timestamps (`clock`, `lastchange`, `lastclock`, `r_clock`) are UNIX
          EPOCH SECONDS, not ISO strings. Convert with
          `pd.to_datetime(df["clock"], unit="s")`.
        - Severity / priority is an integer 0-5:
          0 not_classified, 1 information, 2 warning, 3 average, 4 high, 5 disaster.
        - `history` REQUIRES `itemids` AND the matching `history` value-type
          (0 float [default], 3 unsigned int, 1 char, 2 log, 4 text) — a bare
          history.get returns nothing. Bound it with time_from (epoch).
        - `trends` gives hourly min/avg/max for numeric items and REQUIRES
          `itemids`. Prefer trends over history for long ranges.
        - To go from a host name to its metrics: hosts (filter by name) →
          items (params.hostids=[...]) → history/trends (params.itemids=[...]).
        - Aggregate by fetching rows and grouping in pandas.

        Examples:
        ```python
        # Open high/disaster problems, newest first
        df = client.execute_query('{"table": "problems", "params": {"severity": [4,5], "sortfield": "eventid", "sortorder": "DESC"}, "limit": 200}')
        # All items for a host
        df = client.execute_query('{"table": "items", "params": {"hostids": ["10084"], "search": {"key_": "cpu"}}, "limit": 100}')
        # Last 24h of a metric as trends
        import time
        since = int(time.time()) - 24*3600
        df = client.execute_query('{"table": "trends", "params": {"itemids": ["28336"], "time_from": %d}, "limit": 5000}' % since)
        ```
        """
        return text


# Alias so dynamic naming ("Zabbix" → "ZabbixClient") and the explicit
# client_path both resolve to the same class.
ZabbixClient = ZabbixClient
