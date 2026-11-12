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
_SPL_REFUSED_COMMANDS = (
    "delete", "outputlookup", "outputcsv", "outputtext", "collect", "summaryindex",
    "mcollect", "meventcollect", "tscollect", "dump", "crawl", "input", "sendemail",
    "sendalert", "outputtelemetry", "dbxoutput", "script", "run", "runshellscript",
    "dbxquery", "savedsearch", "map", "deletemodel",
)
# What the check assumes about Splunk's lexer, and what it does not:
# - It assumes a command starts only at the start of the search, after a `|`,
#   or at the opening `[` of a bracketed block (an appendpipe/append/join
#   subpipeline or a subsearch). The client sends text that does not start
#   with `|` as `search <text>`, so before the first `|` or `[` no listed
#   command can start, and the command rules begin there.
# - It assumes a command name is ASCII letters, digits and underscores, and
#   that the name may end at any other character (a letter outside ASCII
#   included).
# - It does not assume how backticks or quotes pair, nor whether Splunk keeps
#   quotes and backslashes. Between the mark and the name Splunk may skip
#   whitespace, quotes, backslashes, inline comments (```...```) and macros
#   (`name(args)`, which can expand to nothing). Every comment and macro ends
#   with a backtick, wherever it ends (a macro argument can hold a quoted or
#   escaped backtick), so a command Splunk could run follows `|`, `[` or a
#   backtick, then optional whitespace, quotes or backslashes. A listed name
#   in any such place is refused; that includes the first word of a comment
#   or macro after the first mark, and text in a quoted value (the message
#   says how to search for such text).
# - A comment or macro removed with nothing, or a quote or backslash dropped,
#   joins the letters on its two sides, so a name can be spelled across
#   backticks, quotes and backslashes. A comment or macro starts at a backtick
#   and ends at any later backtick, so the pieces that can join are words
#   glued to one of those characters, in text order, any of them skipped (see
#   `_spl_split_words`).
# - It does not assume what Splunk does with control characters or
#   zero-width characters (it might drop them and join the letters around
#   them), so those are refused anywhere in the search.
# Each rule is one left-to-right pass (possessive quantifiers, one search per
# rule), so the check is linear in the length of the search.
# Search macros expand on the server, so a macro body is never seen by this
# check; that residual risk rests on the connection using a read-only role.
_SPL_WORD = r"(?-i:[A-Za-z0-9_])"
_SPL_WORD_START = r"(?<!" + _SPL_WORD + r")"
_SPL_WORD_END = r"(?!" + _SPL_WORD + r")"
# What may stand between a mark (or the dataset word) and what follows it.
_SPL_GAP = r"[\s\"'\\]*+"
# The characters a split name can be glued to.
_SPL_GLUE = "`\"'\\"
_SPL_GLUE_CLASS = r"[`\"'\\]"
_SPL_COMMAND = re.compile(
    r"(?P<mark>[|\[`])" + _SPL_GAP + r"(?P<name>" + "|".join(_SPL_REFUSED_COMMANDS) + r")"
    + _SPL_WORD_END,
    re.IGNORECASE,
)
_SPL_FIRST_MARK = re.compile(r"[|\[]")
# `from savedsearch:<name>` / `union savedsearch:<name>` run a stored search as
# a dataset, like the savedsearch command. Refused wherever the word is, with
# whitespace, quotes, backslashes, comments or macros before the colon: the
# word then that gap then a backtick, and any later backtick then the gap then
# the colon.
_SPL_DATASET = re.compile(_SPL_WORD_START + r"savedsearch" + _SPL_GAP + r":", re.IGNORECASE)
_SPL_DATASET_THEN_BACKTICK = re.compile(
    _SPL_WORD_START + r"savedsearch" + _SPL_GAP + r"`", re.IGNORECASE
)
_SPL_BACKTICK_THEN_COLON = re.compile(r"`" + _SPL_GAP + r":")
# MLTK `fit ... into <model>` saves the model as a lookup; plain `fit` (and
# `apply`) only read. Refused conservatively: a `fit` with the word `into`
# anywhere after it, so a later command that uses the word is refused too.
# Two searches (the first fit, then one `into` after it) keep this linear.
_SPL_FIT = re.compile(r"[|\[`]" + _SPL_GAP + r"fit" + _SPL_WORD_END, re.IGNORECASE)
_SPL_INTO = re.compile(_SPL_WORD_START + r"into" + _SPL_WORD_END, re.IGNORECASE)
# A word glued on its left (group `left`), or else on its right, to a
# backtick, quote or backslash.
_SPL_GLUED_WORD = re.compile(
    r"(?<=" + _SPL_GLUE_CLASS + r")(?P<left>[A-Za-z0-9_]++)"
    r"|(?<![A-Za-z0-9_])[A-Za-z0-9_]++(?=" + _SPL_GLUE_CLASS + r")"
)
# The gap then `|`, `[` or a backtick, matched on the reversed text: the
# place before a word where Splunk could start a command.
_SPL_MARK_BEFORE = re.compile(_SPL_GAP + r"[|\[`]")
# Control characters other than tab, newline and carriage return, and
# zero-width characters.
_SPL_INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200d\u2060\ufeff]")
# Inline comments, paired left to right.
_SPL_INLINE_COMMENT = re.compile(r"```.*?```", re.DOTALL)
_SPL_UNCLOSED_COMMENT = (
    "SPL has an inline comment (```) that is never closed. Close it with ``` or remove it. "
    'If the ``` is literal text inside a quoted value, it is still read as a comment mark; '
    'search for the value with a wildcard in its place, for example "*" instead of "```".'
)


# Words checked when spelled across backticks, quotes or backslashes: names
# that count where a command can start, and words that count anywhere after
# the first mark (the dataset word, and the `into` that makes a `fit` save its
# model).
_SPL_JOINED_COMMANDS = frozenset(_SPL_REFUSED_COMMANDS + ("fit",))
_SPL_JOINED_ANYWHERE = frozenset(("savedsearch", "into"))


def _spl_word_pieces(words) -> dict[str, frozenset[str]]:
    """For each piece of a checked word, the prefixes it can continue."""
    pieces: dict[str, set[str]] = {}
    for word in words:
        for start in range(1, len(word)):
            for end in range(start + 1, len(word) + 1):
                pieces.setdefault(word[start:end], set()).add(word[:start])
    return {piece: frozenset(prefixes) for piece, prefixes in pieces.items()}


def _spl_prefixes(words) -> frozenset[str]:
    return frozenset(w[:i] for w in words for i in range(1, len(w)))


_SPL_COMMAND_PIECES = _spl_word_pieces(_SPL_JOINED_COMMANDS)
_SPL_ANYWHERE_PIECES = _spl_word_pieces(_SPL_JOINED_ANYWHERE)
_SPL_COMMAND_PREFIXES = _spl_prefixes(_SPL_JOINED_COMMANDS)
_SPL_ANYWHERE_PREFIXES = _spl_prefixes(_SPL_JOINED_ANYWHERE)
_NO_PREFIXES: frozenset[str] = frozenset()

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
        _check_spl_envelope(query, checked=query)
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


def _check_spl_envelope(query, checked: Optional[str] = None) -> None:
    """Check the SPL a Splunk query carries inline, decoded exactly as the
    client decodes it before running it, unless it is `checked` (text that
    already passed `check_spl`)."""
    from app.data_sources.clients.splunk_client import SplunkClient

    try:
        spl = SplunkClient.inline_spl(query)
    except (RecursionError, ValueError) as e:
        # JSON the decoder gives up on (nested too deep, a number too long)
        # is not a query the client could run either.
        raise ReadOnlyViolation(SPL_ENVELOPE_GUIDANCE) from e
    if spl is not None and spl != checked:
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
    invisible = _SPL_INVISIBLE.search(spl)
    if invisible:
        raise ReadOnlyViolation(
            f"SPL contains an invisible character (U+{ord(invisible.group()):04X}): a control "
            "character or a zero-width character, which Splunk may drop and so join the "
            "letters around it. Remove it; only tab, newline and carriage return are allowed."
        )
    if "```" in _SPL_INLINE_COMMENT.sub(" ", spl):
        raise ReadOnlyViolation(_SPL_UNCLOSED_COMMENT)
    first_mark = _SPL_FIRST_MARK.search(spl)
    if first_mark is None:
        # No `|` or `[`: the whole text is the one `search` command.
        joined, fit_end, into_end = None, None, None
    else:
        begin = first_mark.start()
        match = _SPL_COMMAND.search(spl, begin)
        if match:
            mark, name = match.group("mark"), match.group("name")
            raise ReadOnlyViolation(
                f"SPL command '{name}' is not allowed: only searches that read data can run. "
                f'It is refused wherever Splunk could start it: after "|", after "[", or after a '
                f"backtick that ends an inline comment or a macro, with any whitespace, quotes "
                f"or backslashes in between. "
                f'If "{mark}{name}" is literal text inside a quoted value (such as a bracketed '
                f"log tag) or a comment, it is still refused; reword the comment, or search for "
                f'the value with a wildcard in place of the "{mark}", for example "*{name}" '
                f'instead of "{mark}{name}".'
            )
        joined, fit_end, into_end = _spl_split_words(spl, begin)
        fit = _SPL_FIT.search(spl, begin)
        if fit:
            fit_end = fit.end() if fit_end is None else min(fit_end, fit.end())
    if joined:
        raise ReadOnlyViolation(
            f"SPL command '{joined}' is not allowed: only searches that read data can run. "
            "Its letters are split by backticks, quotes or backslashes (inline comments, macros "
            "or quoted pieces inside the word), which Splunk can join back into the command. "
            "Write words without those characters inside them."
        )
    if _SPL_DATASET.search(spl) or _spl_dataset_behind_backticks(spl):
        raise ReadOnlyViolation(
            "SPL dataset 'savedsearch:' is not allowed: it runs a stored search whose SPL "
            "is not checked. Write the stored search's SPL in the query instead. "
            'If "savedsearch:" is literal text inside a quoted value, it is still refused; '
            'search for it with a wildcard in place of the ":", for example "savedsearch*" '
            'instead of "savedsearch:".'
        )
    if fit_end is not None and (
        _SPL_INTO.search(spl, fit_end) or (into_end is not None and into_end > fit_end)
    ):
        raise ReadOnlyViolation(
            "SPL command 'fit ... into' is not allowed: it saves a model. "
            "Run fit without 'into', or apply an existing model."
        )


def _spl_dataset_behind_backticks(spl: str) -> bool:
    """`savedsearch` then comments or macros, then the colon (whitespace,
    quotes or backslashes allowed around the backticks)."""
    word = _SPL_DATASET_THEN_BACKTICK.search(spl)
    return bool(word and _SPL_BACKTICK_THEN_COLON.search(spl, word.end() - 1))


def _spl_split_words(spl: str, begin: int) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Words Splunk could read, from `begin` on, once comments or macros
    inside them are removed with nothing and quotes or backslashes inside
    them dropped: a refused name (where a command can start) or `savedsearch`
    (anywhere), else None; where the first `fit` read that way ends; and
    where the last `into` read that way ends.

    The pieces that join are words glued to a backtick, quote or backslash:
    the first piece has one on its right and, for a command name, starts
    where a command can start (after `|`, `[` or a backtick, then optional
    whitespace, quotes or backslashes); each later piece has one on its left,
    and every piece but the last also one on its right. A removed span can
    end at any later backtick, so any glued words in between may be skipped:
    the scan keeps every prefix seen so far (the live prefixes) and extends
    it by each later piece.

    One pass over the glued words. For each piece only the live prefixes it
    can continue are looked up. The live prefixes only grow and are bounded
    by the checked words, and a piece seen again with no new live prefix
    since its last look-up changes nothing but where `into` last ended, so
    it is not looked up again: the work per word is a constant.
    """
    if not any(glue in spl for glue in _SPL_GLUE):
        return None, None, None
    reversed_spl = spl[::-1]
    size = len(spl)
    command_live: set[str] = set()
    anywhere_live: set[str] = set()
    # Bumped whenever a live prefix is added. A (piece, glued on its right)
    # looked up at the current version maps to whether it ended an `into`.
    version = 0
    looked_up: dict[tuple[str, bool], tuple[int, bool]] = {}
    fit_end: Optional[int] = None
    into_end: Optional[int] = None
    for match in _SPL_GLUED_WORD.finditer(spl, begin):
        start, end = match.span()
        piece = match.group().lower()
        glued_right = end < size and spl[end] in _SPL_GLUE
        if match.group("left") is not None:
            key = (piece, glued_right)
            seen = looked_up.get(key)
            if seen is not None and seen[0] == version:
                if seen[1]:
                    into_end = end
            else:
                at_version = version
                ends_into = False
                for prefix in command_live & _SPL_COMMAND_PIECES.get(piece, _NO_PREFIXES):
                    joined = prefix + piece
                    if joined == "fit":
                        fit_end = end if fit_end is None else fit_end
                    elif joined in _SPL_JOINED_COMMANDS:
                        return joined, fit_end, into_end
                    elif glued_right and joined not in command_live:
                        command_live.add(joined)
                        version += 1
                for prefix in anywhere_live & _SPL_ANYWHERE_PIECES.get(piece, _NO_PREFIXES):
                    joined = prefix + piece
                    if joined == "into":
                        into_end = end
                        ends_into = True
                    elif joined in _SPL_JOINED_ANYWHERE:
                        return joined, fit_end, into_end
                    elif glued_right and joined not in anywhere_live:
                        anywhere_live.add(joined)
                        version += 1
                looked_up[key] = (at_version, ends_into)
        if glued_right:
            if (piece in _SPL_COMMAND_PREFIXES and piece not in command_live
                    and _SPL_MARK_BEFORE.match(reversed_spl, size - start)):
                command_live.add(piece)
                version += 1
            if piece in _SPL_ANYWHERE_PREFIXES and piece not in anywhere_live:
                anywhere_live.add(piece)
                version += 1
    return None, fit_end, into_end
