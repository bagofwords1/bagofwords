"""Built-in blocklist of catastrophic tool calls.

Checked before every other permission rule for connection tools and remote
shell: no permission mode, per-tool ``allow`` or remembered answer can run a
call this module flags. The list is narrow on purpose — it names actions that
are almost never what an analytics assistant should do without a human typing
them (wiping a disk, dropping a database), not everything that writes.

Shell commands are checked with text patterns. SQL is read with sqlglot: the
SQL guard's parser plus a token scan over five dialects, so a destructive
statement is found wherever it sits in a batch, not only at the start. Neither
is a sandbox: they stop what an agent sends, and the per-tool settings remain
the real control.

The check is pure and CPU-bound (sqlglot is pure Python), so async callers run
it with ``asyncio.to_thread``.
"""

import logging
import re
import shlex
from typing import Any, Iterable, Optional

import sqlglot
from sqlglot import exp

from app.ai.code_execution.sql_parser import parse_guard_sql

logger = logging.getLogger(__name__)

# ── shell ───────────────────────────────────────────────────────────────────

# Protected roots. A target is compared after trailing "/" and "/*" are
# stripped, so "/etc/", "/home/*" and "~/*" all count; "*" alone too.
_RM_ROOTS = {"/", "~", "$HOME", "${HOME}", ".", "..", "*",
             "/home", "/root", "/etc", "/usr", "/var", "/boot", "/bin", "/lib", "/opt", "/srv"}
_POWER_PROGRAMS = {"shutdown", "reboot", "halt", "poweroff"}
_WRAPPERS = {"sudo", "doas", "command", "exec", "nohup", "env", "nice", "time", "timeout", "ionice", "stdbuf", "xargs"}
# Wrapper options that take a value, so the value is not read as the program.
_WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T",
             "--user", "--group", "--close-from", "--chdir", "--host", "--prompt", "--role", "--type",
             "--other-user", "--command-timeout"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "xargs": {"-n", "-P", "-I", "-L", "-d", "-s", "-E"},
}

_SHELL_PATTERNS = [
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\bdd\b[^|;&]*\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)"),
    re.compile(r">\s*/dev/(sd|hd|nvme|xvd|vd|disk)\w*"),
    re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"),          # :(){ :|:& };:
    re.compile(r"\bsystemctl\s+(\S+\s+)*(reboot|poweroff|halt|kexec)\b"),
    re.compile(r"\bformat-volume\b", re.IGNORECASE),
    re.compile(r"\bclear-disk\b", re.IGNORECASE),
    re.compile(r"^\s*format(\.com)?\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bremove-item\b(?=.*-recurse)(?=.*-force)[^|;]*\s['\"]?[a-z]:\\?['\"]?(\s|$)", re.IGNORECASE),
    re.compile(r"\b(rd|rmdir)\s+/s\s+/q\s+[a-z]:\\?(\s|$)", re.IGNORECASE),
    re.compile(r"\bstop-computer\b|\brestart-computer\b", re.IGNORECASE),
]


def _tokens(segment: str) -> list:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _shell_segments(command: str, depth: int = 0) -> list:
    """The command, each simple segment between separators, and the body of
    any ``sh -c '…'`` / backtick / ``$(…)`` inside it, so a blocked action
    can't hide behind a separator or a nested shell."""
    out = [command]
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        seg: list = []
        for tok in lexer:
            if tok and all(c in ";&|()\n" for c in tok):
                out.append(" ".join(seg))
                seg = []
            else:
                seg.append(tok)
        out.append(" ".join(seg))
    except ValueError:
        out.extend(re.split(r"[;&|\n]+", command))
    if depth < 3:
        nested = re.findall(r"`([^`]*)`", command) + re.findall(r"\$\(([^()]*)\)", command)
        for seg in list(out):
            nested.extend(_nested_bodies(_tokens(seg)))
        for body in nested:
            out.extend(_shell_segments(body, depth + 1))
    return [s.strip() for s in out if s and s.strip()]


_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "su", "runuser"}


def _nested_bodies(toks: list) -> list:
    """Command strings run by a nested shell in this segment: ``bash -l -c X``,
    ``sh -xc X``, ``su root -c X``, ``su -c X``, ``--command=X``."""
    bodies = []
    for i, t in enumerate(toks):
        if t.rsplit("/", 1)[-1] not in _SHELLS:
            continue
        for j in range(i + 1, len(toks)):
            a = toks[j]
            if a in ("-c", "--command") or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                if j + 1 < len(toks):
                    bodies.append(toks[j + 1])
                break
            if a.startswith("--command="):
                bodies.append(a.split("=", 1)[1])
                break
    return bodies


def _program(tokens: list) -> tuple:
    """(program basename, remaining tokens) after wrappers such as
    ``sudo -u root``, ``timeout 10``, ``nice -n 5`` or ``env X=1``."""
    i = 0
    while i < len(tokens):
        base = tokens[i].rsplit("/", 1)[-1]
        if base not in _WRAPPERS:
            return base, tokens[i + 1:]
        i += 1
        value_opts = _WRAPPER_VALUE_OPTS.get(base, set())
        while i < len(tokens):
            t = tokens[i]
            if t in value_opts:
                i += 2
            elif t.startswith("-"):  # also --user=root style
                i += 1
            elif base == "env" and "=" in t:
                i += 1
            elif base == "timeout" and re.fullmatch(r"\d+(\.\d+)?[smhd]?", t):
                i += 1
            else:
                break
    return "", []


def _is_recursive_force_rm(args: list) -> bool:
    flags, targets = set(), []
    for t in args:
        if t == "--":
            continue
        if t.startswith("--"):
            flags.add(t)
        elif t.startswith("-") and len(t) > 1:
            flags.update(f"-{c}" for c in t[1:])
        else:
            targets.append(t)
    if "--no-preserve-root" in flags:
        return True
    recursive = bool(flags & {"-r", "-R", "--recursive"})
    force = bool(flags & {"-f", "--force"})
    return recursive and force and any(_protected_target(t) for t in targets)


def _protected_target(target: str) -> bool:
    t = target
    while t.endswith("/*") or (t.endswith("/") and t != "/"):
        t = t[:-2] if t.endswith("/*") else t[:-1]
    return (t or "/") in _RM_ROOTS


# Database command-line clients and the options that carry SQL to run.
_DB_CLIENT_SQL_OPTS = {
    "psql": {"-c", "--command"},
    "mysql": {"-e", "--execute"},
    "mariadb": {"-e", "--execute"},
    "sqlcmd": {"-Q", "-q"},
    "clickhouse-client": {"-q", "--query"},
    "clickhouse": {"-q", "--query"},
    "snowsql": {"-q", "--query"},
    "bq": {"query"},
    "sqlite3": set(),
    "duckdb": {"-c"},
    "trino": {"--execute"},
}


def _db_client_sql(tokens: list) -> tuple:
    """(SQL passed by option to a DB client in this segment, whether a DB
    client appears at all)."""
    sql, seen = [], False
    for i, t in enumerate(tokens):
        base = t.rsplit("/", 1)[-1]
        if base not in _DB_CLIENT_SQL_OPTS:
            continue
        seen = True
        opts = _DB_CLIENT_SQL_OPTS[base]
        for j in range(i + 1, len(tokens)):
            a = tokens[j]
            if a in opts and j + 1 < len(tokens):
                sql.append(tokens[j + 1])
            elif "=" in a and a.split("=", 1)[0] in opts:
                sql.append(a.split("=", 1)[1])
    return sql, seen


def shell_block_reason(command: str) -> Optional[str]:
    """Why a remote shell command is blocked, or None."""
    if not isinstance(command, str) or not command.strip():
        return None
    segments = _shell_segments(command)
    client_anywhere = any(_db_client_sql(_tokens(seg))[1] for seg in segments)
    for seg in segments:
        tokens = _tokens(seg)
        prog, args = _program(tokens)
        # rm anywhere in the segment, so no wrapper spelling can hide it.
        for i, tok in enumerate(tokens):
            if tok.rsplit("/", 1)[-1] == "rm" and _is_recursive_force_rm(tokens[i + 1:]):
                return "recursive forced delete of a root, home or wildcard path"
        if prog in _POWER_PROGRAMS or (prog in ("init", "telinit") and args[:1] in (["0"], ["6"])):
            return "shuts down or restarts the server"
        for pat in _SHELL_PATTERNS:
            if pat.search(seg):
                return "destructive system command"
        # SQL run through a database client: psql -c "DROP …", mysql -e …,
        # and SQL piped into one (echo "DROP TABLE x" | psql).
        client_sql, _ = _db_client_sql(tokens)
        for text in client_sql:
            reason = sql_block_reason(text, sql_context="sql")
            if reason:
                return reason
        if client_anywhere:
            for tok in tokens:
                reason = sql_block_reason(tok, sql_context="shared")
                if reason:
                    return reason
    return None


# ── SQL / tool names (any connection tool) ──────────────────────────────────

# How much SQL rule an argument gets depends on how sure we are it is SQL:
#   "sql"    — a key that certainly holds SQL (sql, sqlQuery, statement…), or a
#              shared key on a tool whose name says it runs SQL;
#   "shared" — keys a search tool uses too (query, q, command…): only
#              spellings a search phrase doesn't produce, unless the text
#              starts like a SQL script (then it is read as "sql", as before);
#   "other"  — anything else: only a text that is a single DROP statement
#              ("Drop table orders_tmp after migration" in a title passes).
_SQL_SHARED_KEYS = {"query", "q", "command", "script", "code"}
# A tool whose name says it runs SQL: then a shared key holds SQL.
_SQL_TOOL_NAME = re.compile(
    r"sql|statement|stmt|database|^query$|(^|[_-])(run|execute|exec)[_-]?(query|queries)|(^|[_-])db([_-]|$)",
    re.IGNORECASE)


def _key_kind(key: str, tool_name: str = "") -> str:
    k = re.sub(r"[_\-]", "", key.lower())
    if "sql" in k or k in ("statement", "statements", "stmt", "stmts"):
        return "sql"
    if k in _SQL_SHARED_KEYS:
        return "sql" if _SQL_TOOL_NAME.search(tool_name or "") else "shared"
    return "other"


_TOOL_NAME = re.compile(
    r"^(drop|purge|wipe|destroy|nuke)[_-]?(all|database|db|table|schema|everything)"
    r"|^(delete|remove|truncate)[_-]?all([_-]|$)",
    re.IGNORECASE,
)


REASON_DROP = "drops a database, schema or table"
REASON_TRUNCATE = "truncates a table"
REASON_DELETE = "deletes every row of a table (no WHERE)"
REASON_UNREADABLE = "could not read SQL safely"
_REASON_ORDER = (REASON_DROP, REASON_TRUNCATE, REASON_DELETE, REASON_UNREADABLE)

# "" is sqlglot's default dialect ("generic" is not a dialect name there).
_DIALECTS = ("", "tsql", "postgres", "mysql", "snowflake")
# Above this many UTF-8 bytes the parser is skipped (about 4 s across the five
# dialects at 200 KB); the token scan still runs.
_MAX_PARSE_BYTES = 200 * 1024
# Above this, a "sql" or "shared" text that passes its gate is refused as
# unreadable without tokenizing (tokenizing has no other bound: about 6 s/MB).
_MAX_SCAN_BYTES = 1024 * 1024
# A necessary condition only: no finding is possible without one of these
# words, so a text without them passes before any tokenizing. Never decisive.
_GATE_WORDS = ("drop", "truncate", "delete", "exec")
# EXEC bodies are read as SQL again, at most this many levels deep.
_MAX_EXEC_DEPTH = 2


# ── frozen copy of the regex SQL check (3875d2971) ──────────────────────────
# FROZEN COPY FOR PARITY — DO NOT EDIT. These patterns and _parity_legacy_check
# are byte-for-byte today's regex check (tool_blocklist.py at 3875d2971, only
# renamed). They decide one case: a "shared" or "other" text that no sqlglot
# tokenizer can read (an unclosed quote or comment), which keeps exactly its
# old outcome. _promoted also reads _LEGACY_SQL_START and _LEGACY_SQL_COMMENTS.
# A unit test pins the recorded outcomes; a byte check compares the patterns.
_LEGACY_NAME = r'(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[\w$#@]+)'
_LEGACY_IDENT = _LEGACY_NAME + r"(?:\." + _LEGACY_NAME + r")*"
_LEGACY_ALIAS = r"(?:\s+(?:as\s+)?(?!where\b|returning\b|using\b)[\w]+)?"
_LEGACY_SQL_DROP = re.compile(r"^drop\s+(database|schema|table)\b", re.IGNORECASE)
_LEGACY_SQL_DROP_ONLY = re.compile(
    r"^drop\s+(database|schema|table)\s+(if\s+exists\s+)?" + _LEGACY_IDENT + r"(\s*,\s*" + _LEGACY_IDENT + r")*"
    r"(\s+(cascade|restrict))?\s*;?\s*$", re.IGNORECASE)
_LEGACY_SQL_DROP_ANYWHERE = re.compile(
    r"(^|[\s(])drop\s+(database|schema|table)\s+(if\s+exists\s+)?" + _LEGACY_IDENT, re.IGNORECASE)
_LEGACY_SQL_TRUNCATE = re.compile(
    r"^truncate\s+(table\s+)?(only\s+)?" + _LEGACY_IDENT + r"(\s*,\s*" + _LEGACY_IDENT + r")*"
    r"(\s+(cascade|restrict|(restart|continue)\s+identity))*\s*$", re.IGNORECASE)
_LEGACY_SQL_DELETE = re.compile(
    r"^delete\s+from\s+(only\s+)?" + _LEGACY_IDENT + _LEGACY_ALIAS + r"(\s+returning\b.*)?\s*$",
    re.IGNORECASE | re.DOTALL)
_LEGACY_SQL_DELETE_NO_FROM = re.compile(r"^delete\s+(only\s+)?" + _LEGACY_IDENT + r"\s*$", re.IGNORECASE)
_LEGACY_SQL_TRUNCATE_TABLE = re.compile(r"^truncate\s+table\b", re.IGNORECASE)
_LEGACY_SQL_START = re.compile(
    r"^\s*(select|with|insert|update|merge|create|alter|exec|execute|begin|declare|use)\b", re.IGNORECASE)
_LEGACY_SQL_COMMENTS = re.compile(r"(--[^\n]*\n?|/\*.*?\*/)", re.DOTALL)
_LEGACY_SQL_BATCH_SEP = re.compile(r";|^\s*go\s*$", re.IGNORECASE | re.MULTILINE)
_LEGACY_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")
_LEGACY_SQL_EXEC_BODY = re.compile(
    r"\bexec(?:ute)?\b\s*(?:\(\s*)?(?:[\w.\[\]]+\s*)?N?'((?:[^']|'')*)'", re.IGNORECASE)


def _parity_legacy_check(text: str, level: str, depth: int = 0) -> Optional[str]:
    """Today's regex SQL check, frozen (see the comment above). ``level`` is
    "shared" or "other"; "sql" is used only by its own EXEC recursion."""
    if not isinstance(text, str) or not text.strip():
        return None
    clean = _LEGACY_SQL_COMMENTS.sub(" ", text)
    if level == "other":
        return "drops a database, schema or table" if _LEGACY_SQL_DROP_ONLY.match(clean.strip()) else None
    if depth < 2:
        for body in _LEGACY_SQL_EXEC_BODY.findall(clean):
            reason = _parity_legacy_check(body.replace("''", "'"), "sql", depth + 1)
            if reason:
                return reason
    clean = _LEGACY_SQL_LITERAL.sub("''", clean)
    batch = bool(_LEGACY_SQL_START.match(clean))  # plainly a SQL script
    for stmt in _LEGACY_SQL_BATCH_SEP.split(clean):
        s = (stmt or "").strip()
        if not s:
            continue
        if _LEGACY_SQL_DROP.search(s):
            return "drops a database, schema or table"
        # T-SQL batches need no separators: "SELECT 1 DROP TABLE x".
        if batch and _LEGACY_SQL_DROP_ANYWHERE.search(s):
            return "drops a database, schema or table"
        if level == "sql" or batch:
            if _LEGACY_SQL_TRUNCATE.search(s):
                return "truncates a table"
            if _LEGACY_SQL_DELETE.search(s) or _LEGACY_SQL_DELETE_NO_FROM.search(s):
                return "deletes every row of a table (no WHERE)"
        else:  # shared
            if _LEGACY_SQL_TRUNCATE_TABLE.search(s) and _LEGACY_SQL_TRUNCATE.search(s):
                return "truncates a table"
            if _LEGACY_SQL_DELETE.search(s):
                return "deletes every row of a table (no WHERE)"
    return None


def _inert_dead_comment_openers(text: str) -> str:
    """Make every ``/*`` after the last ``*/`` inert (``/`` + NUL). Such an
    opener can never close, and _LEGACY_SQL_COMMENTS rescans to the end of
    the text for each one: quadratic, holding the GIL, so even a worker
    thread would stall the event loop. No legacy pattern names ``*`` or NUL
    outside the comment regex, so the change never alters a result."""
    r = text.rfind("*/")
    start = r + 1 if r >= 0 else 0
    return text[:start] + text[start:].replace("/*", "/\x00")


def _promoted(text: str) -> bool:
    """Today's rule for "this shared-key text is plainly a SQL script": it
    starts with a SQL statement word once comments are stripped. Such a text
    gets the full "sql" rules, as it did before."""
    return bool(_LEGACY_SQL_START.match(_LEGACY_SQL_COMMENTS.sub(" ", _inert_dead_comment_openers(text))))


def _parity_fallback(text: str, level: str) -> Optional[str]:
    """Today's result for a "shared" or "other" text that no tokenizer reads
    (and, at "other", for any text over _MAX_PARSE_BYTES). Never used at "sql"."""
    return _parity_legacy_check(_inert_dead_comment_openers(text), level)


# ── SQL read with sqlglot ───────────────────────────────────────────────────

# Literal and quoted-name tokens. sqlglot keeps the word as the token text
# ('DELETE' is a STRING with text DELETE), so these are never keywords.
_LITERAL_TOKENS = frozenset({
    "STRING", "NATIONAL_STRING", "HEREDOC_STRING", "RAW_STRING", "BYTE_STRING",
    "BIT_STRING", "HEX_STRING", "UNICODE_STRING", "IDENTIFIER",
})
_LIT = "\x00literal"  # keyword slot of a literal token; equals no keyword
_DROP_KINDS = frozenset({"TABLE", "DATABASE", "SCHEMA"})
# Words after which a statement starts (procedural IF bodies, procedure,
# trigger and task bodies, DO blocks, BEGIN … END, loops).
_STATEMENT_START_WORDS = frozenset({"BEGIN", "THEN", "ELSE", "AS", "DO", "LOOP"})
# TRUNCATE followed by these is a function call, a qualified name or an alias.
_TRUNCATE_NOT_A_STATEMENT = frozenset({"(", ".", ",", ")", ";", "FROM"})
# No statement ends with these (T-SQL, the one dialect without separators,
# reserves them), so no DELETE statement can start right after them.
_DELETE_AFTER = frozenset({"OR", "OF", "GRANT", "REVOKE", ",", "."})
# No DELETE statement continues with these.
_DELETE_BEFORE = frozenset({";", ")", ",", "ON", ".", "OR", "AS", "-", "NOT"})
# ON DELETE <action>: foreign-key actions and Postgres rules.
_ON_DELETE_ACTIONS = frozenset({"CASCADE", "RESTRICT", "TO"})
_ON_DELETE_ACTION_PAIRS = frozenset({("SET", "NULL"), ("SET", "DEFAULT"), ("NO", "ACTION")})
# MERGE … THEN DELETE followed by another clause.
_MERGE_DELETE_BEFORE = frozenset({"WHEN", "OUTPUT", "RETURNING"})
# Statement keywords that end an open DELETE window at its own depth.
_STATEMENT_WORDS = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "CREATE", "ALTER",
    "EXEC", "EXECUTE", "MERGE", "WITH",
})
_EXEC_WORDS = frozenset({"EXEC", "EXECUTE"})


def _passes_gate(low: str, level: str) -> bool:
    if level == "other":
        return "drop" in low
    if level == "sql":
        return any(w in low for w in _GATE_WORDS)
    return ("drop" in low or ("truncate" in low and "table" in low) or ("delete" in low and "from" in low)
            or ("exec" in low and ("drop" in low or "truncate" in low or "delete" in low)))


def _tokenize(text: str, dialect: str) -> Optional[list]:
    try:
        return sqlglot.tokenize(text, read=dialect)
    except Exception:
        return None


def _keyword(tok) -> str:
    """The text a token is compared by: upper-cased, with a leading
    dollar-quote tag dropped (``$$BEGIN`` and ``$tag$BEGIN`` are one VAR in
    the default, tsql and mysql tokenizers). Literals compare as nothing."""
    kind = tok.token_type.name
    if kind in _LITERAL_TOKENS:
        return _LIT
    word = tok.text.upper()
    if kind == "VAR" and word.startswith("$"):
        end = word.find("$", 1)
        if end > 0 and all(c.isalnum() or c == "_" for c in word[1:end]):
            word = word[end + 1:]
    return word


def _segments(tokens: list) -> Iterable[tuple]:
    """(keywords, tokens) per statement segment: split at ``;`` and at a GO
    alone on its line. Comments are never tokens."""
    kws: list = []
    toks: list = []
    last = len(tokens) - 1
    for i, tok in enumerate(tokens):
        kind = tok.token_type.name
        if kind == "SEMICOLON":
            yield kws, toks
            kws, toks = [], []
            continue
        if (kind not in _LITERAL_TOKENS and tok.text.upper() == "GO"
                and (i == 0 or tokens[i - 1].line < tok.line) and (i == last or tokens[i + 1].line > tok.line)):
            yield kws, toks
            kws, toks = [], []
            continue
        kws.append(_keyword(tok))
        toks.append(tok)
    yield kws, toks


def _exec_literals(kws: list, toks: list, literals: set) -> None:
    """In a segment with EXEC/EXECUTE, each string literal and their
    concatenation are SQL to run (``EXEC('DR' + 'OP TABLE x')``)."""
    if not any(k in _EXEC_WORDS for k in kws):
        return
    parts = []
    for i, t in enumerate(toks):
        if t.token_type.name not in _LITERAL_TOKENS or t.token_type.name == "IDENTIFIER":
            continue
        if i > 0 and kws[i - 1] in _EXEC_WORDS and toks[i - 1].token_type.name != "VAR":
            # A tokenizer that reads EXEC/EXECUTE as a command keeps the rest of
            # the statement as one raw string ("('DROP TABLE x')"): read it as
            # an EXEC statement, so its own literals are rechecked too (and the
            # raw text is read only once).
            literals.add("EXEC " + t.text)
            continue
        parts.append(t.text)
    literals.update(parts)
    if len(parts) > 1:
        literals.add("".join(parts))


def _is_word(kw: Optional[str]) -> bool:
    return bool(kw) and kw != "ON" and all(c.isalnum() or c == "_" for c in kw)


def _delete_exempt(kws: list, i: int) -> bool:
    """A DELETE token in a position where no DELETE statement can be."""
    prev = kws[i - 1] if i > 0 else None
    nxt = kws[i + 1] if i + 1 < len(kws) else None
    nxt2 = kws[i + 2] if i + 2 < len(kws) else None
    if prev in _DELETE_AFTER:
        return True  # privilege and event lists, qualified names
    if prev == "ON" and (nxt in _ON_DELETE_ACTIONS or (nxt, nxt2) in _ON_DELETE_ACTION_PAIRS):
        return True  # ON DELETE CASCADE, CREATE RULE … ON DELETE TO t
    if nxt is None or nxt in _DELETE_BEFORE:
        return True
    return prev == "THEN" and nxt in _MERGE_DELETE_BEFORE  # MERGE … THEN DELETE WHEN …


def _scan_sql_tokens(tokens: list, found: set, literals: Optional[set]) -> None:
    """The token check at level "sql", one linear pass per segment."""
    for kws, toks in _segments(tokens):
        n = len(kws)
        depth = 0
        open_deletes: set = set()  # depths with a DELETE still waiting for its WHERE
        for i, kw in enumerate(kws):
            if kw == "(":
                depth += 1
                continue
            if kw == ")":
                if depth in open_deletes:
                    found.add(REASON_DELETE)
                    open_deletes.discard(depth)
                depth = max(depth - 1, 0)
                continue
            nxt = kws[i + 1] if i + 1 < n else None
            if kw == "DROP":
                # DROP [up to two modifier words] TABLE|DATABASE|SCHEMA.
                j = i + 1
                while j < n and j <= i + 3:
                    if kws[j] in _DROP_KINDS:
                        found.add(REASON_DROP)
                        break
                    if j == i + 3 or not _is_word(kws[j]):
                        break
                    j += 1
            elif kw == "TRUNCATE":
                at_start = i == 0 or kws[i - 1] in _STATEMENT_START_WORDS
                if nxt == "TABLE" or (at_start and nxt is not None and nxt not in _TRUNCATE_NOT_A_STATEMENT):
                    found.add(REASON_TRUNCATE)
            elif kw == "WHERE":
                open_deletes.discard(depth)
            if kw in _STATEMENT_WORDS and not (kw == "WITH" and nxt == "("):  # WITH ( is a T-SQL table hint
                if depth in open_deletes:
                    found.add(REASON_DELETE)
                    open_deletes.discard(depth)
            if kw == "DELETE" and not _delete_exempt(kws, i):
                open_deletes.add(depth)
        if open_deletes:
            found.add(REASON_DELETE)
        if literals is not None:
            _exec_literals(kws, toks, literals)


def _name_end(kws: list, toks: list, j: int) -> Optional[int]:
    """Index after a table name starting at j (parts joined by "."), or None."""
    def part(k: int) -> bool:
        if k >= len(toks):
            return False
        if toks[k].token_type.name == "IDENTIFIER":
            return True
        return kws[k] != _LIT and bool(toks[k].text) and all(c.isalnum() or c in "_$#@" for c in toks[k].text)

    if not part(j):
        return None
    j += 1
    while j + 1 < len(kws) and kws[j] == "." and part(j + 1):
        j += 2
    return j


def _scan_shared_tokens(tokens: list, found: set, literals: Optional[set]) -> None:
    """The anchored shapes of a "shared" text, at a segment start only (a
    token port of the old shared regexes)."""
    for kws, toks in _segments(tokens):
        n = len(kws)
        if literals is not None:
            _exec_literals(kws, toks, literals)
        if n < 2:
            continue
        if kws[0] == "DROP" and kws[1] in _DROP_KINDS:
            found.add(REASON_DROP)
        elif kws[0] == "TRUNCATE" and kws[1] == "TABLE":
            j = 3 if n > 2 and kws[2] == "ONLY" else 2
            j = _name_end(kws, toks, j)
            while j is not None and j < n and kws[j] == ",":
                j = _name_end(kws, toks, j + 1)
            while j is not None and j < n:
                if kws[j] in ("CASCADE", "RESTRICT"):
                    j += 1
                elif kws[j] in ("RESTART", "CONTINUE") and j + 1 < n and kws[j + 1] == "IDENTITY":
                    j += 2
                else:
                    j = None
            if j is not None:
                found.add(REASON_TRUNCATE)
        elif kws[0] == "DELETE" and kws[1] == "FROM":
            j = _name_end(kws, toks, 3 if n > 2 and kws[2] == "ONLY" else 2)
            if j is None:
                continue

            def alias(k: int) -> bool:  # a bare word, never WHERE, RETURNING or USING
                return (k < n and kws[k] != _LIT and kws[k] not in ("WHERE", "RETURNING", "USING")
                        and bool(toks[k].text) and all(c.isalnum() or c == "_" for c in toks[k].text))

            if j < n and kws[j] == "AS" and alias(j + 1):
                j += 2
            elif alias(j):
                j += 1
            if j == n or kws[j] == "RETURNING":
                found.add(REASON_DELETE)


def _drop_of_a_table(node) -> bool:
    return str(node.args.get("kind") or "").upper() in _DROP_KINDS


def _scan_tree(text: str, level: str, token_lists: dict, found: set, literals: Optional[set]) -> None:
    """The parse-tree check. A dialect whose parser raises adds nothing."""
    for d in _DIALECTS:
        tokens = token_lists.get(d)
        if tokens is None:
            continue  # its tokenizer raised, so its parser would too
        try:
            statements = parse_guard_sql(text, d)
        except Exception:  # ParseError, TokenError, RecursionError on deep nesting, …
            continue
        words = {_keyword(t) for t in tokens} if level == "shared" else None
        for statement in statements:
            if statement is None:
                continue
            for node in statement.find_all(exp.Drop, exp.TruncateTable, exp.Delete, exp.Command):
                if isinstance(node, exp.Drop):
                    if _drop_of_a_table(node):
                        found.add(REASON_DROP)
                elif isinstance(node, exp.TruncateTable):
                    if words is None or "TABLE" in words:
                        found.add(REASON_TRUNCATE)
                elif isinstance(node, exp.Delete):
                    if not node.args.get("where") and (words is None or "FROM" in words):
                        found.add(REASON_DELETE)
                elif literals is not None and str(node.this).upper() in _EXEC_WORDS:
                    # EXEC <body>: the body is read again as an EXEC statement,
                    # so its literals are rechecked as SQL to run.
                    body = node.expression.name if isinstance(node.expression, exp.Expression) else ""
                    if body.strip():
                        literals.add("EXEC " + body)


def _other_findings(text: str) -> list:
    """Level "other": only a text that is one DROP of a table, database or
    schema. A text over _MAX_PARSE_BYTES, or one that no tokenizer reads, gets
    today's rule instead (the parity fallback). Level "other" has no scan cap:
    the fallback is a linear regex pass and runs without tokenizing, at any size."""
    if len(text.encode("utf-8")) > _MAX_PARSE_BYTES:
        reason = _parity_fallback(text, "other")
        return [reason] if reason else []
    parsed = False
    for d in _DIALECTS:
        try:
            statements = parse_guard_sql(text, d)
        except Exception:
            continue
        parsed = True
        real = [s for s in statements if s is not None]
        if len(real) == 1 and isinstance(real[0], exp.Drop) and _drop_of_a_table(real[0]):
            return [REASON_DROP]
    if not parsed and all(_tokenize(text, d) is None for d in _DIALECTS):
        reason = _parity_fallback(text, "other")
        return [reason] if reason else []
    return []


def _sql_findings(text: str, level: str, depth: int = 0) -> list[str]:
    """The reasons ``text`` is refused at ``level`` ("sql", "shared" or
    "other"); empty means it passes."""
    if level == "shared" and _promoted(text):
        level = "sql"
    if not _passes_gate(text.lower(), level):
        return []
    if level == "other":
        return _other_findings(text)
    size = len(text.encode("utf-8"))
    if size > _MAX_SCAN_BYTES:
        logger.info("blocklist: %s-level SQL text of %d bytes is over the scan cap; refused", level, size)
        return [REASON_UNREADABLE]
    token_lists = {d: _tokenize(text, d) for d in _DIALECTS}
    if all(tokens is None for tokens in token_lists.values()):
        if level == "shared":
            # No tokenizer reads it (an unclosed quote or comment): today's rules decide.
            reason = _parity_fallback(text, "shared")
            return [reason] if reason else []
        logger.info("blocklist: no SQL tokenizer reads a %d-byte text with a gate word; refused", size)
        return [REASON_UNREADABLE]
    found: set = set()
    literals: Optional[set] = set() if depth < _MAX_EXEC_DEPTH else None
    readable = [tokens for tokens in token_lists.values() if tokens is not None]
    if level == "sql":
        if "/*!" in text:
            # MySQL runs the body of /*! … */: read it as code too.
            copy = text.replace("/*!", "   ")
            readable += [t for t in (_tokenize(copy, d) for d in _DIALECTS) if t is not None]
        for tokens in readable:
            _scan_sql_tokens(tokens, found, literals)
    else:
        for tokens in readable:
            _scan_shared_tokens(tokens, found, literals)
    if size <= _MAX_PARSE_BYTES:
        _scan_tree(text, level, token_lists, found, literals)
    for literal in literals or ():
        found.update(_sql_findings(literal, "sql", depth + 1))
    return [reason for reason in _REASON_ORDER if reason in found]


def sql_block_reason(text: str, *, sql_context: Any = True) -> Optional[str]:
    """Why a SQL text is blocked, or None. ``sql_context`` is "sql" (or
    True), "shared" or "other" (or False); see the levels above. Literals and
    comments are never keywords, so a query that searches for 'drop table' in
    a log is not refused; ``EXEC('…')`` bodies are read as SQL on their own."""
    level = {True: "sql", False: "other"}.get(sql_context, sql_context)
    if not isinstance(text, str) or not text.strip():
        return None
    findings = _sql_findings(text, level)
    return findings[0] if findings else None


def _strings(value: Any, key: str = "", depth: int = 0) -> Iterable[tuple]:
    if depth > 6:
        return
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, str(k), depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, key, depth + 1)


def _bare_tool_name(tool_name: str) -> str:
    # Namespaced names: "db.drop_table", "server__drop_table", "x/purge_all".
    return re.split(r"[./:]|__", tool_name.strip())[-1]


def block_reason(*, tool_name: str, arguments: Any, remote_shell: bool = False) -> Optional[str]:
    """Why this connection-tool call is on the built-in blocklist, or None."""
    if isinstance(tool_name, str) and _TOOL_NAME.search(_bare_tool_name(tool_name)):
        return "the tool's name marks it as a mass-delete action"
    args = arguments if isinstance(arguments, dict) else {}
    if remote_shell:
        reason = shell_block_reason(args.get("command", ""))
        if reason:
            return reason
    bare = _bare_tool_name(tool_name) if isinstance(tool_name, str) else ""
    for key, text in _strings(args):
        reason = sql_block_reason(text, sql_context=_key_kind(key, bare))
        if reason:
            return reason
    return None
