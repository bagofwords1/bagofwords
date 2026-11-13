"""An identity filter written by any renderer is read back as that identity.

Contract: when a viewer's identity value is rendered into a scoping query by
the connection's own renderer (whatever it escapes: quotes, backslashes,
double quotes), `verify_identity_binds_in_queries` accepts the query. For the
grammars it reads structurally (SQL, SOQL, HogQL, SPL) it still refuses the
same value used outside a filter, and a filter on a different identity whose
text happens to equal the value's escaped form. Connections it reads as text
(KQL) accept the value anywhere, as before.
"""
from __future__ import annotations

import pytest

from app.ai.code_execution import query_params
from app.ai.code_execution.identity_bind import CapturedQuery
from app.ai.code_execution.query_params import ParamError, render_sql_with_params, verify_identity_binds_in_queries
from app.ai.code_execution.sql_guard import CLIENT_DIALECTS
from app.schemas.param_schema import ParamSpec
from tests.unit.test_query_params import _odata_client, _run_capturing, mode, odata_http  # noqa: F401  (fixtures)

BACKSLASH = chr(92)
IDENTITIES = ["it's", "DOMAIN" + BACKSLASH + "user", 'a"b', "x" + BACKSLASH]
SPEC = [ParamSpec.model_validate({"name": "v", "type": "string", "source": "identity"})]

SQL_FAMILIES = sorted(set(CLIENT_DIALECTS) | {
    "Db2Client", "IbmiClient", "VerticaClient", "SybaseClient", "SapHanaClient", "PinotClient", "TimbrClient",
    "NetsuiteClient", "OracleBIClient", "SisenseClient", "CSVClient", "QVDClient", "PBIXClient",
    "PowerBIReportServerClient", "FastQueryClient", "PostHogClient", "SalesforceClient",
})
NO_BACKSLASH = {"AwsAthenaClient", "AwsRedshiftClient", "TimbrClient"}
# Timbr is read as Spark SQL, where the doubled quote its renderer writes is
# two strings side by side: a value holding a quote reads as another value,
# so the identity check refuses the query (fails closed).
QUOTE_READ_AS_TWO_STRINGS = {"TimbrClient"}

SCOPED = "SELECT id FROM orders WHERE owner = :v"
SELECT_LIST_ONLY = "SELECT :v AS owner, id FROM orders"
SPL_SCOPED = "search index=x owner=:v"
SPL_BARE_TERM = "search index=x :v"
KQL_SCOPED = "T | where owner == :v"
KQL_ANYWHERE = "T | project c = :v"


def _capture(template, family, value):
    text = render_sql_with_params(template, {"v": value}, renderer=query_params.renderer_for_name(family))
    return CapturedQuery(text, family)


def _verdict(template, family, value):
    return verify_identity_binds_in_queries([_capture(template, family, value)], {"v": value}, SPEC)


def _escaped_twice(family, value):
    """The value's literal body, as a value of its own: a different identity
    whose text equals how the renderer wrote the first one."""
    literal = query_params.renderer_for_name(family).quote(value)
    return literal[literal.index(literal[-1]) + 1:-1]


@pytest.mark.parametrize("value", IDENTITIES)
@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_a_sql_filter_on_the_identity_binds_and_one_outside_a_filter_does_not(family, value):
    if family in NO_BACKSLASH and BACKSLASH in value:
        with pytest.raises(ParamError):
            _capture(SCOPED, family, value)
        return
    if family in QUOTE_READ_AS_TWO_STRINGS and "'" in value:
        assert _verdict(SCOPED, family, value) is not None
        return
    assert _verdict(SCOPED, family, value) is None
    assert _verdict(SELECT_LIST_ONLY, family, value) is not None


@pytest.mark.parametrize("value", IDENTITIES)
@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_a_sql_filter_on_the_identity_escaped_twice_does_not_bind(family, value):
    if family in NO_BACKSLASH and BACKSLASH in value:
        return  # refused at render, above
    other = _escaped_twice(family, value)
    if other == value:
        return  # this renderer writes the value as it is
    query = render_sql_with_params(SCOPED, {"v": other}, renderer=query_params.renderer_for_name(family))
    assert verify_identity_binds_in_queries([CapturedQuery(query, family)], {"v": value}, SPEC) is not None


@pytest.mark.parametrize("value", IDENTITIES)
def test_a_splunk_field_filter_on_the_identity_binds_and_a_bare_term_does_not(value):
    assert _verdict(SPL_SCOPED, "SplunkClient", value) is None
    assert _verdict(SPL_BARE_TERM, "SplunkClient", value) is not None
    other = _escaped_twice("SplunkClient", value)
    if other != value:
        query = render_sql_with_params(SPL_SCOPED, {"v": other}, renderer=query_params.renderer_for_name("SplunkClient"))
        assert verify_identity_binds_in_queries([CapturedQuery(query, "SplunkClient")], {"v": value}, SPEC) is not None


def test_a_splunk_field_filter_on_a_negative_identity_number_binds():
    # SPL reads `owner=-5` as one field pair; a space after '=' would not be.
    assert render_sql_with_params(SPL_SCOPED, {"v": -5}, renderer=query_params.renderer_for_name("SplunkClient")) == \
        "search index=x owner=-5"
    assert _verdict(SPL_SCOPED, "SplunkClient", -5) is None


# The OData clients are checked on the requests they send: the real clients
# run, with only the transport under their sessions stubbed.
ODATA_IDENTITIES = IDENTITIES + [
    "alice@example.com", "a&b", "100%", "a+b", "O'Brien", "".join(chr(c) for c in (0x5D3, 0x5E0, 0x5D4)),
]
ODATA_PARAM_CALLS = {
    "PriorityErpClient": "execute_query('CUSTOMERS?$filter=Owner eq :v&$top=5', params={{'v': {v!r}}})",
    "MecmClient": "execute_query('SMS_R_System?$filter=Owner eq :v&$top=5', params={{'v': {v!r}}})",
    "SapDatasphereClient": "execute_query('$filter=Owner eq :v&$top=5', 'S/M', params={{'v': {v!r}}})",
}


@pytest.mark.parametrize("value", ODATA_IDENTITIES)
@pytest.mark.parametrize("family", list(ODATA_PARAM_CALLS))
def test_an_odata_filter_binds_the_identity_it_was_rendered_with_and_not_another(mode, odata_http, family, value):
    client = _odata_client(family)
    queries, _, error = _run_capturing(client, "return ds_clients['c']." + ODATA_PARAM_CALLS[family].format(v=value))
    assert error is None and len(odata_http) == 1
    assert verify_identity_binds_in_queries(queries, {"v": value}, SPEC) is None
    queries, _, error = _run_capturing(client, "return ds_clients['c']." + ODATA_PARAM_CALLS[family].format(v="someone@else"))
    assert error is None and len(odata_http) == 2
    assert verify_identity_binds_in_queries(queries, {"v": value}, SPEC) is not None


@pytest.mark.parametrize("value", IDENTITIES)
def test_kql_is_read_as_text_and_accepts_the_value_anywhere(value):
    assert _verdict(KQL_SCOPED, "AzureDataExplorerClient", value) is None
    assert _verdict(KQL_ANYWHERE, "AzureDataExplorerClient", value) is None
    assert verify_identity_binds_in_queries(
        [CapturedQuery("T | where owner == 'someone else'", "AzureDataExplorerClient")], {"v": value}, SPEC,
    ) is not None
