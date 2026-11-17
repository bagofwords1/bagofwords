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
    nested = re.findall(r"`([^`]*)`", command) + re.findall(r"\$\(([^()]*)\)", command)
    for seg in list(out):
        nested.extend(_nested_bodies(_tokens(seg)))
    nested = [body for body in nested if body.strip()]
    if nested and depth >= _MAX_SHELL_DEPTH:
        raise _ShellTooDeep()  # a body this check would not read: refuse, never skip
    for body in nested:
        out.extend(_shell_segments(body, depth + 1))
    return [s.strip() for s in out if s and s.strip()]


# Nested shell bodies are read this many levels deep; a deeper one is refused.
_MAX_SHELL_DEPTH = 3
REASON_SHELL_UNREADABLE = "could not read the command safely"


class _ShellTooDeep(Exception):
    pass


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
    return _shell_reason(command, _Budget())


def _shell_reason(command: str, budget: "_Budget") -> Optional[str]:
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        segments = _shell_segments(command)
    except _ShellTooDeep:
        return REASON_SHELL_UNREADABLE
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
            reason = _sql_reason(text, "sql", budget)
            if reason:
                return reason
        if client_anywhere:
            for tok in tokens:
                reason = _sql_reason(tok, "shared", budget)
                if reason:
                    return reason
    return None


# ── SQL / tool names (any connection tool) ──────────────────────────────────

# How much SQL rule an argument gets depends on how sure we are it is SQL:
#   "sql"    — a key that certainly holds SQL (sql, sqlQuery, statement…), a
#              parameter the tool's own input schema describes as SQL, or, on
#              a tool whose name says it runs SQL, a shared key or any key
#              naming a query or statement (query_text, QueryString, queries);
#   "shared" — keys a search tool uses too (query, q, command…), and any other
#              text on a tool whose name says it runs SQL: only spellings a
#              search phrase doesn't produce, unless the text starts like a
#              SQL script (then it is read as "sql", as before);
#   "other"  — anything else: only a text that is a single DROP statement
#              ("Drop table orders_tmp after migration" in a title passes).
# A list item gets its key's kind ("queries": ["SELECT 1", "DELETE …"]).
_SQL_SHARED_KEYS = {"query", "q", "command", "script", "code"}
# A tool whose name says it runs SQL: an engine name before query or queries,
# with or without words between (postgres_query, postgres_raw_query,
# bigquery_run_readonly_query), run/execute + query, read/write/start + query.
_SQL_TOOL_NAME = re.compile(
    r"sql|statement|stmt|database|^query$|(^|[_-])(run|execute|exec)[_-]?(query|queries)|(^|[_-])db([_-]|$)"
    r"|(^|[_-])(read|write|start)[_-]?(query|queries)([_-]|$)"
    r"|(^|[_-])(postgres|postgresql|pg|mysql|mariadb|mssql|tsql|oracle|snowflake|bigquery|bq|duckdb|redshift"
    r"|athena|clickhouse|trino|presto|databricks|sqlite|synapse|teradata|vertica|cockroach|spanner)"
    r"[_-]?(\w*[_-])?(query|queries)([_-]|$)",
    re.IGNORECASE)
# Search, natural-language, AI and history tools are never SQL tools,
# whatever engine they name (snowflake_cortex_search_query, pg_query_history,
# databricks_ai_query): their "query" is a question, a prompt or a filter.
# Compared by whole name part.
_NOT_SQL_TOOL_WORDS = frozenset({
    "search", "semantic", "vector", "natural", "genie", "analyst", "ask", "history", "log", "logs", "list",
    "named", "saved", "complete", "ai", "cortex", "prompt",
})
# On a SQL tool, a key naming a query or statement holds SQL.
_SQL_TOOL_KEY = re.compile(r"quer(y|ies)|statement|stmt")
# A parameter whose schema title or description says it holds SQL: it names
# (T-)SQL, or it has statement(s) and run/execute among its words. The whole
# text is read (both checks are linear).
_SQL_DESCRIPTION_WORD = re.compile(r"\b(t-?)?sql\b", re.IGNORECASE)
# Schema nesting read for SQL descriptions; a schema deeper than this makes
# every argument of the tool "sql" (fail closed: the unread part could say so).
_MAX_SCHEMA_DEPTH = 32
# $ref hops followed from one schema node; a chain still unresolved after
# this many makes the parameter "sql" (fail closed, the same reason).
_MAX_REF_HOPS = 8
# Schema nodes read for one parameter's descriptions (its $ref chains,
# allOf/anyOf/oneOf members and array items); more makes it "sql".
_MAX_DESCRIPTION_NODES = 256


class _EveryParam(frozenset):
    """_sql_params result for a schema too deep to read: every name is SQL."""

    def __contains__(self, name: object) -> bool:
        return True


_EVERY_PARAM = _EveryParam()


def _is_sql_tool(tool_name: str) -> bool:
    if not tool_name or not _SQL_TOOL_NAME.search(tool_name):
        return False
    parts = re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", tool_name).lower())
    return not _NOT_SQL_TOOL_WORDS.intersection(parts)


def _described_as_sql(text: str) -> bool:
    if _SQL_DESCRIPTION_WORD.search(text):
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words & {"statement", "statements"}) and bool(words & {"run", "execute"})


def _key_kind(key: str, tool_name: str = "", sql_params: frozenset = frozenset()) -> str:
    k = re.sub(r"[_\-]", "", key.lower())
    if "sql" in k or k in ("statement", "statements", "stmt", "stmts") or key in sql_params:
        return "sql"
    if _is_sql_tool(tool_name):
        return "sql" if k in _SQL_SHARED_KEYS or _SQL_TOOL_KEY.search(k) else "shared"
    return "shared" if k in _SQL_SHARED_KEYS else "other"


def _resolve_ref(node: dict, root: dict) -> Optional[dict]:
    """The schema a local ``$ref`` (#/$defs/X, #/definitions/X, any #/ path)
    points to, or None."""
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    target: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    return target if isinstance(target, dict) else None


def _param_described_as_sql(sub: dict, root: dict, memo: dict) -> bool:
    """Whether a property schema's title or description says it holds SQL:
    its own, what its $ref chain points to, its allOf/anyOf/oneOf members
    (the shapes pydantic writes) and its array ``items``, each with their
    refs. A cycle stops at the first node seen again. Fail closed: a $ref
    chain still unresolved after _MAX_REF_HOPS, or more than
    _MAX_DESCRIPTION_NODES nodes to read, counts as SQL. ``memo`` holds the
    verdict of each node already read, so a $def shared by many properties
    is read once per schema."""
    starts, visited = [sub], set()
    while starts:
        node = starts.pop()
        for _ in range(_MAX_REF_HOPS + 1):
            if node is None or id(node) in visited:
                break
            visited.add(id(node))
            if len(visited) > _MAX_DESCRIPTION_NODES:
                return True
            if id(node) not in memo:
                memo[id(node)] = _described_as_sql(f"{node.get('title') or ''} {node.get('description') or ''}")
            if memo[id(node)]:
                return True
            for field in ("allOf", "anyOf", "oneOf"):
                members = node.get(field)
                if isinstance(members, list):
                    starts.extend(m for m in members if isinstance(m, dict))
            if isinstance(node.get("items"), dict):
                starts.append(node["items"])
            node = _resolve_ref(node, root)
        else:
            if node is not None and id(node) not in visited:
                logger.info("blocklist: schema $ref chain longer than %d hops; parameter read as SQL",
                            _MAX_REF_HOPS)
                return True
    return False


def _sql_params(schema: Any) -> frozenset:
    """Names of the parameters an input schema describes as SQL, at any
    nesting (object properties, array items, $defs, anyOf/oneOf/allOf), with
    local $refs followed. A schema nested past _MAX_SCHEMA_DEPTH gives
    _EVERY_PARAM."""
    names: set = set()
    root = schema if isinstance(schema, dict) else {}
    memo: dict = {}
    stack = [(schema, 0)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        if depth > _MAX_SCHEMA_DEPTH:
            logger.info("blocklist: tool schema nested past %d levels; every argument read as SQL",
                        _MAX_SCHEMA_DEPTH)
            return _EVERY_PARAM
        children: list = []
        props = node.get("properties")
        if isinstance(props, dict):
            for name, sub in props.items():
                if isinstance(sub, dict):
                    if _param_described_as_sql(sub, root, memo):
                        names.add(str(name))
                    children.append(sub)
        for field in ("items", "additionalProperties"):
            children.append(node.get(field))
        for field in ("$defs", "definitions"):
            if isinstance(node.get(field), dict):
                children.extend(node[field].values())
        for field in ("anyOf", "oneOf", "allOf"):
            if isinstance(node.get(field), list):
                children.extend(node[field])
        stack.extend((child, depth + 1) for child in children if isinstance(child, dict))
    return frozenset(names)


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
# EXEC bodies are read as SQL again, at most this many levels deep; a body
# still holding SQL to run at that depth is refused as unreadable.
_MAX_EXEC_DEPTH = 2
# SQL held in literals (an EXEC body, a concatenation of an EXEC's literals,
# a procedure argument) is rechecked one item at a time, each read alone so
# nothing in one can affect another. At most this many rechecks per call
# (block_reason, sql_block_reason or shell_block_reason), across every level
# and argument; a call that needs more is refused as unreadable.
_MAX_RECHECKS = 64
# Argument values nested deeper than this are refused as unreadable.
_MAX_ARGUMENT_DEPTH = 64


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
_EXEC_PROCS = frozenset({"SP_EXECUTESQL", "SP_EXECUTE"})


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


# Token types after which a tokenizer keeps the rest of the statement as one
# raw STRING: EXEC (tsql, postgres), EXECUTE (every dialect), PREPARE, ….
_COMMAND_TYPES = frozenset({"COMMAND", "EXECUTE"})
_RAW_LITERAL = re.compile(r"[Nn]?'((?:[^']|'')*)'")
# A raw EXEC/EXECUTE/PREPARE body that runs SQL held in a variable:
# EXEC(@q), EXEC sp_executesql @q, EXECUTE IMMEDIATE :v, PREPARE s FROM @q.
_DYNAMIC_BODY = re.compile(r"^\s*(\(\s*)?[@:]|^\s*sp_executesql\s+[@:]|^\s*immediate\s+(?!['\"$])|\bfrom\s+[@:]",
                           re.IGNORECASE)
_PREPARE_AS = re.compile(r"^\s*\w+\s*(\([^)]*\)\s*)?as\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _add(items: dict, texts: list) -> None:
    for t in texts:
        items[t] = None
    if len(texts) > 1:
        items["".join(texts)] = None  # split strings: EXEC('DR' + 'OP TABLE x')


# Statement words some tokenizers read as a plain name (VAR): a segment
# starting with one is never a procedure call.
_NOT_A_PROCEDURE = frozenset({
    "IF", "WHILE", "DECLARE", "PRINT", "THROW", "RAISERROR", "WAITFOR", "RETURN", "GOTO", "BREAK", "CONTINUE",
    "SETUSER", "BULK", "NOTIFY", "DBCC",
})
# Tokens an argument list of a procedure call can start with.
_PROC_ARGUMENT_START = frozenset({
    "STRING", "NATIONAL_STRING", "UNICODE_STRING", "NULL", "NUMBER", "DASH", "PARAMETER", "DEFAULT", "VAR",
    "IDENTIFIER",
})


def _proc_call_at_start(toks: list) -> Optional[int]:
    """Index of the procedure name a segment starts with when the segment is
    a procedure call without EXEC (``sp_MSforeachtable 'DELETE FROM ?'``,
    ``dbo.run N'…'``, ``[dbo].[run] NULL, N'…'``), or None. T-SQL runs the
    first statement of a batch without EXEC, so such a segment runs its
    literals like EXEC does. The name is a plain, qualified or bracketed name
    (never a keyword token or a statement word in _NOT_A_PROCEDURE) followed
    by an argument, not by "(", "=", ":" or more name."""
    def name_part(k: int) -> bool:
        return k < len(toks) and toks[k].token_type.name in ("VAR", "IDENTIFIER")

    if not name_part(0):
        return None
    i = 0
    while i + 1 < len(toks) and toks[i + 1].token_type.name == "DOT":
        i += 1
        while i + 1 < len(toks) and toks[i + 1].token_type.name == "DOT":
            i += 1
        i += 1
        if not name_part(i):
            return None
    if i == 0 and toks[0].token_type.name == "VAR" and toks[0].text.upper() in _NOT_A_PROCEDURE:
        return None
    if i + 1 >= len(toks) or toks[i + 1].token_type.name not in _PROC_ARGUMENT_START:
        return None
    return i


def _literal_items(tokens: list, items: dict) -> None:
    """Text in this SQL that would itself run as SQL, added to ``items`` in
    order: the string literals of a statement with EXEC/EXECUTE or one that
    is a procedure call without EXEC (and their concatenation), the literal of
    PREPARE … FROM '…' and the statement of PREPARE … AS …. When SQL held in a variable is run (EXEC(@q), EXEC
    sp_executesql @q, PREPARE s FROM @q), the text can come from any
    statement, so every literal of the whole text is added too."""
    every: list = []
    dynamic = False
    for kws, toks in _segments(tokens):
        proc = _proc_call_at_start(toks)
        has_exec = proc is not None or any(k in _EXEC_WORDS for k in kws)
        if (proc is not None and toks[proc].text.upper() in _EXEC_PROCS
                and toks[proc + 1:proc + 2] and toks[proc + 1].token_type.name == "PARAMETER"):
            dynamic = True  # sp_executesql @q
        parts: list = []
        for i, t in enumerate(toks):
            kind = t.token_type.name
            if kws[i] in _EXEC_WORDS and kind not in _COMMAND_TYPES:
                # EXEC read as a word: EXEC(@q), EXEC sp_executesql @q.
                after = [x.token_type.name for x in toks[i + 1:i + 4]]
                nxt = kws[i + 1] if i + 1 < len(kws) else None
                if after[:1] == ["PARAMETER"] or after[:2] == ["L_PAREN", "PARAMETER"] or (
                        nxt == "SP_EXECUTESQL" and after[1:2] == ["PARAMETER"]):
                    dynamic = True
                continue
            if kind not in _LITERAL_TOKENS or kind == "IDENTIFIER":
                continue
            if i > 0 and toks[i - 1].token_type.name in _COMMAND_TYPES:
                # The raw rest of a command: its own literals are what it holds.
                inner = [m.replace("''", "'") for m in _RAW_LITERAL.findall(t.text)]
                every.extend(inner)
                head = kws[i - 1]
                if head in _EXEC_WORDS or head == "PREPARE":
                    dynamic = dynamic or bool(_DYNAMIC_BODY.search(t.text))
                    _add(items, inner)
                    if head == "PREPARE":
                        m = _PREPARE_AS.match(t.text)
                        if m:
                            items[m.group(2)] = None
                    elif (re.search(r"[\"$]", t.text)
                          or _passes_gate(_RAW_LITERAL.sub("''", t.text).lower(), "sql")):
                        # More than literals: a double- or dollar-quoted body, or
                        # SQL after the call (a T-SQL batch needs no separator).
                        # Read as an EXEC statement by the tokenizers that read
                        # EXEC as a word.
                        items["EXEC " + t.text] = None
                continue
            every.append(t.text)
            if has_exec:
                parts.append(t.text)
        _add(items, parts)
    if dynamic:
        _add(items, every)


class _Budget:
    """Rechecks left for one call (_MAX_RECHECKS)."""

    def __init__(self) -> None:
        self.left = _MAX_RECHECKS


def _recheck_literals(text: str, items: dict, depth: int, seen: frozenset, budget: _Budget) -> set:
    """Read each item as SQL on its own, at level "sql". Only items with a
    gate word are read. An item whose exact text was already read at a
    shallower level (``seen``) is not read again: the tokenizers that keep an
    EXEC body raw give back the "EXEC " + body item this module made,
    unchanged. Only the exact text is skipped: a near copy (other case or
    spacing) can hold different SQL one level deeper. Past _MAX_EXEC_DEPTH,
    any item still to read makes the text unreadable, and so does a call
    that needs more than _MAX_RECHECKS reads."""
    seen = seen | {text}
    pending = [t for t in items if _passes_gate(t.lower(), "sql") and t not in seen]
    if not pending:
        return set()
    if depth >= _MAX_EXEC_DEPTH:
        logger.info("blocklist: SQL to run nested past %d levels; refused", _MAX_EXEC_DEPTH)
        return {REASON_UNREADABLE}
    if len(pending) > budget.left:
        logger.info("blocklist: more than %d SQL literals to recheck in one call; refused", _MAX_RECHECKS)
        return {REASON_UNREADABLE}
    budget.left -= len(pending)
    seen = seen | set(pending)
    found: set = set()
    for t in pending:
        found.update(_sql_findings(t, "sql", depth + 1, seen, budget))
    return found


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


def _scan_sql_tokens(tokens: list, found: set) -> None:
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


def _scan_shared_tokens(tokens: list, found: set) -> None:
    """The anchored shapes of a "shared" text, at a segment start only (a
    token port of the old shared regexes)."""
    for kws, toks in _segments(tokens):
        n = len(kws)
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


def _scan_tree(text: str, level: str, token_lists: dict, found: set) -> None:
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
            # EXEC bodies come from the tokens (_literal_items): the tree's
            # Command node holds the same raw text.
            for node in statement.find_all(exp.Drop, exp.TruncateTable, exp.Delete):
                if isinstance(node, exp.Drop):
                    if _drop_of_a_table(node):
                        found.add(REASON_DROP)
                elif isinstance(node, exp.TruncateTable):
                    if words is None or "TABLE" in words:
                        found.add(REASON_TRUNCATE)
                elif not node.args.get("where") and (words is None or "FROM" in words):
                    found.add(REASON_DELETE)


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


def _sql_findings(text: str, level: str, depth: int = 0, seen: frozenset = frozenset(),
                  budget: Optional[_Budget] = None) -> list[str]:
    """The reasons ``text`` is refused at ``level`` ("sql", "shared" or
    "other"); empty means it passes. ``depth``, ``seen`` and ``budget``
    belong to the recheck of SQL held in literals (_recheck_literals)."""
    budget = budget or _Budget()
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
    readable = [tokens for tokens in token_lists.values() if tokens is not None]
    if level == "sql":
        if "/*!" in text:
            # MySQL runs the body of /*! … */: read it as code too.
            copy = text.replace("/*!", "   ")
            readable += [t for t in (_tokenize(copy, d) for d in _DIALECTS) if t is not None]
        for tokens in readable:
            _scan_sql_tokens(tokens, found)
    else:
        for tokens in readable:
            _scan_shared_tokens(tokens, found)
    if size <= _MAX_PARSE_BYTES:
        _scan_tree(text, level, token_lists, found)
    items: dict = {}  # SQL to run held in literals, in order
    for tokens in readable:
        _literal_items(tokens, items)
    found.update(_recheck_literals(text, items, depth, seen, budget))
    return [reason for reason in _REASON_ORDER if reason in found]


def sql_block_reason(text: str, *, sql_context: Any = True) -> Optional[str]:
    """Why a SQL text is blocked, or None. ``sql_context`` is "sql" (or
    True), "shared" or "other" (or False); see the levels above. Literals and
    comments are never keywords, so a query that searches for 'drop table' in
    a log is not refused; ``EXEC('…')`` bodies are read as SQL on their own."""
    return _sql_reason(text, sql_context, _Budget())


def _sql_reason(text: str, sql_context: Any, budget: _Budget) -> Optional[str]:
    level = {True: "sql", False: "other"}.get(sql_context, sql_context)
    if not isinstance(text, str) or not text.strip():
        return None
    findings = _sql_findings(text, level, budget=budget)
    return findings[0] if findings else None


_TOO_DEEP = object()  # a value nested past _MAX_ARGUMENT_DEPTH


def _strings(value: Any, key: str = "", depth: int = 0) -> Iterable[tuple]:
    """(key, text) for every string in the arguments; a list item gets its
    key. A non-empty value past _MAX_ARGUMENT_DEPTH yields _TOO_DEEP."""
    if isinstance(value, str):
        yield key, value
    elif depth >= _MAX_ARGUMENT_DEPTH:
        if isinstance(value, (dict, list, tuple)) and value:
            yield key, _TOO_DEEP
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, str(k), depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, key, depth + 1)


def _bare_tool_name(tool_name: str) -> str:
    # Namespaced names: "db.drop_table", "server__drop_table", "x/purge_all".
    return re.split(r"[./:]|__", tool_name.strip())[-1]


def block_reason(*, tool_name: str, arguments: Any, remote_shell: bool = False,
                 input_schema: Any = None) -> Optional[str]:
    """Why this connection-tool call is on the built-in blocklist, or None.
    ``input_schema`` is the tool's own JSON schema, when known: a parameter it
    describes as SQL gets the full SQL rules."""
    if isinstance(tool_name, str) and _TOOL_NAME.search(_bare_tool_name(tool_name)):
        return "the tool's name marks it as a mass-delete action"
    args = arguments if isinstance(arguments, dict) else {}
    budget = _Budget()  # one recheck budget for the whole call
    if remote_shell:
        reason = _shell_reason(args.get("command", ""), budget)
        if reason:
            return reason
    bare = _bare_tool_name(tool_name) if isinstance(tool_name, str) else ""
    sql_params = _sql_params(input_schema)
    for key, text in _strings(args):
        if text is _TOO_DEEP:
            logger.info("blocklist: arguments nested past %d levels; refused", _MAX_ARGUMENT_DEPTH)
            return REASON_UNREADABLE
        reason = _sql_reason(text, _key_kind(key, bare, sql_params), budget)
        if reason:
            return reason
    return None
