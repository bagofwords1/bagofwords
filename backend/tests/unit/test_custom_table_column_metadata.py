"""A custom table's columns say what they mean, and keep saying it.

Promises pinned here, through the service's public methods and the rendered
agent context (real sessions, real SQLite extraction, real artifacts):

- on every refresh each column inherits the description of the indexed column
  it copies, a computed column carries its expression, and each is marked
  additive / not additive / unknown;
- a description changed in the catalog follows on the next refresh; a value
  an admin edited does not move;
- a source definition that changed under the copy is flagged on the column
  and recorded on the run;
- the agent is told, on the column, not to sum what does not add up.
"""
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import select

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.data_sources.fast import column_metadata, lineage
from app.dependencies import async_session_maker
from app.models.connection_table import KIND_TABLE, ConnectionTable
from app.models.custom_table_run import CustomTableRun
from app.schemas.data_source_schema import DataSourceSummarySchema
from tests.unit.test_fast_artifact_lifecycle import sandbox  # noqa: F401 - fixture

DEFINITION = (
    "SELECT region, SUM(amount) AS revenue, COUNT(DISTINCT customer_id) AS buyers, "
    "SUM(amount) / COUNT(*) AS average_order FROM orders GROUP BY region"
)
COMMENTS = {"region": "Sales region of the order", "amount": "Net amount in USD",
            "customer_id": "The buying customer"}


def _by_name(columns):
    return {c["name"]: c for c in columns}


async def _index_source(sandbox, comments=COMMENTS, amount_type="REAL"):  # noqa: F811
    """Give the connection a source table and its indexed catalog row.

    The catalog row is written directly: SQLite has no column comments, so no
    indexing run through the API could produce a commented catalog for it.
    """
    with closing(sqlite3.connect(sandbox.source)) as db, db:
        db.execute("DROP TABLE IF EXISTS orders")
        db.execute("CREATE TABLE orders (region TEXT, customer_id INTEGER, amount REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [
            ("north", 1, 10.0), ("north", 1, 30.0), ("north", 2, 20.0), ("south", 2, 5.0)])
    async with async_session_maker() as db:
        row = (await db.execute(select(ConnectionTable).where(
            ConnectionTable.connection_id == sandbox.connection_id, ConnectionTable.kind == KIND_TABLE,
        ))).scalars().first()
        columns = [
            {"name": "region", "dtype": "TEXT", "description": comments.get("region")},
            {"name": "customer_id", "dtype": "INTEGER", "description": comments.get("customer_id")},
            {"name": "amount", "dtype": amount_type, "description": comments.get("amount")},
        ]
        if row is None:
            db.add(ConnectionTable(connection_id=sandbox.connection_id, name="orders", kind=KIND_TABLE,
                                   columns=columns, pks=[], fks=[]))
        else:
            row.columns = columns
        await db.commit()


async def _define(sandbox, definition=DEFINITION):  # noqa: F811
    async with async_session_maker() as db:
        _, table = await sandbox.load(db)
        table.definition_sql = definition
        await db.commit()


async def _refresh(sandbox):  # noqa: F811
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
    async with async_session_maker() as db:
        _, table = await sandbox.load(db)
        return table


async def _edit(sandbox, columns):  # noqa: F811
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.update(db, connection, table, columns=columns)
    async with async_session_maker() as db:
        _, table = await sandbox.load(db)
        return table


# -- publish and refresh --------------------------------------------------------

@pytest.mark.db
@pytest.mark.asyncio
async def test_a_refresh_describes_every_column_from_the_catalog(sandbox):  # noqa: F811
    """Also the path for a table that predates this: it starts with bare
    columns and is described by its next refresh."""
    await _index_source(sandbox)
    await _define(sandbox)
    cols = _by_name((await _refresh(sandbox)).columns)

    region = cols["region"]
    assert region["description"] == COMMENTS["region"]
    assert region["metadata"]["source"] == {"kind": "column", "table": "orders", "column": "region"}

    revenue, buyers, average = cols["revenue"], cols["buyers"], cols["average_order"]
    for derived in (revenue, buyers, average):
        assert derived["metadata"]["source"]["kind"] == "derived"
        assert derived["metadata"]["source"]["expression"] in derived["description"]
        assert derived["metadata"]["fingerprint"]
    assert COMMENTS["amount"] in revenue["description"]       # the meaning of what it sums
    assert revenue["metadata"]["additive"] is True
    assert (buyers["metadata"]["additive"], buyers["metadata"]["additive_reason"]) == (False, "distinct_count")
    assert (average["metadata"]["additive"], average["metadata"]["additive_reason"]) == (False, "ratio")
    # SUM(amount) is the `revenue` column, COUNT(*) is not a column: no recipe is invented.
    assert "recompute" not in average["metadata"]
    # The stored types are the load's own, untouched.
    assert all(c.get("dtype") for c in cols.values())


@pytest.mark.db
@pytest.mark.asyncio
async def test_a_column_with_no_readable_origin_is_left_undescribed(sandbox):  # noqa: F811
    await _index_source(sandbox)
    await _define(sandbox, "SELECT region AS r FROM orders UNION ALL SELECT region FROM orders")
    (col,) = (await _refresh(sandbox)).columns
    assert "description" not in col and "source" not in col["metadata"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_a_changed_source_description_follows_on_the_next_refresh(sandbox):  # noqa: F811
    await _index_source(sandbox)
    await _define(sandbox)
    await _refresh(sandbox)
    await _index_source(sandbox, {**COMMENTS, "region": "Region the order shipped to", "amount": "Gross amount"})
    cols = _by_name((await _refresh(sandbox)).columns)
    assert cols["region"]["description"] == "Region the order shipped to"
    assert "Gross amount" in cols["revenue"]["description"]
    assert all("drift" not in c["metadata"] for c in cols.values()), "a reworded comment is not drift"


@pytest.mark.db
@pytest.mark.asyncio
async def test_what_an_admin_edited_survives_refreshes_and_is_marked(sandbox):  # noqa: F811
    await _index_source(sandbox)
    await _define(sandbox)
    await _refresh(sandbox)

    table = await _edit(sandbox, [
        {"name": "region", "description": "Where the customer is billed"},
        {"name": "revenue", "additive": False},
    ])
    edited = _by_name(table.columns)
    assert edited["region"]["description"] == "Where the customer is billed"   # applied without a reload
    assert edited["region"]["metadata"]["edited"] == ["description"]
    assert edited["region"]["metadata"]["inherited"]["description"] == COMMENTS["region"]

    await _index_source(sandbox, {**COMMENTS, "region": "A newer comment"})
    cols = _by_name((await _refresh(sandbox)).columns)
    assert cols["region"]["description"] == "Where the customer is billed"
    assert cols["region"]["metadata"]["inherited"]["description"] == "A newer comment"
    assert cols["revenue"]["metadata"]["additive"] is False
    assert cols["revenue"]["metadata"]["edited"] == ["additive"]
    assert "edited" not in cols["buyers"]["metadata"]

    # Dropping the edit hands the column back to the source.
    cols = _by_name((await _edit(sandbox, [])).columns)
    assert cols["region"]["description"] == "A newer comment"
    assert "edited" not in cols["region"]["metadata"] and cols["revenue"]["metadata"]["additive"] is True


@pytest.mark.db
@pytest.mark.asyncio
async def test_a_definition_that_moved_under_the_copy_is_flagged_and_recorded(sandbox):  # noqa: F811
    await _index_source(sandbox)
    await _define(sandbox)
    await _refresh(sandbox)
    await _index_source(sandbox, amount_type="TEXT")      # what `amount` is changed at the source
    table = await _refresh(sandbox)
    cols = _by_name(table.columns)
    assert cols["revenue"]["metadata"]["drift"]["previous_fingerprint"]
    assert "drift" not in cols["region"]["metadata"] and "drift" not in cols["buyers"]["metadata"]

    async with async_session_maker() as db:
        runs = (await db.execute(select(CustomTableRun).where(
            CustomTableRun.connection_table_id == sandbox.table_id).order_by(CustomTableRun.started_at)
        )).scalars().all()
    flagged = [e for e in runs[-1].events or [] if "revenue" in e["message"]]
    assert flagged and flagged[0]["level"] == "warning"
    assert not any("revenue" in e["message"] for e in runs[0].events or [])

    # The flag waits for an admin: another refresh keeps it, a review clears it.
    assert "drift" in _by_name((await _refresh(sandbox)).columns)["revenue"]["metadata"]
    reviewed = await _edit(sandbox, [{"name": "revenue", "acknowledge_drift": True}])
    assert "drift" not in _by_name(reviewed.columns)["revenue"]["metadata"]
    assert "drift" not in _by_name((await _refresh(sandbox)).columns)["revenue"]["metadata"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_an_undescribed_table_gets_a_description_made_of_facts(sandbox):  # noqa: F811
    await _index_source(sandbox)
    await _define(sandbox)
    table = await _refresh(sandbox)
    text = table.to_prompt_table().description
    assert "region" in text and "buyers" in text and "distinct count" in text and "orders" in text
    assert text == table.metadata_json["generated_description"]

    async with async_session_maker() as db:
        _, row = await sandbox.load(db)
        row.description = "Curated by finance"
        await db.commit()
    assert (await _refresh(sandbox)).to_prompt_table().description == "Curated by finance"


@pytest.mark.db
@pytest.mark.asyncio
async def test_the_preview_shows_what_would_be_inherited_before_saving(sandbox):  # noqa: F811
    await _index_source(sandbox)
    async with async_session_maker() as db:
        connection, _ = await sandbox.load(db)
        preview = await sandbox.service.preview(db, connection, DEFINITION)
    cols = _by_name(preview["columns"])
    assert cols["region"]["description"] == COMMENTS["region"]
    assert cols["buyers"]["metadata"]["additive"] is False
    assert "buyers" in preview["suggested_description"]
    assert preview["sources"] == ["orders"] and preview["rls_sources"] == []
    assert preview["rows"], "the preview still returns rows"


# -- Power BI, against an indexed-catalog fixture -------------------------------

def _measure(name, expression):
    return {"name": name, "dtype": "measure", "description": expression[:200] or None,
            "metadata": {"role": "measure", "expression": expression, "returns": "Double"}}


def _powerbi_catalog(total_sales="SUM(Sales[Amount])", rls=False):
    def table(name, columns):
        meta = {"datasetId": "ds-1", "datasetName": "Retail", "tableName": name}
        if rls:
            meta["rowLevelSecurity"] = True
        return {"name": f"Retail/{name}", "columns": columns, "pks": [], "fks": [], "metadata_json": {"powerbi": meta}}
    return [
        table("Sales", [
            {"name": "Amount", "dtype": "Double", "metadata": {"role": "column"}},
            _measure("Total Sales", total_sales),
            _measure("Orders", "COUNTROWS(Sales)"),
            _measure("Buyers", "DISTINCTCOUNT(Sales[CustomerKey])"),
            _measure("Average Order", "DIVIDE([Total Sales], [Orders])"),
            _measure("Sales YTD", "TOTALYTD([Total Sales], 'Date'[Date])"),
            _measure("Net Sales", "[Total Sales] - [Refunds]"),
            _measure("Refunds", 'CALCULATE(SUM(Sales[Amount]), Sales[Kind] = "Refund")'),
            _measure("Protected", ""),          # the expression was not readable when indexed
        ]),
        table("Date", [{"name": "Year", "dtype": "Int64", "metadata": {"role": "column"}}]),
    ]


POWERBI_DAX = """EVALUATE SUMMARIZECOLUMNS('Date'[Year],
    "Total Sales", [Total Sales], "Orders", [Orders], "Buyers", [Buyers],
    "Average Order", [Average Order], "Sales YTD", [Sales YTD], "Net Sales", [Net Sales],
    "Protected", [Protected], "Refund Share", DIVIDE([Refunds], [Total Sales]))"""
POWERBI_OUTPUT = ["Year", "Total Sales", "Orders", "Buyers", "Average Order", "Sales YTD", "Net Sales",
                  "Protected", "Refund Share"]


def _powerbi(catalog, previous=None, overrides=None):
    stored = [{"name": n, "dtype": "BIGINT" if n in ("Year", "Orders", "Buyers") else "DOUBLE"}
              for n in POWERBI_OUTPUT]
    found = lineage.trace(POWERBI_DAX, catalog, "powerbi", POWERBI_OUTPUT)
    return column_metadata.enrich(stored, found, catalog, previous=previous, overrides=overrides)


@pytest.mark.parametrize("column, additive, reason", [
    ("Total Sales", True, "sum"),
    ("Orders", True, "count"),
    ("Net Sales", True, "sum"),                     # followed through [Total Sales] - [Refunds]
    ("Buyers", False, "distinct_count"),
    ("Average Order", False, "ratio"),
    ("Sales YTD", False, "time_intelligence"),
    ("Refund Share", False, "ratio"),               # computed in the query itself
    ("Protected", "unknown", "unreadable"),
])
def test_power_bi_measures_are_classified_from_their_indexed_expressions(column, additive, reason):
    meta = _by_name(_powerbi(_powerbi_catalog()).columns)[column]["metadata"]
    assert (meta["additive"], meta["additive_reason"]) == (additive, reason)


def test_a_power_bi_measure_keeps_its_expression_and_role():
    cols = _by_name(_powerbi(_powerbi_catalog()).columns)
    buyers = cols["Buyers"]
    assert buyers["metadata"]["source"] == {"kind": "measure", "table": "Retail/Sales", "column": "Buyers"}
    assert buyers["metadata"]["role"] == "measure"
    assert buyers["metadata"]["expression"] == "DISTINCTCOUNT(Sales[CustomerKey])"
    assert "DISTINCTCOUNT" in buyers["description"]
    assert cols["Year"]["metadata"]["source"]["table"] == "Retail/Date"


def test_a_changed_measure_expression_is_drift_on_what_uses_it():
    first = _powerbi(_powerbi_catalog())
    assert first.drift == []
    second = _powerbi(_powerbi_catalog(total_sales="SUMX(Sales, Sales[Amount] * 1.17)"), previous=first.columns)
    moved = {d["column"] for d in second.drift}
    # The measure itself and the query's own expression that reads it.
    assert {"Total Sales", "Refund Share"} <= moved
    assert "Orders" not in moved and "Buyers" not in moved
    cols = _by_name(second.columns)
    assert "SUM(Sales[Amount])" in cols["Total Sales"]["metadata"]["drift"]["previous_expression"]
    # The query's own ratio reads the measure; its text did not change, so no "previously" is claimed.
    assert "previous_expression" not in cols["Refund Share"]["metadata"]["drift"]
    assert "SUMX" in cols["Total Sales"]["description"]          # the description follows
    third = _powerbi(_powerbi_catalog(total_sales="SUMX(Sales, Sales[Amount] * 1.17)"), previous=second.columns)
    assert third.drift == [] and "drift" in _by_name(third.columns)["Total Sales"]["metadata"]


def test_an_admin_can_settle_an_unknown_and_it_stays_settled():
    cols = _by_name(_powerbi(_powerbi_catalog(), overrides={"Protected": {"additive": True}}).columns)
    meta = cols["Protected"]["metadata"]
    assert meta["additive"] is True and meta["edited"] == ["additive"]
    assert meta["inherited"]["additive"] == "unknown"


def test_a_source_marked_with_row_level_security_is_reported():
    assert _powerbi(_powerbi_catalog()).rls_sources == []
    marked = _powerbi(_powerbi_catalog(rls=True))
    assert "Retail/Sales" in marked.rls_sources


def test_the_generated_description_states_grain_measures_source_and_schedule():
    enriched = _powerbi(_powerbi_catalog())
    text = column_metadata.describe_table(
        enriched.columns, sources=enriched.sources, connection_name="Tenant",
        schedule=column_metadata.schedule_text("interval", 120, None), aggregated=enriched.aggregated)
    assert "One row per Year" in text
    assert "Buyers (not additive: distinct count)" in text and "Total Sales (additive)" in text
    assert "Protected (additivity unknown)" in text
    assert "Retail/Sales" in text and "Tenant" in text and "every 2 hours" in text


@pytest.mark.parametrize("mode, minutes, at, expected", [
    ("interval", 60, None, "every hour"), ("interval", 15, None, "every 15 minutes"),
    ("interval", 1440, None, "every day"), ("time", None, "06:30", "daily at 06:30"), (None, None, None, None),
])
def test_schedule_wording(mode, minutes, at, expected):
    assert column_metadata.schedule_text(mode, minutes, at) == expected


# -- what a computed input is, wherever it was computed ---------------------------

EVENTS = [{"name": "events", "pks": [], "fks": [], "columns": [
    {"name": "user_id", "dtype": "integer"}, {"name": "month", "dtype": "date"},
    {"name": "cents", "dtype": "integer"}, {"name": "year", "dtype": "integer"}]}]


def _sql_table(sql, output, dtypes=None):
    found = lineage.trace(sql, EVENTS, "postgresql", output)
    stored = [{"name": n, "dtype": (dtypes or {}).get(n, "int64")} for n in output]
    return _by_name(column_metadata.enrich(stored, found, EVENTS).columns)


MONTHLY_USERS = "WITH m AS (SELECT month, COUNT(DISTINCT user_id) AS users FROM events GROUP BY month) "


@pytest.mark.parametrize("outer", [
    "CAST(users AS BIGINT)", "users", "users * 1", "COALESCE(users, 0)", "ROUND(users)", "(users)",
])
def test_a_distinct_count_stays_one_through_a_cte_and_a_cast(outer):
    """The outer query only renames or retypes it; a yearly sum would still
    count a returning user once per month."""
    cols = _sql_table(MONTHLY_USERS + f"SELECT month, {outer} AS users FROM m", ["month", "users"])
    meta = cols["users"]["metadata"]
    assert (meta["additive"], meta["additive_reason"]) == (False, "distinct_count")
    assert column_metadata.agent_note(meta, "int64").startswith("NOT ADDITIVE")


def test_the_same_holds_through_a_subquery_and_for_other_non_additive_inputs():
    cols = _sql_table(
        "SELECT month, CAST(mean AS BIGINT) AS mean, CAST(total AS BIGINT) AS total FROM "
        "(SELECT month, AVG(cents) AS mean, SUM(cents) AS total FROM events GROUP BY month) t",
        ["month", "mean", "total"])
    assert cols["mean"]["metadata"]["additive_reason"] == "average"
    assert cols["total"]["metadata"]["additive"] is True          # a retyped sum is still a sum


def test_an_input_whose_computation_cannot_be_read_is_unknown_not_additive():
    cols = _sql_table(
        "SELECT month, CAST(users AS BIGINT) AS users FROM "
        "(SELECT month, 1 AS users FROM events UNION ALL SELECT month, 2 FROM events) t",
        ["month", "users"])
    assert cols["users"]["metadata"]["additive"] == "unknown"


def test_a_grouping_key_that_came_through_a_cte_is_still_a_key():
    cols = _sql_table(
        "WITH y AS (SELECT year, SUM(cents) AS total FROM events GROUP BY year) SELECT year, total FROM y",
        ["year", "total"])
    assert cols["year"]["metadata"]["additive_reason"] == "grouping_key"
    assert cols["total"]["metadata"]["additive"] is True


def test_an_offset_after_aggregation_is_flagged_and_a_unit_conversion_inside_sum_is_not():
    cols = _sql_table(
        "SELECT month, SUM(cents) + 1 AS padded, SUM(cents / 100.0) AS dollars FROM events GROUP BY month",
        ["month", "padded", "dollars"], {"dollars": "double"})
    assert (cols["padded"]["metadata"]["additive"], cols["padded"]["metadata"]["additive_reason"]) == (False, "offset")
    assert column_metadata.agent_note(cols["padded"]["metadata"], "int64").startswith("NOT ADDITIVE")
    assert cols["dollars"]["metadata"]["additive"] is True
    assert column_metadata.agent_note(cols["dollars"]["metadata"], "double") is None


def test_a_direct_min_or_max_rolls_up_with_the_same_function():
    cols = _sql_table(
        "SELECT month, MIN(cents) AS lowest, CAST(MAX(cents) AS BIGINT) AS \"highest paid\", "
        "MAX(cents) - MIN(cents) AS spread FROM events GROUP BY month",
        ["month", "lowest", "highest paid", "spread"])
    assert cols["lowest"]["metadata"]["recompute"] == "MIN(lowest)"
    assert cols["highest paid"]["metadata"]["recompute"] == 'MAX("highest paid")'
    assert "recompute" not in cols["spread"]["metadata"]            # not a plain extremum
    note = column_metadata.agent_note(cols["lowest"]["metadata"], "int64")
    assert "MIN(lowest)" in note and "cannot answer" not in note


def test_a_power_bi_ratio_times_a_factor_gets_no_recomputation():
    """DIVIDE([Total Sales], [Orders]) * 100 is a percentage: SUM / SUM would be a hundredth of it."""
    catalog = _powerbi_catalog()
    catalog[0]["columns"] += [
        _measure("Sales Per Order Pct", "DIVIDE([Total Sales], [Orders]) * 100"),
        _measure("Sales Per Order Or Zero", "DIVIDE([Total Sales], [Orders], 0)"),
        _measure("Largest Sale", "MAX(Sales[Amount])"),
        _measure("Largest Sale Plus", "MAX(Sales[Amount]) + 1"),
    ]
    names = ["Year", "Total Sales", "Orders", "Sales Per Order Pct", "Sales Per Order Or Zero", "Largest Sale",
             "Largest Sale Plus", "Inline Pct"]
    dax = ("EVALUATE SUMMARIZECOLUMNS('Date'[Year], " + ", ".join(f'"{n}", [{n}]' for n in names[1:7])
           + ', "Inline Pct", 100 * DIVIDE([Total Sales], [Orders]))')
    found = lineage.trace(dax, catalog, "powerbi", names)
    cols = _by_name(column_metadata.enrich([{"name": n, "dtype": "double"} for n in names], found, catalog).columns)
    for name in ("Sales Per Order Pct", "Inline Pct"):
        assert cols[name]["metadata"]["additive_reason"] == "ratio"
        assert "recompute" not in cols[name]["metadata"], name
    assert cols["Sales Per Order Or Zero"]["metadata"]["recompute"] == 'SUM("Total Sales") / SUM(Orders)'
    assert cols["Largest Sale"]["metadata"]["recompute"] == 'MAX("Largest Sale")'
    assert "recompute" not in cols["Largest Sale Plus"]["metadata"]


# -- what the agent reads -------------------------------------------------------

def _rendered(columns, how):
    from app.ai.prompt_formatters import Table, TableColumn

    table = Table(
        name="yearly_sales", is_active=True, pks=[], fks=[],
        columns=[TableColumn(name=c["name"], dtype=c.get("dtype"), description=c.get("description"),
                             metadata=c.get("metadata")) for c in columns],
    )
    ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="1", name="Sales", type="duckdb"), tables=[table])])
    return ctx.render("full") if how == "full" else ctx.render_combined(top_k_per_ds=10, index_limit=200)


def _column_tag(xml, name):
    return next(line for line in xml.splitlines() if f'<column name="{name}"' in line)


@pytest.mark.parametrize("how", ["full", "combined"])
def test_the_agent_is_told_on_the_column_not_to_sum_what_does_not_add_up(how):
    xml = _rendered(_powerbi(_powerbi_catalog()).columns, how)
    for name in ("Buyers", "Average Order", "Sales YTD", "Refund Share"):
        tag = _column_tag(xml, name)
        assert 'aggregation="NOT ADDITIVE' in tag and "SUM" in tag, tag
    assert "distinct count" in _column_tag(xml, "Buyers")
    assert 'aggregation="additivity unknown' in _column_tag(xml, "Protected")
    for name in ("Total Sales", "Orders", "Net Sales", "Year"):
        assert "aggregation=" not in _column_tag(xml, name)


@pytest.mark.parametrize("how", ["full", "combined"])
def test_a_ratio_of_two_columns_tells_the_agent_how_to_recompute_it(how):
    """[Average Order] is DIVIDE([Total Sales], [Orders]) and both are columns
    of the table, so the right roll-up is stated; [Refund Share] divides by a
    measure that is not a column, so nothing is suggested."""
    columns = _powerbi(_powerbi_catalog()).columns
    assert _by_name(columns)["Average Order"]["metadata"]["recompute"] == 'SUM("Total Sales") / SUM(Orders)'
    assert "recompute" not in _by_name(columns)["Refund Share"]["metadata"]
    assert 'SUM(&quot;Total Sales&quot;) / SUM(Orders)' in _column_tag(_rendered(columns, how), "Average Order")
    # Once an admin calls a part not additive, it is no longer offered as one.
    overridden = _powerbi(_powerbi_catalog(), overrides={"Orders": {"additive": False}}).columns
    assert "recompute" not in _by_name(overridden)["Average Order"]["metadata"]


@pytest.mark.parametrize("how", ["full", "combined"])
def test_an_admins_verdict_is_what_the_agent_reads(how):
    columns = _powerbi(_powerbi_catalog(), overrides={
        "Protected": {"additive": True}, "Total Sales": {"additive": False},
        "Buyers": {"description": "Customers with at least one order"},
    }).columns
    xml = _rendered(columns, how)
    assert "aggregation=" not in _column_tag(xml, "Protected")
    assert 'aggregation="NOT ADDITIVE' in _column_tag(xml, "Total Sales")
    assert 'description="Customers with at least one order"' in _column_tag(xml, "Buyers")


@pytest.mark.parametrize("how", ["full", "combined"])
def test_columns_nobody_would_sum_and_ordinary_tables_carry_no_note(how):
    plain = [{"name": "city", "dtype": "VARCHAR", "metadata": {"additive": False, "additive_reason": "not_numeric"}},
             {"name": "store_id", "dtype": "BIGINT", "metadata": {"additive": False, "additive_reason": "key"}},
             {"name": "amount", "dtype": "DOUBLE", "metadata": {"additive": "unknown", "additive_reason": "unreadable"}},
             {"name": "legacy", "dtype": "DOUBLE"}, {"name": "pbi", "dtype": "measure", "metadata": {"role": "measure"}}]
    assert "aggregation=" not in _rendered(plain, how)
