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
# Renderers that refuse a backslash value: Athena, Redshift and Timbr, and
# the engines that may read a backslash as an escape (ansi_or_backslash).
NO_BACKSLASH = {"AwsAthenaClient", "AwsRedshiftClient", "TimbrClient", "VerticaClient", "SybaseClient",
                "OracleBIClient"}
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
    if family not in CLIENT_DIALECTS and BACKSLASH in _capture(SCOPED, family, value):
        # Without the engine's own reading, a backslash is outside the
        # allow-list: refused as unverifiable, wherever it stands.
        assert _verdict(SCOPED, family, value) == query_params.UNVERIFIABLE_SYNTAX_MESSAGE
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


# Engines that may read a backslash in a string as an escape, or not (the
# ansi_or_backslash renderer): the identity check reads the capture both
# ways, and a filter counts only when it binds in both readings. Each text
# below binds in one reading only.
EITHER_READING = ["VerticaClient", "SybaseClient", "OracleBIClient"]
BINDS_IN_ONE_READING = {
    "with backslash escapes only":
        "SELECT id FROM orders WHERE owner = 'alice' AND note = '" + BACKSLASH + "' OR 1 = 1 --'",
    "with standard quoting only":
        "SELECT id FROM orders WHERE owner = 'alice' AND note = '" + BACKSLASH + "' AND note = ' OR 1 = 1 --'",
    # The backslash reading must not also take another engine's comment
    # rules (Snowflake's // line comment): these engines read // as code.
    "with // read as a comment only":
        "SELECT id FROM orders WHERE owner = 'alice' AND note = '" + BACKSLASH + "' AND note = ' // 1 OR 1 = 1 --'",
}


_EXEMPT_CLIENT_CLASSES = {
    "OpenSearchClient": ("app.data_sources.clients.opensearch_client", "OpenSearchClient"),
    "CosmosdbClient": ("app.data_sources.clients.cosmosdb_client", "CosmosdbClient"),
    "ElasticsearchClient": ("app.data_sources.clients.elasticsearch_client", "ElasticsearchClient"),
}


def _declared_capture(text, family):
    """A capture as the wrapper records it: tagged with the client's
    declared string-literal style."""
    from app.ai.code_execution.client_profiles import _CLIENT_TABLE
    import importlib

    keys = [key for key, entry in _CLIENT_TABLE.items() if entry.family == family]
    # Exempt clients (no guard, no renderer) are not in the client table.
    (module, name), = keys or [_EXEMPT_CLIENT_CLASSES[family]]
    cls = getattr(importlib.import_module(module), name)
    return CapturedQuery(text, family, literal_style=cls.sql_literal_style)


@pytest.mark.parametrize("reading", list(BINDS_IN_ONE_READING))
@pytest.mark.parametrize("family", EITHER_READING)
def test_a_filter_that_binds_in_one_reading_only_is_refused_where_the_engine_may_read_either(family, reading):
    capture = _declared_capture(BINDS_IN_ONE_READING[reading], family)
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is not None


@pytest.mark.parametrize("family", EITHER_READING)
def test_a_filter_that_binds_in_both_readings_is_accepted_where_the_engine_may_read_either(family):
    capture = _declared_capture("SELECT id FROM orders WHERE owner = 'alice'", family)
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is None


# Whether a block comment ends at the first */ or nests is not pinned for
# the engines read with a declared literal style: a comment opened inside
# another leaves the filter unproven.
COMMENT_INSIDE_A_COMMENT = "SELECT id FROM orders WHERE owner = 'alice' /* /* */ OR 1 = 1 --*/"


@pytest.mark.parametrize("family", EITHER_READING + ["OpenSearchClient", "CosmosdbClient"])
def test_a_comment_inside_a_comment_leaves_the_filter_unproven_where_nesting_is_not_pinned(family):
    capture = _declared_capture(COMMENT_INSIDE_A_COMMENT, family)
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is not None


# A client with neither a guard dialect nor a renderer dialect that declares
# the "doubled" style (OpenSearch) is read where '' and a backslash escape,
# not with Snowflake's other rules ($$ strings, // comments).
def test_a_filter_hidden_only_by_another_engines_string_rule_is_refused_for_a_doubled_style_client():
    capture = _declared_capture("SELECT id FROM orders WHERE owner = 'alice' AND note = $$ OR 1 = 1 OR $$",
                                "OpenSearchClient")
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is not None


@pytest.mark.parametrize("family", ["OpenSearchClient", "CosmosdbClient"])
def test_a_plain_filter_is_accepted_for_a_declared_style_client(family):
    capture = _declared_capture("SELECT id FROM orders WHERE owner = 'alice'", family)
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is None


# Every SQL-reading client without a guard dialect: the identity check reads
# its captures with a reading that is not the engine's own (a renderer's,
# a declared style's, or the generic one). There, any comment opener left
# after strings are emptied leaves the filter unproven.
NO_ENGINE_READING = sorted(
    {family for family in SQL_FAMILIES if family not in CLIENT_DIALECTS} | set(_EXEMPT_CLIENT_CLASSES)
)
NL = chr(10)
COMMENT_SHAPES = {
    "a // running into a block comment":
        "SELECT id FROM orders WHERE owner = 'alice' AND 1 = 4 //* " + NL + "OR 1 = 1 --*/ 2",
    "a //": "SELECT id FROM orders WHERE owner = 'alice' AND 1 = 4 // 2" + NL + "OR 1 = 1",
    "a comment inside a comment": COMMENT_INSIDE_A_COMMENT,
    "a -- comment": "SELECT id FROM orders WHERE owner = 'alice' -- a note",
    "a # comment": "SELECT id FROM orders WHERE owner = 'alice' # a note",
    "a block comment": "SELECT id FROM orders WHERE owner = 'alice' /* a note */",
}


def test_every_client_without_a_guard_dialect_is_covered():
    assert {"VerticaClient", "SybaseClient", "OracleBIClient", "NetsuiteClient", "PostHogClient",
            "SalesforceClient", "ElasticsearchClient", "OpenSearchClient", "CosmosdbClient"} <= set(NO_ENGINE_READING)
    assert not set(NO_ENGINE_READING) & set(CLIENT_DIALECTS)


@pytest.mark.parametrize("shape", list(COMMENT_SHAPES))
@pytest.mark.parametrize("family", NO_ENGINE_READING)
def test_a_comment_leaves_the_filter_unproven_where_the_reading_is_not_the_engines_own(family, shape):
    capture = _declared_capture(COMMENT_SHAPES[shape], family)
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is not None


@pytest.mark.parametrize("family", NO_ENGINE_READING)
def test_a_plain_filter_is_still_accepted_where_the_reading_is_not_the_engines_own(family):
    for text in ("SELECT id FROM orders WHERE owner = 'alice'",
                 "SELECT id FROM orders WHERE owner = 'alice' AND note = 'a -- b /* c */ # d // e % f'"):
        capture = _declared_capture(text, family)
        assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is None, text


def test_a_percent_sign_leaves_the_filter_unproven_where_the_engine_may_read_it_as_a_comment():
    # SQL Anywhere with percent_as_comment on.
    capture = _declared_capture(
        "SELECT id FROM orders WHERE owner = 'alice' AND 2 = 2 % (2" + NL + "OR 1 = 1 % 2)", "SybaseClient")
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is not None


def test_a_percent_sign_in_a_string_still_binds_where_the_engine_may_read_it_as_a_comment():
    capture = _declared_capture("SELECT id FROM orders WHERE owner = 'alice' AND note LIKE '50%'", "SybaseClient")
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is None


# Text one of the two readings cannot parse: not proven in either.
UNREADABLE_IN_A_READING = [
    "SELECT id FROM orders WHERE owner = 'alice' AND note = E'x' OR 1 = 1",
    "SELECT id FROM orders WHERE owner = 'alice' AND note = E'" + BACKSLASH + "' OR id = 'x'",
]


@pytest.mark.parametrize("text", UNREADABLE_IN_A_READING)
@pytest.mark.parametrize("family", EITHER_READING)
def test_text_a_reading_cannot_parse_leaves_the_filter_unproven_where_the_engine_may_read_either(family, text):
    capture = _declared_capture(text, family)
    assert verify_identity_binds_in_queries([capture], {"v": "alice"}, SPEC) is not None


@pytest.mark.parametrize("family", EITHER_READING)
def test_a_plain_vertica_style_filter_is_still_accepted_where_the_engine_may_read_either(family):
    for text in ("SELECT id FROM orders WHERE owner = 'alice' AND amount > 10",
                 "SELECT * FROM SALES.ORDERS WHERE OWNER = 'alice' FETCH FIRST 10 ROWS ONLY"):
        assert verify_identity_binds_in_queries([_declared_capture(text, family)], {"v": "alice"}, SPEC) is None


def test_the_fallback_log_names_the_generic_reading_for_soql(caplog):
    import logging

    from app.ai.code_execution import identity_bind

    log = logging.getLogger(identity_bind.__name__)
    was_disabled, log.disabled = log.disabled, False  # alembic disables existing loggers
    try:
        with caplog.at_level(logging.WARNING, logger=identity_bind.__name__):
            identity_bind.log_fallback("SELECT Id FROM Account", "SalesforceClient", "v", "backslash")
    finally:
        log.disabled = was_disabled
    readings = [r.reading for r in caplog.records if getattr(r, "event", None) == "identity_bind_check_fallback"]
    assert readings == ["generic"]


# Where the reading is not the engine's own, a filter counts only when the
# text is plain enough that every reasonable reading agrees on it: an
# allow-list of characters and string forms. Anything else is refused with
# a fixed message saying the syntax cannot be verified on this connection.
UNVERIFIABLE = ("a query uses syntax the viewer filter check cannot verify on this connection type; "
                "refusing to serve results that may be unscoped")
PLAIN = "SELECT a, b FROM t WHERE owner = 'v' AND x > 1 ORDER BY a"
PLAIN_FILTERS = [
    PLAIN,
    'SELECT "Name", b FROM t WHERE owner = ' + "'v'",
    "SELECT t.a, u.b FROM t JOIN u ON t.id = u.id WHERE t.owner = 'v' AND u.x <> 2",
]
# The shapes the reviews and hunts found, each for the clients it was found on.
UNVERIFIABLE_SHAPES = {
    "a bracketed name": (
        "SELECT id FROM orders WHERE owner = 'v' AND 1 = [a--] OR 1 = 1",
        ["SybaseClient"]),
    "a quote in a bracketed name": (
        "SELECT id FROM orders WHERE owner = 'v' AND 1 = ['] OR 1 = 1 OR x = [']",
        ["SybaseClient", "SisenseClient", "OracleBIClient"]),
    "a quote in a backtick name": (
        "SELECT id FROM orders WHERE owner = 'v' AND 1 = `'` OR 1 = 1 OR x = `'`",
        ["OpenSearchClient", "PinotClient"]),
    "WITH UR": ("SELECT id FROM orders WHERE owner = 'v' OR 1 = 1 WITH UR", ["Db2Client", "IbmiClient"]),
    "OPTIMIZE FOR": ("SELECT id FROM orders WHERE owner = 'v' OR 1 = 1 OPTIMIZE FOR 10 ROWS",
                     ["Db2Client", "IbmiClient"]),
    "FOR READ ONLY": ("SELECT id FROM orders WHERE owner = 'v' OR 1 = 1 FOR READ ONLY", ["Db2Client", "IbmiClient"]),
    "WITH HINT": ("SELECT id FROM orders WHERE owner = 'v' OR 1 = 1 WITH HINT(NO_CS_JOIN)", ["SapHanaClient"]),
    "an E string": (
        "SELECT id FROM orders WHERE owner = 'v' AND note = E'" + BACKSLASH + "'' OR 1 = 1 OR note = '",
        ["VerticaClient"]),
    "a backslash in a quoted name": (
        'SELECT id FROM orders WHERE owner = ' + "'v'" + ' AND "e' + BACKSLASH + '"" = 1',
        NO_ENGINE_READING),
    "a doubled quote in a quoted name": (
        'SELECT id FROM orders WHERE owner = ' + "'v'" + ' AND "a""b" = 1', NO_ENGINE_READING),
    "a percent sign": ("SELECT id FROM orders WHERE owner = 'v' AND 2 = 2 % (2" + NL + "OR 1 = 1 % 2)",
                       ["SybaseClient"]),
    **{"earlier: " + name: (text.replace("'alice'", "'v'"), NO_ENGINE_READING)
       for name, text in COMMENT_SHAPES.items()},
}
SHAPE_CASES = [(name, family) for name, (_, families) in UNVERIFIABLE_SHAPES.items() for family in families]


def _refusal(text, family, value="v"):
    capture = _declared_capture(text, family) if family is not None else text
    return verify_identity_binds_in_queries([capture], {"v": value}, SPEC)


@pytest.mark.parametrize("name, family", SHAPE_CASES)
def test_a_shape_found_by_review_is_refused_as_unverifiable_where_the_reading_is_not_the_engines_own(name, family):
    assert _refusal(UNVERIFIABLE_SHAPES[name][0], family) == UNVERIFIABLE


@pytest.mark.parametrize("text", PLAIN_FILTERS)
@pytest.mark.parametrize("family", NO_ENGINE_READING + [None])
def test_a_plain_filter_is_accepted_on_every_client_without_the_engines_reading(family, text):
    assert _refusal(text, family) is None


# Each character or sequence outside the allow-list, and the error kind it is
# logged with. A quote opens a string left unterminated.
REFUSED_OUTSIDE_A_STRING = {
    **{c: "char" for c in "#$&:;?@[]^`{}~" + '"'},
    BACKSLASH: "backslash",
    "'": "unparseable",
    chr(0): "char", chr(13): "char", chr(11): "char", chr(12): "char", chr(127): "char", chr(0x85): "char",
    chr(0xE9): "char", chr(0x5D0): "char",
    "--": "sequence", "/*": "sequence", "*/": "sequence", "//": "sequence",
}
_STRING_START, _STRING_END = PLAIN.index("'"), PLAIN.rindex("'")
OUTSIDE_POSITIONS = [i for i in range(len(PLAIN) + 1) if not _STRING_START < i <= _STRING_END]
# Clients with no SQL renderer (and untagged captures) may also send text
# that is not SQL, which is read as before: for them the gate applies once
# the text starts as a query.
NOT_ONLY_SQL = {"ElasticsearchClient", "OpenSearchClient", "CosmosdbClient", None}


@pytest.mark.parametrize("family", NO_ENGINE_READING + [None])
def test_any_refused_character_or_sequence_outside_a_string_flips_a_plain_filter_to_refused(family):
    assert _refusal(PLAIN, family) is None
    positions = [i for i in OUTSIDE_POSITIONS if family not in NOT_ONLY_SQL or i > len("SELECT")]
    for refused in list(REFUSED_OUTSIDE_A_STRING) + (["%"] if family == "SybaseClient" else []):
        for i in positions:
            text = PLAIN[:i] + refused + PLAIN[i:]
            assert _refusal(text, family) == UNVERIFIABLE, (refused, i)


@pytest.mark.parametrize("family", NO_ENGINE_READING + [None])
def test_inside_a_string_anything_but_a_backslash_or_a_control_character_is_allowed(family):
    for inside in ("a -- b /* c */ # d // e % f ; g [h] `i` $j", "".join(chr(c) for c in (0x5D3, 0x5E0)), "it''s",
                   'say "hi"', "tab" + chr(9) + "lf" + NL):
        assert _refusal("SELECT a FROM t WHERE owner = 'v' AND note = '" + inside + "'", family) is None, inside
    for inside in (BACKSLASH, chr(0), chr(13), chr(127)):
        assert _refusal("SELECT a FROM t WHERE owner = 'v' AND note = 'x" + inside + "'", family) == UNVERIFIABLE


def _identity_records(run):
    """`run()`, with the records the identity check logs at WARNING."""
    import logging

    from app.ai.code_execution import identity_bind

    records = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append
    log = logging.getLogger(identity_bind.__name__)
    was_disabled, was_level = log.disabled, log.level
    log.disabled = False  # alembic disables existing loggers
    log.setLevel(logging.WARNING)
    log.addHandler(handler)
    try:
        result = run()
    finally:
        log.removeHandler(handler)
        log.disabled = was_disabled
        log.setLevel(was_level)
    return result, records


@pytest.mark.parametrize("text, family, kind", [
    ("SELECT a FROM t WHERE owner = 'alice@corp.io' AND x = 1;", "SybaseClient", "char"),
    ("SELECT a FROM t WHERE owner = 'alice@corp.io' -- note", "PostHogClient", "sequence"),
    ("SELECT a FROM t WHERE owner = 'alice@corp.io' AND note = 'x" + BACKSLASH + "'", "OracleBIClient",
     "backslash"),
    ("SELECT a FROM t WHERE owner = 'alice@corp.io' AND 2 = 5 % 2", "SybaseClient", "percent"),
    ("SELECT a FROM t WHERE owner = 'alice@corp.io' OR 1 = 1 WITH UR", "Db2Client", "unparseable"),
    ("SELECT a FROM t WHERE owner = 'alice@corp.io' AND note = 'open", None, "unparseable"),
])
def test_an_unverifiable_refusal_is_logged_by_structure_and_told_with_the_fixed_message(text, family, kind):
    value = "alice@corp.io"
    result, records = _identity_records(lambda: _refusal(text, family, value))
    assert result == UNVERIFIABLE
    assert value not in result and "SELECT" not in result
    logged = [r for r in records if getattr(r, "event", None) == "identity_bind_check_unverifiable"]
    assert len(logged) == 1
    record = logged[0]
    assert (record.client_type, record.param, record.reason, record.query_length) == \
        (family or "unknown", "v", kind, len(text))
    everything = record.getMessage() + " " + " ".join(str(v) for v in vars(record).values())
    assert value not in everything and "SELECT" not in everything and "alice" not in everything
