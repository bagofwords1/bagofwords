from app.data_sources.clients.progress import discovery_progress, discovery_items, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin, urlencode, quote
import time
import requests
from app.core.egress import pinned_session
import pandas as pd
from defusedxml import ElementTree as ET


# OData EDM primitive types that are numeric — used as the fallback signal for
# "this property is a measure" when a model does not annotate measures
# explicitly. SAP Datasphere analytic models DO annotate measures (see
# _MEASURE_ANNOTATIONS), but relational views and sparsely-annotated models do
# not, so numeric-type detection is the safety net.
_NUMERIC_EDM_TYPES = {
    "Edm.Decimal", "Edm.Double", "Edm.Single", "Edm.Int16", "Edm.Int32",
    "Edm.Int64", "Edm.Byte", "Edm.SByte",
}

# Annotation term local-names that mark a property as a measure in SAP's
# analytics vocabulary. Datasphere emits these on analytic-model properties.
# We match on the term's local name (namespace-insensitive) so both
# `Analytics.Measure` and `com.sap.vocabularies.Analytics.v1.Measure` match.
_MEASURE_ANNOTATION_TERMS = {"Measure", "AggregatedProperty", "AggregatedProperties"}
_AGGREGATION_ANNOTATION_TERMS = {"CustomAggregate", "ContextDefiningProperties"}
# Terms that mark a property as a dimension (groupable attribute). SAP's
# Analytics vocabulary has `Dimension` (deprecated in favour of
# `AnalyticalContext/Dimension`); both local names are accepted.
_DIMENSION_ANNOTATION_TERMS = {"Dimension", "AnalyticalContext"}

# Rows returned when the caller neither bounds the query with `$top` nor passes
# `max_rows`: an analytical query without `$top` would otherwise page the whole
# model into memory (the code-execution wrapper never passes max_rows).
DEFAULT_MAX_ROWS = 10_000

# EDMX / CSDL namespaces vary by OData version; we match element local-names
# instead of pinning a namespace so v4 CSDL from any SAP release parses.


class SapDatasphereClient(DataSourceClient):
    """SAP Datasphere semantic-layer client (cloud, OData Consumption API).

    Discovers every consumption-exposed object the caller can access via the
    Datasphere **catalog API**, reads each analytic model's semantic `$metadata`
    (measures vs. dimensions), and queries the **analytical OData** endpoint
    with server-side aggregation (measures aggregate by the dimensions named in
    ``$select``). This is the semantic layer — restricted/calculated measures
    and exception aggregation are applied by Datasphere — as opposed to the raw
    SQL/Open-SQL path served by the separate SAP HANA connector.

    Auth is dual-mode and injected by the connection layer:
      - **system** scope: an OAuth *Technical User* (client_credentials). Drives
        catalog discovery/indexing and shared queries.
      - **user** scope: a per-user *Interactive* token (authorization_code),
        passed as ``access_token``. Required to read Data-Access-Control
        (row-level-security) protected models, which return empty to a
        technical user.
    """

    def __init__(
        self,
        host: str,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        scopes: Optional[str] = None,
        space: Optional[str] = None,
        catalog_path: str = "/api/v1/dwc/catalog",
        consumption_path: str = "/api/v1/dwc/consumption",
        verify_ssl: bool = True,
        timeout_sec: int = 60,
    ):
        # Tenant consumption host, e.g. "mytenant.us10.hcs.cloud.sap". Accept a
        # bare host or a full URL; normalize to an https origin with no path.
        self.host = self._normalize_host(host)
        self.token_url = (token_url or "").strip() or None
        self.client_id = client_id
        self.client_secret = client_secret
        # A pre-obtained per-user delegated token (authorization_code). When set,
        # every request rides on it and NO client_credentials grant is done.
        self._access_token: Optional[str] = access_token
        self.scopes = scopes or ""
        # Optional comma-separated space filter limiting discovery.
        self._space_filter = {
            s.strip() for s in (space or "").split(",") if s.strip()
        }
        self.catalog_path = "/" + (catalog_path or "").strip().strip("/")
        self.consumption_path = "/" + (consumption_path or "").strip().strip("/")
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec

        self._http: Optional[requests.Session] = None
        self._token_cache: Optional[str] = None
        self._token_expiry: float = 0.0
        # Discovery cache — get_schemas() is a catalog crawl + per-asset
        # $metadata fetch; run it once per client instance.
        self._schemas_cache: Optional[List[Table]] = None
        # Persisted catalog metadata ({"Space/Model": {...sap_datasphere...}})
        # handed over by the connection layer (attach_table_metadata) so a
        # query can locate its model without a live catalog crawl.
        self._table_metadata_map: Dict[str, Dict] = {}
        # "Space/Model" names whose $metadata could not be read on the last
        # get_schemas() run; reported through unindexed_table_prefixes() so the
        # refresh keeps their previous entries instead of failing outright.
        self._unread_assets: List[str] = []

    @staticmethod
    def _normalize_host(host: str) -> str:
        h = (host or "").strip().rstrip("/")
        if not h:
            return h
        if h.startswith("http://") or h.startswith("https://"):
            return h
        return f"https://{h}"

    # ------------------------------------------------------------------
    # Auth / session
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        if self._http is None:
            self._http = pinned_session()
            self._http.verify = self.verify_ssl
        return self._http

    def _token(self) -> str:
        """Return a bearer token.

        Per-user delegated token wins outright. Otherwise perform (and cache) a
        client_credentials grant against the tenant's OAuth token endpoint.
        """
        if self._access_token:
            return self._access_token

        now = time.time()
        if self._token_cache and now < self._token_expiry:
            return self._token_cache

        if not self.token_url:
            raise RuntimeError(
                "No token_url configured and no per-user access_token supplied — "
                "cannot authenticate to SAP Datasphere."
            )
        if not (self.client_id and self.client_secret):
            raise RuntimeError(
                "Missing client_id/client_secret for the client_credentials grant."
            )

        data = {"grant_type": "client_credentials"}
        if self.scopes:
            data["scope"] = self.scopes
        resp = self._session().post(
            self.token_url,
            data=data,
            auth=(self.client_id, self.client_secret),
            timeout=self.timeout_sec,
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"OAuth token request failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json() or {}
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("OAuth token endpoint returned no access_token")
        # Refresh a minute before the stated expiry; default to 10 min if absent.
        expires_in = int(payload.get("expires_in") or 600)
        self._token_cache = token
        self._token_expiry = now + max(60, expires_in - 60)
        return token

    def connect(self):
        """Prime the token (surfaces auth errors early)."""
        self._token()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
        }

    def _get(self, url: str, *, accept_xml: bool = False, timeout: Optional[int] = None):
        """GET an absolute URL (or a path relative to the tenant host) with auth."""
        if not (url.startswith("http://") or url.startswith("https://")):
            url = urljoin(self.host + "/", url.lstrip("/"))
        headers = self._headers()
        if accept_xml:
            headers["Accept"] = "application/xml"
        return self._session().get(
            url, headers=headers, timeout=timeout or self.timeout_sec
        )

    # ------------------------------------------------------------------
    # Catalog discovery
    # ------------------------------------------------------------------

    def _list_assets(self) -> List[Dict]:
        """Return the consumption-exposed assets the caller can access.

        Uses the catalog `assets` collection (one call spans all authorized
        spaces). Follows OData `@odata.nextLink` paging. Honors the space
        filter when configured.
        """
        assets: List[Dict] = []
        url = f"{self.catalog_path}/assets"
        seen_pages = 0
        while url and seen_pages < 100:
            resp = self._get(url)
            if resp.status_code >= 300:
                raise RuntimeError(
                    f"Catalog listing failed: HTTP {resp.status_code} {resp.text[:300]}"
                )
            payload = resp.json() or {}
            for a in payload.get("value") or []:
                space = a.get("spaceName") or a.get("spaceId") or ""
                if self._space_filter and space not in self._space_filter:
                    continue
                assets.append(a)
            url = payload.get("@odata.nextLink")
            seen_pages += 1
        return assets

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> List[Table]:
        return self.get_tables()

    def get_tables(self) -> List[Table]:
        """One BOW Table per analytic model / exposed view, columns split into
        dimensions and measures parsed from each asset's OData `$metadata`."""
        if self._schemas_cache is not None:
            return self._schemas_cache

        self._unread_assets = []
        tables: List[Table] = []
        for asset in discovery_items(self._list_assets(), 'assets', label=lambda asset: asset.get('name')):
            name = asset.get("name") or ""
            space = asset.get("spaceName") or asset.get("spaceId") or ""
            if not name or not space:
                continue

            label = asset.get("label") or None
            analytical = bool(
                asset.get("supportsAnalyticalQueries")
                or asset.get("assetAnalyticalMetadataUrl")
            )
            meta_url = (
                asset.get("assetAnalyticalMetadataUrl")
                if analytical
                else asset.get("assetRelationalMetadataUrl")
            )
            data_url = (
                asset.get("assetAnalyticalDataUrl")
                if analytical
                else asset.get("assetRelationalDataUrl")
            )
            # Fall back to constructing the URLs when the catalog omits them.
            kind = "analytical" if analytical else "relational"
            if not meta_url:
                meta_url = f"{self.consumption_path}/{kind}/{quote(space)}/{quote(name)}/$metadata"
            if not data_url:
                data_url = f"{self.consumption_path}/{kind}/{quote(space)}/{quote(name)}/{quote(name)}"

            full_name = f"{space}/{name}"
            columns = self._parse_metadata_columns(meta_url)
            if columns is None:
                # Unreadable on this run (a 403 on a DAC-protected model the
                # technical user can list but not read, a timeout, ...). Absent
                # is not gone: leave it out and report it as unread so the
                # refresh keeps its previous entry and selection.
                self._unread_assets.append(full_name)
                continue

            tables.append(Table(
                name=full_name,
                description=label,
                columns=columns,
                pks=[],
                fks=[],
                is_active=True,
                metadata_json={"sap_datasphere": {
                    "space": space,
                    "asset": name,
                    "kind": kind,
                    "data_url": data_url,
                    "metadata_url": meta_url,
                }},
            ))

        if not getattr(self, "_discovery_failures", None):
            self._schemas_cache = tables
        return tables

    def unindexed_table_prefixes(self) -> List[str]:
        """Models whose `$metadata` the last crawl could not read (see
        `DataSourceClient.unindexed_table_prefixes`): their previous catalog
        entries are kept instead of the whole refresh being rejected."""
        return list(self._unread_assets)

    def attach_table_metadata(self, tables: List[Dict]) -> None:
        """Receive the connection's persisted table metadata so queries resolve
        their model from it instead of re-crawling the catalog (the connection
        layer calls this right after constructing the client)."""
        mapping: Dict[str, Dict] = {}
        for row in tables or []:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            meta = row.get("metadata_json")
            if isinstance(meta, dict):
                meta = meta.get("sap_datasphere")
            if name and isinstance(meta, dict) and meta.get("data_url"):
                mapping[str(name)] = meta
        self._table_metadata_map = mapping

    def _parse_metadata_columns(self, meta_url: str) -> Optional[List[TableColumn]]:
        """Fetch and parse an OData `$metadata` document into TableColumns,
        tagging each as role=measure or role=dimension.

        Returns ``None`` when the document could not be fetched or holds no
        entity type, so the caller can treat the asset as unread this run.

        Classification: explicit SAP analytics annotations win per property
        (``Analytics.Measure`` / ``Aggregation.*`` → measure,
        ``Analytics.Dimension`` / ``AnalyticalContext`` or
        ``sap:aggregation-role`` → as stated). The numeric-EDM-type fallback
        applies only to unannotated properties of a model that annotates NO
        measure at all (relational views, sparse models) — in an annotated model
        an unannotated ``Edm.Int32`` is a year or an ID, not a measure.
        """
        try:
            resp = self._get(meta_url, accept_xml=True)
            if resp.status_code >= 300:
                return None
            root = ET.fromstring(resp.content)
        except IndexingCancelled:
            raise
        except Exception:
            return None

        # Find the first EntityType (the consumption entity).
        entity_type = None
        for el in root.iter():
            if _local(el.tag) == "EntityType":
                entity_type = el
                break
        if entity_type is None:
            return None

        props = [p for p in entity_type if _local(p.tag) == "Property" and p.get("Name")]
        roles = {p.get("Name"): self._annotated_role(p) for p in props}
        model_annotates_measures = any(r == "measure" for r in roles.values())

        columns: List[TableColumn] = []
        for prop in props:
            pname = prop.get("Name")
            ptype = prop.get("Type") or "Edm.String"
            role = roles[pname]
            if role is None:
                role = (
                    "measure"
                    if not model_annotates_measures and ptype in _NUMERIC_EDM_TYPES
                    else "dimension"
                )
            is_measure = role == "measure"
            columns.append(TableColumn(
                name=pname,
                dtype="measure" if is_measure else _edm_to_dtype(ptype),
                description=None,
                metadata={"role": role, "edm_type": ptype},
            ))
        return columns

    @staticmethod
    def _annotated_role(prop) -> Optional[str]:
        """'measure' / 'dimension' from the property's explicit annotations, or
        None when it carries none."""
        # OData V2-style SAP attribute (sap:aggregation-role="measure|dimension").
        for attr, value in prop.attrib.items():
            if _local(attr) == "aggregation-role" and value:
                v = value.strip().lower()
                if v in ("measure", "dimension"):
                    return v
        role = None
        for child in prop:
            if _local(child.tag) != "Annotation":
                continue
            term = child.get("Term") or ""
            term_local = _local_term(term)
            enabled = (child.get("Bool") or "true").strip().lower() != "false"
            if term_local in _MEASURE_ANNOTATION_TERMS or term_local in _AGGREGATION_ANNOTATION_TERMS:
                if enabled:
                    return "measure"
                role = role or "dimension"
            elif term_local in _DIMENSION_ANNOTATION_TERMS or "AnalyticalContext" in term:
                if enabled:
                    role = "dimension"
        return role

    def get_schema(self, table_name: str) -> Table:
        tables = self.get_schemas()
        for t in tables:
            if t.name == table_name:
                return t
        # Match by asset name only — refuse an ambiguous one rather than
        # silently picking the first space that happens to have it.
        matches = [
            t for t in tables
            if ((t.metadata_json or {}).get("sap_datasphere") or {}).get("asset") == table_name
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous SAP Datasphere model '{table_name}' (in spaces "
                + ", ".join(sorted(m.metadata_json["sap_datasphere"]["space"] for m in matches))
                + "); use the exact 'Space/Model' name."
            )
        if matches:
            return matches[0]
        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: Optional[str] = None,
        table_name: Optional[str] = None,
        select: Optional[str] = None,
        filter: Optional[str] = None,
        orderby: Optional[str] = None,
        top: Optional[int] = None,
        parameters: Optional[Dict[str, str]] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Query an analytic model's analytical OData endpoint.

        The framework passes ``query`` positionally (like every SQL client), so
        the primary form is ``execute_query("<odata options>", "Space/Model")``
        where the options string is e.g. ``"$select=Country,Revenue&$orderby=
        Revenue desc&$top=100"`` — measures aggregate server-side over the
        dimensions named in ``$select``. The structured ``select`` / ``filter``
        / ``orderby`` / ``top`` kwargs are an alternative. ``table_name``
        (``"Space/Model"``, exactly as shown in the schema) locates the asset.
        ``parameters`` supplies analytic-model variables as ``{name: value}``.
        """
        # Robustness: the agent may pass the model name as the first positional
        # arg (no OData markers) instead of a query — swap it into table_name.
        if query and not table_name and "$" not in query and self._resolve_asset(query):
            table_name, query = query, None
        # Or embed the model as "Space/Model" inside a combined first arg.
        if query and not table_name:
            head = query.split("?", 1)[0].split("&", 1)[0].strip()
            if "/" in head and "$" not in head and self._resolve_asset(head):
                table_name = head
                query = query[len(head):].lstrip("?& ")

        meta = self._resolve_asset(table_name)
        if not meta:
            raise ValueError(
                "execute_query needs a target model: pass table_name exactly as "
                "shown in the schema (format 'Space/Model')."
            )
        data_url = meta["data_url"]

        # Analytic-model variables: (P1='v1',P2='v2')/Set prefix on the entity
        # set. Datasphere expects each value as a quoted OData literal (its own
        # docs quote even numeric-looking IDs, e.g. (Product_ID='8013311')/Set).
        if parameters:
            pairs = ",".join(f"{k}={_param_literal(v)}" for k, v in parameters.items())
            data_url = self._apply_parameter_set(data_url, pairs)

        qs = self._build_query_string(query, select, filter, orderby, top)
        url = data_url + (("?" + qs) if qs else "")

        # Bound unbounded queries: without `$top` or `max_rows` Datasphere would
        # otherwise page the whole model back to us.
        if max_rows is None and "$top" not in qs:
            max_rows = DEFAULT_MAX_ROWS

        rows: List[Dict] = []
        pages = 0
        while url and pages < 1000:
            resp = self._get(url)
            if resp.status_code >= 300:
                raise RuntimeError(
                    f"OData query failed: HTTP {resp.status_code} {resp.text[:300]}"
                )
            payload = resp.json() or {}
            rows.extend(payload.get("value") or [])
            url = payload.get("@odata.nextLink")
            pages += 1
            if max_rows is not None and len(rows) >= max_rows:
                rows = rows[:max_rows]
                break

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # Drop OData control columns if present.
        df = df[[c for c in df.columns if not str(c).startswith("@odata")]]
        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        return df

    def _resolve_asset(self, table_name: Optional[str]) -> Optional[Dict]:
        if not table_name:
            return None
        meta = self._resolve_from_attached(table_name)
        if meta:
            return meta
        if self._table_metadata_map and self._schemas_cache is None:
            # The persisted catalog is authoritative for query-time resolution;
            # do not fall through to a live crawl for a name it does not hold.
            return None
        try:
            t = self.get_schema(table_name)
        except ValueError:
            raise
        except Exception:
            return None
        return (t.metadata_json or {}).get("sap_datasphere")

    def _resolve_from_attached(self, table_name: str) -> Optional[Dict]:
        """Resolve against the persisted catalog metadata: exact name, then
        case-insensitive name, then bare asset name (unambiguous only)."""
        if not self._table_metadata_map:
            return None
        meta = self._table_metadata_map.get(table_name)
        if meta:
            return meta
        lowered = table_name.strip().lower()
        matches = [m for n, m in self._table_metadata_map.items() if n.strip().lower() == lowered]
        if not matches:
            matches = [
                m for m in self._table_metadata_map.values()
                if str(m.get("asset") or "").strip().lower() == lowered
            ]
        if len({(m.get("space"), m.get("asset")) for m in matches}) > 1:
            raise ValueError(
                f"Ambiguous SAP Datasphere model '{table_name}'; use the exact 'Space/Model' name."
            )
        return matches[0] if matches else None

    @staticmethod
    def _apply_parameter_set(data_url: str, pairs: str) -> str:
        # Insert (pairs)/Set before the trailing entity segment:
        # .../analytical/space/Model/Model  ->  .../analytical/space/Model(pairs)/Set
        base, _, entity = data_url.rpartition("/")
        return f"{base}({pairs})/Set"

    @staticmethod
    def _build_query_string(
        query: Optional[str],
        select: Optional[str],
        filter: Optional[str],
        orderby: Optional[str],
        top: Optional[int],
    ) -> str:
        if query and query.strip():
            # Caller supplied raw OData options; use verbatim (strip a leading ?).
            return query.strip().lstrip("?")
        params: List[Tuple[str, str]] = []
        if select:
            params.append(("$select", select))
        if filter:
            params.append(("$filter", filter))
        if orderby:
            params.append(("$orderby", orderby))
        if top is not None:
            params.append(("$top", str(int(top))))
        # Safe encoding that leaves OData-significant chars readable; spaces and
        # reserved chars (&, +, #) inside values are percent-encoded.
        return urlencode(params, quote_via=quote, safe="$,()='/")

    # ------------------------------------------------------------------
    # Connection test & prompt
    # ------------------------------------------------------------------

    def test_connection(self) -> Dict:
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}
        try:
            assets = self._list_assets()
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Authenticated, but catalog listing failed: {e}",
            }
        n = len(assets)
        msg = f"Connected to SAP Datasphere. Found {n} exposed asset(s)."
        if n == 0:
            msg += (
                " (No assets visible — check the OAuth client's scoped roles/space "
                "membership, and that objects are marked 'Expose for Consumption'.)"
            )
        return {"success": True, "message": msg, "assets": n}

    def get_schemas_count(self) -> int:
        return len(self.get_schemas())

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    @property
    def description(self) -> str:
        return "SAP Datasphere semantic layer (analytical OData).\n\n" + self.system_prompt()

    def system_prompt(self) -> str:
        return """
## SAP Datasphere Query Guide (analytical OData)

Each analytic model is one schema table named `Space/Model`. Columns are tagged
`role=dimension` (groupable attributes) or `role=measure` (aggregated values).
You do NOT write SQL, DAX, or MDX — you pass OData query options and the model
aggregates each measure **server-side** over the dimensions you `$select`.

### How to query — execute_query(odata_query, table_name)

`execute_query` takes the OData query options as the FIRST positional argument
(a string) and the model name as the SECOND argument — exactly like the Power BI
client. BOTH are required.

```python
# Total revenue by country (Revenue aggregates over the Country dimension)
df = db_clients['sap_datasphere'].execute_query(
    "$select=Country,Revenue&$orderby=Revenue desc&$top=100",   # OData options (1st arg)
    "SALES/SalesAnalyticModel",                                  # model 'Space/Model' (2nd arg)
)

# Filter first, then aggregate by two dimensions
df = db_clients['sap_datasphere'].execute_query(
    "$select=Country,Product,Revenue&$filter=Country eq 'US'&$top=1000",
    "SALES/SalesAnalyticModel",
)
```

### Rules
- FIRST arg = OData options string; SECOND arg = the model name `Space/Model`
  exactly as shown in the schema. Never call with only keyword arguments.
- Tables marked `kind=relational` in their `meta:` line are exposed views, not
  analytic models: they return raw rows and `$select` does NOT aggregate. Use
  `$filter`/`$top` to bound them and aggregate in pandas.
- Every query is bounded: add `$top` yourself, or at most 10,000 rows are returned.
- In `$select`, list the dimensions to group by plus the measures to return.
  Omitting a dimension aggregates across it. Selecting only measures gives a grand total.
- `$filter` uses OData syntax: `eq, ne, gt, ge, lt, le, and, or`; string literals
  in single quotes (`Country eq 'US'`). `$orderby Field [desc]`, `$top N`.
- Prefer the model's defined measures — never recompute an aggregate it already exposes.
- Analytic-model variables: pass `parameters={"P_Year": "2025"}`.
"""


# ---------------------------------------------------------------------------
# Module-level helpers (namespace-insensitive CSDL parsing)
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Local name of a possibly namespaced XML tag."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _local_term(term: str) -> str:
    """Local name of an annotation Term (e.g. 'com.sap...Analytics.v1.Measure' → 'Measure')."""
    return term.rsplit(".", 1)[-1] if "." in term else term


def _edm_to_dtype(edm: str) -> str:
    """Map an EDM type to a short dtype label for the schema display."""
    if edm in _NUMERIC_EDM_TYPES:
        return "number"
    if edm in ("Edm.Boolean",):
        return "boolean"
    if edm in ("Edm.Date", "Edm.DateTimeOffset", "Edm.Time", "Edm.Duration"):
        return "datetime"
    return "string"


def _param_literal(v: str) -> str:
    """Quote an analytic-model parameter value as an OData string literal.

    Datasphere's (Param='value')/Set syntax quotes every value — including
    numeric-looking IDs — so we always single-quote (doubling embedded quotes).
    """
    return "'" + str(v).replace("'", "''") + "'"


# Compatibility alias for any dynamic resolver expecting 'SapDatasphereClient'.
SAPDatasphereClient = SapDatasphereClient
