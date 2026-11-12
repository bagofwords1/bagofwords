"""Read-only guard for SQL that reaches a data source.

Every query an agent runs, and every custom-table definition an admin saves, is
checked here on the *final* text — after the sandbox code built it and after
`params` were rendered — so string tricks in generated Python (`"DEL" + "ETE"`,
`bytes.fromhex(...)`, `" ".join(...)`) are no way around it. The AST scan of the
sandbox code still runs first; this is the check that sees what actually runs.

Policy, per client:

  * **A client whose SQL dialect sqlglot knows** (Postgres, MySQL, SQL Server,
    Snowflake, BigQuery, ...): parse it and allow exactly one read statement —
    SELECT / set operation / WITH ... SELECT, plus SHOW / DESCRIBE / EXPLAIN.
    Anything that writes anywhere in the tree (a DELETE inside a CTE,
    `SELECT ... INTO`) is refused. If sqlglot cannot parse the text, we do not
    block a possibly-valid read on a parser gap; we fall back to the keyword
    blocklist over the text with string literals and comments removed.
  * **Splunk**: SPL is not SQL but can write; its writing commands are refused.
  * **Db2** (LUW, z/OS, IBM i): sqlglot has no Db2 dialect. One statement, a
    read verb (SELECT / WITH / VALUES) and no write keyword, generic or Db2's.
  * **Everything else** (Salesforce SOQL, PostHog HogQL, Mongo, Qlik, ...): not
    checked here. Their query APIs are read-only, and connections are expected
    to use read-only credentials regardless.

This is defence in depth, not a substitute for read-only database accounts:
a SELECT can still call a function with side effects.
"""

import logging
import re
from typing import Optional, Union

from app.ai.code_execution.client_profiles import _CLIENT_TABLE, guard_profile

logger = logging.getLogger(__name__)
# sqlglot warns on every statement it keeps as an opaque Command (SHOW, EXPLAIN).
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class ReadOnlyViolation(ValueError):
    """The statement would modify the source."""


# Client family name -> sqlglot dialect, for consumers that only hold a name
# (a captured query's client tag). Guard decisions look clients up by type
# instead (`client_profiles.guard_profile`); this is derived from that table.
CLIENT_DIALECTS = {
    entry.family: entry.profile.dialect
    for entry in _CLIENT_TABLE.values()
    if entry.profile is not None and entry.profile.kind == "sql"
}

# Statements sqlglot leaves as an opaque Command that are still reads.
_READ_COMMANDS = {"SHOW", "DESCRIBE", "DESC", "EXPLAIN", "PRAGMA"}

_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|EXEC|EXECUTE"
    r"|MERGE|CALL|REPLACE\s+INTO|UPSERT|LOAD|COPY|VACUUM|ATTACH|DETACH|INTO\s+OUTFILE"
    r"|INTO\s+DUMPFILE|SET)\b",
    re.IGNORECASE,
)
_LITERALS_AND_COMMENTS = re.compile(
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/",
    re.DOTALL,
)

# SPL commands an agent search must not run, by what they do:
# - write to indexes, lookups, inputs or files on the search head: delete,
#   outputlookup, outputcsv, outputtext, collect (and its alias summaryindex),
#   mcollect, meventcollect, tscollect, dump, and the deprecated crawl / input;
# - alert or send data out: sendemail, sendalert, outputtelemetry, dbxoutput;
# - run external code: script (alias run), runshellscript, dbxquery;
# - run another search whose SPL is not in the text checked here: savedsearch
#   (a stored search), map (builds and runs a search per input row);
# - delete a Machine Learning Toolkit model: deletemodel.
# Reads that only look like these (inputlookup, loadjob, rest) stay allowed.
# A command starts after a `|` or at the opening `[` of a bracketed block (an
# appendpipe/append/join subpipeline or a subsearch), so both positions match.
# Between that mark and the command name Splunk may read whitespace, inline
# comments (```...```) and macros (`name(args)`, which can expand to nothing),
# so all three count as the gap, and so does a backtick left over from a run
# of more than three (such a run pairs more than one way). The quantifiers are
# possessive: each attempt takes the longest gap and never backtracks, which
# keeps the match linear.
# Search macros expand on the server, so a macro body is never seen by this
# check; that residual risk rests on the connection using a read-only role.
_SPL_GAP = r"(?:\s|```(?:(?!```).)*+```|`[^`]*+`|`)*+"
_SPL_WRITE_COMMANDS = re.compile(
    r"(?P<mark>[|\[])" + _SPL_GAP
    + r"(?P<name>delete|outputlookup|outputcsv|outputtext|collect|summaryindex|mcollect"
    r"|meventcollect|tscollect|dump|crawl|input|sendemail|sendalert|outputtelemetry"
    r"|dbxoutput|script|run|runshellscript|dbxquery|savedsearch|map|deletemodel)\b",
    re.IGNORECASE | re.DOTALL,
)
# `from savedsearch:<name>` / `union savedsearch:<name>` run a stored search as
# a dataset, like the savedsearch command. Refused wherever the word is.
_SPL_SAVEDSEARCH_DATASET = re.compile(r"\bsavedsearch" + _SPL_GAP + ":", re.IGNORECASE | re.DOTALL)
# MLTK `fit ... into <model>` saves the model as a lookup; plain `fit` (and
# `apply`) only read. Refused conservatively: a `fit` with the word `into`
# anywhere after it, so a later command that uses the word is refused too.
# Two searches (the first fit, then one `into` after it) keep this linear.
_SPL_FIT = re.compile(r"[|\[]" + _SPL_GAP + r"fit\b", re.IGNORECASE | re.DOTALL)
_SPL_INTO = re.compile(r"\binto\b", re.IGNORECASE)
# Inline comments, paired left to right.
_SPL_INLINE_COMMENT = re.compile(r"```.*?```", re.DOTALL)
_SPL_MACRO = re.compile(r"`[^`]*+`")
# The same spans with double-quoted values kept as text (a ``` inside a quoted
# value is then not a comment mark). A quote or comment left open runs to the
# end; an open comment is reported through the `close` group being empty.
_SPL_QUOTED_OR_SPAN = re.compile(
    r'(?P<quote>"(?:[^"\\]|\\.?)*+(?:"|\Z))'
    r"|(?P<comment>```(?:(?!```).)*+(?P<close>```)?)"
    r"|(?P<macro>`[^`]*+`)",
    re.DOTALL,
)
_SPL_UNCLOSED_COMMENT = (
    "SPL has an inline comment (```) that is never closed. Close it with ``` or remove it."
)

# How a Splunk query is written; repeated when an envelope's SPL is not text.
SPL_ENVELOPE_GUIDANCE = (
    'Splunk query must be an SPL string or a JSON envelope like '
    '{"spl": "search index=web | stats count by host", "earliest": "-24h", "limit": 1000}, '
    'or a knowledge-object envelope like {"dashboard": "<app>/<name>", "panel": "<title>"} '
    'or {"saved_search": "<app>/<name>"}.'
)


# Teradata lets a read start with lock clauses: LOCKING [DATABASE|TABLE|VIEW|ROW]
# [name] FOR|IN ACCESS|READ|SHARE [MODE] [NOWAIT]. ACCESS (dirty read) is the
# usual reporting idiom. WRITE / EXCLUSIVE locks would block the source's own
# loads, so a read-only agent never takes them.
_TERADATA_LOCK_PREFIX = re.compile(
    r"^\s*LOCK(?:ING)?\s+(?:(?:DATABASE|TABLE|VIEW|ROW)\s+)?(?:[\w.\"$#]+\s+)?(?:FOR|IN)\s+"
    r"(?P<mode>\w+)(?:\s+MODE)?(?:\s+NOWAIT)?\s*",
    re.IGNORECASE,
)
_TERADATA_READ_LOCKS = {"ACCESS", "READ", "SHARE"}
_TERADATA_CHARSET_CLAUSE = re.compile(r"\bCHARACTER\s+SET\s+\w+", re.IGNORECASE)
# First word of a statement Teradata runs without changing anything.
_TERADATA_READ_VERBS = {"SELECT", "SEL", "WITH", "HELP", "SHOW", "EXPLAIN", "("}


def _check_teradata(sql: str) -> str:
    """Teradata-only pre-check: the leading verb must be a read.

    An allowlist on purpose. Teradata has short forms and statements sqlglot
    cannot parse (CT = CREATE TABLE, CV = CREATE VIEW, MODIFY USER, GIVE,
    COMMENT ON ... AS), and unparsed text only gets the generic keyword check,
    which does not know them. None of them starts with a read verb.
    Returns the statement with its lock clauses removed.
    """
    text = _LITERALS_AND_COMMENTS.sub(lambda m: m.group() if m.group().startswith(("'", '"')) else " ", sql)
    body = text
    while True:
        m = _TERADATA_LOCK_PREFIX.match(body)
        if not m:
            break
        mode = m.group("mode").upper()
        if mode not in _TERADATA_READ_LOCKS:
            raise ReadOnlyViolation(
                f"LOCKING ... FOR {mode} is not allowed. Use LOCKING ROW FOR ACCESS for reads."
            )
        body = body[m.end():]
    stripped = body.lstrip()
    verb = "(" if stripped.startswith("(") else (stripped.split(None, 1)[0].upper() if stripped else "")
    if verb not in _TERADATA_READ_VERBS:
        raise ReadOnlyViolation(
            f"Statement '{verb or '(empty)'}' is not allowed. Only SELECT queries are allowed."
        )
    return body


# Db2 (LUW, z/OS, IBM i) has no sqlglot dialect, so its reads are checked by
# an allowlist on the leading verb plus the keyword blocklist, like Teradata's
# unparsed path (guard profile kind "nonparsing_sql").
_DB2_READ_VERBS = {"SELECT", "WITH", "VALUES", "("}
# Db2-specific statements and routines that change state. QCMDEXC runs an IBM i
# CL command and is callable as a scalar function inside a SELECT; ADMIN_CMD
# runs Db2 LUW administrative commands.
_DB2_WRITE_KEYWORDS = re.compile(
    r"\b(LOCK|REFRESH|DECLARE|RENAME|COMMENT|LABEL|QCMDEXC|ADMIN_CMD|SYSPROC|EXPORT|IMPORT"
    r"|RUNSTATS|REORG|TRANSFER|SAVEPOINT|ROLLBACK|COMMIT|RELEASE|CONNECT|DISCONNECT)\b",
    re.IGNORECASE,
)


def _check_db2(sql: str) -> None:
    """Db2 read-only check: one statement, a read verb, no write keyword.

    `SELECT ... FROM FINAL TABLE (INSERT ...)` (a data-change table reference)
    starts with SELECT but is caught by the INSERT/UPDATE/DELETE/MERGE keywords.
    """
    _single_statement_check(sql)
    text = _LITERALS_AND_COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    verb = "(" if text.startswith("(") else (text.split(None, 1)[0].upper() if text else "")
    if verb not in _DB2_READ_VERBS:
        raise ReadOnlyViolation(
            f"Statement '{verb or '(empty)'}' is not allowed. Only SELECT queries are allowed."
        )
    _keyword_check(sql)
    match = _DB2_WRITE_KEYWORDS.search(text)
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def dialect_for_client(client) -> Optional[str]:
    profile = guard_profile(client)
    return profile.dialect if profile is not None else None


def ensure_read_only(client, query) -> Union[str, dict]:
    """Return exactly what `client` must be sent for `query`, or raise
    ReadOnlyViolation if it would modify the source behind `client`.

    A `str` comes back as the text to send (unchanged today). A `dict` envelope
    on a client that accepts one (Splunk) comes back unchanged. Splunk SPL
    carried inline (bare, dict or JSON string) is checked here after decoding,
    so a refusal happens before the query is metered; SPL stored in a
    dashboard or saved search is only known after the client fetches it, and
    is checked at the one place every search is dispatched (`_run_search`).
    Any other non-`str` statement to a guarded client is refused.
    A client the guard does not know gets its input back unchanged.
    """
    profile = guard_profile(client)
    if profile is None:
        return query
    if not isinstance(query, str):
        if isinstance(query, dict) and profile.accepts_dict:
            if any(not isinstance(key, str) for key in query):
                raise ReadOnlyViolation(f"Envelope keys must be text. {SPL_ENVELOPE_GUIDANCE}")
            if profile.kind == "spl":
                _check_spl_envelope(query)
            return query
        raise ReadOnlyViolation(
            f"execute_query takes the statement as text, not {type(query).__name__}."
        )
    if not query.strip():
        return query
    if profile.kind == "spl":
        check_spl(query)
        _check_spl_envelope(query)
    elif profile.kind == "nonparsing_sql":
        _check_db2(query)
    elif profile.kind == "cypher":
        from app.data_sources.clients.cypher_guard import CypherNotAllowed, check_cypher

        try:
            check_cypher(query, allow_gds=client.allow_gds)
        except CypherNotAllowed as e:
            raise ReadOnlyViolation(str(e)) from e
    elif profile.dialect:
        check_sql(query, profile.dialect)
    return query


def _check_spl_envelope(query) -> None:
    """Check the SPL a Splunk query carries inline, decoded exactly as the
    client decodes it before running it."""
    from app.data_sources.clients.splunk_client import SplunkClient

    try:
        spl = SplunkClient.inline_spl(query)
    except (RecursionError, ValueError) as e:
        # JSON the decoder gives up on (nested too deep, a number too long)
        # is not a query the client could run either.
        raise ReadOnlyViolation(SPL_ENVELOPE_GUIDANCE) from e
    if spl is not None:
        check_spl(spl)


def check_sql(sql: str, dialect: str) -> None:
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    teradata = dialect == "teradata"
    if teradata:
        _check_teradata(sql)

    try:
        # A comment after the final `;` parses as a bare Semicolon node holding
        # only that comment; it runs nothing, so it is not a second statement.
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except SqlglotError as e:
        logger.warning("sql_guard: %s parser could not read query, using keyword check: %s",
                       dialect, str(e)[:200])
        if teradata:
            # `CHARACTER SET UNICODE` in a CAST is a read; without this the
            # keyword check would see SET. The leading-verb allowlist above
            # already ran; what is left is a second statement smuggled after a
            # `;` (Teradata multi-statement requests).
            _keyword_check(_TERADATA_CHARSET_CLAUSE.sub(" ", sql))
            _single_statement_check(sql)
        else:
            _keyword_check(sql)
        return

    if len(statements) != 1:
        raise ReadOnlyViolation(
            f"Only a single read-only statement is allowed; got {len(statements)} statements."
        )
    stmt = statements[0]

    if teradata and type(stmt).__name__ == "LockingStatement":
        # The lock mode was checked by _check_teradata; what it wraps must
        # itself be a read.
        lock = stmt.this
        mode = str(lock.args.get("lock_type") or "").upper() if lock is not None else ""
        if mode not in _TERADATA_READ_LOCKS:
            raise ReadOnlyViolation(f"LOCKING ... FOR {mode or '?'} is not allowed.")
        stmt = stmt.expression
        if stmt is None:
            raise ReadOnlyViolation("LOCKING must be followed by a SELECT statement.")

    if isinstance(stmt, exp.Command):
        verb = (stmt.this or "").strip().split(" ")[0].upper()
        if teradata and verb == "HELP":
            # HELP TABLE / COLUMN / VIEW / SESSION only report metadata.
            return
        if verb not in _READ_COMMANDS or (verb == "PRAGMA" and "=" in sql):
            raise ReadOnlyViolation(f"Statement '{verb}' is not allowed. Only SELECT queries are allowed.")
        if verb == "EXPLAIN":
            # EXPLAIN ANALYZE executes its statement on Postgres/MySQL.
            _keyword_check(getattr(stmt.expression, "this", "") or "")
        return

    if isinstance(stmt, exp.Use):
        # Picking a compute warehouse changes no data or privileges. USE ROLE /
        # DATABASE / SCHEMA could widen what reads can reach, so they stay refused.
        kind = stmt.args.get("kind")
        kind = kind.name if isinstance(kind, exp.Expression) else (kind or "")
        if kind.upper() == "WAREHOUSE":
            return
        raise ReadOnlyViolation("Only 'USE WAREHOUSE' is allowed. Only SELECT queries are allowed.")

    reads = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe, exp.Show, exp.Pragma)
    if not isinstance(stmt, reads):
        raise ReadOnlyViolation(
            f"Statement '{stmt.key.upper()}' is not allowed. Only SELECT queries are allowed."
        )
    if isinstance(stmt, exp.Pragma) and "=" in sql:
        raise ReadOnlyViolation("PRAGMA assignments are not allowed.")

    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
              exp.Alter, exp.TruncateTable, exp.Command, exp.Into, exp.Copy)
    for node in stmt.walk():
        if isinstance(node, writes):
            raise ReadOnlyViolation(
                f"Query contains a write operation ('{node.key.upper()}'). Only SELECT queries are allowed."
            )


def _keyword_check(sql: str) -> None:
    match = _WRITE_KEYWORDS.search(_LITERALS_AND_COMMENTS.sub(" ", sql))
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def _single_statement_check(sql: str) -> None:
    """Unparsed text: refuse anything after a `;` other than whitespace."""
    parts = _LITERALS_AND_COMMENTS.sub(" ", sql).split(";")
    if any(p.strip() for p in parts[1:]):
        raise ReadOnlyViolation("Only a single read-only statement is allowed.")


def check_spl(spl: str) -> None:
    """Refuse SPL that writes to indexes, lookups, files or the outside, and
    SPL that is not text."""
    if not isinstance(spl, str):
        raise ReadOnlyViolation(
            f"The SPL in a Splunk query must be text, not {type(spl).__name__}. {SPL_ENVELOPE_GUIDANCE}"
        )
    for text in _spl_readings(spl):
        match = _SPL_WRITE_COMMANDS.search(text)
        if match:
            mark, name = match.group("mark"), match.group("name")
            raise ReadOnlyViolation(
                f"SPL command '{name}' is not allowed: only searches that read data can run. "
                f'If "{mark}{name}" is literal text inside a quoted value (such as a bracketed '
                f'log tag), it is still refused; search for it with a wildcard in place of the '
                f'"{mark}", for example "*{name}" instead of "{mark}{name}".'
            )
        if _SPL_SAVEDSEARCH_DATASET.search(text):
            raise ReadOnlyViolation(
                "SPL dataset 'savedsearch:' is not allowed: it runs a stored search whose SPL "
                "is not checked. Write the stored search's SPL in the query instead."
            )
        fit = _SPL_FIT.search(text)
        if fit and _SPL_INTO.search(text, fit.end()):
            raise ReadOnlyViolation(
                "SPL command 'fit ... into' is not allowed: it saves a model. "
                "Run fit without 'into', or apply an existing model."
            )


def _spl_readings(spl: str) -> list[str]:
    """The texts `check_spl` matches, refusing if any one shows a refused
    command: Splunk's exact handling of comments, macros and quotes is not
    something this check can rely on, so it fails closed across all of them.

    - as written (comments and macros are gaps in the command pattern, paired
      from each `|` / `[`);
    - comments paired left to right and removed, with a space or with nothing
      (a removed span can rejoin a split command name), each also with macros
      removed with nothing;
    - the same with ``` inside a double-quoted value kept as text.

    An inline comment left open in either pairing is refused.
    """
    if "`" not in spl:
        return [spl]
    readings = [spl]
    if "```" in _SPL_INLINE_COMMENT.sub(" ", spl):
        raise ReadOnlyViolation(_SPL_UNCLOSED_COMMENT)
    for sep in (" ", ""):
        uncommented = _SPL_INLINE_COMMENT.sub(sep, spl)
        readings += [uncommented, _SPL_MACRO.sub("", uncommented)]

    pieces: list[tuple[str, str]] = []
    last = 0
    for m in _SPL_QUOTED_OR_SPAN.finditer(spl):
        pieces.append(("text", spl[last:m.start()]))
        kind = next(k for k in ("quote", "comment", "macro") if m.group(k) is not None)
        if kind == "comment" and m.group("close") is None:
            raise ReadOnlyViolation(_SPL_UNCLOSED_COMMENT)
        pieces.append((kind, m.group()))
        last = m.end()
    pieces.append(("text", spl[last:]))
    for comment_sep, macro_sep in ((" ", None), ("", None), (" ", ""), ("", "")):
        readings.append("".join(
            comment_sep if kind == "comment"
            else (macro_sep if macro_sep is not None else piece) if kind == "macro"
            else piece
            for kind, piece in pieces
        ))
    return list(dict.fromkeys(readings))
