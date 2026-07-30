"""Salesforce data-source client.

Authentication (auto-detected from the fields the constructor receives):

- **JWT Bearer** (`consumer_key` + `private_key` + `username`) — the OAuth 2.0
  `urn:ietf:params:oauth:grant-type:jwt-bearer` server-to-server flow. A
  Connected App's certificate signs a short-lived JWT that is exchanged for an
  access token; no interactive login, no stored password. This is the
  recommended path and mirrors the customer's existing ETL setup.
- **Access token** (`access_token` + `instance_url`) — a pre-obtained session,
  used by the per-user delegated OAuth path and by tests.
- **Username / password** (`username` + `password` [+ `security_token`]) — the
  legacy SOAP login, kept for backwards compatibility.

Schema discovery enumerates the org's queryable objects dynamically (standard
AND custom `__c`), filtering system/noise objects — replacing the previous
hardcoded five-object list. Field types are mapped to canonical dtypes and
reference fields become foreign keys.

Query results are shaped so callers can treat them like any other tabular
source: nested relationship payloads are flattened to dotted columns, and a
result with no rows still carries the columns the SELECT list asked for.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from functools import cached_property
from typing import Any, Dict, Generator, List, Optional

import pandas as pd
import requests
from simple_salesforce import Salesforce

from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter

logger = logging.getLogger(__name__)

# Hard cap on rows returned by a single execute_query. SOQL is model-generated
# and may omit LIMIT; `query_all` would otherwise stream an entire object into
# memory. We paginate up to this ceiling and stop.
MAX_ROWS = 10_000

# Upper bound on objects indexed when discovering dynamically, so a large org
# (which can expose 1000+ sobjects) can't produce a runaway catalog. Curated
# CRM objects are prioritized so they survive the cap; the dropped count is
# logged (never silently truncated).
MAX_INDEX_OBJECTS = 500

JWT_EXP_SECONDS = 180
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Objects a CRM user almost always cares about — surfaced first so they win the
# MAX_INDEX_OBJECTS cap, and used as the fallback set if discovery is blocked.
PRIORITY_OBJECTS = [
    "Account", "Contact", "Lead", "Opportunity", "Case", "Campaign",
    "User", "Task", "Event", "Contract", "Order", "Product2", "Pricebook2",
    "Quote", "OpportunityLineItem", "CampaignMember",
]

# Suffixes of Salesforce system/plumbing objects that are queryable but carry no
# analytical value (change-data-capture, sharing rows, field history, chatter).
_NOISE_SUFFIXES = (
    "Share", "History", "Feed", "ChangeEvent", "Tag", "EventRelation",
    "StatusUpdate",
)

# Salesforce field type -> canonical dtype used across the catalog.
_SF_TYPE_MAP = {
    "id": "str",
    "reference": "reference",
    "string": "str",
    "textarea": "str",
    "picklist": "str",
    "multipicklist": "str",
    "combobox": "str",
    "email": "str",
    "phone": "str",
    "url": "str",
    "encryptedstring": "str",
    "base64": "str",
    "address": "str",
    "location": "str",
    "boolean": "bool",
    "int": "int",
    "long": "int",
    "double": "float",
    "currency": "float",
    "percent": "float",
    "date": "date",
    "datetime": "datetime",
    "time": "time",
    "anyType": "str",
}

# `SELECT COUNT() FROM X` answers in `totalSize` and returns no records at all.
_COUNT_QUERY_RE = re.compile(r"^\s*SELECT\s+COUNT\s*\(\s*\)\s+FROM\s", re.IGNORECASE)


class SalesforceConnectionError(RuntimeError):
    """Establishing the Salesforce session failed (credentials, auth, transport).

    Distinct from a query failure so the two never get labelled as each other —
    and so `execute_query` can re-raise it untouched instead of prefixing a
    connection error with a query-error message.
    """


def format_salesforce_error(exc: Exception) -> str:
    """Render a Salesforce API exception as `ERRORCODE: message`.

    simple_salesforce's own `__str__` leads with the full request URL — which
    for a query carries the entire URL-encoded SOQL — and leaves the errorCode
    and message at the very end. Callers truncate error strings (the planner
    observation keeps 200 characters), so the actionable part is exactly the
    part that gets cut. Salesforce returns a list of
    `{message, errorCode, fields}` objects; surface those first and drop the
    URL, since the failing query is already reported alongside the error.
    """
    content = getattr(exc, "content", None)
    if isinstance(content, (bytes, str)):
        try:
            content = json.loads(content)
        except Exception:
            content = None

    entries: List[Any] = []
    if isinstance(content, list):
        entries = content
    elif isinstance(content, dict):
        entries = [content]

    parts: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("errorCode") or "").strip()
        message = str(entry.get("message") or "").strip()
        if code and message:
            text = f"{code}: {message}"
        elif code or message:
            text = code or message
        else:
            continue
        fields = [str(f) for f in (entry.get("fields") or []) if f]
        if fields:
            text += f" (fields: {', '.join(fields)})"
        parts.append(text)

    if parts:
        return " | ".join(parts)

    # Not a structured Salesforce API error. Keep the exception class — a bare
    # str() on a KeyError is just the key, which tells the caller nothing.
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _outside_parens(text: str) -> str:
    """Drop parenthesized spans, so `SUM(Amount) total` tokenizes as `SUM total`."""
    out: List[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def soql_select_columns(query: str) -> List[str]:
    """Column names a SOQL SELECT list produces, in order.

    Lets a zero-row result carry the same columns a non-empty one would have
    had. Best-effort: returns [] for anything it can't parse, and callers then
    fall back to whatever the records themselves produced.
    """
    if not isinstance(query, str):
        return []
    head = re.match(r"\s*SELECT\s+", query, re.IGNORECASE)
    if not head:
        return []

    # Split the select list on top-level commas, stopping at the top-level FROM.
    # Parenthesized child subqueries carry their own FROM, hence the depth test.
    items: List[str] = []
    current: List[str] = []
    depth = 0
    in_quote = False
    i, n = head.end(), len(query)
    while i < n:
        ch = query[i]
        if in_quote:
            current.append(ch)
            if ch == "\\" and i + 1 < n:
                current.append(query[i + 1])
                i += 2
                continue
            if ch == "'":
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif depth == 0 and ch == ",":
            items.append("".join(current))
            current = []
        elif (
            depth == 0
            and query[i:i + 4].lower() == "from"
            and (query[i - 1].isspace() if i else True)
            and (i + 4 >= n or not (query[i + 4].isalnum() or query[i + 4] == "_"))
        ):
            break
        else:
            current.append(ch)
        i += 1
    items.append("".join(current))

    columns: List[str] = []
    expr_index = 0
    for raw in items:
        item = raw.strip()
        if not item:
            continue
        if item.startswith("("):
            # Child relationship subquery — the column is the relationship name.
            match = re.search(r"\bFROM\s+([A-Za-z0-9_.]+)", item, re.IGNORECASE)
            if match:
                columns.append(match.group(1))
            else:
                columns.append(f"expr{expr_index}")
                expr_index += 1
            continue
        tokens = _outside_parens(item).split()
        if len(tokens) >= 2:
            # `SUM(Amount) total` — and be lenient about a stray `AS`, which
            # SOQL rejects but models write anyway.
            columns.append(tokens[-1])
        elif "(" in item:
            # Unaliased aggregate: Salesforce names these expr0, expr1, ...
            columns.append(f"expr{expr_index}")
            expr_index += 1
        elif tokens:
            columns.append(tokens[0])
    return columns


def flatten_record(record: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flatten one SOQL record into scalar, dotted-name columns.

    Parent traversal (`SELECT Account.Name`) arrives as a nested object, and a
    child subquery arrives as a nested `records` envelope — both of which land
    in the DataFrame as dict-valued columns that break charting and
    serialization. Parents become `Account.Name`; child rows are serialized to
    compact JSON so the value stays primitive without discarding data.
    """
    flat: Dict[str, Any] = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            if isinstance(value.get("records"), list):
                children = [flatten_record(r) for r in value["records"] if isinstance(r, dict)]
                flat[name] = json.dumps(children, default=str) if children else None
            else:
                flat.update(flatten_record(value, prefix=f"{name}."))
            continue
        flat[name] = value
    return flat


def _records_to_df(records: List[Dict[str, Any]], expected_columns: List[str]) -> pd.DataFrame:
    """Build the result frame, preserving the SELECT list's columns.

    `pd.DataFrame([])` has zero columns, so an object that simply holds no
    matching rows used to come back shapeless — downstream code indexing a
    column raised a bare KeyError, and empty-result guards read it as "the
    query was never run". A zero-row frame keeps its columns here, the way
    every SQL client's does.
    """
    rows = [flatten_record(r) for r in records if isinstance(r, dict)]
    df = pd.DataFrame(rows)
    ordered_expected = list(dict.fromkeys(expected_columns or []))
    if not ordered_expected:
        return df
    if df.empty:
        return pd.DataFrame(columns=ordered_expected)

    # A parent lookup that is null on every row yields no dotted column at all;
    # fill it so the caller sees nulls rather than a missing column. Limited to
    # dotted names — a bare name may legitimately be absent (a compound field
    # expands into its own subcolumns) and inventing it would be wrong.
    for name in ordered_expected:
        if name not in df.columns and "." in name:
            df[name] = None

    ordered = [c for c in ordered_expected if c in df.columns]
    ordered += [c for c in df.columns if c not in ordered]
    return df[ordered]


class SalesforceClient(DataSourceClient):
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        security_token: Optional[str] = None,
        domain: Optional[str] = "login",
        sandbox: bool = False,
        consumer_key: Optional[str] = None,
        private_key: Optional[str] = None,
        access_token: Optional[str] = None,
        instance_url: Optional[str] = None,
        objects: Optional[str] = None,
    ):
        self.username = username
        self.password = password
        self.security_token = security_token
        self.domain = (domain or "login").strip()
        self.sandbox = bool(sandbox)
        self.consumer_key = consumer_key
        self.private_key = private_key
        self.access_token = access_token
        self.instance_url = (instance_url or "").rstrip("/") or None
        # Optional explicit object allowlist (comma-separated) — overrides
        # dynamic discovery when set.
        self.objects = [o.strip() for o in objects.split(",") if o.strip()] if objects else None

    # ── auth ─────────────────────────────────────────────────────────────────

    @property
    def _auth_mode(self) -> str:
        if self.access_token and self.instance_url:
            return "token"
        if self.consumer_key and self.private_key and self.username:
            return "jwt"
        if self.username and self.password:
            return "userpass"
        return "none"

    def _login_url(self) -> str:
        """Base OAuth host for the token endpoint / JWT audience."""
        if self.sandbox:
            return "https://test.salesforce.com"
        # A custom My Domain (anything other than the login/test aliases) logs
        # in against that host; otherwise production login.
        if self.domain and self.domain not in ("login", "test"):
            return f"https://{self.domain}.my.salesforce.com"
        return "https://login.salesforce.com"

    def _sf_domain(self) -> str:
        """`domain` kwarg for simple_salesforce's SOAP (username/password) login.
        Fixes the long-standing bug where sandbox/domain were captured but never
        forwarded, so sandbox and My-Domain logins silently hit production.
        """
        if self.sandbox:
            return "test"
        return self.domain or "login"

    def _jwt_access(self) -> tuple[str, str]:
        """Run the JWT Bearer flow; return (access_token, instance_url)."""
        import jwt  # PyJWT

        login_url = self._login_url()
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self.consumer_key,
                "sub": self.username,
                "aud": login_url,
                "exp": now + JWT_EXP_SECONDS,
            },
            self.private_key,
            algorithm="RS256",
        )
        resp = requests.post(
            f"{login_url}/services/oauth2/token",
            data={"grant_type": JWT_GRANT, "assertion": assertion},
            timeout=60,
        )
        if resp.status_code != 200:
            detail = resp.text[:500]
            try:
                body = resp.json()
                detail = f"{body.get('error')}: {body.get('error_description', detail)}"
            except Exception:
                pass
            raise SalesforceConnectionError(
                f"Salesforce JWT authentication failed ({resp.status_code}): {detail}. "
                "Check the Connected App consumer key, the certificate, the user "
                "(sub), and that the app is admin pre-authorized for that user."
            )
        data = resp.json()
        return data["access_token"], data["instance_url"].rstrip("/")

    @cached_property
    def sf(self) -> Salesforce:
        mode = self._auth_mode
        if mode == "token":
            return Salesforce(instance_url=self.instance_url, session_id=self.access_token)
        if mode == "jwt":
            token, instance_url = self._jwt_access()
            return Salesforce(instance_url=instance_url, session_id=token)
        if mode == "userpass":
            return Salesforce(
                username=self.username,
                password=self.password,
                security_token=self.security_token or "",
                domain=self._sf_domain(),
            )
        raise SalesforceConnectionError(
            "Salesforce client has no usable credentials: provide a Connected App "
            "(consumer_key + private_key + username), an access_token + instance_url, "
            "or username + password."
        )

    @contextmanager
    def connect(self) -> Generator[Salesforce, None, None]:
        """Yield a connection to Salesforce.

        Only the connect step is wrapped. Wrapping the yielded body as well
        relabelled every query failure — malformed SOQL, an unknown field — as
        a connection error, and stacked a second prefix onto a message that
        already had one.
        """
        try:
            session = self.sf
        except SalesforceConnectionError:
            raise
        except Exception as e:
            raise SalesforceConnectionError(
                f"Salesforce connection failed: {format_salesforce_error(e)}"
            ) from e
        yield session

    def test_connection(self):
        """Test the Salesforce connection with a lightweight authenticated call."""
        try:
            with self.connect() as sf:
                sf.limits()
                return {"success": True, "message": "Connected to Salesforce"}
        except SalesforceConnectionError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            return {"success": False, "message": format_salesforce_error(e)}

    # ── schema discovery ─────────────────────────────────────────────────────

    def _discover_object_names(self) -> List[str]:
        """Enumerate queryable standard + custom objects, minus system/noise.

        Priority objects come first so they survive the MAX_INDEX_OBJECTS cap.
        """
        if self.objects:
            return self.objects

        global_desc = self.sf.describe()
        candidates: List[str] = []
        for obj in global_desc.get("sobjects", []):
            name = obj.get("name")
            if not name:
                continue
            if not obj.get("queryable"):
                continue
            if obj.get("deprecatedAndHidden") or obj.get("customSetting"):
                continue
            if any(name.endswith(suffix) for suffix in _NOISE_SUFFIXES):
                continue
            candidates.append(name)

        candidate_set = set(candidates)
        ordered = [n for n in PRIORITY_OBJECTS if n in candidate_set]
        ordered += sorted(n for n in candidate_set if n not in set(PRIORITY_OBJECTS))

        if len(ordered) > MAX_INDEX_OBJECTS:
            logger.warning(
                "Salesforce discovery found %d indexable objects; capping at %d "
                "(set the connection's `objects` list to index specific ones). "
                "Dropped %d objects.",
                len(ordered), MAX_INDEX_OBJECTS, len(ordered) - MAX_INDEX_OBJECTS,
            )
            ordered = ordered[:MAX_INDEX_OBJECTS]
        return ordered

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Discover object schemas dynamically (standard + custom)."""
        names = self._discover_object_names()
        reporter = make_reporter(progress_callback)
        reporter.phase("Indexing Salesforce objects", total=len(names))

        schemas: List[Table] = []
        for name in names:
            try:
                schemas.append(self.get_schema(name))
            except Exception as e:
                # A single unreadable object (field-level security, odd metadata)
                # must not abort the whole catalog.
                logger.warning(
                    "Skipping Salesforce object %s: %s", name, format_salesforce_error(e)
                )
            reporter.tick(name)
        return schemas

    def get_schema(self, object_name: str) -> Table:
        """Get schema for a specific Salesforce object, with FKs and dtypes."""
        try:
            with self.connect() as sf:
                describe = sf.__getattr__(object_name).describe()
        except SalesforceConnectionError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Salesforce describe failed for {object_name}: {format_salesforce_error(e)}"
            ) from e

        columns: List[TableColumn] = []
        fks: List[ForeignKey] = []
        for field in describe["fields"]:
            fname = field["name"]
            ftype = field.get("type", "string")
            dtype = _SF_TYPE_MAP.get(ftype, "str")
            column = TableColumn(name=fname, dtype=dtype, description=field.get("label"))
            columns.append(column)

            if ftype == "reference":
                references = [r for r in (field.get("referenceTo") or []) if r]
                if references:
                    fks.append(ForeignKey(
                        column=column,
                        references_name=references[0],
                        references_column=TableColumn(name="Id", dtype="str"),
                    ))

        return Table(
            name=object_name,
            columns=columns,
            pks=[TableColumn(name="Id", dtype="str")],
            fks=fks,
        )

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ── querying ─────────────────────────────────────────────────────────────

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a SOQL query and return results as a DataFrame.

        Paginates up to MAX_ROWS so a LIMIT-less query can't stream an entire
        object into memory. Nested relationship payloads are flattened, and a
        result with no rows keeps the SELECT list's columns so an empty object
        is distinguishable from a broken query.
        """
        expected_columns = soql_select_columns(query)
        records: List[Dict[str, Any]] = []
        total_size: Optional[int] = None
        try:
            with self.connect() as sf:
                result = sf.query(query)
                records = list(result.get("records", []))
                while not result.get("done") and len(records) < MAX_ROWS:
                    result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
                    records.extend(result.get("records", []))
                records = records[:MAX_ROWS]
                total_size = result.get("totalSize")
        except SalesforceConnectionError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Salesforce query failed: {format_salesforce_error(e)}"
            ) from e

        # `SELECT COUNT() FROM X` reports its answer in `totalSize` and returns
        # no records; without this the count is silently lost to an empty frame.
        if not records and total_size is not None and _COUNT_QUERY_RE.match(query or ""):
            return pd.DataFrame([{"expr0": int(total_size)}])

        return _records_to_df(records, expected_columns)

    # ── prompts / description ────────────────────────────────────────────────

    def system_prompt(self):
        """Provide a detailed system prompt for LLM integration."""
        text = """
## Salesforce SOQL Query Guide

This connection speaks **SOQL**, not SQL — generic SQL guidance does not apply
here. There are no JOINs, no table aliases, no `SELECT *`, no subqueries in
FROM, no CASE expressions and no window functions. Run queries with
`execute_query("<soql>")`, which returns a pandas DataFrame.

### Query rules

- **No JOIN, no table aliases.** `LEFT JOIN Order o ON a.Id = o.AccountId` is
  not valid SOQL. Relate records one of two ways instead:
  * **Parent traversal** (up to 5 levels), which replaces most joins:
    `SELECT Name, Account.Name, Account.Owner.Name FROM Opportunity`
    — these arrive as flattened columns `Account.Name`, `Account.Owner.Name`.
  * **Two queries + pandas**, when you need to aggregate across objects: query
    each object separately and `pd.merge` the DataFrames on the id column.
- **Aggregate aliases take no `AS`:** `SUM(Amount) totalSales` is correct;
  `SUM(Amount) AS totalSales` fails with `unexpected token: 'as'`.
- **ORDER BY and HAVING must repeat the expression, never the alias:**
  `ORDER BY SUM(Amount) DESC`. `ORDER BY totalSales` fails with
  `No such column 'totalSales' on entity '<Object>'` — an alias is not a column.
- **Unaliased aggregates come back as `expr0`, `expr1`, …** in select-list
  order. Alias them, or read the `expr` columns.
- **Every non-aggregated selected field must appear in GROUP BY.**
- **No `SELECT *`** — list fields explicitly. Take the names from the indexed
  schema; if an object's fields are not in the schema excerpt, inspect the
  object before guessing.
- **Custom fields end in `__c`; custom relationship traversal uses `__r`:**
  `Widget__r.Name`, not `Widget__c.Name`. A field named `Revenue__c` cannot be
  referenced as `Revenue`.
- **Nulls:** `WHERE Amount != null` / `WHERE Amount = null`. `IS NULL` and
  `IS NOT NULL` are not valid SOQL.
- **Literals:** strings in single quotes (`'Acme'`); dates unquoted
  (`2024-01-01`, `2024-01-01T00:00:00Z`); relative ranges via date literals
  such as `LAST_N_DAYS:30`, `THIS_QUARTER`, `LAST_YEAR`.
- **Row limiting:** `LIMIT n` — not `TOP n`, not `FETCH FIRST n ROWS ONLY`.
  `OFFSET` is supported but capped at 2000.
- **Counting:** `SELECT COUNT() FROM Account` returns one row in a column named
  `expr0`. Prefer `SELECT COUNT(Id) total FROM Account` when grouping.
- Long text areas and some formula fields cannot be filtered, sorted or grouped.

### Reading results

- **An empty DataFrame means zero matching rows, not a broken query** — the
  columns are still present. Check the filter, or whether the object holds data
  in this org, before rewriting the query.
- Results are capped at 10,000 rows. For larger sets, aggregate in SOQL with
  `GROUP BY` rather than fetching rows and summing in pandas.
- A null parent lookup yields a null value, not a missing column. A child
  subquery column holds JSON — prefer querying the child object directly.

### Examples

```python
# Accounts, with the fields named explicitly
df = client.execute_query(
    "SELECT Id, Name, Industry, AnnualRevenue FROM Account ORDER BY Name LIMIT 200"
)

# Total won opportunity amount per account: aggregate in SOQL, alias without AS,
# and order by the expression rather than the alias.
df = client.execute_query(\"\"\"
    SELECT AccountId, SUM(Amount) totalSales, COUNT(Id) dealCount
    FROM Opportunity
    WHERE IsWon = true AND Amount != null
    GROUP BY AccountId
    ORDER BY SUM(Amount) DESC
    LIMIT 100
\"\"\")

# Add the account names — two queries merged in pandas, since SOQL has no JOIN.
accounts = client.execute_query("SELECT Id, Name FROM Account LIMIT 10000")
df = df.merge(accounts, left_on="AccountId", right_on="Id", how="left")

# Parent traversal instead of a join, when no aggregation is needed.
df = client.execute_query(\"\"\"
    SELECT Name, Amount, CloseDate, Account.Name, Account.Industry
    FROM Opportunity
    WHERE IsWon = true
    ORDER BY Amount DESC
    LIMIT 500
\"\"\")   # columns: Name, Amount, CloseDate, Account.Name, Account.Industry

# Recent cases, using a date literal
df = client.execute_query(\"\"\"
    SELECT CaseNumber, Status, Priority, Subject
    FROM Case
    WHERE CreatedDate = LAST_N_DAYS:30
    LIMIT 500
\"\"\")

# Custom object and custom field
df = client.execute_query(
    "SELECT Id, Name, Revenue__c FROM Widget__c WHERE Revenue__c != null "
    "ORDER BY Revenue__c DESC LIMIT 100"
)
```

Both standard objects (Account, Contact, Opportunity, Lead, Case, …) and custom
objects (suffixed `__c`) are available; consult the indexed schema for the exact
objects and fields in this org.
"""
        return text

    @property
    def description(self):
        text = "Salesforce Client, execute SOQL queries to retrieve Salesforce data."
        return text + "\n\n" + self.system_prompt()
