"""Salesforce as an extraction source: SOQL, paged to the end.

The agent-facing `execute_query` stops at 10,000 rows on purpose — a
model-written query must not pull an object into memory. Materializing a
custom table is the opposite job: it wants every row, and can take them a page
at a time. Salesforce's query cursor (`nextRecordsUrl`) is exactly that, so
rows stream in the API's own pages and memory stays at one batch.

SOQL cannot select from a subquery, so a refresh window cannot be wrapped
around the admin's query the way it is in SQL. It is written into the query's
own WHERE clause instead (`window_soql`), or placed by the admin with
`{{since}}` / `{{until}}`.
"""

import re
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal

_LIMIT = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
# Clauses that end the WHERE clause, in the order SOQL allows them.
_TAIL = re.compile(r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET|FOR\s+(?:VIEW|REFERENCE|UPDATE)|WITH\s+SECURITY_ENFORCED)\b",
                   re.IGNORECASE)
_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SINCE = re.compile(r"\{\{\s*since\s*\}\}", re.IGNORECASE)
_UNTIL = re.compile(r"\{\{\s*until\s*\}\}", re.IGNORECASE)


class SalesforceSource:
    """estimate / preview / stream_batches over the Salesforce query API."""

    def __init__(self, client):
        self.client = client

    def estimate(self, sql: str):
        from app.data_sources.fast.extractor import Estimate

        return Estimate(supported=False, note="Salesforce reports a result's size only once it runs")

    def preview(self, sql: str, limit: int):
        df = self.client.execute_query(bounded_soql(sql, limit)).head(limit)
        cols = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
        return cols, df.astype(object).where(df.notna(), None).values.tolist()

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        from app.data_sources.clients.salesforce_client import (
            _records_to_df,
            format_salesforce_error,
            soql_select_columns,
        )
        from app.data_sources.fast.normalize import rows_to_arrow

        expected = soql_select_columns(sql)
        columns = None
        pending: list = []

        def flush(records):
            nonlocal columns
            df = _records_to_df(records, expected)
            # A lookup that is null arrives as `Account: null`, not as its
            # fields: that is a null `Account.Name`, not a column of its own.
            stray = [c for c in df.columns
                     if c not in expected and any(str(e).startswith(f"{c}.") for e in expected)]
            if stray:
                df = df.drop(columns=stray)
            df = _typed(df)
            if columns is None:
                columns = [str(c) for c in df.columns]
            else:
                # Later pages can lack a field that was null throughout them.
                for name in columns:
                    if name not in df.columns:
                        df[name] = None
                extra = [str(c) for c in df.columns if c not in columns]
                columns = columns + extra
                df = df[columns]
            return rows_to_arrow(columns, df.astype(object).where(df.notna(), None).values.tolist())

        try:
            with self.client.connect() as sf:
                result = sf.query(sql)
                while True:
                    pending.extend(result.get("records", []))
                    while len(pending) >= batch_rows:
                        chunk, pending = pending[:batch_rows], pending[batch_rows:]
                        yield flush(chunk)
                    if result.get("done", True) or not result.get("nextRecordsUrl"):
                        break
                    result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
        except GeneratorExit:
            raise
        except Exception as e:
            raise RuntimeError(f"Salesforce query failed: {format_salesforce_error(e)}") from e
        if pending or columns is None:
            if not pending and not expected:
                raise RuntimeError(
                    "This query returned no rows and its columns could not be read "
                    "from the SELECT list, so the table has no shape to be created with."
                )
            yield flush(pending)


_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _typed(df):
    """Salesforce sends dates and date-times as text. Stored as text they
    would sort and compare as text — and a checkpoint read back from them
    could not be written into SOQL, where a date is a bare token. A column
    whose every value is a date or date-time becomes one."""
    for name in df.columns:
        values = [v for v in df[name].tolist() if v is not None and v == v]
        if not values or not all(isinstance(v, str) for v in values):
            continue
        if all(_DATETIME.match(v) for v in values):
            df[name] = [_parse_datetime(v) for v in df[name].tolist()]
        elif all(_DATE.match(v) for v in values):
            df[name] = [date.fromisoformat(v) if isinstance(v, str) else None for v in df[name].tolist()]
    return df.astype(object)


def _parse_datetime(text):
    if not isinstance(text, str):
        return None
    value = text.replace("Z", "+00:00")
    # `+0000` (the API's form) to `+00:00`.
    if re.search(r"[+-]\d{4}$", value):
        value = value[:-2] + ":" + value[-2:]
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def bounded_soql(soql: str, limit: int) -> str:
    """`soql` returning at most `limit` rows (its own smaller LIMIT is kept)."""
    text = soql.rstrip().rstrip(";").rstrip()
    found = _LIMIT.search(_outside(text))
    if found:
        current = int(found.group().split()[-1])
        if current <= limit:
            return text
        return _LIMIT.sub(f"LIMIT {limit}", text, count=1)
    return f"{text} LIMIT {limit}"


def _outside(text: str) -> str:
    """`text` with quoted strings and parenthesized spans blanked out, the
    same length, so positions found in it are positions in the original."""
    out, depth, quote = [], 0, False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < len(text):
                out.append("  ")
                i += 2
                continue
            if ch == "'":
                quote = False
            out.append(" ")
        elif ch == "'":
            quote = True
            out.append(" ")
        elif ch == "(":
            depth += 1
            out.append(" ")
        elif ch == ")":
            depth = max(0, depth - 1)
            out.append(" ")
        else:
            out.append(ch if depth == 0 else " ")
        i += 1
    return "".join(out)


def soql_literal(value) -> str:
    """A SOQL literal: date and datetime values are written bare, in UTC."""
    if isinstance(value, bool):
        raise ValueError("unsupported cursor value")
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        return format(value, "f") if isinstance(value, Decimal) else repr(value)
    text = str(value)
    if not re.fullmatch(r"[0-9A-Za-z:\-+. _/]*", text):
        raise ValueError("The cursor column holds text that cannot be used as a checkpoint safely.")
    return f"'{text}'"


def window_soql(soql: str, column: str, kind, window) -> str:
    """`soql` restricted to rows whose `column` is in the window.

    With `{{since}}` / `{{until}}` in the query they are filled in. Otherwise
    the condition is added to the top-level WHERE clause (or one is created),
    ahead of GROUP BY / ORDER BY / LIMIT.
    """
    from app.data_sources.fast.incremental import IncrementalConfigError

    def lit(value):
        try:
            return soql_literal(value)
        except ValueError as e:
            raise IncrementalConfigError(str(e)) from None

    if _SINCE.search(soql) or _UNTIL.search(soql):
        lo = window.lo if window.lo is not None else _floor(kind, window.hi)
        hi = window.hi if window.hi is not None else _ceiling(kind, window.lo)
        out = _SINCE.sub(lambda _m: lit(lo), soql)
        return _UNTIL.sub(lambda _m: lit(hi), out)

    if window.lo is None and window.hi is None:
        return soql
    if not _FIELD.match(column or ""):
        raise IncrementalConfigError(
            f"'{column}' cannot be used as a Salesforce cursor: it must be a field name "
            f"such as SystemModstamp or Account.LastModifiedDate."
        )
    parts = []
    if window.lo is not None:
        parts.append(f"{column} >= {lit(window.lo)}")
    if window.hi is not None:
        parts.append(f"{column} < {lit(window.hi)}")
    predicate = " AND ".join(parts)
    if window.include_nulls:
        predicate = f"(({predicate}) OR {column} = null)"

    text = soql.rstrip().rstrip(";").rstrip()
    flat = _outside(text)
    tail = _TAIL.search(flat)
    end = tail.start() if tail else len(text)
    where = _WHERE.search(flat[:end])
    if where:
        existing = text[where.end():end].strip()
        return f"{text[:where.end()]} ({existing}) AND {predicate} {text[end:]}".rstrip()
    return f"{text[:end].rstrip()} WHERE {predicate} {text[end:]}".rstrip()


def _floor(kind, like):
    if kind == "number" or isinstance(like, (int, float, Decimal)) and not isinstance(like, bool):
        return -(2 ** 63)
    if kind == "date" or (isinstance(like, date) and not isinstance(like, datetime)):
        return date(1700, 1, 1)
    return datetime(1700, 1, 1, tzinfo=timezone.utc)


def _ceiling(kind, like):
    if kind == "number" or isinstance(like, (int, float, Decimal)) and not isinstance(like, bool):
        return 2 ** 63 - 1
    if kind == "date" or (isinstance(like, date) and not isinstance(like, datetime)):
        return date(4000, 12, 31)
    return datetime(4000, 12, 31, tzinfo=timezone.utc)
