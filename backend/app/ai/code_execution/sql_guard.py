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
    Postgres and Redshift text is first read by the Postgres lexer
    (`pg_lexer`), as the server reads it: one statement only, and the
    lexer's canonical text is what is checked and what is sent. On that text
    it also refuses the TABLE shorthand and the names of functions
    (`DENIED_FUNCTIONS`) that run SQL held in a string, reach the server's
    files, or change the session or the server, wherever such a name
    stands, and checks the statement an EXPLAIN explains like unparsed text.
    Text in the other dialects is first read for what sqlglot's tokenizer
    and the engine read differently (`check_engine_reading`): comments one
    of them runs as code, comment, string and name boundaries they place
    differently, spaces, line breaks and names outside ASCII; such text is
    refused before sqlglot parses it.
  * **DuckDB connections** (DuckDBClient): DuckDB's own parser reads the
    text, which must be one SELECT or one of a short list of reads (SHOW,
    DESCRIBE, a few PRAGMAs, EXPLAIN of a SELECT; `duckdb_read`), calling
    no file reader, settings or catalog function, and naming only the
    connection's own tables.
  * **Splunk**: SPL is not SQL but can write; its writing commands are refused.
  * **SQL with no sqlglot dialect** (Db2 LUW, z/OS and IBM i, Vertica,
    Sybase, SAP HANA, Pinot): one statement, a read verb (SELECT / WITH /
    VALUES) and no write word, generic or the engine's, with literals and
    comments read by the engine's rules.
  * **Everything else** is not checked here. A connector is either declared
    exempt (`client_profiles.EXEMPT_CLIENTS`: each entry names why, with the
    file:line that shows it: its query surface cannot write or run a
    free-form statement, it checks every statement where it sends it, or,
    for one remote agent, an accepted decision to trust it), or it is
    `unknown` (`client_profiles.classify`). The wrapper refuses an `unknown`
    connector's call when any parameter of its `execute_query` that can be
    passed by position receives a value other than None, whatever its type
    and however it was passed (by position or by name), and when the call
    does not fit that signature; it forwards only calls whose values all go
    to keyword-only parameters or `**kwargs`, unchecked: a statement passed
    that way to an `unknown` connector is not refused.
    Connections are expected to use read-only credentials regardless.

A NUL character is refused in every checked statement: some drivers and
engines stop reading there while the checks read on.

This is defence in depth, not a substitute for read-only database accounts:
a SELECT can still call a function with side effects.
"""

import bisect
import functools
import logging
import re
import unicodedata
from dataclasses import dataclass
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
# these are the backslash readings taken. Athena runs Hive DDL. (Redshift
# text is read by the Postgres lexer.)
_BACKSLASH_ESCAPE_DIALECTS = frozenset({
    "mysql", "bigquery", "clickhouse", "snowflake", "hive", "spark", "spark2", "databricks", "duckdb",
    "athena",
})
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
# string, reach another connection or the server's files, or change the
# session or the server beyond the read, often in ways a rollback does not
# undo. Transaction-scoped advisory locks end with the read and stay allowed.
# The Postgres engine test lists every volatile catalog function and fails on
# one that is neither matched here nor reviewed as fine in a read.
_PG_DENIED_FUNCTION = re.compile(
    # SQL in a string, other connections.
    r"ts_stat|ts_rewrite|query_to_xml\w*|table_to_xml\w*|cursor_to_xml\w*|dblink\w*"
    # Session locks and settings, sequences, notifications, other backends.
    r"|pg_advisory_lock\w*|pg_try_advisory_lock\w*|pg_advisory_unlock\w*"
    r"|set_config|setval|nextval|pg_notify|pg_cancel_backend|pg_terminate_backend|pg_reload_conf"
    # Replication slots and origins, logical decoding.
    r"|pg_(?:create|copy)_\w*replication_slot|pg_drop_replication_slot|pg_replication_slot_advance"
    r"|pg_logical_\w*|pg_replication_origin_(?!status\Z)\w*"
    # Statistics, WAL, backup, recovery, logs, collations.
    r"|pg_stat_reset\w*|pg_stat_statements_reset"
    r"|pg_switch_(?:wal|xlog)|pg_create_restore_point|pg_backup_\w*|pg_(?:start|stop)_backup"
    r"|pg_promote|pg_(?:wal|xlog)_replay_\w*"
    r"|pg_rotate_logfile\w*|pg_log_backend_memory_contexts|pg_import_system_collations"
    # Index maintenance, upgrade and catalog internals.
    r"|brin_summarize_\w*|brin_desummarize_range|gin_clean_pending_list"
    r"|binary_upgrade_\w*|pg_nextoid|pg_stop_making_pinned_objects|pg_extension_config_dump"
    # Server files (adminpack included) and large objects. Exact large-object
    # names, so a column such as lo_price still reads.
    r"|pg_file_(?!settings\Z)\w*|pg_logdir_ls|pg_read_\w*|pg_ls_\w*|pg_stat_file"
    r"|lo_(?:close|creat|create|export|from_bytea|get|import|lseek(?:64)?|open|put|tell(?:64)?"
    r"|truncate(?:64)?|unlink)|loread|lowrite"
    # Newer servers: planner statistics restore and clear, standby snapshots,
    # replication slot sync.
    r"|pg_(?:clear|restore)_(?:relation|attribute)_stats|pg_log_standby_snapshot|pg_sync_replication_slots"
)
# DuckDB: functions that read files, settings, secrets, the environment or
# the catalog, or run SQL held in a string. The one list every DuckDB
# connection's rule applies (`duckdb_guard.check_duckdb_reads`). DuckDB's
# own parser tells a call from a column, so this is matched on the calls it
# reports only. The built-in macros here select from catalog table
# functions in their definition.
_DUCKDB_DENIED_FUNCTION = re.compile(
    r"current_setting|getenv|duckdb_settings|duckdb_secrets|duckdb_extensions|query|query_table|glob"
    r"|format_type|get_block_size|pg_get_constraintdef|pg_get_viewdef|(?s:read_.*|.*_scan)"
)
DENIED_FUNCTIONS: dict[str, re.Pattern] = {
    "postgres": _PG_DENIED_FUNCTION, "redshift": _PG_DENIED_FUNCTION, "duckdb": _DUCKDB_DENIED_FUNCTION,
}
# A name in code text, quoted or not, or a number (so a name glued after a
# number is read as its own name, as by `check_unparsed`).
_CODE_NAME = re.compile(r'"(?P<quoted>(?:[^"]|"")*)"|' + _UNPARSED_WORD.pattern)
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
# How `check_unparsed` starts a refusal for a write word (as opposed to a
# leading verb or a second statement).
_FORBIDDEN_OPERATION = "Query contains forbidden operation"

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


# SQL with no sqlglot dialect (guard profile kind "nonparsing_sql", by
# engine): an allowlist on the leading verb, one statement and no write word,
# on the text with its literals and comments emptied the way the engine
# reads them.
_NONPARSING_READ_VERBS = frozenset({"SELECT", "WITH", "VALUES", "("})
# Db2-specific statements and routines that change state. QCMDEXC runs an IBM i
# CL command and is callable as a scalar function inside a SELECT; ADMIN_CMD
# runs Db2 LUW administrative commands. `SELECT ... FROM FINAL TABLE (INSERT
# ...)` (a data-change table reference) is caught by the generic write words.
_DB2_WRITE_KEYWORDS = re.compile(
    r"\b(LOCK|REFRESH|DECLARE|RENAME|COMMENT|LABEL|QCMDEXC|ADMIN_CMD|SYSPROC|EXPORT|IMPORT"
    r"|RUNSTATS|REORG|TRANSFER|SAVEPOINT|ROLLBACK|COMMIT|RELEASE|CONNECT|DISCONNECT)\b",
    re.IGNORECASE,
)
_NONPARSING_WRITE_WORDS = {"db2": _DB2_WRITE_KEYWORDS}
# What each engine's literal and comment rules add to standard quoting
# ('...' with '' inside, "..." names, -- and /* */ comments). Where it is
# not known whether the engine reads a form (a server option, a string
# prefix), the text is read both ways and must pass both.
# A backslash escape in a string: Vertica's E'...'; SQL Anywhere's strings
# (\\, \n, \x41).
_NONPARSING_BACKSLASH = frozenset({"vertica", "sybase"})
# `[...]` as a quoted name: SQL Anywhere.
_NONPARSING_BRACKETS = frozenset({"sybase"})
# `//` as a line comment (refused outside strings): Pinot's Calcite parser,
# SQL Anywhere.
_NONPARSING_SLASH_COMMENTS = frozenset({"pinot", "sybase"})
# `%` as a line comment: SQL Anywhere when its percent_as_comment option is
# on.
_NONPARSING_PERCENT_COMMENTS = frozenset({"sybase"})
# Backtick-quoted names: perhaps Pinot and SQL Anywhere. To the others a
# backtick is code.
_NONPARSING_BACKTICKS = frozenset({"pinot", "sybase"})
# Dollar-quoted strings ($$...$$), refused outside strings: Vertica.
_NONPARSING_DOLLAR_QUOTES = frozenset({"vertica"})
# A dollar-quote tag, and a run of name characters a tag may stand in.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_\x80-\U0010FFFF][A-Za-z0-9_\x80-\U0010FFFF]*\$")
_NAME_CHARS_RUN = re.compile(r"[A-Za-z0-9_$\x80-\U0010FFFF]+")


def _opens_dollar_quote(code: str) -> bool:
    """Whether `code` may open a dollar-quoted string: `$$` anywhere, or a
    `$tag$` other than inside a name. A run of name characters that starts
    with a letter, `_` or a character outside ASCII is one name to a
    Postgres-style lexer; in a run that starts with a digit (a number, as
    in `1$x$`) or with `$`, a tag may open a string."""
    if "$$" in code:
        return True
    for run in _NAME_CHARS_RUN.finditer(code):
        first = run.group()[0]
        if ("0" <= first <= "9" or first == "$") and _DOLLAR_TAG.search(run.group()):
            return True
    return False


def _nonparsing_readings(sql: str, engine: Optional[str]) -> list[tuple[str, str]]:
    """`sql` with its literals and comments emptied, in each way `engine`
    may read it, each with the reading's name."""
    out = []
    for backslash in (False, True) if engine in _NONPARSING_BACKSLASH else (False,):
        for percent in (False, True) if engine in _NONPARSING_PERCENT_COMMENTS else (False,):
            for backticks in (False, True) if engine in _NONPARSING_BACKTICKS else (False,):
                parts = [name for name, on in (("backslash escapes", backslash), ("% line comments", percent),
                                               ("backtick-quoted names", backticks)) if on]
                name = "with " + " and ".join(parts) if parts else "with standard quoting"
                out.append((name, _generic_reading(sql, False, backslash, brackets=engine in _NONPARSING_BRACKETS,
                                                   backticks=backticks, percent_comments=percent)))
    return out


def _check_nonparsing(sql: str, engine: Optional[str]) -> None:
    """One read statement, as the engine's literal and comment rules read
    `sql` (each reading must pass). A comment opened inside another is
    refused: Db2 and Sybase nest comments, the readings here do not. So is
    a NUL, and a line break other than a newline anywhere: the readings end
    a line comment at a newline only."""
    if _NUL in sql:
        raise ReadOnlyViolation("The query holds a NUL character, after which the source would not read it. Remove it.")
    if _OTHER_LINE_BREAK.search(sql):
        raise ReadOnlyViolation(
            "The query has a line break other than a newline (a lone carriage return, a vertical tab, a form feed "
            "or a Unicode line separator), which this source may end a comment at. Use plain newlines."
        )
    for kind, start, end in _standard_spans(sql):
        if kind == "comment" and sql.startswith("/*", start) and "/*" in sql[start + 2:end]:
            raise _comment_inside_comment()
    readings = _nonparsing_readings(sql, engine)
    for _reading, code_only in readings:
        if engine in _NONPARSING_SLASH_COMMENTS and "//" in code_only:
            raise ReadOnlyViolation(
                "The query has '//' outside a string, which this source reads as the start of a comment. "
                "Remove it, or write the division with a space between the slashes."
            )
        if engine in _NONPARSING_DOLLAR_QUOTES and _opens_dollar_quote(code_only):
            raise ReadOnlyViolation(
                "The query has a dollar-quoted string ($$...$$), which the query checks do not read. "
                "Write a standard string, doubling each quote inside it ('it''s')."
            )
    for reading, code_only in readings:
        try:
            check_unparsed(code_only, read_verbs=_NONPARSING_READ_VERBS,
                           extra_write_words=_NONPARSING_WRITE_WORDS.get(engine))
        except ReadOnlyViolation as e:
            raise ReadOnlyViolation(f"{e} (This is how the text reads {reading}.)") from e


def dialect_for_client(client) -> Optional[str]:
    profile = guard_profile(client)
    return profile.dialect if profile is not None else None


def ensure_read_only(client, query) -> Union[str, dict]:
    """Return exactly what `client` must be sent for `query`, or raise
    ReadOnlyViolation if it would modify the source behind `client`.

    A `str` comes back as the text to send: for Postgres and Redshift the
    lexer's canonical text (comments dropped, every string written as a
    standard literal), the text that was checked; for other clients the
    input. A `str` holding a NUL is refused for every guarded client. A `dict` envelope
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
    if _NUL in query:
        raise ReadOnlyViolation(
            "The query holds a NUL character, after which the source would not read it. Remove it."
        )
    if not query.strip():
        return query
    if profile.kind == "spl":
        check_spl(query)
        _check_spl_envelope(query, checked=query)
    elif profile.kind == "nonparsing_sql":
        _check_nonparsing(query, profile.engine)
    elif profile.kind == "cypher":
        from app.data_sources.clients.cypher_guard import CypherNotAllowed, check_cypher

        try:
            check_cypher(query, allow_gds=client.allow_gds)
        except CypherNotAllowed as e:
            raise ReadOnlyViolation(str(e)) from e
    elif profile.kind == "sql":
        if profile.dialect in _LEXED_DIALECTS:
            return _check_postgres(query, profile.dialect)
        if profile.dialect == "duckdb":
            _check_duckdb_client(client, query)
        else:
            check_sql(query, profile.dialect, unknown_lexer=profile.unknown_lexer)
    else:
        raise ReadOnlyViolation(_UNCHECKED_PROFILE)
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


# DuckDB connections (DuckDBClient). The statement is read with DuckDB's own
# parser (`duckdb_guard.inspect_duckdb_sql`), which serializes one SELECT
# only; the reads below are recognised first, on DuckDB's tokens.
_DUCKDB_LISTING_PRAGMAS = ("show_tables", "show_tables_expanded", "database_list", "version", "platform")
_DUCKDB_SPACE = frozenset(" \t\n\r\f\v")
# A word of the reads list. `$`, which DuckDB also reads inside a name, is
# left out: a name holding one is read as a SELECT instead.
_DUCKDB_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A table named in a string (`PRAGMA table_info('main.orders')`).
_DUCKDB_STRING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}")
# What no part of a table name on a database-file connection may hold: a
# path, a URL or a file name DuckDB would read in place of a table.
_DUCKDB_PATH_CHARS = frozenset("./" + chr(92) + ":*?[]{}")


@dataclass(frozen=True)
class DuckdbRead:
    """One of the reads a DuckDB connection takes besides a plain SELECT.

    `verb` is the command (SHOW, PRAGMA, DESCRIBE, DESC or EXPLAIN); `name`
    the table it names, as folded parts (one to three), if any;
    `select` the text of the SELECT it describes or explains, if any.
    `scoped` is True when a table selection checks the table or the
    SELECT's tables, False when a table selection refuses the read."""

    verb: str
    name: Optional[tuple[str, ...]] = None
    select: Optional[str] = None
    scoped: bool = False


def _duckdb_tokens(sql: str):
    """The tokens of `sql` as DuckDB reads them, for the few kinds the reads
    list is made of: (kind, value, end) for a word, a "quoted" name, a
    'string' or one of `( ) . ;`. Whitespace and comments are skipped (block
    comments nest and a line comment ends at a CR or an LF, as in DuckDB).
    Anything else, an unterminated comment or quote included, yields one
    ("other", "", position) and ends the tokens."""
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in _DUCKDB_SPACE:
            i += 1
        elif sql.startswith("--", i):
            while i < n and sql[i] not in "\r\n":
                i += 1
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                yield ("other", "", i)
                return
        elif word := _DUCKDB_WORD.match(sql, i):
            i = word.end()
            yield ("word", word.group(), i)
        elif c in "\"'":
            end = sql.find(c, i + 1)
            if end < 0:
                yield ("other", "", i)
                return
            yield ("quoted" if c == '"' else "string", sql[i + 1:end], end + 1)
            i = end + 1
        elif c in "().;":
            i += 1
            yield ("punct", c, i)
        else:
            yield ("other", "", i)
            return


def _duckdb_name_at(tokens: list, i: int) -> Optional[tuple[tuple[str, ...], int]]:
    """A plain or dotted name of one to three parts (words or non-empty
    "quoted" parts) at tokens[i]: (parts folded as DuckDB folds them,
    index after it)."""
    from app.ai.code_execution.duckdb_guard import ascii_lower

    parts: list[str] = []
    while i < len(tokens) and len(parts) < 3:
        kind, value, _end = tokens[i]
        if kind not in ("word", "quoted") or not value:
            return None
        parts.append(ascii_lower(value))
        i += 1
        if i < len(tokens) and tokens[i][:2] == ("punct", ".") and len(parts) < 3:
            i += 1
            continue
        return tuple(parts), i
    return None


def duckdb_read(sql: str) -> Optional[DuckdbRead]:
    """Which of a DuckDB connection's reads `sql` is, or None when it is
    none of them (it must then be one SELECT).

    The reads: SHOW TABLES, SHOW ALL TABLES, SHOW DATABASES and the PRAGMAs
    show_tables, show_tables_expanded, database_list, version and platform;
    SHOW <name>; DESCRIBE / DESC <name> and PRAGMA
    table_info(<name>) or table_info('<name>'); DESCRIBE <select> and
    EXPLAIN <select>, whose SELECT is the rest of the text after the
    command. Case-insensitive, with comments anywhere and one optional
    trailing `;`. Any other PRAGMA, an assignment included, raises
    ReadOnlyViolation."""
    tokens = list(_duckdb_tokens(sql))
    if not tokens or tokens[0][0] != "word":
        return None
    verb, after_verb = tokens[0][1].upper(), tokens[0][2]
    if verb == "EXPLAIN":
        return DuckdbRead("EXPLAIN", select=sql[after_verb:])
    if verb not in ("SHOW", "DESCRIBE", "DESC", "PRAGMA"):
        return None
    rest = tokens[1:]

    def ends_at(i: int) -> bool:
        return rest[i:] == [] or (len(rest) == i + 1 and rest[i][:2] == ("punct", ";"))

    def words(*expected: str) -> bool:
        return (len(rest) >= len(expected) and ends_at(len(expected))
                and all(kind == "word" and value.upper() == word
                        for (kind, value, _end), word in zip(rest, expected)))

    if verb == "SHOW" and (words("TABLES") or words("ALL", "TABLES") or words("DATABASES")):
        return DuckdbRead("SHOW")
    if verb in ("SHOW", "DESCRIBE", "DESC"):
        named = _duckdb_name_at(rest, 0)
        if named and ends_at(named[1]):
            if not _duckdb_describes_table(sql, named[0]):
                # A name DuckDB reads as a listing (tables, databases, ...).
                return DuckdbRead(verb)
            return DuckdbRead(verb, name=named[0], scoped=verb != "SHOW")
        return DuckdbRead("DESCRIBE", select=sql[after_verb:], scoped=True) if verb == "DESCRIBE" else None
    pragma = rest[0][1].lower() if rest and rest[0][0] == "word" else ""
    if pragma in _DUCKDB_LISTING_PRAGMAS and ends_at(1):
        return DuckdbRead("PRAGMA")
    if pragma == "table_info" and len(rest) > 2 and rest[1][:2] == ("punct", "("):
        if rest[2][0] == "string":
            named = ((tuple(rest[2][1].lower().split(".")), 3)
                     if _DUCKDB_STRING_NAME.fullmatch(rest[2][1]) else None)
        else:
            named = _duckdb_name_at(rest, 2)
        if named and named[1] < len(rest) and rest[named[1]][:2] == ("punct", ")") and ends_at(named[1] + 1):
            return DuckdbRead("PRAGMA", name=named[0], scoped=True)
    raise ReadOnlyViolation(
        "This PRAGMA is not allowed on this connection. Allowed: PRAGMA "
        + ", ".join(_DUCKDB_LISTING_PRAGMAS) + ", table_info(<table>). Only SELECT queries are allowed otherwise."
    )


def _duckdb_table(name: tuple[str, ...]) -> tuple[str, str, str]:
    """(catalog, schema, table) of a name of one to three parts."""
    return ("", "", "", *name)[-3:]


def _duckdb_describes_table(sql: str, name: tuple[str, ...]) -> bool:
    """Whether DuckDB reads `sql`, a SHOW / DESCRIBE / DESC of `name`, as
    describing the table `name` (True) or as listing tables, databases or
    schemas, as it does for a few single names (False). Its parser decides;
    a reading that names another table is refused."""
    from app.ai.code_execution.duckdb_guard import inspect_duckdb_sql

    tables = inspect_duckdb_sql(sql).tables
    if not tables:
        return False
    if tables != {_duckdb_table(name)}:
        raise ReadOnlyViolation("The statement could not be read as describing one table. "
                                "Only SELECT queries are allowed otherwise.")
    return True


def _check_duckdb_client(client, sql: str) -> None:
    """A DuckDB connection's rule: one SELECT (or one of the reads of
    `duckdb_read`) as DuckDB reads it, calling no denied function and no
    table function but a few generators, and naming only the connection's
    own tables (`_check_duckdb_table`), with or without a table selection."""
    check_engine_reading(sql, "duckdb")
    read = duckdb_read(sql)
    if read is None:
        _check_duckdb_select(client, sql)
    elif read.name is not None:
        _check_duckdb_table(client, _duckdb_table(read.name))
    elif read.select is not None:
        _check_duckdb_select(client, read.select, after=read.verb)


def _check_duckdb_select(client, sql: str, *, after: Optional[str] = None) -> None:
    """`sql` is one SELECT DuckDB's parser reads, with the rule every DuckDB
    connection applies (`duckdb_guard.check_duckdb_reads`) and this
    connection's tables (`_check_duckdb_table`). `after`: the command
    (DESCRIBE, EXPLAIN) `sql` follows. A fixable error (text DuckDB cannot
    parse, a CTE name that is not plain) is raised as it is
    (`DuckdbQueryError`), unless the text follows a command."""
    from app.ai.code_execution.duckdb_guard import DuckdbQueryError, check_duckdb_reads, inspect_duckdb_sql

    try:
        check_duckdb_reads(
            inspect_duckdb_sql(sql), lambda table: _check_duckdb_table(client, table),
            violation=ReadOnlyViolation,
            describe_error=ReadOnlyViolation(
                "DESCRIBE and SHOW are allowed only as the whole statement, on a table name. SUMMARIZE is "
                "not available on this connection; a SELECT with aggregates such as count, min, max and avg "
                "gives the same figures. Only SELECT queries are allowed otherwise."),
        )
    except DuckdbQueryError as e:
        if after is None:
            raise
        raise ReadOnlyViolation(f"{after} takes a table name or a SELECT on this connection. {e}") from e


def _check_duckdb_table(client, table: tuple[str, str, str]) -> None:
    """A table a DuckDB connection may name, given as folded parts
    (`duckdb_guard.ascii_lower`). Over URIs: one of the views it registers
    (`view_names()`), in the default schema and catalog. On a database file:
    a name whose parts hold no path, URL or file-name character, and that
    is not one of DuckDB's built-in catalog views. An Iceberg connection
    names its tables the database-file way: the client registers each
    catalog table by name in an in-memory DuckDB before running the query."""
    from app.ai.code_execution.duckdb_guard import ascii_lower, names_catalog, tables_line
    from app.data_sources.clients.iceberg_client import IcebergClient

    catalog, schema, name = table
    shown = ".".join(part for part in table if part)
    if isinstance(client, IcebergClient) or client._is_direct_db_connection():
        if not name or any(c in _DUCKDB_PATH_CHARS for part in table for c in part):
            raise ReadOnlyViolation(
                f"Table {shown} is not allowed on this connection: query its tables by their names, "
                "not by a file path or URL."
            )
        if names_catalog(table):
            raise ReadOnlyViolation(
                f"Table {shown} is not allowed on this connection: DuckDB's own catalog views cannot be "
                "queried here. Query the tables of this connection by name."
            )
        return
    views = client.view_names()
    if name in {ascii_lower(v) for v in views} and schema in ("", "main") and catalog in ("", "memory"):
        return
    # The tables list comes first: with data hidden, the message is cut
    # where the name the agent wrote holds a quote, ": " or " (".
    raise ReadOnlyViolation(f"{tables_line(sorted(views, key=str.lower))} "
                            f"Table {shown} is not one of the tables of this connection.")


def check_sql(sql: str, dialect: str, *, unknown_lexer: bool = False) -> None:
    """Refuse `sql` unless it is one read in `dialect`. `unknown_lexer`:
    the engine is checked as `dialect` but its own lexer is not known
    (`check_engine_reading`)."""
    if dialect in _LEXED_DIALECTS:
        _check_postgres(sql, dialect)
        return

    check_engine_reading(sql, dialect, unknown_lexer=unknown_lexer)
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


# Marks some SQL readers take as the start of a comment and others do not.
_COMMENT_MARKS = re.compile(r"--|/\*|\*/|#|//")


def check_one_select(sql, dialect: str) -> None:
    """Refuse `sql` unless it is one SELECT (WITH ... SELECT, a set operation
    of SELECTs) as `dialect` reads it. For a client that checks the SQL it
    sends at its own dispatch point and whose engine's own reader is not
    known, so nothing is guessed at: text sqlglot cannot parse is refused
    (no unparsed fallback, unlike `check_sql`), and so is a backslash or a
    comment mark (`--`, `/*`, `*/`, `#`, `//`) outside a string or a quoted
    name, and a name in [ ] or ` ` holding its own closing mark (sqlglot
    reads `]]` and a doubled backtick as the mark itself; SQLite ends a
    bracketed name at the first `]`, and the OpenSearch SQL docs do not say).
    Raises ReadOnlyViolation."""
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    if type(sql) is not str:
        raise ReadOnlyViolation(f"The query must be text, not {type(sql).__name__}.")
    check_engine_reading(sql, dialect, unknown_lexer=True)
    tokens = Dialect.get_or_raise(dialect).tokenize(sql)
    for token in tokens:
        close = {"[": "]", "`": "`"}.get(sql[token.start]) if token.token_type == TokenType.IDENTIFIER else None
        if close and close in sql[token.start + 1:token.end]:
            raise ReadOnlyViolation(
                f"The query has a name in {sql[token.start]}{close} holding '{close}', which this source may read "
                "as the end of the name. Remove it from the name."
            )
    masked = _masked(sql, _literal_spans(sql, tokens))
    mark = _COMMENT_MARKS.search(masked)
    if mark:
        raise ReadOnlyViolation(
            f"The query has '{mark.group()}' outside a string or a quoted name, which this source may read as a "
            "comment where the query checks do not, or the other way round. Remove it."
        )
    try:
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except Exception as e:
        raise ReadOnlyViolation("The query could not be read as one SELECT statement, so it was not run.") from e
    if len(statements) != 1:
        raise ReadOnlyViolation(f"Only a single SELECT statement is allowed; got {len(statements)} statements.")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.SetOperation, exp.Subquery)):
        raise ReadOnlyViolation(f"Statement '{stmt.key.upper()}' is not allowed. Only SELECT queries are allowed.")
    _refuse_writes(stmt, dialect)


def _check_postgres(sql: str, dialect: str = "postgres") -> str:
    """Check Postgres (or Redshift) text as the server reads it; return the
    canonical text, which is what must be sent."""
    from app.ai.code_execution.pg_lexer import lex_postgres

    lex = lex_postgres(sql, mode=dialect)
    if lex.statements != 1:
        raise ReadOnlyViolation(
            f"Only a single read-only statement is allowed; got {lex.statements} statements."
        )
    _check_postgres_code(lex.code_only, dialect)
    _check_parsed(lex.text, dialect,
                  lambda: check_unparsed(lex.code_only, read_verbs=_UNPARSED_READ_VERBS))
    return lex.text


def _check_postgres_code(code_only: str, dialect: str = "postgres") -> None:
    """The Postgres (and Redshift) rules read on the lexer's code (literal
    bodies emptied), parsed or not: no TABLE shorthand, no denied function,
    and the statement an EXPLAIN explains is itself a read."""
    words = _QUOTED_NAME.sub(" ", code_only)
    for word in _UNPARSED_WORD.finditer(words):
        if (word.group("name") or "").upper() == "TABLE":
            raise ReadOnlyViolation(
                "The TABLE shorthand is not allowed; write SELECT * FROM the table instead. "
                "Only SELECT queries are allowed."
            )
    _check_denied_calls(code_only, dialect)
    leading = _LEADING_WORD.match(words)
    if leading and leading.group(1).upper() == "EXPLAIN":
        head = _EXPLAIN_HEAD.match(words)
        if head is None:
            raise ReadOnlyViolation("EXPLAIN must be followed by a SELECT statement.")
        try:
            check_unparsed(words[head.end():], read_verbs=_EXPLAINED_VERBS)
        except ReadOnlyViolation as e:
            if not str(e).startswith(_FORBIDDEN_OPERATION):
                raise
            raise ReadOnlyViolation(
                f"{e} In an EXPLAIN, a column or table name that is also an SQL word must be written "
                'in double quotes, for example "start".'
            ) from e


def _denied_function(name: str) -> ReadOnlyViolation:
    return ReadOnlyViolation(
        f"The function {name.upper()}() is not allowed in agent queries: it runs SQL held in a string, "
        "reaches another connection or the server's files, or changes the session or the server. "
        "The name may be refused even where it is not a call; rename such a column or alias. "
        "Only SELECT queries are allowed."
    )


def _check_denied_calls(code: str, dialect: str) -> None:
    """Refuse a name denied for `dialect` anywhere in code text (literal
    bodies emptied), quoted or not, with or without a schema before it, and
    whether or not '(' follows it."""
    denied = DENIED_FUNCTIONS.get(dialect)
    if denied is None:
        return
    for match in _CODE_NAME.finditer(code):
        quoted = match.group("quoted")
        name = quoted.replace('""', '"') if quoted is not None else match.group("name")
        if name is not None and denied.fullmatch(name.lower()):
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

    reads = (exp.Select, exp.SetOperation, exp.Subquery, exp.Values, exp.Describe, exp.Show, exp.Pragma)
    if not isinstance(stmt, reads):
        raise ReadOnlyViolation(
            f"Statement '{stmt.key.upper()}' is not allowed. Only SELECT queries are allowed."
        )
    if isinstance(stmt, exp.Pragma) and "=" in sql:
        raise ReadOnlyViolation("PRAGMA assignments are not allowed.")

    _refuse_writes(stmt, dialect)


def _refuse_writes(stmt, dialect: str) -> None:
    """Refuse a write anywhere in the parsed `stmt` (a DELETE inside a CTE,
    `SELECT ... INTO`, an opaque Command) and, in dialects that have them,
    calls of the `DENIED_FUNCTIONS`."""
    from sqlglot import exp

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
        if denied is not None and isinstance(node, (exp.Column, exp.Dot)):
            # A denied name as a column or a field is refused too.
            if denied.fullmatch(node.name.lower()):
                raise _denied_function(node.name)


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
                f"{_FORBIDDEN_OPERATION} '{name.upper()}'. Only SELECT queries are allowed."
            )
    match = extra_write_words.search(words) if extra_write_words is not None else None
    if match:
        raise ReadOnlyViolation(
            f"{_FORBIDDEN_OPERATION} '{match.group().upper()}'. Only SELECT queries are allowed."
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
        if kind in _LITERAL_TOKENS:
            pieces.append("''")
        elif kind == "IDENTIFIER":
            # Kept, quoted: the write-word scan skips quoted names, and the
            # deny-list scan reads them.
            pieces.append('"' + token.text.replace('"', '""') + '"')
        else:
            pieces.append(token.text)
    backslash = dialect in _BACKSLASH_ESCAPE_DIALECTS
    return [(f"as the {dialect} tokenizer reads it", " ".join(pieces))] + [
        (_READING_NAMES[(h, b)], _generic_reading(sql, h, b)) for h, b in _GENERIC_READINGS if backslash or not b
    ]


@functools.lru_cache(maxsize=None)
def _reading_starts(hash_comments: bool, brackets: bool, backticks: bool, percent_comments: bool) -> re.Pattern:
    marks = "'\"" + ("`" if backticks else "") + ("#" if hash_comments else "") + ("%" if percent_comments else "")
    return re.compile("[" + re.escape(marks) + (r"\[" if brackets else "") + r"]|--|/\*")


def _generic_reading(sql: str, hash_comments: bool, backslash: bool, brackets: Optional[bool] = None, *,
                     backticks: bool = True, percent_comments: bool = False) -> str:
    """`sql` with each literal, quoted name and comment replaced by a space:
    quotes doubled to escape them, `` `...` `` names unless `backticks` is
    off, plus `#` (or `%`) line comments and backslash escapes when asked
    (and `[...]` names when neither `#` nor backslashes are, or when
    `brackets` says so). In a `#` reading (MySQL's), `--` starts a comment
    only before whitespace or a control character. From anything left open,
    the rest is read as code. One pass."""
    if brackets is None:
        brackets = not (hash_comments or backslash)
    starts = _reading_starts(hash_comments, brackets, backticks, percent_comments)
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
        if mark in ("--", "#", "%"):
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


# ----------------------------------------------------------------------------
# Where sqlglot's tokenizer and the engine read the text differently
# ----------------------------------------------------------------------------

_NUL = chr(0)
# Read by the Postgres lexer (`pg_lexer`), which does this check itself.
_LEXED_DIALECTS = frozenset({"postgres", "redshift"})
_UNREADABLE = "The query could not be read as SQL (an unterminated string, quoted name or comment?). Fix it and retry."
_UNKNOWN_DIALECT = "The query checks do not know this source's SQL dialect, so the query was not run."
_UNCHECKED_PROFILE = "The query checks do not know how to check this source's queries, so the query was not run."
# Quotes ClickHouse reads as quotes and sqlglot reads as name characters:
# the curly single quotes around a string, the curly double quotes around a
# name.
_CLICKHOUSE_CURLY_QUOTES = re.compile("[" + chr(0x2018) + chr(0x2019) + chr(0x201C) + chr(0x201D) + "]")
# A word starting with a digit that MySQL and MariaDB read as a number and
# then a word, where sqlglot reads one name: one holding a dot (`1.5from`,
# `1.e5x`), or digits and an exponent (`1e5from`, `1e+5x`). Other digit-led
# words (`2023_sales`, `1st`, `1e`, `0x1g`) are one name to both.
_MYSQL_NUMBER_LED = re.compile(r"[0-9]*\.|[0-9]+[eE][0-9+-]")
# Tokens after which a dot, written right after them, separates two parts
# of a name (`t.5x`, `"t".5x`); after anything else MySQL, MariaDB and
# ClickHouse read a dot and a digit as a number (`-.5from`).
_BEFORE_A_NAME_DOT = frozenset({"VAR", "IDENTIFIER", "R_PAREN", "R_BRACKET", "NUMBER"})
# A currency symbol (Unicode category Sc).
_CURRENCY_SIGN = regex.compile(r"\p{Sc}")
# Line ends other than LF (a CR before an LF ends the line where the LF
# does): readers disagree on whether they end a line comment.
_OTHER_LINE_BREAK = re.compile("\r(?!\n)|[\x0b\x0c\x1c-\x1e\x85" + chr(0x2028) + chr(0x2029) + "]")
# The characters outside ASCII that `str.isspace()` takes for whitespace, as
# sqlglot does; an engine may read them as name characters.
_NON_ASCII_SPACES = frozenset(
    chr(c) for c in (0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000)
)
_NON_ASCII_SPACE = re.compile("[" + "".join(sorted(_NON_ASCII_SPACES)) + "]")
# The ones an engine was shown to read as whitespace, by dialect (pinned by
# the dialect tests): SQL Server 2019 every one; DuckDB 1.5.4 all but U+0085,
# U+1680, U+2028 and U+2029, which it reads as name characters.
_ENGINE_SPACES = {
    "tsql": _NON_ASCII_SPACES,
    "duckdb": _NON_ASCII_SPACES - {chr(0x85), chr(0x1680), chr(0x2028), chr(0x2029)},
}
# Dialects whose sqlglot tokenizer nests block comments while the engine
# may not (Calcite, behind Druid and Dremio, ends a block comment at its
# first `*/`; Teradata, ClickHouse, Fabric are not shown to nest them).
_UNNESTED_COMMENT_DIALECTS = frozenset({"druid", "dremio", "teradata", "clickhouse", "fabric"})
# A comment MySQL and MariaDB run as code (`/*!`, `/*M!`, and any other
# letter before the `!`), by its text after `/*`, and by its opening.
_EXECUTABLE_COMMENT_TEXT = re.compile(r"[A-Za-z]?!")
_EXECUTABLE_COMMENT_START = re.compile(r"/\*[A-Za-z]?!")
# A dollar-quote delimiter as DuckDB (Postgres's lexer) reads one.
_DOLLAR_DELIMITER = re.compile(r"\$(?:[A-Za-z_\x80-\U0010FFFF][A-Za-z0-9_\x80-\U0010FFFF]*)?\$")
_NAME_CLASS = "A-Za-z0-9_$\x80-\U0010FFFF"


def _comment_inside_comment() -> ReadOnlyViolation:
    return ReadOnlyViolation(
        "The query has a comment inside a comment (/* ... /* ... */), which this source may end at the first */. "
        "Remove the inner /*."
    )


def _masked(sql: str, spans) -> str:
    """`sql` with each (start, end) span written as spaces, so positions
    stay where they are."""
    out, at = [], 0
    for start, end in sorted(spans):
        if start < at:
            continue
        out.append(sql[at:start])
        out.append(" " * (end - start))
        at = end
    out.append(sql[at:])
    return "".join(out)


def _standard_spans(sql: str) -> list[tuple[str, int, int]]:
    """The literal spans of `sql` read with standard-SQL rules: ('string',
    ...) for '...' (a doubled quote stays inside), ('name', ...) for "...",
    ('comment', ...) for -- and /* */ (not nested). Each (kind, start, end
    exclusive); one left open runs to the end. One pass."""
    out = []
    i, n = 0, len(sql)
    starts = re.compile(r"['\"]|--|/\*")
    while True:
        m = starts.search(sql, i)
        if m is None:
            return out
        mark, start = m.group(), m.start()
        if mark == "--":
            end = sql.find("\n", start)
            end, kind = (n if end < 0 else end), "comment"
        elif mark == "/*":
            end = sql.find("*/", start + 2)
            end, kind = (n if end < 0 else end + 2), "comment"
        else:
            end = _quoted_end(sql, start, mark, False)
            end, kind = (n if end < 0 else end), ("string" if mark == "'" else "name")
        out.append((kind, start, end))
        i = end


def _refuse_athena_backslash(sql: str) -> None:
    """Approved rule: a backslash outside a "..." name is refused (Athena
    runs text on a Hive-based or a Trino-based engine, which read a
    backslash in a string differently)."""
    names = [(s, e) for kind, s, e in _standard_spans(sql) if kind == "name"]
    if "\\" in _masked(sql, names):
        raise ReadOnlyViolation(
            "Athena queries cannot hold a backslash outside a double-quoted name: its engines read a backslash "
            "in a string differently. Remove it, or match the character with a wildcard or a regex class."
        )


def _is_name_char(c: str) -> bool:
    return c.isalnum() or c in "_$#"


def _refuse_alternative_quotes(sql: str) -> None:
    """Oracle q'<d>...<d>' strings (and the n/N-prefixed ones): sqlglot's
    Oracle tokenizer does not read them, so it places the literal's end
    elsewhere than Oracle does."""
    for kind, start, _end in _standard_spans(sql):
        if kind != "string" or start == 0 or sql[start - 1] not in "qQ":
            continue
        prefix = start - 1
        if prefix > 0 and sql[prefix - 1] in "nN":
            prefix -= 1
        if prefix == 0 or not _is_name_char(sql[prefix - 1]):
            raise ReadOnlyViolation(
                "Oracle alternative-quote strings (q'[...]', nq'[...]' and similar) are not allowed. "
                "Write a standard string, doubling each quote inside it ('it''s')."
            )


@functools.lru_cache(maxsize=None)
def _name_run(spaces: frozenset[str]) -> re.Pattern:
    """A run of characters an engine may read as one name, ending at the
    characters it reads as whitespace (`spaces`)."""
    if not spaces:
        return re.compile("[" + _NAME_CLASS + "]+")
    return re.compile("(?:(?![" + "".join(sorted(spaces)) + "])[" + _NAME_CLASS + "])+")


def _gap_comments(sql: str, tokens, tokenizer_class) -> list[tuple[str, int, int]]:
    """The comments sqlglot's tokenizer read between its tokens, each
    (opening mark, start, end exclusive), read the way that tokenizer reads
    them (nested where it nests them)."""
    closes: dict[str, Optional[str]] = {}
    for comment in tokenizer_class.COMMENTS:
        if isinstance(comment, str):
            closes[comment] = None
        else:
            closes[comment[0]] = comment[1]
    marks = sorted(closes, key=len, reverse=True)
    nested = tokenizer_class.NESTED_COMMENTS
    gaps, at = [], 0
    for token in tokens:
        gaps.append((at, token.start))
        at = token.end + 1
    gaps.append((at, len(sql)))
    out = []
    for lo, hi in gaps:
        i = lo
        while i < hi:
            mark = next((m for m in marks if sql.startswith(m, i)), None)
            if mark is None:
                i += 1
                continue
            close, j = closes[mark], i + len(mark)
            if close is None:
                while j < hi and sql[j] not in "\n\r":
                    j += 1
            else:
                depth = 1
                while j < hi:
                    if sql.startswith(close, j):
                        depth, j = depth - 1, j + len(close)
                        if not depth:
                            break
                    elif nested and sql.startswith(mark, j):
                        depth, j = depth + 1, j + len(mark)
                    else:
                        j += 1
            out.append((mark, i, j))
            i = j
    return out


def check_engine_reading(sql: str, dialect: str, *, unknown_lexer: bool = False) -> None:
    """Refuse `sql` where sqlglot's `dialect` tokenizer and the engine read
    it differently, so the checks would vouch for a statement the engine
    does not run. Postgres and Redshift text is read by their lexer instead.
    `unknown_lexer`: the engine is checked as `dialect` but its own lexer is
    not known, so text whose reading turns on a backslash or on where a
    number ends is refused too. A missing or unknown dialect is refused.
    Raises ReadOnlyViolation."""
    if not isinstance(dialect, str) or not dialect:
        raise ReadOnlyViolation(_UNKNOWN_DIALECT)
    if dialect in _LEXED_DIALECTS:
        return
    if _NUL in sql:
        raise ReadOnlyViolation("The query holds a NUL character, after which the source would not read it. Remove it.")
    if dialect == "hive" and "${" in sql:
        # HiveServer2 substitutes `${...}` variables in the text before it
        # reads it (strings and comments included), so the engine would run
        # text these checks never saw.
        raise ReadOnlyViolation(
            "The query holds '${', which this source replaces with a variable's value before it reads the "
            "query. Remove it."
        )
    if unknown_lexer and "\\" in sql:
        raise ReadOnlyViolation(
            "The query holds a backslash, which this source may read as an escape where the query checks do not, "
            "or the other way round. Remove it, or match the character with a wildcard."
        )
    if dialect == "athena":
        _refuse_athena_backslash(sql)
    elif dialect == "oracle":
        _refuse_alternative_quotes(sql)
    from sqlglot.dialects.dialect import Dialect

    try:
        reader = Dialect.get_or_raise(dialect)
    except Exception as e:
        raise ReadOnlyViolation(_UNKNOWN_DIALECT) from e
    try:
        tokens = reader.tokenize(sql)
    except Exception as e:
        raise ReadOnlyViolation(_UNREADABLE) from e
    literal = _literal_spans(sql, tokens)
    literal_starts = {start for start, _end in literal}
    code = [t for t in tokens if t.start not in literal_starts]
    masked = _masked(sql, literal)
    if "{#" in masked:
        raise ReadOnlyViolation("The query has '{#', which some readers take as the start of a comment. Remove it.")
    if _OTHER_LINE_BREAK.search(masked):
        raise ReadOnlyViolation(
            "The query has a line break other than a newline (a lone carriage return, a vertical tab, a form feed "
            "or a Unicode line separator) outside a string, which readers end comments at differently. "
            "Use plain newlines."
        )
    if dialect == "clickhouse" and _CLICKHOUSE_CURLY_QUOTES.search(masked):
        raise ReadOnlyViolation(
            "The query has a curly quote outside a string or a quoted name, which ClickHouse reads as a quote "
            "where the query checks read part of a name. Use straight quotes: '...' for a string, \"...\" for a name."
        )
    spaces = _ENGINE_SPACES.get(dialect, frozenset())
    for space in _NON_ASCII_SPACE.finditer(masked):
        if space.group() not in spaces:
            raise ReadOnlyViolation(
                f"The query has a space outside ASCII (U+{ord(space.group()):04X}) outside a string or a quoted "
                "name, which this source may read as part of a name. Use a plain space."
            )
    _check_names_outside_ascii(masked, code, spaces)
    comments = _gap_comments(sql, tokens, reader.tokenizer_class)
    if dialect == "mysql":
        _check_mysql_comments(sql, tokens, comments)
        _check_mysql_escaped_quotes(sql, tokens)
        _check_mysql_brackets(masked, comments)
    if dialect in _UNNESTED_COMMENT_DIALECTS:
        if any(mark == "/*" and "/*" in sql[start + 2:end] for mark, start, end in comments):
            raise _comment_inside_comment()
    code_starts = [t.start for t in code]
    # Calcite's lexer (Druid, Dremio) reads `//` as a line comment. sqlglot's
    # Druid reads it as code; for Dremio, whose lexer is not pinned, it is
    # refused anywhere outside a string or a quoted name.
    if (dialect == "druid" and any(_in_token(code, code_starts, m.start()) for m in re.finditer("//", masked))
            or dialect == "dremio" and "//" in masked):
        raise ReadOnlyViolation(
            f"The query has '//' outside a string, which {'Druid' if dialect == 'druid' else 'Dremio'} reads as "
            "the start of a comment. Remove it, or write the division with a space between the slashes."
        )
    # Hive releases differ on block comments; Impala and Spark read them.
    if dialect == "hive" and re.search(r"/\*|\*/", masked):
        raise ReadOnlyViolation(
            "The query has a /* */ comment, which some Hive releases do not read as a comment. Remove it, "
            "or use a -- comment."
        )
    if dialect == "bigquery":
        if any(t.token_type.name == "IDENTIFIER" and sql[t.start] == "`" and "\\" in sql[t.start:t.end + 1]
               for t in tokens):
            raise ReadOnlyViolation(
                "A backtick-quoted name holds a backslash, which BigQuery reads as an escape. Remove it."
            )
    if dialect == "clickhouse":
        if any(t.token_type.name == "IDENTIFIER" and sql[t.start] in ("`", "\"") and "\\" in sql[t.start:t.end + 1]
               for t in tokens):
            raise ReadOnlyViolation(
                "A quoted name holds a backslash, which ClickHouse reads as an escape (so the name it reads is "
                "not the one written). Remove it."
            )
    if dialect in ("spark", "databricks"):
        if any(mark == "--" and end < len(sql) and sql[end - 1] == "\\" for mark, start, end in comments):
            raise ReadOnlyViolation(
                "A -- comment ends in a backslash, which Spark reads as continuing the comment on the next line. "
                "Remove the backslash."
            )
    if dialect in ("tsql", "fabric"):
        _check_glued_constants(sql, code)
        _check_currency_signs(sql, masked, code, code_starts)
    if dialect == "mysql" or unknown_lexer:
        _check_digit_led_names(sql, tokens, every=unknown_lexer)
    elif dialect == "clickhouse":
        _check_numbers_after_a_lone_dot(sql, tokens)
    if dialect == "duckdb":
        _check_dollar_tags(sql, masked, tokens)
        _check_e_strings(sql, tokens)


def _literal_spans(sql: str, tokens) -> list[tuple[int, int]]:
    """The (start, end) spans of `tokens`' strings and quoted names (an
    IDENTIFIER token sqlglot read from unquoted text is code)."""
    return [(t.start, t.end + 1) for t in tokens if t.token_type.name in _LITERAL_TOKENS
            or (t.token_type.name == "IDENTIFIER" and sql[t.start:t.start + 1] in ("\"", "`", "["))]


def _check_currency_signs(sql: str, masked: str, code, code_starts: list[int]) -> None:
    """SQL Server reads a currency sign that starts a word as a money
    constant (`$1.` is the number 1); sqlglot reads it as a name, so the
    constant's end, and what follows it, differ. A `$` inside a name (after
    a letter, `_`, `@` or `#`) continues the name for both."""
    for sign in _CURRENCY_SIGN.finditer(masked):
        at = sign.start()
        if not _in_token(code, code_starts, at):
            continue  # in a comment
        token = code[bisect.bisect_right(code_starts, at) - 1]
        first = sql[token.start]
        if sign.group() == "$" and at > token.start and token.token_type.name == "VAR" \
                and (first.isalpha() or first in "_@#"):
            continue
        raise ReadOnlyViolation(
            "The query has a currency sign outside a string or a quoted name, which SQL Server reads as the start "
            "of a money constant where the query checks read a name. Write the number without it "
            "(CAST(1.50 AS money)), or quote the name with [ ]."
        )


def _check_digit_led_names(sql: str, tokens, *, every: bool) -> None:
    """MySQL and MariaDB read a number glued to a word (`1.5from`) as the
    number and then the word; sqlglot reads one name. An unquoted name
    starting with a digit that they read as a number (`_MYSQL_NUMBER_LED`,
    or after a lone dot) is refused, or every such name (`every`: the
    engine's lexer is not known), and so is a number glued to a word."""
    for i, token in enumerate(tokens):
        kind = token.token_type.name
        raw = sql[token.start:token.end + 1]
        if kind in ("VAR", "IDENTIFIER") and "0" <= raw[0] <= "9" \
                and (every or _MYSQL_NUMBER_LED.match(raw) or _after_a_lone_dot(tokens, i)):
            raise _digit_led_name(raw)
        if kind == "NUMBER" and i + 1 < len(tokens) and tokens[i + 1].start == token.end + 1 \
                and _is_name_char(sql[tokens[i + 1].start]):
            raise _digit_led_name(sql[token.start:tokens[i + 1].end + 1])


def _check_numbers_after_a_lone_dot(sql: str, tokens) -> None:
    """ClickHouse reads a dot and a digit after an operator (`-.5from`) as a
    number and then the word; sqlglot reads a dot and one name."""
    for i, token in enumerate(tokens):
        if token.token_type.name in ("VAR", "IDENTIFIER") and "0" <= sql[token.start] <= "9" \
                and _after_a_lone_dot(tokens, i):
            raise _digit_led_name(sql[tokens[i - 1].start:token.end + 1])


def _after_a_lone_dot(tokens, i: int) -> bool:
    """Whether `tokens[i]` is written right after a dot that does not
    separate two parts of a name (`_BEFORE_A_NAME_DOT`)."""
    dot = tokens[i - 1] if i else None
    if dot is None or dot.token_type.name != "DOT" or dot.end + 1 != tokens[i].start:
        return False
    before = tokens[i - 2] if i > 1 else None
    return before is None or before.end + 1 != dot.start or before.token_type.name not in _BEFORE_A_NAME_DOT


def _digit_led_name(text: str) -> ReadOnlyViolation:
    return ReadOnlyViolation(
        f"A name starts with a digit ({text[:40]}), which this source may read as a number and then a word. "
        "Quote the name with backticks, or put a space after the number."
    )


def _check_e_strings(sql: str, tokens) -> None:
    """DuckDB reads backslash escapes in an E'...' string; sqlglot's DuckDB
    tokenizer does not, so the string ends elsewhere for it."""
    for token in tokens:
        if token.token_type.name == "BYTE_STRING" and sql[token.start] in "eE" \
                and "\\" in sql[token.start:token.end + 1]:
            raise ReadOnlyViolation(
                "An E'...' string holds a backslash, which DuckDB reads as an escape where the query checks do not. "
                "Write a standard string ('...') holding the character itself."
            )


def _in_token(tokens, starts: list[int], at: int) -> bool:
    """Whether position `at` is inside one of `tokens` (sorted by start;
    `starts` their starts)."""
    i = bisect.bisect_right(starts, at) - 1
    return i >= 0 and tokens[i].start <= at <= tokens[i].end


def _check_names_outside_ascii(masked: str, code, spaces: frozenset[str]) -> None:
    """Every name in code holding a character outside ASCII must be exactly
    one name to sqlglot: engines that take such characters in names read
    the whole run as one, while sqlglot may read a number (a digit outside
    ASCII), a keyword (a letter that upper-cases to ASCII) or a split."""
    if masked.isascii():
        return
    by_start = {t.start: t for t in code}
    starts = [t.start for t in code]
    for run in _name_run(spaces).finditer(masked):
        if run.group().isascii() or not _in_token(code, starts, run.start()):
            continue
        token = by_start.get(run.start())
        if token is None or token.token_type.name != "VAR" or token.end != run.end() - 1:
            raise ReadOnlyViolation(
                "A name holds a character outside ASCII that the query checks read differently from the source "
                "(a digit outside ASCII at its start, a letter read as a keyword, or a space). "
                "Quote the name."
            )


def _check_mysql_comments(sql: str, tokens, comments) -> None:
    if (any(_EXECUTABLE_COMMENT_TEXT.match(c) for t in tokens for c in t.comments)
            or any(_EXECUTABLE_COMMENT_START.match(sql, start) for mark, start, _end in comments if mark == "/*")):
        raise ReadOnlyViolation(
            "The query has an executable comment (/*! ... */ or /*M! ... */), which MySQL and MariaDB run as code. "
            "Remove it, or write its text as plain SQL."
        )
    for mark, start, _end in comments:
        if mark == "--" and start + 2 < len(sql) and sql[start + 2] > " ":
            raise ReadOnlyViolation(
                "The query has '--' not followed by a space, which MySQL reads as two minus signs, not a comment. "
                "Write '-- ' for a comment, or '- -' for two minus signs."
            )


def _check_mysql_escaped_quotes(sql: str, tokens) -> None:
    """sqlglot reads MySQL's default mode, in which a backslash in a string
    escapes the next character. A server in NO_BACKSLASH_ESCAPES mode reads
    the backslash as a character, and one in ANSI_QUOTES mode reads "..."
    as a name, in which a backslash escapes nothing. Every mode ends a
    literal where sqlglot does unless a backslash escapes the literal's own
    quote character (a backslash before a backslash or a letter is read as
    two characters either way, and moves no quote), so that is refused."""
    for token in tokens:
        if token.token_type.name not in _LITERAL_TOKENS:
            continue
        raw = sql[token.start:token.end + 1]
        opening = next((at for at, c in enumerate(raw) if c in "'\""), -1)
        if opening < 0:
            continue
        quote, i, end = raw[opening], opening + 1, len(raw) - 1
        while i < end:
            if raw[i] == "\\":
                if raw[i + 1] == quote:
                    raise ReadOnlyViolation(
                        "A string holds a backslash right before its quote character, which a MySQL or MariaDB "
                        "server in another SQL mode (NO_BACKSLASH_ESCAPES, ANSI_QUOTES) reads as the end of the "
                        "string. Double the quote instead ('it''s')."
                    )
                i += 2
            else:
                # A quote inside the literal is doubled.
                i += 2 if raw[i] == quote else 1


def _check_mysql_brackets(masked: str, comments) -> None:
    """MariaDB in MSSQL mode reads [...] as a quoted name, where sqlglot
    reads a bracket and then whatever follows, strings included. Every
    other MySQL and MariaDB mode rejects a bracket outside a literal or a
    comment, so one there is refused."""
    for match in re.finditer(r"[\[\]]", masked):
        if not any(start <= match.start() < end for _mark, start, end in comments):
            raise ReadOnlyViolation(
                "The query has a square bracket outside a string or a comment, which MariaDB in MSSQL mode reads "
                "as quoting a name. Quote names with backticks."
            )


def _check_glued_constants(sql: str, code) -> None:
    """SQL Server reads a hex (0x..) or money ($1, and other currency signs)
    constant and then a word where sqlglot reads one name."""
    for token in code:
        raw = sql[token.start:token.end + 1]
        hex_glued = raw[:2] in ("0x", "0X") and token.token_type.name != "HEX_STRING"
        money_glued = (len(raw) > 1 and unicodedata.category(raw[0]) == "Sc" and raw[1].isdigit()
                       and any(c.isalpha() or c == "_" for c in raw[1:]))
        if hex_glued or money_glued:
            raise ReadOnlyViolation(
                "The query has a hex or money constant written right before a word, which SQL Server reads as the "
                "constant and then that word. Put a space or an operator between them."
            )


def _check_dollar_tags(sql: str, masked: str, tokens) -> None:
    """A dollar-quote tag outside ASCII must be read by sqlglot as the same
    delimiter DuckDB reads (sqlglot's own dollar string, opened and closed
    by that tag)."""
    for m in _DOLLAR_DELIMITER.finditer(masked):
        if not m.group().isascii():
            raise ReadOnlyViolation(
                "A dollar-quote tag holds a character outside ASCII that the query checks read differently "
                "from the source. Use an ASCII tag such as $q$, or a standard string."
            )
    for token in tokens:
        if token.token_type.name != "HEREDOC_STRING":
            continue
        raw = sql[token.start:token.end + 1]
        tag = _DOLLAR_DELIMITER.match(raw)
        if tag is not None and not tag.group().isascii() and not (len(raw) >= 2 * len(tag.group())
                                                                   and raw.endswith(tag.group())):
            raise ReadOnlyViolation(
                "A dollar-quote tag holds a character outside ASCII that the query checks read differently "
                "from the source. Use an ASCII tag such as $q$, or a standard string."
            )


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
