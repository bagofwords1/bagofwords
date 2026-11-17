"""Built-in blocklist of catastrophic tool calls.

Checked before every other permission rule for connection tools and remote
shell: no permission mode, per-tool ``allow`` or remembered answer can run a
call this module flags. The list is narrow on purpose — it names actions that
are almost never what an analytics assistant should do without a human typing
them (wiping a disk, dropping a database), not everything that writes.

Like the remote shell rules, these are text patterns, not a sandbox: they stop
the obvious spellings an agent produces, and the per-tool settings remain the
real control.
"""

import re
import shlex
from typing import Any, Iterable, Optional

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

# A table name: bare, "quoted", [bracketed] or `backticked`, optionally dotted.
_NAME = r'(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[\w$#@]+)'
_IDENT = _NAME + r"(?:\." + _NAME + r")*"
# An optional table alias ("DELETE FROM orders o", "… AS o"), never WHERE.
_ALIAS = r"(?:\s+(?:as\s+)?(?!where\b|returning\b|using\b)[\w]+)?"

# How much SQL rule an argument gets depends on how sure we are it is SQL:
#   "sql"    — a key that certainly holds SQL (sql, sqlQuery, statement…), or a
#              shared key on a tool whose name says it runs SQL;
#   "shared" — keys a search tool uses too (query, q, command…): only
#              spellings a search phrase doesn't produce;
#   "other"  — anything else: only a text that is a single DROP statement
#              ("Drop table orders_tmp after migration" in a title passes).
_SQL_DROP = re.compile(r"^drop\s+(database|schema|table)\b", re.IGNORECASE)
_SQL_DROP_ONLY = re.compile(
    r"^drop\s+(database|schema|table)\s+(if\s+exists\s+)?" + _IDENT + r"(\s*,\s*" + _IDENT + r")*"
    r"(\s+(cascade|restrict))?\s*;?\s*$", re.IGNORECASE)
_SQL_DROP_ANYWHERE = re.compile(r"(^|[\s(])drop\s+(database|schema|table)\s+(if\s+exists\s+)?" + _IDENT, re.IGNORECASE)
_SQL_TRUNCATE = re.compile(
    r"^truncate\s+(table\s+)?(only\s+)?" + _IDENT + r"(\s*,\s*" + _IDENT + r")*"
    r"(\s+(cascade|restrict|(restart|continue)\s+identity))*\s*$", re.IGNORECASE)
# DELETE FROM <table> with no WHERE (a RETURNING clause doesn't narrow it).
_SQL_DELETE = re.compile(
    r"^delete\s+from\s+(only\s+)?" + _IDENT + _ALIAS + r"(\s+returning\b.*)?\s*$", re.IGNORECASE | re.DOTALL)
# T-SQL's FROM-less form ("DELETE orders") reads like plain words ("delete
# account"), so it is only checked where the argument is certainly SQL.
_SQL_DELETE_NO_FROM = re.compile(r"^delete\s+(only\s+)?" + _IDENT + r"\s*$", re.IGNORECASE)
_SQL_TRUNCATE_TABLE = re.compile(r"^truncate\s+table\b", re.IGNORECASE)
_SQL_START = re.compile(
    r"^\s*(select|with|insert|update|merge|create|alter|exec|execute|begin|declare|use)\b", re.IGNORECASE)
_SQL_COMMENTS = re.compile(r"(--[^\n]*\n?|/\*.*?\*/)", re.DOTALL)
_SQL_BATCH_SEP = re.compile(r";|^\s*go\s*$", re.IGNORECASE | re.MULTILINE)
_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")
# EXEC('…'), EXEC sp_executesql N'…', EXECUTE ('…'): the string is SQL to run.
_SQL_EXEC_BODY = re.compile(r"\bexec(?:ute)?\b\s*(?:\(\s*)?(?:[\w.\[\]]+\s*)?N?'((?:[^']|'')*)'", re.IGNORECASE)
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


def sql_block_reason(text: str, *, sql_context: Any = True, _depth: int = 0) -> Optional[str]:
    """Why a SQL text is blocked, or None. ``sql_context`` is "sql" (or
    True), "shared" or "other" (or False); see the levels above. String
    literals are blanked before scanning, so a query that searches for
    'drop table' in a log is not refused; ``EXEC('…')`` bodies are scanned
    on their own."""
    level = {True: "sql", False: "other"}.get(sql_context, sql_context)
    if not isinstance(text, str) or not text.strip():
        return None
    clean = _SQL_COMMENTS.sub(" ", text)
    if level == "other":
        return "drops a database, schema or table" if _SQL_DROP_ONLY.match(clean.strip()) else None
    if _depth < 2:
        for body in _SQL_EXEC_BODY.findall(clean):
            reason = sql_block_reason(body.replace("''", "'"), sql_context="sql", _depth=_depth + 1)
            if reason:
                return reason
    clean = _SQL_LITERAL.sub("''", clean)
    batch = bool(_SQL_START.match(clean))  # plainly a SQL script
    for stmt in _SQL_BATCH_SEP.split(clean):
        s = (stmt or "").strip()
        if not s:
            continue
        if _SQL_DROP.search(s):
            return "drops a database, schema or table"
        # T-SQL batches need no separators: "SELECT 1 DROP TABLE x".
        if batch and _SQL_DROP_ANYWHERE.search(s):
            return "drops a database, schema or table"
        if level == "sql" or batch:
            if _SQL_TRUNCATE.search(s):
                return "truncates a table"
            if _SQL_DELETE.search(s) or _SQL_DELETE_NO_FROM.search(s):
                return "deletes every row of a table (no WHERE)"
        else:  # shared
            if _SQL_TRUNCATE_TABLE.search(s) and _SQL_TRUNCATE.search(s):
                return "truncates a table"
            if _SQL_DELETE.search(s):
                return "deletes every row of a table (no WHERE)"
    return None


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
