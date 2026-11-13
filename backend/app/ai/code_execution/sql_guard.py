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
    `SELECT ... INTO`) is refused. If sqlglot cannot parse the text (or fails
    in any way), we do not block a possibly-valid read on a parser gap; we
    fall back to an allowlist over the text with string literals and
    comments removed: one statement, a read verb first, no write word. The
    text is read with the dialect's own tokenizer and in each generic way
    (`#` comments or not; backslash escapes or not, where the engine reads
    them); any reading refusing refuses, and the error names it.
    Postgres text is first read by its own lexer (`pg_lexer`), as the server
    reads it: one statement only, and the lexer's canonical text is what is
    checked and what is sent. On that text it also refuses the TABLE
    shorthand and calls to a short list of functions (`DENIED_FUNCTIONS`)
    that run SQL held in a string or change the session or the server, and
    checks the statement an EXPLAIN explains like unparsed text.
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

import regex

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

# Text sqlglot cannot parse (`check_unparsed`): the verbs a read may start
# with, and every word that changes data, state or the session, matched as a
# whole name the way Postgres splits names: a name runs over letters, digits,
# `_` and `$`, and a word right after a number is a separate word to older
# servers (`1do` is a number then DO; `t1do` is one name).
_UNPARSED_READ_VERBS = frozenset({"SELECT", "WITH", "VALUES", "SHOW", "EXPLAIN"})
_NAME_CHAR = r"A-Za-z0-9_$\x80-\U0010FFFF"
_UNPARSED_WRITE_WORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE",
    "EXEC", "EXECUTE", "MERGE", "CALL", "UPSERT", "LOAD", "COPY", "VACUUM", "ATTACH", "DETACH", "SET", "INTO",
    "COMMIT", "ROLLBACK", "BEGIN", "START", "LOCK", "LISTEN", "UNLISTEN", "NOTIFY", "SECURITY", "IMPORT",
    "REASSIGN", "PREPARE", "DEALLOCATE", "DISCARD", "RESET", "CHECKPOINT", "CLUSTER", "REINDEX", "REFRESH", "DO",
})
# A number runs as far as the engine reads one: hex, octal and binary
# (`0x1F`, `0o17`, `0b101`; `0x` alone is an empty binary value to SQL
# Server) and digit separators (`1_000`), so a word glued after it is still
# a word.
_DIGITS_RUN = r"[0-9](?:_?[0-9])*"
_UNPARSED_WORD = re.compile(
    r"(?P<name>[A-Za-z_\x80-\U0010FFFF][" + _NAME_CHAR + r"]*)"
    r"|0[xX][0-9A-Fa-f]*|0[oO][0-7]*|0[bB][01]*"
    r"|(?:" + _DIGITS_RUN + r"(?:\.(?:" + _DIGITS_RUN + r")?)?|\." + _DIGITS_RUN + r")(?:[eE][+-]?[0-9]+)?"
)
# The readings of unparsed text besides the dialect's own tokenizer, each a
# (`#` starts a comment, a backslash escapes the next character) pair: the
# standard one, and the MySQL / BigQuery / ClickHouse / Hive-like ones.
# Only the standard one reads `[...]` as a quoted name (SQL Server, SQLite);
# to the others `[` is code (an array subscript).
_GENERIC_READINGS = ((False, False), (True, False), (False, True), (True, True))
_READING_NAMES = {
    (False, False): "with standard quoting",
    (True, False): "with # line comments",
    (False, True): "with backslash escapes",
    (True, True): "with # line comments and backslash escapes",
}
# Dialects whose engine (or a server mode, a string prefix or a statement
# kind it also runs) reads a backslash in a literal as an escape: only for
# these are the backslash readings taken. Redshift is here until it gets its
# own lexer; Athena runs Hive DDL.
_BACKSLASH_ESCAPE_DIALECTS = frozenset({
    "mysql", "bigquery", "clickhouse", "snowflake", "hive", "spark", "spark2", "databricks", "duckdb",
    "redshift", "athena",
})
_GENERIC_STARTS = {
    (False, False): re.compile(r"['\"`\[]|--|/\*"),
    (True, False): re.compile(r"['\"`#]|--|/\*"),
    (False, True): re.compile(r"['\"`]|--|/\*"),
    (True, True): re.compile(r"['\"`#]|--|/\*"),
}
_QUOTE_STOPS = {
    (close, backslash): re.compile("[" + re.escape(close) + ("\\\\" if backslash else "") + "]")
    for close in ("'", '"', "`", "]") for backslash in (False, True)
}
# sqlglot token types that are a literal's value, not code.
_LITERAL_TOKENS = frozenset({
    "STRING", "NATIONAL_STRING", "RAW_STRING", "BIT_STRING", "HEX_STRING", "BYTE_STRING", "HEREDOC_STRING",
    "UNICODE_STRING",
})
_QUOTED_NAME = re.compile(r'"(?:[^"]|"")*"')
_LEADING_WORD = re.compile(r"[\s(]*([A-Za-z_][" + _NAME_CHAR + r"]*)")

# Functions a read must not call, per dialect, matched on the bare function
# name (lowercased, without its schema or quotes): they run SQL held in a
# string, reach another connection, or change the session or the server
# beyond the read. Transaction-scoped advisory locks end with the read and
# stay allowed.
_PG_DENIED_FUNCTION = re.compile(
    r"ts_stat|ts_rewrite|query_to_xml\w*|table_to_xml\w*|cursor_to_xml\w*|dblink\w*"
    r"|pg_advisory_lock\w*|pg_try_advisory_lock\w*|pg_advisory_unlock\w*"
    r"|set_config|setval|nextval|pg_notify|pg_cancel_backend|pg_terminate_backend|pg_reload_conf"
)
DENIED_FUNCTIONS: dict[str, re.Pattern] = {"postgres": _PG_DENIED_FUNCTION, "redshift": _PG_DENIED_FUNCTION}
# A name in code text, quoted or not, or a number (so a name glued after a
# number is read as its own name, as by `check_unparsed`).
_CODE_NAME = re.compile(r'"(?P<quoted>(?:[^"]|"")*)"|' + _UNPARSED_WORD.pattern)
_CALL_OPEN = re.compile(r"\s*\(")
# EXPLAIN and its options, before the statement it explains: a parenthesised
# option list (not a parenthesised statement), or the older ANALYZE / VERBOSE
# words.
_EXPLAIN_HEAD = re.compile(
    r"\s*EXPLAIN(?![" + _NAME_CHAR + r"])\s*"
    r"(?:\((?!\s*(?:\(|(?:SELECT|WITH|VALUES|TABLE)(?![" + _NAME_CHAR + r"])))[^()]*\)\s*"
    r"|(?:ANALY[SZ]E(?![" + _NAME_CHAR + r"])\s*)?(?:VERBOSE(?![" + _NAME_CHAR + r"])\s*)?)",
    re.IGNORECASE,
)
_EXPLAINED_VERBS = frozenset({"SELECT", "WITH", "VALUES"})

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
#   at the opening `[` of a bracketed block (an appendpipe/append/join
#   subpipeline or a subsearch), or where a macro that expands to text ending
#   in `|` or `[` ends. The client sends text that does not start with `|` as
#   `search <text>`, so the search starts with that command. Before the first
#   `|` or `[`, a command can start only after a backtick that may end a
#   macro or a comment: any backtick but the first in the text (nothing is
#   open before it). A backtick that opens a comment looks like one that
#   closes it, so no other backtick is let off.
# - It assumes a command name is ASCII letters, digits and underscores, or
#   letters outside ASCII that case-fold to one of those (the Kelvin sign,
#   long s and dotted or dotless i), and that the name may end at any other
#   character, or end or start at one of those folding letters (a word is
#   read with every such letter as its ASCII letter, or with every one as a
#   break between words). A letter whose case folding is several ASCII
#   letters (the sharp s, the Latin ligatures) may be read as those letters,
#   so the rules also run on the search read that way.
# - It does not assume how backticks or quotes pair, nor whether Splunk keeps
#   quotes and backslashes. Between the mark and the name Splunk may skip
#   whitespace, quotes, backslashes, inline comments (```...```) and macros
#   (`name(args)`, which can expand to nothing). Every comment and macro ends
#   with a backtick, wherever it ends (a macro argument can hold a quoted or
#   escaped backtick), so a command Splunk could run follows `|`, `[` or a
#   backtick, then optional whitespace, quotes or backslashes. A listed name
#   in any such place is refused; that includes the first word of a comment
#   or macro (unless it opens at the first backtick in the text), and text
#   in a quoted value (the message says how to search for such text).
# - A comment or macro removed with nothing joins the letters on its two
#   sides; it starts at a backtick and ends at any later backtick, so words
#   in between may be skipped. A quote or backslash dropped joins only the
#   letters right next to it. So a name can be spelled across backticks,
#   quotes and backslashes (see `_spl_split_words`).
# - It does not assume what Splunk does with format characters or characters
#   that are invisible by default (it might drop them, read them as spaces or
#   keep them), so the rules run on the search as written, without them, and
#   with a space for each, and one between two letters, digits or
#   underscores is refused. Text in some languages needs them (direction
#   marks, zero-width joiners, emoji selectors). Control characters other
#   than tab, newline and carriage return, lone surrogates, private-use
#   characters and noncharacters cannot be sent cleanly, so they are refused
#   anywhere.
# Each rule is one left-to-right pass (possessive quantifiers, one search per
# rule), so the check is linear in the length of the search.
# Search macros expand on the server, so a macro body is never seen by this
# check; that residual risk rests on the connection using a read-only role.
_SPL_WORD = r"(?-i:[A-Za-z0-9_])"
_SPL_WORD_START = r"(?<!" + _SPL_WORD + r")"
_SPL_WORD_END = r"(?!" + _SPL_WORD + r")"
# Letters outside ASCII that `re.IGNORECASE` matches to an ASCII letter, and
# that letter.
_SPL_FOLD = {0x0130: "i", 0x0131: "i", 0x017F: "s", 0x212A: "k"}
# Letters whose full case folding is several ASCII letters (the sharp s and
# the Latin ligatures), and those letters: `re.IGNORECASE` matches none of
# them, so the rules also run on the search read with them replaced.
_SPL_MULTI_FOLD = {
    0x00DF: "ss", 0x1E9E: "ss", 0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi",
    0xFB04: "ffl", 0xFB05: "st", 0xFB06: "st",
}
_SPL_MULTI_FOLD_CHAR = re.compile("[" + "".join(chr(code) for code in _SPL_MULTI_FOLD) + "]")
_SPL_FOLD_MARK = dict.fromkeys(_SPL_FOLD, " ")
_SPL_FOLD_CHARS = "".join(chr(code) for code in _SPL_FOLD)
_SPL_NAME_CHAR = r"[A-Za-z0-9_" + _SPL_FOLD_CHARS + r"]"
# What may stand between a mark (or the dataset word) and what follows it:
# whitespace, quotes, backslashes, and letters that fold to ASCII (a word
# may end, or start, at one).
_SPL_GAP = r"(?-i:[\s\"'\\" + _SPL_FOLD_CHARS + r"])*+"
# Before a name: that gap, or the gap without those letters (a name may also
# start with one, read as its ASCII letter).
_SPL_NAME_GAP = r"(?:" + _SPL_GAP + r"|[\s\"'\\]*+)"
# The characters a split name can be glued to, and a run of them.
_SPL_GLUE = "`\"'\\"
_SPL_GLUE_CLASS = r"[`\"'\\]"
_SPL_GLUE_RUN = re.compile(_SPL_GLUE_CLASS + r"*+")
_SPL_NAMES = r"(?P<name>" + "|".join(_SPL_REFUSED_COMMANDS) + r")" + _SPL_WORD_END
# Searched from after the first backtick before the first `|` or `[`, or else
# from the first `|` or `[`.
_SPL_COMMAND = re.compile(r"(?P<mark>[|\[`])" + _SPL_NAME_GAP + _SPL_NAMES, re.IGNORECASE)
_SPL_FIRST_MARK = re.compile(r"[|\[]")
# How much of the matched text a refusal quotes.
_SPL_SHOWN_MAX = 80
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
# The dataset word spelled across backticks, quotes or backslashes counts when
# the gap then a colon, or the gap then a backtick, follows it.
_SPL_GAP_THEN_COLON = re.compile(_SPL_GAP + r":")
_SPL_GAP_THEN_BACKTICK = re.compile(_SPL_GAP + r"`")
# A colon, the gap, then a backtick, matched on the reversed text: the last
# place a comment or macro can end right before a colon.
_SPL_LAST_BACKTICK_THEN_COLON = re.compile(r":" + _SPL_GAP + r"`")
# MLTK `fit ... into <model>` saves the model as a lookup; plain `fit` (and
# `apply`) only read. Refused conservatively: a `fit` with the word `into`
# anywhere after it, so a later command that uses the word is refused too.
# Two searches (the first fit, then one `into` after it) keep this linear.
_SPL_FIT = re.compile(r"[|\[`]" + _SPL_NAME_GAP + r"fit" + _SPL_WORD_END, re.IGNORECASE)
_SPL_INTO = re.compile(_SPL_WORD_START + r"into" + _SPL_WORD_END, re.IGNORECASE)
# A word glued on its left (group `left`), or else on its right (group
# `right`), to a backtick, quote or backslash, and the glue run after it.
_SPL_GLUED_WORD = re.compile(
    r"(?<=" + _SPL_GLUE_CLASS + r")(?P<left>" + _SPL_NAME_CHAR + r"++)(?P<left_run>"
    + _SPL_GLUE_CLASS + r"*+)"
    r"|(?<!" + _SPL_NAME_CHAR + r")(?P<right>" + _SPL_NAME_CHAR + r"++)(?P<right_run>"
    + _SPL_GLUE_CLASS + r"++)"
)
# The gap, matched on the reversed text back from a word: what may stand
# between the word and the mark before it.
_SPL_GAP_BEFORE = re.compile(_SPL_GAP)
# Built once, from the `regex` module's Unicode tables. Control characters
# (Cc) other than tab, newline and carriage return, lone surrogates (Cs),
# private-use characters (Co) and noncharacters: a search cannot carry them
# cleanly.
_SPL_UNSENDABLE = regex.compile(r"(?![\t\n\r])[\p{Cc}\p{Cs}\p{Co}\p{Noncharacter_Code_Point}]")
# Format characters (Cf) and default-ignorable code points: none of them
# shows, and Splunk may drop them, read them as spaces or keep them.
_SPL_INVISIBLE = regex.compile(r"[\p{Cf}\p{Default_Ignorable_Code_Point}]")
# A run of them between two characters a name is made of.
_SPL_INVISIBLE_IN_WORD = regex.compile(
    r"(?<=" + _SPL_NAME_CHAR + r")[\p{Cf}\p{Default_Ignorable_Code_Point}]++(?=" + _SPL_NAME_CHAR + r")"
)
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
_SPL_ONLY = {
    prefix: frozenset((prefix,)) for prefix in _SPL_COMMAND_PREFIXES | _SPL_ANYWHERE_PREFIXES
}
# Pieces that can start a checked word.
_SPL_STARTS = _SPL_COMMAND_PREFIXES | _SPL_ANYWHERE_PREFIXES
# Pieces that can start or continue a checked word.
_SPL_RELEVANT_PIECES = frozenset(
    set(_SPL_COMMAND_PIECES) | set(_SPL_ANYWHERE_PIECES) | _SPL_COMMAND_PREFIXES | _SPL_ANYWHERE_PREFIXES
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

    A `str` comes back as the text to send: for Postgres the lexer's
    canonical text (comments dropped, every string written as a standard
    literal), the text that was checked; for other clients the input. A `dict` envelope
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
    if type(query) is not str:
        if isinstance(query, str):
            # A str subclass can answer the string methods the checks call
            # with something other than its text.
            raise ReadOnlyViolation(
                f"execute_query takes the statement as a plain str, not {type(query).__name__}."
            )
        if type(query) is dict and profile.accepts_dict:
            if any(type(key) is not str for key in query):
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
    elif profile.dialect == "postgres":
        return _check_postgres(query)
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
    # The client strips a bare string; whitespace at either end changes no
    # rule's verdict, so the stripped text needs no second check.
    if spl is not None and (checked is None or spl not in (checked, checked.strip())):
        check_spl(spl)


def check_sql(sql: str, dialect: str) -> None:
    if dialect == "postgres":
        _check_postgres(sql)
        return

    teradata = dialect == "teradata"
    if teradata:
        _check_teradata(sql)

    def unparsed() -> None:
        if teradata:
            # `CHARACTER SET UNICODE` in a CAST is a read; without this the
            # keyword check would see SET. The leading-verb allowlist above
            # already ran; what is left is a second statement smuggled after a
            # `;` (Teradata multi-statement requests).
            _keyword_check(_TERADATA_CHARSET_CLAUSE.sub(" ", sql))
            _single_statement_check(sql)
        else:
            # Refused if any reading refuses: a quote in a `#` comment or a
            # backslash-escaped quote must not hide code in one of them.
            for reading, code_only in _unparsed_readings(sql, dialect):
                try:
                    check_unparsed(code_only, read_verbs=_UNPARSED_READ_VERBS | {"DESCRIBE", "DESC"})
                    _check_denied_calls(code_only, dialect)
                except ReadOnlyViolation as e:
                    raise ReadOnlyViolation(f"{e} (This is how the text reads {reading}.)") from e

    _check_parsed(sql, dialect, unparsed)


def _check_postgres(sql: str) -> str:
    """Check Postgres text as the server reads it; return the canonical
    text, which is what must be sent."""
    from app.ai.code_execution.pg_lexer import lex_postgres

    lex = lex_postgres(sql)
    if lex.statements != 1:
        raise ReadOnlyViolation(
            f"Only a single read-only statement is allowed; got {lex.statements} statements."
        )
    _check_postgres_code(lex.code_only)
    _check_parsed(lex.text, "postgres",
                  lambda: check_unparsed(lex.code_only, read_verbs=_UNPARSED_READ_VERBS))
    return lex.text


def _check_postgres_code(code_only: str) -> None:
    """The Postgres rules read on the lexer's code (literal bodies emptied),
    parsed or not: no TABLE shorthand, no denied function, and the statement
    an EXPLAIN explains is itself a read."""
    words = _QUOTED_NAME.sub(" ", code_only)
    for word in _UNPARSED_WORD.finditer(words):
        if (word.group("name") or "").upper() == "TABLE":
            raise ReadOnlyViolation(
                "The TABLE shorthand is not allowed; write SELECT * FROM the table instead. "
                "Only SELECT queries are allowed."
            )
    _check_denied_calls(code_only, "postgres")
    leading = _LEADING_WORD.match(words)
    if leading and leading.group(1).upper() == "EXPLAIN":
        head = _EXPLAIN_HEAD.match(words)
        if head is None:
            raise ReadOnlyViolation("EXPLAIN must be followed by a SELECT statement.")
        check_unparsed(words[head.end():], read_verbs=_EXPLAINED_VERBS)


def _denied_function(name: str) -> ReadOnlyViolation:
    return ReadOnlyViolation(
        f"The function {name.upper()}() is not allowed in agent queries: it runs SQL held in a string, "
        "reaches another connection, or changes the session or the server. Only SELECT queries are allowed."
    )


def _check_denied_calls(code: str, dialect: str) -> None:
    """Refuse a call to a function denied for `dialect` in code text
    (literal bodies emptied): any name, quoted or not and with or without a
    schema before it, followed by '('."""
    denied = DENIED_FUNCTIONS.get(dialect)
    if denied is None:
        return
    for match in _CODE_NAME.finditer(code):
        quoted = match.group("quoted")
        name = quoted.replace('""', '"') if quoted is not None else match.group("name")
        if name is not None and denied.fullmatch(name.lower()) and _CALL_OPEN.match(code, match.end()):
            raise _denied_function(name)


def _func_names(node) -> list[str]:
    """The names a sqlglot function node may have been written as."""
    from sqlglot import exp

    if isinstance(node, exp.Anonymous):
        return [str(node.name or "")]
    return list(type(node).sql_names())


def _check_parsed(sql: str, dialect: str, unparsed) -> None:
    """The parsed-path rules on `sql`; `unparsed()` when sqlglot cannot
    parse it (any exception: a parser failure must not let the text run
    unchecked)."""
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp

    teradata = dialect == "teradata"
    try:
        # A comment after the final `;` parses as a bare Semicolon node holding
        # only that comment; it runs nothing, so it is not a second statement.
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except Exception as e:
        logger.warning("sql_guard: %s parser could not read query, using the unparsed check: %s",
                       dialect, str(e)[:200])
        unparsed()
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
    denied = DENIED_FUNCTIONS.get(dialect)
    for node in stmt.walk():
        if isinstance(node, writes):
            raise ReadOnlyViolation(
                f"Query contains a write operation ('{node.key.upper()}'). Only SELECT queries are allowed."
            )
        if denied is not None and isinstance(node, exp.Func):
            for name in _func_names(node):
                if denied.fullmatch(name.strip('"').lower()):
                    raise _denied_function(name)


def _keyword_check(sql: str) -> None:
    match = _WRITE_KEYWORDS.search(_LITERALS_AND_COMMENTS.sub(" ", sql))
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def check_unparsed(code_only: str, *, read_verbs: frozenset[str], extra_write_words: Optional[re.Pattern] = None) -> None:
    """Check text sqlglot could not parse: one statement, a read verb first,
    and no write word anywhere. `code_only` is the text with its literals
    and comments already emptied; quoted names are skipped here."""
    words = _QUOTED_NAME.sub(" ", code_only)
    parts = [part for part in words.split(";") if part.strip()]
    if len(parts) != 1:
        raise ReadOnlyViolation(
            f"Only a single read-only statement is allowed; got {len(parts)} statements."
        )
    leading = _LEADING_WORD.match(parts[0])
    verb = leading.group(1).upper() if leading else ""
    if verb not in read_verbs:
        raise ReadOnlyViolation(
            f"Statement '{verb or '(empty)'}' is not allowed. Only SELECT queries are allowed."
        )
    for word in _UNPARSED_WORD.finditer(words):
        name = word.group("name")
        if name is not None and name.upper() in _UNPARSED_WRITE_WORDS:
            raise ReadOnlyViolation(
                f"Query contains forbidden operation '{name.upper()}'. Only SELECT queries are allowed."
            )
    match = extra_write_words.search(words) if extra_write_words is not None else None
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def _unparsed_readings(sql: str, dialect: str) -> list[tuple[str, str]]:
    """Text sqlglot could not parse, with its literals and comments emptied,
    in every way it may be read, each with the reading's name: by the
    dialect's own tokenizer (refused if that tokenizer cannot read it), and
    by each generic reading (the backslash ones only where the engine reads
    backslash escapes)."""
    from sqlglot.dialects.dialect import Dialect

    try:
        tokens = Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception as e:
        raise ReadOnlyViolation(
            "The query could not be read as SQL (an unterminated string, quoted name or comment?). Fix it and retry."
        ) from e
    pieces = []
    for token in tokens:
        kind = token.token_type.name
        pieces.append("''" if kind in _LITERAL_TOKENS else '""' if kind == "IDENTIFIER" else token.text)
    backslash = dialect in _BACKSLASH_ESCAPE_DIALECTS
    return [(f"as the {dialect} tokenizer reads it", " ".join(pieces))] + [
        (_READING_NAMES[(h, b)], _generic_reading(sql, h, b)) for h, b in _GENERIC_READINGS if backslash or not b
    ]


def _generic_reading(sql: str, hash_comments: bool, backslash: bool) -> str:
    """`sql` with each literal, quoted name and comment replaced by a space:
    quotes doubled to escape them, plus `#` line comments and backslash
    escapes when asked (and `[...]` names when neither is). In a `#`
    reading (MySQL's), `--` starts a comment only before whitespace or a
    control character. From anything left open, the rest is read as code.
    One pass."""
    starts = _GENERIC_STARTS[(hash_comments, backslash)]
    out = []
    i, n = 0, len(sql)
    while True:
        m = starts.search(sql, i)
        if m is None:
            out.append(sql[i:])
            break
        mark, start = m.group(), m.start()
        if mark == "--" and hash_comments and sql[m.end():m.end() + 1] > " ":
            out.append(sql[i:m.end()])
            i = m.end()
            continue
        out.append(sql[i:start])
        if mark in ("--", "#"):
            end = sql.find("\n", m.end())
            end = n if end < 0 else end
        elif mark == "/*":
            end = sql.find("*/", m.end())
            end = -1 if end < 0 else end + 2
        else:
            end = _quoted_end(sql, start, "]" if mark == "[" else mark, backslash and mark in "'\"")
        if end < 0:
            out.append(sql[start:])
            break
        out.append(" ")
        i = end
    return "".join(out)


def _quoted_end(sql: str, start: int, close: str, backslash: bool) -> int:
    """Where the quoted text opening at `start` ends (exclusive), or -1."""
    stop = _QUOTE_STOPS[(close, backslash)]
    i = start + 1
    while True:
        m = stop.search(sql, i)
        if m is None:
            return -1
        if m.group() == "\\":
            i = m.end() + 1
        elif close != "]" and sql.startswith(close, m.end()):
            i = m.end() + 1
        else:
            return m.end()


def _single_statement_check(sql: str) -> None:
    """Unparsed text: refuse anything after a `;` other than whitespace."""
    parts = _LITERALS_AND_COMMENTS.sub(" ", sql).split(";")
    if any(p.strip() for p in parts[1:]):
        raise ReadOnlyViolation("Only a single read-only statement is allowed.")


def check_spl(spl: str) -> None:
    """Refuse SPL that writes to indexes, lookups, files or the outside, and
    SPL that is not text."""
    if type(spl) is not str:
        # A str subclass can answer the string methods the check calls with
        # something other than its text.
        raise ReadOnlyViolation(
            f"The SPL in a Splunk query must be text (a plain str), not {type(spl).__name__}. "
            f"{SPL_ENVELOPE_GUIDANCE}"
        )
    unsendable = _SPL_UNSENDABLE.search(spl)
    if unsendable:
        raise ReadOnlyViolation(
            f"SPL contains a character a search cannot carry (U+{ord(unsendable.group()):04X}): "
            "a control character, a lone surrogate, a private-use character or a noncharacter. "
            "Remove it; only tab, newline and carriage return are allowed."
        )
    _check_spl_readings(spl)
    if _SPL_MULTI_FOLD_CHAR.search(spl):
        try:
            _check_spl_readings(spl.translate(_SPL_MULTI_FOLD))
        except ReadOnlyViolation as refused:
            # Every such letter, in code point order: the reading folds them
            # all at once, so any of them may be the one the refusal is about.
            letters = ", ".join(
                f"U+{code:04X} ('{_SPL_MULTI_FOLD[code]}')"
                for code in sorted(_SPL_MULTI_FOLD) if chr(code) in spl
            )
            raise ReadOnlyViolation(
                f"SPL contains letters that case-fold to several letters ({letters}), which "
                f"Splunk may read as those letters. Read that way, the search is refused; write "
                f"the letters instead. {refused}"
            ) from refused


def _check_spl_readings(spl: str) -> None:
    """The rules on the search as written and, when it holds an invisible
    character, read without them and with a space for each."""
    invisible = _SPL_INVISIBLE.search(spl)
    if invisible is None:
        _check_spl_reading(spl)
        return
    code = f"U+{ord(invisible.group()):04X}"
    in_word = _SPL_INVISIBLE_IN_WORD.search(spl)
    if in_word:
        raise ReadOnlyViolation(
            f"SPL contains an invisible character (U+{ord(in_word.group()[0]):04X}) between two "
            "letters, digits or underscores, which Splunk may drop and so join the words around "
            "it. Remove it."
        )
    _check_spl_reading(spl)
    for reading in (_SPL_INVISIBLE.sub("", spl), _SPL_INVISIBLE.sub(" ", spl)):
        try:
            _check_spl_reading(reading)
        except ReadOnlyViolation as refused:
            raise ReadOnlyViolation(
                f"SPL contains an invisible character ({code}), such as a zero-width space or a "
                "text direction mark, which Splunk may drop or read as a space. Read that way, "
                f"the search is refused; remove the character. {refused}"
            ) from refused


def _check_spl_reading(spl: str) -> None:
    """The rules, on one reading of the search."""
    if "```" in _SPL_INLINE_COMMENT.sub(" ", spl):
        raise ReadOnlyViolation(_SPL_UNCLOSED_COMMENT)
    first_mark = _SPL_FIRST_MARK.search(spl)
    early_end = first_mark.start() if first_mark else len(spl)
    # Before the first `|` or `[`, a command can start only after a backtick
    # that may end a macro or a comment: any but the first in the text.
    first_backtick = spl.find("`", 0, early_end)
    begin = first_backtick + 1 if first_backtick >= 0 else early_end
    joined, fit_end, into_end = None, None, None
    if begin < len(spl):
        match = _SPL_COMMAND.search(spl, begin)
        if match:
            raise _spl_command_refusal(match)
        joined, fit_end, into_end = _spl_split_words(spl, begin, first_backtick)
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


def _spl_command_refusal(match: re.Match) -> ReadOnlyViolation:
    """The refusal for a listed name where a command can start, quoting the
    text as written from the mark to the end of the name (its two ends, when
    it is long)."""
    mark, name, shown = match.group("mark"), match.group("name"), match.group()
    if len(shown) > _SPL_SHOWN_MAX:
        shown = shown[:20] + " ... " + shown[25 - _SPL_SHOWN_MAX:]
    comment = (
        "If it is the first word of an inline comment, start the comment with another word "
        "(for example ```note: ...```) or drop it. "
        if mark == "`" else ""
    )
    return ReadOnlyViolation(
        f"SPL command '{name}' is not allowed: only searches that read data can run. "
        'It is refused wherever Splunk could start it: after "|", after "[", or after a '
        "backtick that can end an inline comment or a macro, with any whitespace, quotes "
        f"or backslashes in between. Found: {shown}. {comment}"
        f"If this is literal text inside a quoted value (such as a bracketed log tag) or a "
        f"comment, it is still refused; reword the comment, or search for the value with a "
        f'wildcard in place of the "{mark}", for example "*{name}" instead of "{shown}". '
        f'If a quoted value starts with "{name}", write it as two quoted strings joined '
        f'with "." in eval, for example "{name[:1]}"."{name[1:]}".'
    )


def _spl_split_words(
    spl: str, begin: int, first_backtick: int
) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Words Splunk could read, from `begin` on, once comments or macros
    inside them are removed with nothing and quotes or backslashes inside
    them dropped: a refused name (where a command can start) or `savedsearch`
    (anywhere, before a colon), else None; where the first `fit` read that
    way ends; and where the last `into` read that way ends.

    The pieces that join are words glued to a run of backticks, quotes or
    backslashes: the first piece has a run on its right and, for a command
    name, starts where a command can start (after `|`, `[` or a backtick that
    may end a macro, then optional whitespace, quotes or backslashes); each
    later piece has a run on its left, and every piece but the last also one
    on its right. A piece continues the words that end at the piece before it
    when the same run joins them (dropped quotes or backslashes, or a removed
    comment or macro). A removed comment or macro can also end at any later
    backtick, so a word whose run on the right holds a backtick stays live
    for every later piece whose run on the left holds one, skipping what is
    between.

    A letter that case-folds to ASCII reads as that letter, and the name may
    also end at it, or a word start right after it.

    One pass over the glued words. The live words are prefixes of the checked
    words, so there are a bounded number of them, and for each piece only
    the live words it can continue are looked up; a piece seen again with no
    new word live across a comment or macro since its last look-up is not
    looked up again, and each glue run is scanned at most twice. The work
    per word is a constant. Looking back from a piece for the mark before it
    stops at the last place looked back from, so each character is looked
    back over at most once.
    """
    if not any(glue in spl for glue in _SPL_GLUE):
        return None, None, None
    reversed_spl = spl[::-1]
    size = len(spl)
    # Words live across a removed comment or macro, and a count bumped each
    # time one is added.
    span_command: set[str] = set()
    span_anywhere: set[str] = set()
    version = 0
    # Per piece as written: the version its continuations of those words were
    # looked up at, and what they were.
    seen: dict[str, tuple[int, Optional[tuple]]] = {}
    # Per piece as written and the words it continues next to it: what they
    # were.
    seen_next: dict[tuple, Optional[tuple]] = {}
    # Words ending at the last piece glued on its right, where its glue run
    # ends, and whether that run holds a backtick.
    last_command, last_anywhere = _NO_PREFIXES, _NO_PREFIXES
    last_end = -1
    last_tick = False
    fit_end: Optional[int] = None
    into_end: Optional[int] = None
    colon_tick: list[int] = []

    def dataset_follows(at: int) -> bool:
        if _SPL_GAP_THEN_COLON.match(spl, at):
            return True
        tick = _SPL_GAP_THEN_BACKTICK.match(spl, at)
        if not tick:
            return False
        if not colon_tick:
            last = _SPL_LAST_BACKTICK_THEN_COLON.search(reversed_spl)
            colon_tick.append(size - last.end() if last else -1)
        return colon_tick[0] >= tick.end() - 1

    # The last place looked back from, and whether a command can start there.
    # A later place with only the gap back to it has the same answer.
    asked = [0, False]

    def can_start(at: int) -> bool:
        """Whether a command can start at `at`: after `|`, `[` or a backtick
        (but the first one before the first `|` or `[`), then the gap."""
        last, answer = asked
        if at < last:
            last, answer = 0, False
        if at and spl[at - 1] in "|[`":
            # The mark is right before it (the common case, without a search).
            answer = at - 1 != first_backtick
        else:
            gap_end = _SPL_GAP_BEFORE.match(reversed_spl, size - at, size - last).end()
            if gap_end < size - last:
                mark = size - 1 - gap_end
                answer = spl[mark] in "|[`" and mark != first_backtick
        asked[0], asked[1] = at, answer
        return answer

    relevant, command_prefixes, anywhere_prefixes = (
        _SPL_RELEVANT_PIECES, _SPL_COMMAND_PREFIXES, _SPL_ANYWHERE_PREFIXES
    )
    only, continuations, folded = _SPL_ONLY, _spl_continuations, _spl_folded
    starts = _SPL_STARTS
    # Whether any word is live: when none is, a piece that cannot start one
    # changes nothing.
    live = False
    for match in _SPL_GLUED_WORD.finditer(spl, begin):
        raw, left_run, right, right_run = match.groups()
        glued_left = raw is not None
        if glued_left:
            run = left_run
        else:
            raw, run = right, right_run
        if raw.isascii():
            piece = raw.lower()
            if piece not in relevant or (not live and piece not in starts):
                last_end = -1
                live = bool(span_command or span_anywhere)
                continue
            fold = fold_last = None
        else:
            piece, fold, fold_last = folded(raw)
            if not live and piece not in starts and piece[fold_last + 1:] not in starts:
                last_end = -1
                continue
        start, end = match.span()
        ending_command = ending_anywhere = _NO_PREFIXES
        if glued_left:
            found = None
            adjacent = start == last_end
            if adjacent:
                left_tick = last_tick
            elif span_command or span_anywhere:
                if spl[start - 1] == "`":
                    left_tick = True
                else:
                    left_start = size - _SPL_GLUE_RUN.match(reversed_spl, size - start).end()
                    left_tick = spl.find("`", left_start, start) >= 0
            else:
                left_tick = False
            if left_tick:
                if span_command or span_anywhere:
                    cached = seen.get(raw)
                    if cached is not None and cached[0] == version:
                        found = cached[1]
                    else:
                        found = continuations(span_command, span_anywhere, piece, fold)
                        seen[raw] = (version, found)
            elif adjacent and (last_command or last_anywhere):
                key = (raw, last_command, last_anywhere)
                if key in seen_next:
                    found = seen_next[key]
                else:
                    found = seen_next[key] = continuations(last_command, last_anywhere, piece, fold)
            if found is not None:
                refused, fit_at, into_at, dataset_at, ending_command, ending_anywhere = found
                if refused:
                    return refused, fit_end, into_end
                if fit_at is not None and fit_end is None:
                    fit_end = start + fit_at
                if into_at is not None:
                    into_end = start + into_at
                if dataset_at and any(dataset_follows(start + at) for at in dataset_at):
                    return "savedsearch", fit_end, into_end
        if run:
            tick = "`" in run
            # A word already live across a comment or macro needs no second
            # look when this run holds a backtick: the next piece continues
            # the live words then.
            if piece in command_prefixes and not (tick and piece in span_command) and can_start(start):
                ending_command = ending_command | only[piece] if ending_command else only[piece]
            if piece in anywhere_prefixes and not (tick and piece in span_anywhere):
                ending_anywhere = ending_anywhere | only[piece] if ending_anywhere else only[piece]
            if fold is not None:
                tail = piece[fold_last + 1:]
                if tail in command_prefixes and can_start(start + fold_last + 1):
                    ending_command = ending_command | only[tail]
                if tail in anywhere_prefixes:
                    ending_anywhere = ending_anywhere | only[tail]
            if tick and not (ending_command <= span_command and ending_anywhere <= span_anywhere):
                span_command |= ending_command
                span_anywhere |= ending_anywhere
                version += 1
            last_command, last_anywhere, last_end, last_tick = (
                ending_command, ending_anywhere, end, tick
            )
            live = bool(span_command or span_anywhere or ending_command or ending_anywhere)
        else:
            live = bool(span_command or span_anywhere)
    return None, fit_end, into_end


def _spl_folded(raw: str) -> tuple[str, int, int]:
    """A piece holding a letter that case-folds to ASCII: the piece read with
    that letter, and where the first and the last such letter are."""
    marked = raw.translate(_SPL_FOLD_MARK)
    return raw.translate(_SPL_FOLD).lower(), marked.find(" "), marked.rfind(" ")


def _spl_continuations(from_command, from_anywhere, piece: str, fold: Optional[int]) -> Optional[tuple]:
    """What a piece makes of the live words it continues: a refused name it
    completes (else None); where, from the piece's start, a `fit` or an
    `into` it completes ends, and where a dataset word it completes ends;
    and the words it leaves live (prefixes of a checked word). None when it
    makes nothing of them.

    The piece is read whole and, when it holds a letter that case-folds to
    ASCII (`fold` is where the first one is), also as ending right before
    that letter."""
    refused = fit_at = into_at = None
    dataset_at: list[int] = []
    ending_command: set[str] = set()
    ending_anywhere: set[str] = set()
    readings = [(piece, True)]
    if fold is not None:
        readings.append((piece[:fold], False))
    for text, whole in readings:
        for prefix in from_command & _SPL_COMMAND_PIECES.get(text, _NO_PREFIXES):
            joined = prefix + text
            if joined == "fit":
                fit_at = len(text) if fit_at is None else min(fit_at, len(text))
            elif joined in _SPL_JOINED_COMMANDS:
                refused = joined
            elif whole:
                ending_command.add(joined)
        for prefix in from_anywhere & _SPL_ANYWHERE_PIECES.get(text, _NO_PREFIXES):
            joined = prefix + text
            if joined == "into":
                into_at = len(text) if into_at is None else max(into_at, len(text))
            elif joined in _SPL_JOINED_ANYWHERE:
                dataset_at.append(len(text))
            elif whole:
                ending_anywhere.add(joined)
    if not (refused or fit_at is not None or into_at is not None or dataset_at
            or ending_command or ending_anywhere):
        return None
    return refused, fit_at, into_at, tuple(dataset_at), frozenset(ending_command), frozenset(ending_anywhere)
