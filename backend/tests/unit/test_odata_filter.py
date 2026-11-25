"""The local OData filter/sort used by the on-prem SharePoint lists connector.

Rows are plain dicts; columns resolve through a table like the connector's
catalog (display names, some with spaces or a leading digit).
"""
import pytest

from app.data_sources.clients._odata_filter import (
    Column, ODataFilterError, compile_filter, parse_orderby, sort_rows,
)

COLUMNS = {
    "Title": Column("Title", "string", "Title"),
    "Status": Column("Status", "string", "Status"),
    "Amount": Column("field_2", "float", "Amount"),
    "2017": Column("_x0032_017", "float", "2017"),
    "Total 2017": Column("Total", "float", "Total 2017"),
    "Due": Column("Due", "datetime", "Due"),
    "Done": Column("Done", "boolean", "Done"),
    "Tags": Column("Tags", "string", "Tags"),
    "Owner": Column("Owner", "string", "Owner"),
    "Year": Column("YearCol", "integer", "Year"),
}


def resolve(name):
    for key, col in COLUMNS.items():
        if key.casefold() == name.casefold():
            return col
    return None


ROWS = [
    {"Title": "Flight to Berlin", "Status": "Open", "field_2": 420.0, "_x0032_017": 12.0, "Total": 5.0,
     "Due": "2026-03-02T08:00:00Z", "Done": True, "Tags": ["Q1", "Conference"], "Owner": "Dana", "YearCol": 2026},
    {"Title": "O'Brien taxi", "Status": "closed", "field_2": 38.25, "_x0032_017": None, "Total": 20.0,
     "Due": "2026-04-20T22:15:00Z", "Done": False, "Tags": ["Q2"], "Owner": None, "YearCol": 2025},
    {"Title": "Laptop", "Status": "In Progress", "field_2": 1899.0, "_x0032_017": 4.0, "Total": None,
     "Due": None, "Done": None, "Tags": [], "Owner": "Noa", "YearCol": 2026},
]


def run(expr):
    flt = compile_filter(expr, list(COLUMNS), resolve)
    return [r["Title"] for r in ROWS if flt.matches(r)]


@pytest.mark.parametrize("expr,expected", [
    ("Status eq 'open'", ["Flight to Berlin"]),                       # text is case-insensitive
    ("Status ne 'Open'", ["O'Brien taxi", "Laptop"]),
    ("Amount gt 100 and Amount lt 1000", ["Flight to Berlin"]),
    ("Amount ge 38.25", ["Flight to Berlin", "O'Brien taxi", "Laptop"]),
    ("not (Amount le 420)", ["Laptop"]),
    ("Status eq 'Open' or Amount gt 1000", ["Flight to Berlin", "Laptop"]),
    ("2017 gt 5", ["Flight to Berlin"]),                               # digit-leading column name
    ("Total 2017 ge 5", ["Flight to Berlin", "O'Brien taxi"]),         # name with a space
    ("fields/Amount lt 100", ["O'Brien taxi"]),                        # Graph-style prefix
    ("Title eq 'O''Brien taxi'", ["O'Brien taxi"]),                    # escaped quote
    ("startswith(Title,'flight')", ["Flight to Berlin"]),
    ("endswith(Title, 'TAXI')", ["O'Brien taxi"]),
    ("contains(Title,'ap')", ["Laptop"]),
    ("substringof('ap', Title)", ["Laptop"]),                          # SharePoint argument order
    ("tolower(Status) eq 'in progress'", ["Laptop"]),
    ("Due ge '2026-04-01T00:00:00Z'", ["O'Brien taxi"]),
    ("Due lt datetime'2026-04-01T00:00:00Z'", ["Flight to Berlin"]),
    ("Due ge '2026-04-20'", ["O'Brien taxi"]),                         # date without time = midnight UTC
    ("year(Due) eq 2026 and month(Due) eq 3", ["Flight to Berlin"]),
    ("Due eq null", ["Laptop"]),
    ("Owner ne null", ["Flight to Berlin", "Laptop"]),
    ("Done eq true", ["Flight to Berlin"]),
    ("Done", ["Flight to Berlin"]),                                    # a bare yes/no column
    ("Done eq false", ["O'Brien taxi"]),
    ("Tags eq 'q1'", ["Flight to Berlin"]),                            # multi-value: any matches
    ("Tags ne 'Q1'", ["O'Brien taxi", "Laptop"]),                      # multi-value: none matches
    ("contains(Tags,'conf')", ["Flight to Berlin"]),
    ("Year eq 2025", ["O'Brien taxi"]),                                # column named like a function
    ("year(Due) eq Year", ["Flight to Berlin"]),
])
def test_filter_semantics(expr, expected):
    assert run(expr) == expected


def test_an_empty_value_never_satisfies_an_ordering_comparison():
    # 2017 is empty on one row: neither "gt" nor "le" may match it.
    gt, le = run("2017 gt 0"), run("2017 le 100")
    assert "O'Brien taxi" not in gt and "O'Brien taxi" not in le


@pytest.mark.parametrize("expr", [
    "Nope eq 1", "Status eq", "Status eq 'x' and", "(Status eq 'x'", "Status eq 'unterminated",
    "Due gt 'not a date'", "Amount gt 'many'", "Status = 'x'", "'just a value'", "",
    "startswith(Title)", "Status eq 'x' Amount gt 1",
])
def test_invalid_filters_raise_a_filter_error(expr):
    with pytest.raises(ODataFilterError):
        run(expr)


def test_filter_reports_the_row_keys_it_reads():
    flt = compile_filter("Amount gt 1 and startswith(Title,'x') or 2017 eq null", list(COLUMNS), resolve)
    assert set(flt.keys) == {"field_2", "Title", "_x0032_017"}


def test_filter_never_executes_code():
    with pytest.raises(ODataFilterError):
        run("__import__('os').system('echo hacked') eq 1")


def test_orderby_sorts_case_insensitively_with_empty_values_last():
    rows = [{"t": "b", "n": 2}, {"t": "A", "n": None}, {"t": "c", "n": 1}, {"t": None, "n": 3}]
    cols = {"T": Column("t", "string", "T"), "N": Column("n", "float", "N")}
    find = lambda n: cols.get(n.upper())
    assert [r["t"] for r in sort_rows(rows, parse_orderby("T", list(cols), find))] == ["A", "b", "c", None]
    assert [r["t"] for r in sort_rows(rows, parse_orderby("T desc", list(cols), find))] == ["c", "b", "A", None]
    assert [r["n"] for r in sort_rows(rows, parse_orderby("N desc", list(cols), find))] == [3, 2, 1, None]


def test_orderby_on_dates_is_chronological_and_multi_key_is_stable():
    rows = [
        {"d": "2026-03-01T00:00:00Z", "g": "x"},
        {"d": "2026-01-15T00:00:00+02:00", "g": "y"},
        {"d": "2026-03-01T00:00:00Z", "g": "a"},
    ]
    cols = {"D": Column("d", "datetime", "D"), "G": Column("g", "string", "G")}
    find = lambda n: cols.get(n.upper())
    ordered = sort_rows(rows, parse_orderby("D desc, G", list(cols), find))
    assert [r["g"] for r in ordered] == ["a", "x", "y"]


def test_orderby_rejects_unknown_columns():
    with pytest.raises(ODataFilterError):
        parse_orderby("Missing desc", ["T"], lambda n: None)
