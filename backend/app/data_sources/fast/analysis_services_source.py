"""Materializing from Analysis Services over XMLA.

A custom table here is a DAX query (`EVALUATE <table expression>`) against one
Tabular model, sent as an XMLA `Execute` over HTTP. The strategy is the one
built for Power BI (fast/powerbi_source.py): an exact count, a sample to find
a column to window on, value windows over that column, and a final check that
every counted row arrived. What differs is the endpoint, and the differences
below come from reading the client (clients/xmla_base.py) and the XMLA
protocol. **None of it has been run against a live server**; where a claim is
an assumption it says so.

**One response is the whole result, parsed in memory.** XMLA has no paging and
the client reads a response with `ET.fromstring`. Nothing in the protocol caps
a rowset the way Power BI's REST endpoint does, so the risk here is not a
silently short result but an unbounded one: `EVALUATE Sales` on a large model
is one XML document holding every row. So this source sets its own page: a
request for rows is always wrapped in `TOPN(page + 1, …)`, which bounds the
response at the server. A response of `page + 1` rows proves the window held
more than a page; it is dropped and the window split. (Assumed, not measured:
that the engine honours `TOPN` over an arbitrary table expression without an
order, which is how DAX documents it and how the Power BI source samples.)

**The page is 100,000 rows or 1,000,000 values**, whichever comes first: the
same numbers as Power BI, chosen here rather than imposed. A result over one
page needs a numeric or date column to be fetched in windows; one without is
refused when the table is saved.

**Values arrive as text; their types arrive separately.** A rowset carries an
XSD schema naming each column and its type (`Content=SchemaData`), and a value
whose column is untyped carries its own `xsi:type`. Both are read, so numbers
land as numbers and dates as dates. A column with neither stays text. Unlike
Power BI's JSON, a null is an absent element, so rows are filled out from the
schema's column list.

**The catalog.** The connection's configured catalog is the target. Without
one, the tables the DAX references are matched against the indexed catalog
(`Catalog/Table` rows), and a reference that fits two catalogs is refused.

**MDX is untouched.** A statement that is not DAX (Multidimensional models
answer only MDX) is read as before, through the connector's `execute_query` in
one request (fast/sources.py QuerySource): no estimate, no paging.

**Truncation.** The count is taken first and compared last; a shortfall aborts
the refresh and the previous artifact keeps serving. A response cut off in
transit is not well-formed XML and fails to parse, which fails the refresh.

**Cancelling.** The client holds no XMLA session, so a statement that is
already running cannot be cancelled from here; stopping means not sending the
next window. One request is bounded by the connection's timeout.
"""

from collections.abc import Iterator
from typing import List, Optional, Tuple

from app.data_sources.clients.xmla_base import check_xmla_statement
from app.data_sources.fast.powerbi_source import PowerBISource, window_dax

# Rows / values one request asks for. Ours, not the server's: see above.
PAGE_ROWS = 100_000
PAGE_VALUES = 1_000_000

_INT_TYPES = {"long", "int", "integer", "short", "byte", "unsignedLong", "unsignedInt",
              "unsignedShort", "unsignedByte"}
_FLOAT_TYPES = {"double", "float", "decimal"}
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_SQL_FIELD = "{urn:schemas-microsoft-com:xml-sql}field"


def is_dax(query: str) -> bool:
    """A DAX query starts with EVALUATE or DEFINE; anything else is MDX."""
    from app.data_sources.clients.powerbi_client import PowerBIClient

    text = PowerBIClient._DAX_TOKEN_RE.sub(
        lambda m: " " if m.lastgroup in ("block_comment", "line_comment") else m.group(),
        query or "",
    )
    head = text.lstrip().upper()
    return head.startswith("EVALUATE") or head.startswith("DEFINE")


class AnalysisServicesSource(PowerBISource):
    """Extraction over an XMLA endpoint that answers DAX."""

    PRODUCT = "Analysis Services"
    MAX_BYTES = 0   # nothing caps a response body; rows are bounded by TOPN

    # -- MDX keeps the path it had ------------------------------------------

    def _generic(self):
        from app.data_sources.fast.sources import QuerySource

        return QuerySource(self.client)

    # A DAX definition is held to the connector's statement check as written,
    # as `execute_query` holds it, and so is every statement built from it
    # (`execute_typed`). MDX goes through `execute_query` itself.

    def estimate(self, sql: str):
        if not is_dax(sql):
            return self._generic().estimate(sql)
        check_xmla_statement(sql)
        return super().estimate(sql)

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        if not is_dax(sql):
            return self._generic().preview(sql, limit)
        check_xmla_statement(sql)
        return super().preview(sql, limit)

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        if not is_dax(sql):
            yield from self._generic().stream_batches(sql, batch_rows)
            return
        check_xmla_statement(sql)
        yield from super().stream_batches(sql, batch_rows)

    # -- what one response can carry ----------------------------------------

    def _ceiling(self, columns: int) -> int:
        return max(1, min(PAGE_ROWS, PAGE_VALUES // max(1, columns)))

    def _page(self, expr: str, ceiling: int) -> str:
        # One more than a page: a full answer is then distinguishable from a
        # cut one, which a response of exactly `ceiling` rows would not be.
        return f"TOPN({int(ceiling) + 1}, {expr})"

    def _cut(self, rows: list, ceiling: int) -> bool:
        return len(rows) > ceiling

    def _verify(self, got: int, expected: int) -> None:
        if got >= expected:
            return
        raise RuntimeError(
            f"Extraction returned {got:,} of the {expected:,} rows counted before it "
            f"started. An incomplete result is refused rather than stored; the "
            f"previously cached data is still being served. The model may have "
            f"changed while it was being read: refresh again."
        )

    def _unwindowable_note(self, ceiling: int) -> str:
        return (
            f"Analysis Services returns a whole result in one response, so it is "
            f"read at most {ceiling:,} rows at a time for a result this wide "
            f"({PAGE_ROWS:,} rows or {PAGE_VALUES:,} values, whichever comes first), "
            f"and reading more than one page needs a numeric or date column in the "
            f"result to fetch it by. Add one, aggregate or filter the query."
        )

    # -- talking to the server ----------------------------------------------

    def _target(self, sql: str) -> dict:
        return resolve_catalog(self.client, sql)

    def _run(self, target: dict, dax: str):
        """One XMLA Execute: (rows as dicts in column order, 0).

        The byte count is unused here: nothing caps a response body.
        """
        _columns, rows = execute_typed(self.client, dax, target["catalog"])
        return rows, 0


def _checked_catalog(client, catalog: Optional[str]) -> Optional[str]:
    """The catalog to send `catalog`'s statement to, held to the connector's
    rule: with a configured catalog, only that one (`check_catalog_override`)."""
    from app.data_sources.clients.xmla_base import check_catalog_override

    configured = getattr(client, "catalog", None)
    if configured:
        return check_catalog_override(catalog, configured)
    return catalog


# --------------------------------------------------------------------------
# Which catalog a DAX text runs against
# --------------------------------------------------------------------------

def resolve_catalog(client, dax: str) -> dict:
    """{"catalog": name} for this DAX.

    A pin on the client wins, then the connection's configured catalog, then
    the catalog that owns the tables the DAX references.
    """
    explicit = getattr(client, "extraction_target", None) or {}
    if explicit.get("catalog"):
        return {"catalog": str(explicit["catalog"])}
    if getattr(client, "catalog", None):
        return {"catalog": str(client.catalog)}

    indexed = getattr(client, "_table_metadata_map", None) or {}
    catalogs: dict = {}
    for name, meta in indexed.items():
        catalog = str(meta.get("catalog") or name.split("/", 1)[0] or "")
        if not catalog:
            continue
        entry = catalogs.setdefault(catalog, {"tables": set(), "dax": False})
        table = str(meta.get("tableName") or name.split("/", 1)[-1]).strip().lower()
        if table:
            entry["tables"].add(table)
        entry["dax"] = entry["dax"] or bool(meta.get("supportsDax"))
    if not catalogs:
        raise RuntimeError(
            "This connection has no catalog configured and nothing indexed, so the "
            "model this DAX targets cannot be resolved. Set the connection's "
            "catalog, or index the connection first."
        )

    from app.data_sources.clients.powerbi_client import PowerBIClient

    refs = PowerBIClient._dax_table_references(dax)
    scored = sorted(((len(refs & c["tables"]), name) for name, c in catalogs.items()),
                    key=lambda t: (-t[0], t[1]))
    best = scored[0][0]
    if best == 0:
        if len(catalogs) == 1:
            # One catalog on the server: there is nothing to choose between.
            return {"catalog": next(iter(catalogs))}
        raise RuntimeError(
            "Could not tell which catalog this DAX targets: none of the tables it "
            "references is in the connection's indexed catalog. Use the table "
            "names exactly as the model spells them, or set the connection's catalog."
        )
    tied = sorted(name for score, name in scored if score == best)
    if len(tied) > 1:
        raise RuntimeError(
            f"This DAX matches tables in {len(tied)} catalogs ({', '.join(tied)}). "
            f"Set the connection's catalog to choose one."
        )
    if not catalogs[tied[0]]["dax"]:
        raise RuntimeError(
            f"{tied[0]} is a Multidimensional model, which does not answer DAX. "
            f"Write the custom table in MDX instead."
        )
    return {"catalog": tied[0]}


# --------------------------------------------------------------------------
# A typed rowset
# --------------------------------------------------------------------------

def execute_typed(client, statement: str, catalog: Optional[str]):
    """Run `statement` and return (column names, rows as dicts).

    The client's own `_execute_statement` returns every value as text and
    drops absent (null) columns; a table built from that would store numbers
    and dates as strings. This reads the same response with its schema.

    The statement and catalog pass the checks the connector's `execute_query`
    applies before anything is sent.
    """
    from xml.sax.saxutils import escape

    from app.data_sources.clients.xmla_base import XMLA_NS

    check_xmla_statement(statement)
    catalog = _checked_catalog(client, catalog)
    client.connect()
    properties = client._property_list_xml(catalog, fmt="Tabular", content="SchemaData")
    body = (
        f'<Execute xmlns="{XMLA_NS}">'
        f"<Command><Statement>{escape(statement)}</Statement></Command>"
        f"<Properties><PropertyList>{properties}</PropertyList></Properties>"
        "</Execute>"
    )
    return parse_typed_rowset(client._soap_call("Execute", body))


def parse_typed_rowset(root):
    """(column names, rows) from an XMLA rowset response.

    Column names and declared types come from the rowset's XSD schema when it
    has one; columns seen only in the rows are added in the order they appear.
    """
    from app.data_sources.clients.xmla_base import XMLA_ROWSET_NS, _decode_xmla_name

    tags: list = []            # element tag -> position
    names: dict = {}           # element tag -> column name
    types: dict = {}           # element tag -> declared xsd type (local name)
    for el in root.iter():
        if _local(el.tag) != "complexType" or el.get("name") != "row":
            continue
        for col in el.iter():
            if _local(col.tag) != "element" or not col.get("name"):
                continue
            tag = col.get("name")
            if tag in names:
                continue
            tags.append(tag)
            names[tag] = col.get(_SQL_FIELD) or _decode_xmla_name(tag)
            if col.get("type"):
                types[tag] = col.get("type").split(":")[-1]
        break

    raw_rows = []
    for row_el in root.iter(f"{{{XMLA_ROWSET_NS}}}row"):
        row = {}
        for child in row_el:
            tag = _local(child.tag)
            if tag not in names:
                tags.append(tag)
                names[tag] = _decode_xmla_name(tag)
            declared = (child.get(_XSI_TYPE) or "").split(":")[-1] or types.get(tag)
            row[tag] = _value(child.text, declared)
        raw_rows.append(row)

    columns = [names[t] for t in tags]
    return columns, [{names[t]: r.get(t) for t in tags} for r in raw_rows]


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _value(text, declared: Optional[str]):
    """A rowset value in the type its column (or the value itself) declares.

    A value that does not parse as declared is kept as the text it arrived as
    rather than dropped.
    """
    if text is None:
        return None
    if not declared or declared == "string":
        return text
    try:
        if declared in _INT_TYPES:
            return int(text)
        if declared in _FLOAT_TYPES:
            return float(text)
        if declared == "boolean":
            return text.strip().lower() in ("true", "1")
        if declared == "dateTime":
            # The shared DAX machinery reads dates as ISO text without a zone.
            return text[:-1] if text.endswith("Z") else text
    except (TypeError, ValueError):
        return text
    return text


# --------------------------------------------------------------------------
# A refresh window
# --------------------------------------------------------------------------

def render_window(query: str, column: str, kind, window) -> str:
    """A DAX query restricted to the window; MDX is returned as written (its
    window is then taken from the result, see `filters_query_at_source`)."""
    if not is_dax(query):
        from app.data_sources.fast.incremental import IncrementalConfigError, uses_placeholders

        if uses_placeholders(query):
            raise IncrementalConfigError(
                "{{since}} / {{until}} are available in DAX queries only; an MDX "
                "query is refreshed by running it whole."
            )
        return query
    return window_dax(query, column, kind, window)
