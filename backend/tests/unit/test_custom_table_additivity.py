"""Which expressions may be re-aggregated by summing.

The promise (`app/data_sources/fast/additivity.py`): SUM and COUNT add up;
distinct counts, averages, ratios, MIN/MAX and time intelligence do not; and
what the classifier cannot read is `unknown`, never a guess in either
direction. A wrong "additive" is the failure this exists to prevent.
"""
import pytest
import sqlglot

from app.data_sources.fast import additivity
from app.data_sources.fast.additivity import (
    AVERAGE, DISTINCT, MIN_MAX, RATIO, STATISTIC, TIME, TOTAL, UNKNOWN,
)


def _sql(expression, dialect="postgres"):
    return additivity.of_sql(sqlglot.parse_one(f"SELECT {expression} AS x FROM t", read=dialect).expressions[0].this)


@pytest.mark.parametrize("expression", [
    "SUM(amount)", "COUNT(*)", "COUNT(order_id)", "SUM(amount) - SUM(refund)", "SUM(a) + SUM(b)",
    "SUM(price * quantity)", "2 * SUM(amount)", "COALESCE(SUM(amount), 0)", "ROUND(SUM(amount), 2)",
    "CAST(SUM(amount) AS DECIMAL(18, 2))", "price * quantity", "(SUM(amount))",
])
def test_sums_and_counts_are_additive(expression):
    assert _sql(expression).additive is True


@pytest.mark.parametrize("expression, reason", [
    ("COUNT(DISTINCT customer_id)", DISTINCT),
    ("APPROX_DISTINCT(customer_id)", DISTINCT),
    ("AVG(amount)", AVERAGE),
    ("SUM(amount) / COUNT(*)", RATIO),
    ("SUM(amount) / NULLIF(SUM(quantity), 0)", RATIO),
    ("100.0 * SUM(returned) / SUM(sold)", RATIO),
    ("amount / quantity", RATIO),
    ("MIN(amount)", MIN_MAX),
    ("MAX(placed_at)", MIN_MAX),
    ("STDDEV(amount)", STATISTIC),
    ("SUM(amount) - AVG(amount)", AVERAGE),
    ("ROUND(COUNT(DISTINCT customer_id) * 1.0, 0)", DISTINCT),
])
def test_what_does_not_add_up_is_not_additive_and_says_why(expression, reason):
    verdict = _sql(expression)
    assert verdict.additive is False and verdict.reason == reason


@pytest.mark.parametrize("expression", [
    "SUM(amount) OVER (PARTITION BY region)", "CASE WHEN SUM(a) > 0 THEN SUM(a) ELSE SUM(b) END",
    "my_udf(amount)", "SUM(a) * SUM(b)", "COALESCE(SUM(a), SUM(b))",
])
def test_what_the_classifier_cannot_read_is_unknown_not_additive(expression):
    assert _sql(expression).additive == UNKNOWN


def test_an_unparsed_expression_is_unknown():
    assert additivity.of_sql(None).additive == UNKNOWN


@pytest.mark.parametrize("dialect, expression", [
    ("tsql", "COUNT_BIG(DISTINCT customer_id)"), ("bigquery", "SAFE_DIVIDE(SUM(a), SUM(b))"),
    ("snowflake", "DIV0(SUM(a), SUM(b))"), ("clickhouse", "uniqExact(customer_id)"),
    ("mysql", "COUNT(DISTINCT a, b)"), ("oracle", "MEDIAN(amount)"),
])
def test_dialect_spellings_of_non_additive_functions_are_caught(dialect, expression):
    assert _sql(expression, dialect).additive is not True


@pytest.mark.parametrize("dtype, numeric", [
    ("BIGINT", True), ("DECIMAL(12,2)", True), ("DOUBLE", True), ("integer", True), ("numeric", True),
    ("Edm.Decimal", True), ("Int64", True), ("VARCHAR", False), ("TIMESTAMP", False), ("DATE", False),
    ("BOOLEAN", False), ("INTERVAL", False), (None, False),
])
def test_a_plain_column_is_additive_only_when_numeric(dtype, numeric):
    assert additivity.of_plain_column(dtype).additive is numeric


# -- DAX ----------------------------------------------------------------------

MODEL = {
    "total sales": "SUM(Sales[Amount])",
    "returns": 'CALCULATE(SUM(Sales[Amount]), Sales[Kind] = "Return")',
    "orders": "COUNTROWS(Sales)",
    "buyers": "DISTINCTCOUNT(Sales[CustomerKey])",
    "unreadable": "",
}


def _dax(expression):
    return additivity.of_dax(expression, lambda name: MODEL.get(name.lower()))


@pytest.mark.parametrize("expression", [
    "SUM(Sales[Amount])", "SUMX(Sales, Sales[Price] * Sales[Quantity])", "COUNTROWS(Sales)",
    "COUNT(Sales[OrderId])", 'CALCULATE(SUM(Sales[Amount]), Sales[Channel] = "Web")',
    "[Total Sales] - [Returns]", "[Total Sales]", "2 * [Total Sales]", "( SUM(Sales[Amount]) )",
    "SUM(Sales[Amount]) + SUM(Sales[Tax])  // gross", "COALESCE(SUM(Sales[Amount]), 0)",
])
def test_dax_sums_and_counts_are_additive(expression):
    assert _dax(expression).additive is True


@pytest.mark.parametrize("expression, reason", [
    ("DISTINCTCOUNT(Sales[CustomerKey])", DISTINCT),
    ("COUNTROWS(DISTINCT(Sales[CustomerKey]))", DISTINCT),
    ("COUNTROWS(VALUES(Sales[CustomerKey]))", DISTINCT),
    ("AVERAGE(Sales[Amount])", AVERAGE),
    ("AVERAGEX(Sales, Sales[Price] * Sales[Quantity])", AVERAGE),
    ("DIVIDE([Total Sales], [Orders])", RATIO),
    ("SUM(Sales[Amount]) / COUNTROWS(Sales)", RATIO),
    ("TOTALYTD([Total Sales], 'Date'[Date])", TIME),
    ("CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))", TIME),
    ("CALCULATE([Total Sales], DATEADD('Date'[Date], -1, MONTH))", TIME),
    ("MAX(Sales[Amount])", MIN_MAX),
    ("MIN('Date'[Date])", MIN_MAX),
    ("CALCULATE([Total Sales], ALL(Store))", TOTAL),
    ("CALCULATE([Total Sales], REMOVEFILTERS())", TOTAL),
    ("[Buyers]", DISTINCT),
    ("[Total Sales] - [Buyers]", DISTINCT),
])
def test_dax_that_does_not_add_up_is_not_additive_and_says_why(expression, reason):
    verdict = _dax(expression)
    assert verdict.additive is False and verdict.reason == reason


@pytest.mark.parametrize("expression", [
    "", "   ", "[Unreadable]", "[Not In The Model]", "IF([Total Sales] > 0, [Total Sales], [Returns])",
    "VAR x = SUM(Sales[Amount]) RETURN x", "[Total Sales] * [Orders]", "SWITCH(TRUE(), 1, 2)",
])
def test_dax_the_classifier_cannot_read_is_unknown(expression):
    assert _dax(expression).additive == UNKNOWN


def test_a_measure_reference_is_unknown_without_the_model():
    assert additivity.of_dax("[Total Sales]").additive == UNKNOWN


def test_function_names_inside_dax_strings_and_comments_do_not_count():
    assert _dax('CALCULATE(SUM(Sales[Amount]), Sales[Note] = "DIVIDE / AVERAGE") -- DISTINCTCOUNT').additive is True


def test_measures_that_reference_each_other_in_a_loop_end_as_unknown():
    loop = {"a": "[B]", "b": "[A]"}
    assert additivity.of_dax("[A]", lambda name: loop.get(name.lower())).additive == UNKNOWN
