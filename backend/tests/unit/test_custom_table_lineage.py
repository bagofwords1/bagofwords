"""Where a custom table's columns come from, per definition language.

The contract (`app/data_sources/fast/lineage.py`): a plain or aliased column
traces to the indexed column it copies; anything computed is derived and
keeps its expression; and a column whose origin is not unambiguous gets no
lineage at all. A wrong origin would attach another column's description to
it, so "nothing" is the safe answer and these tests hold it to that.
"""
import pytest

from app.data_sources.fast import lineage
from app.data_sources.fast.lineage import KIND_COLUMN, KIND_DERIVED, KIND_MEASURE, SourceRef


def _col(name, dtype="text", **extra):
    return {"name": name, "dtype": dtype, **extra}


SQL_CATALOG = [
    {"name": "shop.orders", "columns": [
        _col("id", "integer"), _col("customer_id", "integer"), _col("amount", "numeric"),
        _col("region"), _col("placed_at", "timestamp")]},
    {"name": "shop.customers", "columns": [_col("id", "integer"), _col("segment"), _col("region")]},
    {"name": "archive.orders", "columns": [_col("id", "integer"), _col("amount", "numeric")]},
]

SQL_DIALECTS = ["postgresql", "mysql", "mssql", "snowflake", "bigquery", "oracledb", "sqlite", "clickhouse"]


def _trace(sql, outputs, conn_type="postgresql", catalog=SQL_CATALOG, target=None):
    return lineage.trace(sql, catalog, conn_type, outputs, target).columns


def _origin(lin):
    return (lin.kind, lin.source) if lin.source else (lin.kind, lin.expression)


# -- SQL ----------------------------------------------------------------------

@pytest.mark.parametrize("conn_type", SQL_DIALECTS)
def test_a_plain_column_traces_to_the_indexed_column(conn_type):
    cols = _trace("SELECT region, amount FROM shop.orders", ["region", "amount"], conn_type)
    assert _origin(cols["region"]) == (KIND_COLUMN, SourceRef("shop.orders", "region"))
    assert _origin(cols["amount"]) == (KIND_COLUMN, SourceRef("shop.orders", "amount"))


@pytest.mark.parametrize("conn_type", SQL_DIALECTS)
def test_an_alias_keeps_the_origin_and_takes_the_new_name(conn_type):
    cols = _trace("SELECT o.amount AS net_value FROM shop.orders o", ["net_value"], conn_type)
    assert _origin(cols["net_value"]) == (KIND_COLUMN, SourceRef("shop.orders", "amount"))


@pytest.mark.parametrize("conn_type", SQL_DIALECTS)
def test_an_aggregate_is_derived_and_keeps_its_expression(conn_type):
    found = lineage.trace(
        "SELECT region, SUM(amount) AS revenue, COUNT(DISTINCT customer_id) AS buyers "
        "FROM shop.orders GROUP BY region", SQL_CATALOG, conn_type, ["region", "revenue", "buyers"])
    revenue, buyers = found.columns["revenue"], found.columns["buyers"]
    assert revenue.kind == buyers.kind == KIND_DERIVED
    assert "SUM" in revenue.expression.upper() and "amount" in revenue.expression.lower()
    assert "DISTINCT" in buyers.expression.upper()
    assert revenue.reads == [SourceRef("shop.orders", "amount")]
    assert found.aggregated
    assert found.columns["region"].grouping and not revenue.grouping


@pytest.mark.parametrize("expression", [
    "CASE WHEN amount > 100 THEN 'big' ELSE 'small' END", "amount * 2", "CAST(amount AS INTEGER)",
    "COALESCE(region, 'n/a')",
])
def test_anything_computed_is_derived_not_attributed_to_a_column(expression):
    cols = _trace(f"SELECT {expression} AS x FROM shop.orders", ["x"])
    assert cols["x"].kind == KIND_DERIVED and cols["x"].source is None
    assert cols["x"].expression


def test_a_join_attributes_each_column_to_its_own_table():
    cols = _trace(
        "SELECT o.region, c.segment, c.region AS customer_region, SUM(o.amount) AS revenue "
        "FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id GROUP BY 1, 2, 3",
        ["region", "segment", "customer_region", "revenue"])
    assert cols["region"].source == SourceRef("shop.orders", "region")
    assert cols["segment"].source == SourceRef("shop.customers", "segment")
    assert cols["customer_region"].source == SourceRef("shop.customers", "region")
    assert cols["revenue"].kind == KIND_DERIVED


def test_an_unqualified_column_two_joined_tables_share_gets_no_lineage():
    """`region` exists on both sides: the database would refuse it or pick by
    its own rules, and we do not guess which."""
    cols = _trace("SELECT region, segment FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id",
                  ["region", "segment"])
    assert "region" not in cols
    assert cols["segment"].source == SourceRef("shop.customers", "segment")


def test_a_table_name_two_schemas_share_is_not_resolved_unqualified():
    assert _trace("SELECT amount FROM orders", ["amount"]) == {}
    assert _trace("SELECT amount FROM archive.orders", ["amount"])["amount"].source.table == "archive.orders"


def test_star_expands_to_the_tables_columns():
    names = [c["name"] for c in SQL_CATALOG[1]["columns"]]
    cols = _trace("SELECT * FROM shop.customers", names)
    assert {n: cols[n].source for n in names} == {n: SourceRef("shop.customers", n) for n in names}


def test_columns_are_followed_through_a_cte_and_a_subquery():
    cols = _trace(
        "WITH totals AS (SELECT region AS r, SUM(amount) AS s FROM shop.orders GROUP BY region) "
        "SELECT t.r, t.s FROM (SELECT r, s FROM totals) t", ["r", "s"])
    assert cols["r"].source == SourceRef("shop.orders", "region")
    assert cols["s"].kind == KIND_DERIVED and "SUM" in cols["s"].expression.upper()


def test_names_the_source_renamed_are_matched_by_position():
    """Two outputs called `id` are stored as `id`, `id_1`; a source that folds
    case stores REGION. The select list's order still says which is which."""
    cols = _trace("SELECT o.id, c.id, o.region FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id",
                  ["id", "id_1", "REGION"])
    assert cols["id"].source == SourceRef("shop.orders", "id")
    assert cols["id_1"].source == SourceRef("shop.customers", "id")
    assert cols["REGION"].source == SourceRef("shop.orders", "region")


@pytest.mark.parametrize("sql", [
    "SELEC region FRM shop.orders",                      # not SQL
    "",                                                  # nothing
    "SELECT region FROM shop.orders UNION SELECT segment FROM shop.customers",   # two origins
    "SELECT region FROM shop.orders; SELECT 1",          # two statements
    "SELECT region FROM not_indexed",                    # table we know nothing about
    "SELECT region FROM some_function(1) f",             # columns unknown
], ids=["unparseable", "empty", "union", "two statements", "unknown table", "table function"])
def test_what_cannot_be_read_unambiguously_yields_no_lineage(sql):
    assert _trace(sql, ["region"]) == {}


def test_an_unknown_relation_in_the_join_blocks_unqualified_columns_only():
    cols = _trace("SELECT amount, o.region FROM shop.orders o JOIN not_indexed n ON n.id = o.id",
                  ["amount", "region"])
    assert "amount" not in cols          # could be n.amount
    assert cols["region"].source == SourceRef("shop.orders", "region")


def test_incremental_placeholders_do_not_break_the_reading():
    cols = _trace("SELECT region, amount FROM shop.orders WHERE placed_at >= {{since}} AND placed_at < {{until}}",
                  ["region", "amount"])
    assert cols["amount"].source == SourceRef("shop.orders", "amount")


# -- DAX ----------------------------------------------------------------------

def _measure(name, expression):
    return {"name": name, "dtype": "measure", "description": expression[:200] or None,
            "metadata": {"role": "measure", "expression": expression}}


def _model(dataset_id, dataset_name):
    def table(name, columns):
        return {"name": f"{dataset_name}/{name}", "columns": columns,
                "metadata_json": {"powerbi": {"datasetId": dataset_id, "datasetName": dataset_name,
                                              "tableName": name}}}
    return [
        table("Sales", [
            _col("Amount", "Double", metadata={"role": "column"}),
            _col("Channel", "String", metadata={"role": "column"}),
            _measure("Total Sales", "SUM(Sales[Amount])"),
            _measure("Buyers", "DISTINCTCOUNT(Sales[CustomerKey])"),
        ]),
        table("Calendar Dates", [_col("Year", "Int64", metadata={"role": "column"}),
                                 _col("Name", "String", metadata={"role": "column"})]),
        table("Store", [_col("Type", "String", metadata={"role": "column"}),
                        _col("Name", "String", metadata={"role": "column"})]),
    ]


RETAIL = _model("ds-retail", "Retail")
DAX = """
// yearly figures by store type
EVALUATE
SUMMARIZECOLUMNS(
    'Calendar Dates'[Year], Store[Type],
    "Total Sales", [Total Sales],
    "Buyers", [Buyers],
    "Sales per Buyer", DIVIDE([Total Sales], [Buyers]),
    "Channel Copy", Sales[Channel]
)
ORDER BY 'Calendar Dates'[Year]
"""
DAX_OUTPUTS = ["Year", "Type", "Total Sales", "Buyers", "Sales per Buyer", "Channel Copy"]


@pytest.mark.parametrize("conn_type", ["powerbi"])
def test_dax_measures_and_columns_resolve_against_the_indexed_model(conn_type):
    found = lineage.trace(DAX, RETAIL, conn_type, DAX_OUTPUTS)
    cols = found.columns
    assert _origin(cols["Total Sales"]) == (KIND_MEASURE, SourceRef("Retail/Sales", "Total Sales"))
    assert _origin(cols["Buyers"]) == (KIND_MEASURE, SourceRef("Retail/Sales", "Buyers"))
    assert _origin(cols["Year"]) == (KIND_COLUMN, SourceRef("Retail/Calendar Dates", "Year"))
    assert _origin(cols["Type"]) == (KIND_COLUMN, SourceRef("Retail/Store", "Type"))
    assert _origin(cols["Channel Copy"]) == (KIND_COLUMN, SourceRef("Retail/Sales", "Channel"))
    assert found.aggregated and cols["Year"].grouping


def test_a_dax_expression_over_measures_is_derived_and_lists_what_it_reads():
    ratio = lineage.trace(DAX, RETAIL, "powerbi", DAX_OUTPUTS).columns["Sales per Buyer"]
    assert ratio.kind == KIND_DERIVED and "DIVIDE" in ratio.expression
    assert set(ratio.reads) == {SourceRef("Retail/Sales", "Total Sales"), SourceRef("Retail/Sales", "Buyers")}


def test_a_measure_the_query_defines_itself_is_derived_not_the_models():
    dax = ('DEFINE MEASURE Sales[Total Sales] = AVERAGE(Sales[Amount]) '
           'EVALUATE SUMMARIZECOLUMNS(Store[Type], "Total Sales", [Total Sales])')
    total = lineage.trace(dax, RETAIL, "powerbi", ["Type", "Total Sales"]).columns["Total Sales"]
    assert total.kind == KIND_DERIVED and "AVERAGE" in total.expression


def test_a_bare_table_evaluate_copies_that_tables_columns_and_no_measures():
    cols = lineage.trace("EVALUATE Sales", RETAIL, "powerbi", ["Amount", "Channel", "Total Sales"]).columns
    assert cols["Amount"].source == SourceRef("Retail/Sales", "Amount")
    assert "Total Sales" not in cols


def test_a_dax_column_name_two_referenced_tables_share_gets_no_lineage():
    dax = "EVALUATE SUMMARIZECOLUMNS('Calendar Dates'[Name], Store[Name], \"Total Sales\", [Total Sales])"
    cols = lineage.trace(dax, RETAIL, "powerbi", ["Calendar Dates[Name]", "Store[Name]", "Total Sales"]).columns
    # Stored qualified because they collide: each still resolves to its own table.
    assert cols["Calendar Dates[Name]"].source == SourceRef("Retail/Calendar Dates", "Name")
    assert cols["Store[Name]"].source == SourceRef("Retail/Store", "Name")
    ambiguous = lineage.trace(dax, RETAIL, "powerbi", ["Name", "Total Sales"]).columns
    assert "Name" not in ambiguous and "Total Sales" in ambiguous


def test_two_models_that_fit_equally_are_not_chosen_between_unless_pinned():
    catalog = RETAIL + _model("ds-copy", "Retail Copy")
    assert lineage.trace(DAX, catalog, "powerbi", DAX_OUTPUTS).columns == {}
    pinned = lineage.trace(DAX, catalog, "powerbi", DAX_OUTPUTS, {"datasetId": "ds-copy"}).columns
    assert pinned["Total Sales"].source == SourceRef("Retail Copy/Sales", "Total Sales")


def test_text_inside_dax_strings_and_comments_is_not_a_reference():
    dax = ('EVALUATE SUMMARIZECOLUMNS(Store[Type], "Label", "see [Total Sales] /* Sales[Amount] */") '
           '-- Sales[Channel]')
    label = lineage.trace(dax, RETAIL, "powerbi", ["Type", "Label"]).columns["Label"]
    assert label.kind == KIND_DERIVED and label.reads == []


# -- field lists ----------------------------------------------------------------

PRIORITY = [{"name": "ORDERS", "columns": [
    _col("ORDNAME", "Edm.String", description="Order Number"),
    _col("CUSTNAME", "Edm.String", description="Customer Number"),
    _col("QPRICE", "Edm.Decimal", description="Price")]}]


def test_odata_selected_fields_map_one_to_one():
    cols = _trace("ORDERS?$select=ORDNAME,QPRICE&$filter=CUSTNAME eq '1011'", ["ORDNAME", "QPRICE"],
                  "priority_erp", PRIORITY)
    assert cols["ORDNAME"].source == SourceRef("ORDERS", "ORDNAME")
    assert cols["QPRICE"].source == SourceRef("ORDERS", "QPRICE")


def test_odata_without_a_select_copies_every_field_of_the_form():
    names = [c["name"] for c in PRIORITY[0]["columns"]]
    cols = _trace("ORDERS", names, "priority_erp", PRIORITY)
    assert set(cols) == set(names)


def test_odata_on_a_form_that_is_not_indexed_yields_nothing():
    assert _trace("INVOICES?$select=IVNUM", ["IVNUM"], "priority_erp", PRIORITY) == {}


def test_a_businessobjects_query_maps_its_objects_to_the_universe():
    catalog = [{"name": "Sales", "metadata_json": {"businessobjects": {"universe_name": "Sales", "universe_id": "42"}},
                "columns": [_col("Country", "String", metadata={"role": "dimension"}),
                            _col("Sales revenue", "Numeric", metadata={"role": "measure"})]}]
    cols = _trace('{"universe": "Sales", "objects": ["Country", "Sales revenue", "Not there"]}',
                  ["Country", "Sales revenue", "Not there"], "businessobjects", catalog)
    assert _origin(cols["Country"]) == (KIND_COLUMN, SourceRef("Sales", "Country"))
    assert _origin(cols["Sales revenue"]) == (KIND_MEASURE, SourceRef("Sales", "Sales revenue"))
    assert "Not there" not in cols


def test_tracing_never_raises_whatever_it_is_given():
    for definition in (None, "", "{", "EVALUATE", "((((", "SELECT", "\x00"):
        for conn_type in ("postgresql", "powerbi", "priority_erp", "businessobjects", None, "mongodb"):
            assert lineage.trace(definition, [{"name": None}, None, {}], conn_type, ["a"]).columns == {}
