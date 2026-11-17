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

_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "*", ".", "./", "./*", "..", "$HOME", "$HOME/", "${HOME}",
               "/home", "/root", "/etc", "/usr", "/var", "/boot", "/bin", "/lib"}
_POWER_PROGRAMS = {"shutdown", "reboot", "halt", "poweroff"}
_WRAPPERS = {"sudo", "doas", "command", "exec", "nohup", "env", "nice", "time", "timeout"}

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
            toks = _tokens(seg)
            for i, t in enumerate(toks[:-1]):
                # -c, or combined flags such as -lc / -ec.
                if t.startswith("-") and not t.startswith("--") and "c" in t[1:] and i > 0 and toks[i - 1].rsplit("/", 1)[-1] in ("sh", "bash", "zsh", "dash", "ksh"):
                    nested.append(toks[i + 1])
        for body in nested:
            out.extend(_shell_segments(body, depth + 1))
    return [s.strip() for s in out if s and s.strip()]


def _program(tokens: list) -> tuple:
    """(program basename, remaining tokens) after wrappers like ``sudo -n``."""
    i = 0
    while i < len(tokens):
        base = tokens[i].rsplit("/", 1)[-1]
        if base in _WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        return base, tokens[i + 1:]
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
    return recursive and force and any(t in _RM_TARGETS for t in targets)


def shell_block_reason(command: str) -> Optional[str]:
    """Why a remote shell command is blocked, or None."""
    if not isinstance(command, str) or not command.strip():
        return None
    for seg in _shell_segments(command):
        prog, args = _program(_tokens(seg))
        if prog == "rm" and _is_recursive_force_rm(args):
            return "recursive forced delete of a root, home or wildcard path"
        if prog in _POWER_PROGRAMS:
            return "shuts down or restarts the server"
        for pat in _SHELL_PATTERNS:
            if pat.search(seg):
                return "destructive system command"
    return None


# ── SQL / tool names (any connection tool) ──────────────────────────────────

_IDENT = r"[\w\"`\[\].]+"
# DROP is refused wherever it starts a statement. DELETE-without-WHERE and
# TRUNCATE are refused only in arguments that hold SQL (by key name) or in a
# text that is plainly a SQL statement, because "Delete from inbox" or
# "truncate the axis" are ordinary words elsewhere.
_SQL_DROP = re.compile(r"^drop\s+(database|schema|table)\b", re.IGNORECASE)
_SQL_DROP_ANYWHERE = re.compile(r"(^|\s)drop\s+(database|schema|table)\s+(if\s+exists\s+)?" + _IDENT, re.IGNORECASE)
_SQL_TRUNCATE = re.compile(r"^truncate\s+(table\s+)?" + _IDENT + r"\s*$", re.IGNORECASE)
_SQL_DELETE = re.compile(r"^delete\s+from\s+" + _IDENT + r"\s*$", re.IGNORECASE)
_SQL_START = re.compile(
    r"^\s*(select|with|insert|update|merge|create|alter|exec|execute|begin|declare|use)\b", re.IGNORECASE)
_SQL_COMMENTS = re.compile(r"(--[^\n]*\n?|/\*.*?\*/)", re.DOTALL)
_SQL_BATCH_SEP = re.compile(r";|^\s*go\s*$", re.IGNORECASE | re.MULTILINE)
_SQL_KEYS = {"sql", "query", "statement", "statements", "command", "script", "code", "q", "sql_query", "tsql"}

_TOOL_NAME = re.compile(
    r"^(drop|purge|wipe|destroy|nuke)[_-]?(all|database|db|table|schema|everything)"
    r"|^(delete|remove|truncate)[_-]?all([_-]|$)",
    re.IGNORECASE,
)


def sql_block_reason(text: str, *, sql_context: bool = True) -> Optional[str]:
    """Why a SQL text is blocked, or None. ``sql_context`` says the text is in
    an argument meant to hold SQL (enables the DELETE/TRUNCATE rules)."""
    if not isinstance(text, str) or not text.strip():
        return None
    clean = _SQL_COMMENTS.sub(" ", text)
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
        if sql_context or batch:
            if _SQL_TRUNCATE.search(s):
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
    for key, text in _strings(args):
        reason = sql_block_reason(text, sql_context=key.lower() in _SQL_KEYS)
        if reason:
            return reason
    return None
