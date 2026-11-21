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

import fnmatch
import logging
import posixpath
import re
from typing import Any, Callable, Iterable, Optional

import sqlglot
from sqlglot import exp

from app.ai.code_execution.sql_parser import parse_guard_sql

logger = logging.getLogger(__name__)

# ── shell ───────────────────────────────────────────────────────────────────

# Protected roots. A target is compared after trailing "/" and "/*" are
# stripped and the path is normalized (see _protected_target), so "/etc/",
# "/home/*", "~/*", "/etc/." and "/tmp/../etc" all count; "*" alone too.
_RM_ROOTS = {"/", "~", ".", "..", "*",
             "/home", "/root", "/etc", "/usr", "/var", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/srv"}
# The home directory, as _protected_target spells it after expanding ~ and
# $HOME: under /home, so "~/.." normalizes to /home and "~/../.." to /.
_HOME = "/home/~"

# Device nodes. A write to any path under /dev is refused unless the node is
# one of these harmless ones, so a disk needs no name here (sda, nvme0n1,
# vg0/root, nbd0, a glob). The path is normalized first (see _dev_node).
_HARMLESS_DEV = re.compile(
    r"(null|zero|full|random|urandom|stdin|stdout|stderr|tty\w*|console|ptmx|log"
    r"|(fd|pts|shm|mqueue|tcp|udp)(/\S*)?)(?![\w.-])")
# dd keeps its own shorter list (of=/dev/shm/x was refused before, and still is).
_HARMLESS_DD_TARGET = re.compile(r"(null|zero|stdout|stderr)(?![\w.-])")
# What may stand before the path in a word naming a device: an option
# (--device=/dev/sda, --target-directory=…) or a short option (-t/dev/sda).
_DEVICE_OPTION = re.compile(r"(?:--[\w-]+=|-[A-Za-z]*t)?(?=/)")
_GLOB_CHARS = frozenset("*?[")
# A path longer than this with a glob in it is not matched: it counts as a hit.
_MAX_GLOB_CHARS = 256
# Disk tools, formatting tools, tee and truncate, and programs that write what
# they fetch or copy to a path they are given (curl -o, wget -O, ddrescue,
# sponge): refused on a device node wherever it sits in their arguments. dd,
# cp and install: see _token_reason.
_DISK_TOOLS = frozenset({"wipefs", "blkdiscard", "shred", "sgdisk", "sfdisk", "parted", "fdisk", "tee", "truncate",
                         "mke2fs", "mkswap", "curl", "wget", "ddrescue", "sponge"})
# fdisk / sfdisk only list partitions with these (fdisk -l /dev/sda).
_DISK_LISTING = frozenset({"-l", "--list", "-x", "--list-details"})
# A redirect in a text, read without its quotes: > >> >| >& &> and the target.
_REDIRECT_TARGET = re.compile(r">[>|&]?\s*[\"']?([^\s\"';&|<>()]+)")
# A redirect inside one word: the operator and what follows it in the word.
_REDIRECT_IN_WORD = re.compile(r">+[|&]?([^>]*)")
# The end of a Windows drive argument: a space, the end, or what closes or
# follows the command around it ((Remove-Item … C:\), { … C:\ }; …).
_DRIVE_END = r"(?=[\s)};|&]|$)"
_SHELL_PATTERNS = [
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"),          # :(){ :|:& };:
    re.compile(r"\bformat-volume\b", re.IGNORECASE),
    re.compile(r"\bclear-disk\b", re.IGNORECASE),
    # format c: anywhere (cmd /c format c:), not an option's value (-Format H:mm).
    re.compile(r"(?<![\w-])format(\.com)?\s+[a-z]:", re.IGNORECASE),
    # Remove-Item -Recurse -Force on a drive, in one statement: a quoted ; or |
    # before the drive is one more path, not the statement's end.
    re.compile(r"\bremove-item\b(?=.*-recurse)(?=.*-force)(?:\"[^\"]*\"|'[^']*'|[^|;\"'])*\s['\"]?[a-z]:\\?['\"]?"
               + _DRIVE_END, re.IGNORECASE),
    re.compile(r"\b(rd|rmdir)\s+/s\s+/q\s+[a-z]:\\?" + _DRIVE_END, re.IGNORECASE),
]
# Shutting down or restarting: the words anywhere in a command, like mkfs, so
# no program in front of them (ssh host, echo … | sh, docker exec, watch) hides
# them. A command that only mentions one is refused too (accepted false
# refusals, named in the PR: systemctl status reboot.target, echo shutdown),
# except a lone read-only command (see _reads_only).
_POWER_PATTERNS = [
    re.compile(r"(?<![\w-])(shutdown|reboot|halt|poweroff)(?![\w-])", re.IGNORECASE),
    re.compile(r"(?<![\w-])(tel)?init\s+[06](?!\w)"),
    re.compile(r"\bsystemctl\s+(\S+\s+)*kexec\b"),
    re.compile(r"\bstop-computer\b|\brestart-computer\b", re.IGNORECASE),
]


# Programs that only read and never run a command named in their arguments.
# No pager (less +!cmd runs one); journalctl pages only on a terminal, and a
# remote shell has none (ssh -T).
_POWER_READERS = frozenset({"grep", "zgrep", "egrep", "fgrep", "journalctl", "last", "tail", "head", "cat", "ls"})
_SHELL_SYNTAX = re.compile(r"[|;&><$`()]")


def _reads_only(command: str) -> bool:
    """A lone read-only command (grep, last, tail, journalctl…) with no shell
    syntax: none of these runs its arguments, so a shutdown word in them is
    only text. Anything with a pipe, separator, redirect or substitution is
    not, and neither is a command holding any character that is not printable
    (a line break of bash or PowerShell, another control or space character):
    only plain words on one line are read as one command."""
    if _SHELL_SYNTAX.search(command) or not all(c.isprintable() or c == "\t" for c in command):
        return False
    views = _token_views(command.strip())
    return all(words and _program(words[0]) in _POWER_READERS for words, _ in views)


def _program(token: str) -> str:
    """A token as a program name: its basename (/ or \\), lowercased, without
    a Windows .exe/.cmd/.bat ending (SQLCMD.EXE, C:\\tools\\psql.exe)."""
    name = token
    if "/" in token or "\\" in token:
        name = re.split(r"[/\\]", token)[-1]
    name = name.lower()
    return name[:-4] if name.endswith((".exe", ".cmd", ".bat")) else name


# Every character a shell may end a line at: bash at \n; PowerShell (the shell
# of a Windows remote server) at a lone \r too; the rest are the other Unicode
# line boundaries. All are read as \n.
_LINE_BREAKS = {ord(c): "\n" for c in "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"}
_SEPARATOR_CHARS = frozenset(";&|\n")
_RAW_SEPARATORS = re.compile(r"[;&|\n]+")
_ANSI_C_ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|.)", re.DOTALL)
_ANSI_C_NAMED = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
                 "v": "\v"}


def _ansi_c(match: "re.Match") -> str:
    """One escape of a $'…' string, as the character bash makes of it."""
    esc = match.group(1)
    if esc[0] in "xuU" and len(esc) > 1:
        code = int(esc[1:], 16)
    elif esc[0] in "01234567":
        code = int(esc, 8)
    else:
        return _ANSI_C_NAMED.get(esc, esc)
    return chr(code) if code <= 0x10FFFF else match.group(0)


# Characters _lex never reads as syntax, wherever they stand.
_PLAIN_RUN = re.compile(r"[^\\'\"$ \t<>;&|\n()#]+")


def _lex(text: str, comments: bool) -> tuple:
    """A text's words as bash reads them: (words, separators, balanced).

    Quotes, backslashes and $'…' are resolved, so a word is what the program
    receives. ``separators[i]`` is True for a word that is an unquoted run of
    ; & | or line breaks: only those end a simple command (a quoted or
    escaped one is an ordinary word). An unquoted ( or ) is a word of its
    own, never a separator: a substitution or subshell stays inside the
    command around it. An unquoted redirect operator is a word of its own
    too, with its descriptor number and the & or | that belongs to it (2>,
    >>, <<<, &>, 2>&, >|): dd<x is dd, and the word after an operator is
    its target. With ``comments``, an unquoted # at the start of a word
    drops the rest of its line. Never raises: an unclosed quote runs to the
    end of the text and ``balanced`` is False."""
    words: list = []
    seps: list = []
    buf: list = []
    in_word = False
    balanced = True
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal in_word
        if in_word:
            words.append("".join(buf))
            seps.append(False)
            buf.clear()
            in_word = False

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                buf.append(text[i + 1])
                in_word = True
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                balanced, j = False, n
            buf.append(text[i + 1:j])
            in_word = True
            i = j + 1
        elif c == '"':
            in_word = True
            i += 1
            start = i
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '\\"$`\n':
                    buf.append(text[start:i])
                    if text[i + 1] != "\n":
                        buf.append(text[i + 1])
                    i += 2
                    start = i
                else:
                    i += 1
            buf.append(text[start:i])
            if i >= n:
                balanced = False
            i += 1
        elif c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                balanced, j = False, n
            buf.append(_ANSI_C_ESCAPE.sub(_ansi_c, text[i + 2:j]))
            in_word = True
            i = j + 1
        elif c == "$" and i + 1 < n and text[i + 1] == '"':
            i += 1  # $"…" is a double-quoted string
        elif c in " \t":
            flush()
            i += 1
        elif c in "<>" or (c == "&" and text[i + 1:i + 2] == ">"):
            # A redirect operator, with the descriptor number before it and
            # the & or | that belongs to it (2>, >>, <<<, &>, 2>&, >|).
            if in_word and not "".join(buf).isdigit():
                flush()
            j = i + 1
            while j < n and text[j] in "<>":
                j += 1
            if j < n and (text[j] == "&" or (text[j] == "|" and text[j - 1] == ">")):
                j += 1
            buf.append(text[i:j])
            in_word = True
            flush()
            i = j
        elif c in _SEPARATOR_CHARS:
            flush()
            j = i + 1
            while j < n and text[j] in _SEPARATOR_CHARS:
                j += 1
            words.append(text[i:j])
            seps.append(True)
            i = j
        elif c in "()":
            flush()
            words.append(c)
            seps.append(False)
            i += 1
        elif c == "#" and comments and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
        else:
            # An ordinary character, with the run of them after it.
            run = _PLAIN_RUN.match(text, i)
            j = run.end() if run else i + 1
            buf.append(text[i:j])
            in_word = True
            i = j
    flush()
    return tuple(words), tuple(seps), balanced


def _token_views(text: str, raw: bool = False) -> list:
    """The text's readings: each is its words as (words, separators), and
    the rules check each in turn. Every reading that could hide a command is
    checked, so together they can only over-match:

    - bash's reading, with # as an ordinary character (cmd.exe, a # inside a
      here-document) and with # starting a comment;
    - when a quote is left open: bash's words again, without separators;
    - when a quote is left open, the text has a backslash (a POSIX reading
      drops it, a Windows path keeps it: C:\\tools\\psql.exe) or the caller
      asks (``raw``): the raw words with their quote characters stripped,
      and the raw words.

    A reading without separators (None) makes a rule that stops at one read
    on. The raw readings come last."""
    words, seps, balanced = _lex(text, comments=False)
    views = [(words, seps)]
    if "#" in text:
        words, seps, closed = _lex(text, comments=True)
        views.append((words, seps))
        balanced = balanced and closed
    views = list(dict.fromkeys(views))
    if not balanced:
        # Where the open quote really closes is not known, so neither is
        # which separators are real: the same words are read without any.
        views.extend([(words, None) for words, _ in views])
    if raw or not balanced or "\\" in text:
        words = tuple(text.split())
        views.append((tuple(w.replace('"', "").replace("'", "") for w in words), None))
        if words != views[-1][0]:
            views.append((words, None))
    return views


_QUOTING_CHARS = frozenset("\"'\\()")
_QUOTING_TO_SPACES = {ord(c): " " for c in _QUOTING_CHARS}


def _unquoted_view(text: str) -> tuple:
    """One more reading of a command or a nested body, as (words,
    separators): its words with every quote, backslash and parenthesis read
    as a space, so each ; & | and line break is a real separator. This is
    what is run in the end however many shells unquote the text on the way
    (eval "eval '…'", ssh host "bash <<< '…'") and whatever call syntax of
    another interpreter holds it (os.system('…'), system("…")). The word
    rules read it like any other reading, so it can only over-match."""
    words, seps, _ = _lex(text.translate(_QUOTING_TO_SPACES), comments=False)
    return words, seps


def _joined_segments(words: tuple, seps: Optional[tuple]) -> list:
    """The simple commands of one reading, each as a text of its words joined
    by spaces. The words have lost one level of quotes, so reading a segment
    again reads what a second shell would (ssh host "…", eval "…", echo "…" |
    sh): one more over-matching reading. A raw reading is cut at every
    separator character, quoted or not."""
    if seps is None:
        return _RAW_SEPARATORS.split(" ".join(words))
    out, seg = [], []
    for word, sep in zip(words, seps, strict=True):
        if sep:
            out.append(" ".join(seg))
            seg = []
        else:
            seg.append(word)
    out.append(" ".join(seg))
    return out


def _substitution_bodies(text: str) -> list:
    """The body of every backtick pair and of every $( ), <( ) and >( ) in
    the text, at any nesting, found without regard to quotes. Each body
    holds the ones inside it, so reading all of them costs the text's size
    times the nesting: one nested past _MAX_SUBSTITUTION_DEPTH is not
    collected, and neither are bodies that together pass _MAX_SHELL_WORK.
    The command is unreadable then."""
    bodies = re.findall(r"`([^`]*)`", text)
    # For each open (: where it is, whether it starts a substitution, and the
    # deepest nesting of substitutions closed inside it so far.
    stack: list = []
    total = 0
    for i, c in enumerate(text):
        if c == "(":
            stack.append([i, i > 0 and text[i - 1] in "$<>", 0])
        elif c == ")" and stack:
            start, substitution, nesting = stack.pop()
            nesting += substitution
            if nesting > _MAX_SUBSTITUTION_DEPTH:
                raise _ShellUnreadable()
            if stack and stack[-1][2] < nesting:
                stack[-1][2] = nesting
            if substitution:
                total += i - start
                if total > _MAX_SHELL_WORK:
                    raise _ShellUnreadable()
                bodies.append(text[start + 1:i])
    return bodies


# Nested shell bodies are read this many levels deep; a deeper one is refused.
_MAX_SHELL_DEPTH = 3
# Substitutions ($( ), <( ), >( )) are read this many levels inside each
# other; a deeper one is refused.
_MAX_SUBSTITUTION_DEPTH = 4
# Text read for one command, in characters, across every segment, line and
# nested body: 64 times the longest command. A command that needs more (only
# deeply nested substitutions do) is refused, so the work stays linear.
_MAX_SHELL_WORK = 64 * 16 * 1024
REASON_SHELL_UNREADABLE = "could not read the command safely"


class _ShellUnreadable(Exception):
    """The command holds a body this check would not read: refuse, never skip."""


class _ShellReading:
    """Every text of a command the rules read, each once: the command, its
    lines, the simple commands between separators, and the body of any
    ``sh -c '…'``, backtick, ``$( )``, ``<( )`` or ``>( )`` inside them, so
    a blocked action can't hide behind a separator, a line break, a quote
    that only one shell reads, or a nested shell. The command and each
    nested body also get a reading without any quoting (_unquoted_view)."""

    def __init__(self) -> None:
        self.texts: dict = {}   # text -> its readings, in the order found
        self.unquoted: dict = {}  # command or nested body -> its reading without quoting
        self._raw: set = set()  # texts whose raw readings are wanted too
        self._shells: dict = {}  # a reading's words -> the nested shell bodies in them
        self._work = 0

    def _spend(self, text: str) -> None:
        self._work += len(text) + 1
        if self._work > _MAX_SHELL_WORK:
            raise _ShellUnreadable()

    def views(self, text: str) -> list:
        raw = text in self._raw
        if text not in self.texts or (raw and self.texts[text][-1][1] is not None):
            self._spend(text)
            self.texts[text] = _token_views(text, raw)
        return self.texts[text]

    def read(self, roots: list) -> None:
        # (text, depth, whether it is the body of a substitution)
        queue = [(root.strip(), 0, False) for root in roots]
        # A body is read once (its readings are kept), but its depth is the
        # deepest it is found at: a segment with a level of quotes removed
        # shows a body one level early, and that must not loosen the bound.
        # No body is found deeper than _MAX_SHELL_DEPTH, so each is walked at
        # most that many times more.
        deepest: dict = {}
        while queue:
            next_queue = []
            for root, depth, substituted in queue:
                if not root or deepest.get((root, substituted), -1) >= depth:
                    continue
                deepest[(root, substituted)] = depth
                if root not in self.unquoted and not _QUOTING_CHARS.isdisjoint(root):
                    self._spend(root)
                    self.unquoted[root] = _unquoted_view(root)
                # A line is read on its own too: nothing on an earlier line (a
                # quote in a comment or a here-document) can hide it.
                lines = [line.strip() for line in root.split("\n")] if "\n" in root else []
                level = dict.fromkeys([root] + [line for line in lines if line])
                for unit in list(level):
                    segments: list = []
                    for words, seps in self.views(unit):
                        joined = _joined_segments(words, seps)
                        if seps is not None:
                            # A separator left in a segment was quoted: for
                            # the shell that reads the unquoted text it is a
                            # real one, but which reading is right is not
                            # known here, so the raw words (which read on
                            # past it) are checked as well.
                            self._raw.update(s.strip() for s in joined if not _SEPARATOR_CHARS.isdisjoint(s))
                        segments.extend(joined)
                    level.update(dict.fromkeys(s.strip() for s in segments if s.strip()))
                # The substitutions of a text are found at every nesting at
                # once, so those inside a substitution's body are already
                # queued beside it: they are no deeper level of shell.
                substitutions = [] if substituted else _substitution_bodies(root)
                shells: list = []
                for text in level:
                    for words, _ in self.views(text):
                        if words not in self._shells:
                            self._shells[words] = _nested_bodies(words)
                        shells.extend(self._shells[words])
                nested = [(body.strip(), kind) for bodies, kind in ((substitutions, True), (shells, False))
                          for body in dict.fromkeys(bodies) if body.strip()]
                if nested and depth >= _MAX_SHELL_DEPTH:
                    raise _ShellUnreadable()
                next_queue.extend((body, depth + 1, kind) for body, kind in nested)
            queue = next_queue


_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "su", "runuser"}


def _nested_bodies(toks: list) -> list:
    """Command strings run by a nested shell in this segment: ``bash -l -c X``,
    ``sh -xc X``, ``su root -c X``, ``su -c X``, ``--command=X``."""
    bodies = []
    for i, t in enumerate(toks):
        if _program(t) not in _SHELLS:
            continue
        for j in range(i + 1, len(toks)):
            a = toks[j]
            if _program(a) in _SHELLS:
                break  # that shell reads the same options (linear work on "sh sh sh …")
            if a in ("-c", "--command") or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                if j + 1 < len(toks):
                    bodies.append(toks[j + 1])
                break
            if a.startswith("--command="):
                bodies.append(a.split("=", 1)[1])
                break
    return bodies


_VARIABLE_WILDCARD = re.compile(r"\$(\w+|\{[^}]*\})(/+\*)+/*")
_HOME_PREFIX = re.compile(r"~([\w.+-]*)(?=/|$)")
_HOME_OR_PWD = re.compile(r"\$\{(HOME|PWD)[^}]*\}(?=/|$)")
_PATH_CHARS = frozenset("/~.*$")


def _protected_target(target: str) -> bool:
    """Whether a path names a protected root: after "/*" and "/" endings are
    stripped, ~, ~user and $HOME expanded and the path normalized, it is one
    of _RM_ROOTS or a home directory, or only ".." parts (../.., ./..). A
    wildcard under any variable ("$DIR/"*, ${DIR}/*) counts too: with the
    variable unset it is /* (an accepted false refusal when it is set)."""
    if target and _PATH_CHARS.isdisjoint(target):
        return False  # a plain word: every root, home or variable spelling holds one of these
    if _VARIABLE_WILDCARD.fullmatch(target):
        return True
    t = target
    while t.endswith("/*") or (t.endswith("/") and t != "/"):
        t = t[:-2] if t.endswith("/*") else t[:-1]
    if not t:
        return True
    home = _HOME_PREFIX.match(t)
    if home:
        # ~root is /root; ~ and any other ~user are a home directory.
        t = ("/root" if home.group(1) == "root" else _HOME) + t[home.end():]
    else:
        # $HOME is the home directory; $PWD is "." (the working directory).
        # With an expansion operator (${HOME%/*}, ${HOME:?}) it is read as
        # the directory itself, which can only over-match.
        braced = _HOME_OR_PWD.match(t)
        if braced:
            t = (_HOME if braced.group(1) == "HOME" else ".") + t[braced.end():]
        else:
            for var, path in (("$HOME", _HOME), ("$PWD", ".")):
                if t == var or t.startswith(var + "/"):
                    t = path + t[len(var):]
                    break
    t = _absolute(t)
    if t in _RM_ROOTS or t == _HOME or all(part == ".." for part in t.split("/")):
        return True
    # A glob that can name a protected directory (/et?, /[e]tc, /h*).
    if t.startswith("/") and not _GLOB_CHARS.isdisjoint(t):
        return len(t) > _MAX_GLOB_CHARS or any(fnmatch.fnmatchcase(root, t) for root in _GLOB_ROOTS)
    return False


_GLOB_ROOTS = tuple(sorted(root for root in _RM_ROOTS if root.startswith("/") and root != "/"))
# /proc/<pid>/root is the root directory under another name.
_PROC_ROOT = re.compile(r"(/proc/[^/]+/root)+(?=/|$)")


def _absolute(path: str) -> str:
    """A path normalized: "." and ".." parts resolved, repeated slashes
    collapsed, a leading /proc/<pid>/root dropped. A relative path stays
    relative."""
    path = re.sub(r"^/+", "/", posixpath.normpath(path))  # normpath keeps a leading "//"
    proc_root = _PROC_ROOT.match(path)
    return (path[proc_root.end():] or "/") if proc_root else path


def _dev_node(path: str) -> Optional[str]:
    """What a path names under /dev once normalized (//dev/sda, /dev/./sda,
    /tmp/../dev/sda, /dev/shm/../sda are all "sda"; "" is /dev itself), or
    None for a path outside /dev. A glob that can name /dev counts as it
    (/d?v/sda)."""
    if not path.startswith("/"):
        return None
    first, _, rest = _absolute(path).lstrip("/").partition("/")
    if first != "dev" and (_GLOB_CHARS.isdisjoint(first) or len(first) > _MAX_GLOB_CHARS
                           or not fnmatch.fnmatchcase("dev", first)):
        return None
    return rest


def _device(path: str, harmless: "re.Pattern" = _HARMLESS_DEV) -> bool:
    """Whether a path names a device node a write could destroy: anything
    under /dev but a harmless node (/dev/null, /dev/stderr, /dev/shm/…)."""
    node = _dev_node(path)
    return bool(node) and not harmless.match(node)


def _names_device(word: str) -> bool:
    """A word naming a device node: its path alone, as an option's value or
    attached to a short option (see _DEVICE_OPTION)."""
    if "/" not in word:
        return False
    m = _DEVICE_OPTION.match(word)
    return m is not None and _device(word[m.end():])


def _dd_device(path: str) -> bool:
    """dd's of= target under /dev, by dd's own short list of harmless nodes.
    A harmless node counts only spelled plainly (/dev/null, not /dev/./null),
    as before paths were normalized."""
    node = _dev_node(path)
    if node is None:
        return False
    return not (_HARMLESS_DD_TARGET.match(node) and path.startswith("/dev/") and _HARMLESS_DD_TARGET.match(path[5:]))


def _redirects_to_device(text: str) -> bool:
    """A redirect onto a device node anywhere in a text, read without its
    quotes (echo x > /dev/sda, >| //dev/sda, >> "/dev/sda", &>/dev/vg0/root)."""
    return any(_device(m.group(1)) for m in _REDIRECT_TARGET.finditer(text))


# (The part before the first comma, and before the first "..", is matched
# without commas or dots: one way to match, so an unclosed brace costs a
# single pass.)
_BRACE_LIST = re.compile(r"\{([^{},]*,[^{}]*)\}")
_BRACE_PIECES = re.compile(r"[{},]+")
_BRACE_RANGE = re.compile(r"\{[^{},\s.]*\.\.[^{},\s]*\}")
# A word's brace lists are expanded up to this many words; a word with more is
# read as its pieces instead (see _expand_braces).
_MAX_BRACE_WORDS = 64


def _expand_braces(words: tuple, seps: tuple) -> tuple:
    """Words with their brace lists expanded as bash does ({rm,-rf,/etc} is
    three words, /{etc,var} is /etc and /var). A word that would expand to
    more than _MAX_BRACE_WORDS is split at its braces and commas instead:
    every piece is then a word, which can only over-match."""
    if not any("{" in w for w in words):
        return words, seps
    out_words, out_seps = [], []
    for word, sep in zip(words, seps, strict=True):
        if ".." in word and not sep:
            word = _BRACE_RANGE.sub("*", word)  # {a..z} is read as a wildcard
        forms = [word]
        if not sep and _BRACE_LIST.search(word):
            while True:
                expanded = []
                for form in forms:
                    m = _BRACE_LIST.search(form)
                    if m is None:
                        expanded.append(form)
                        continue
                    if len(expanded) + m.group(1).count(",") >= _MAX_BRACE_WORDS:
                        expanded = None
                        break
                    expanded.extend(form[:m.start()] + alt + form[m.end():] for alt in m.group(1).split(","))
                if expanded is None:
                    forms = [piece for piece in _BRACE_PIECES.split(word) if piece]
                    break
                if expanded == forms:
                    break
                forms = expanded
        out_words.extend(forms)
        out_seps.extend([sep] * len(forms))
    return tuple(out_words), tuple(out_seps)


# find's own directory is not a protected root for it (find . -name x -delete).
_WORKING_DIRECTORY = frozenset({".", "$PWD", "${PWD}"})
_EXEC_ACTIONS =frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_GLOBAL_OPTION = re.compile(r"-[HLP]|-O\d*|--")
_DELETING_PROGRAMS = frozenset({"rm", "shred", "unlink"})
# A redirect operator alone (its target is the next word), or with its target
# attached (2>/dev/null, >log, 2>&1).
_REDIRECT_ALONE = re.compile(r"\d*(>>?|>\||<>|<<?<?|[<>]&|&>>?)")
_REDIRECT_ATTACHED = re.compile(r"\d*(>|<|&>)")
_CP_VALUE_OPTIONS = frozenset({"-t", "-S", "-m", "-o", "-g", "--target-directory", "--suffix", "--mode", "--owner",
                               "--group"})
# Option words skipped while looking for find's roots or cp's first operand.
# Past this many the command is not read further: refused (find) or every
# disk device in it counts as a target (cp), so the work stays linear.
_MAX_OPTION_WORDS = 64


def _cp_source(tokens: list, k: int, end: int) -> Optional[int]:
    """Index of the first operand (the source) of cp/install whose options
    start at ``k``, or None. Redirects are skipped; an option that may take a
    value skips it too, which can only make more devices count as targets."""
    skipped = 0
    while k < end and skipped <= _MAX_OPTION_WORDS:
        a = tokens[k]
        if _REDIRECT_ALONE.fullmatch(a):
            k += 2
        elif _REDIRECT_ATTACHED.match(a):
            k += 1
        elif a == "--":
            return k + 1 if k + 1 < end else None
        elif a.startswith("-") and len(a) > 1:
            takes_value = a in _CP_VALUE_OPTIONS or (not a.startswith("--") and a[-1] in "tSmog")
            k += 2 if takes_value else 1
        else:
            return k
        skipped += 1
    return None


_REASON_POWER = "shuts down or restarts the server"
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
# Every program name a rule of _token_reason starts from.
_RULE_PROGRAMS = _DISK_TOOLS | {"rm", "chmod", "chown", "chgrp", "find", "init", "telinit", "dd", "cp", "install"}


class _WordFacts:
    """What a word is, kept for one check: the words of a command come back
    in every reading of every text that holds them."""

    def __init__(self) -> None:
        self.names: dict = {}   # word -> its program name
        self.places: dict = {}  # word -> (names a device node, names a protected root)


def _token_reason(words: tuple, seps: Optional[tuple], power: bool = True,
                  facts: Optional[_WordFacts] = None) -> Optional[str]:
    """rm, find, chmod/chown/chgrp, dd, cp/install, the disk tools, init and
    redirects anywhere in one reading of a text, so no wrapper spelling can
    hide them. rm, find and chmod read the rest of the words; the others the
    words up to the next separator (``seps``: an unquoted ; & | or line
    break, never a quoted one or a parenthesis; None for a raw reading, which
    has none and reads on). ``power`` is off for a lone read-only command,
    whose shutdown words are text. Facts about the words to the right are
    gathered in one pass, so the work is linear ("rm rm rm …" included).
    ``facts`` carries what is known of each word from one reading to the
    next."""
    facts = facts or _WordFacts()
    lexed = seps is not None
    tokens, seps = _expand_braces(words, seps if lexed else (False,) * len(words))
    n = len(tokens)
    # A word's program name; for an assignment (x=rm, alias del=rm) the
    # program it names, which a later word may run ($x -rf /).
    known = facts.names
    for t in tokens:
        if t not in known:
            known[t] = _program(_ASSIGNMENT.sub("", t) if "=" in t else t)
    names = [known[t] for t in tokens]
    if _RULE_PROGRAMS.isdisjoint(names) and not any(">" in t for t in tokens):
        return None  # no program a rule below reads, and no redirect
    places = facts.places
    for t in tokens:
        if t not in places:
            places[t] = (_names_device(t), not t.startswith("-") and _protected_target(t))
    disk = [places[t][0] for t in tokens]
    target = [places[t][1] for t in tokens]
    # Suffix facts: index i covers tokens[i:].
    r_flag, f_flag, no_preserve, tree_r, protected, delete, exec_rm = ([False] * (n + 1) for _ in range(7))
    # Prefix counts: tokens[:i].
    disks, dd_disk, listing, other, find_root, deleting = ([0] * (n + 1) for _ in range(6))
    runlevel = [0] * (n + 1)
    # Substitutions: for the ( of a $( ) and for every word of a backtick
    # pair, the index of the word that closes it; -1 elsewhere.
    group_end = [-1] * (n + 1)
    open_parens: list = []
    backtick_start = -1
    for i, t in enumerate(tokens):
        if t == "(" and not seps[i]:
            open_parens.append(i)
        elif t == ")" and open_parens:
            start = open_parens.pop()
            if start and tokens[start - 1].endswith("$"):
                group_end[start] = i
        if t.count("`") % 2:
            if backtick_start < 0:
                backtick_start = i
            else:
                for k in range(backtick_start, i + 1):
                    group_end[k] = i
                backtick_start = -1
        runlevel[i + 1] = runlevel[i] + (t in ("0", "6"))
        if ">" in t and not seps[i]:
            # A redirect: its target is the rest of the word, or the next word.
            for m in _REDIRECT_IN_WORD.finditer(t):
                to = m.group(1) or (tokens[i + 1] if m.end() == len(t) and i + 1 < n and not seps[i + 1] else "")
                if _device(to):
                    return "destructive system command"
        disks[i + 1] = disks[i] + disk[i]
        dd_disk[i + 1] = dd_disk[i] + (t.startswith("of=") and _dd_device(t[3:]))
        listing[i + 1] = listing[i] + (t in _DISK_LISTING)
        other[i + 1] = other[i] + (not disk[i] and t not in _DISK_LISTING)
        find_root[i + 1] = find_root[i] + (target[i] and t.rstrip("/") not in _WORKING_DIRECTORY)
        deleting[i + 1] = deleting[i] + (names[i] in _DELETING_PROGRAMS)
    next_sep, next_term, next_opt = [n] * (n + 1), [n] * (n + 1), [n] * (n + 1)
    for i in range(n - 1, -1, -1):
        t = tokens[i]
        short = t.startswith("-") and not t.startswith("--") and len(t) > 1
        # A long option may be cut short (GNU: --r, --recurs, --no-p).
        cut = t if len(t) > 2 and t.startswith("--") else "\x00"
        recursive = "--recursive".startswith(cut)
        r_flag[i] = r_flag[i + 1] or recursive or (short and ("r" in t or "R" in t))
        f_flag[i] = f_flag[i + 1] or "--force".startswith(cut) or (short and "f" in t)
        no_preserve[i] = no_preserve[i + 1] or "--no-preserve-root".startswith(cut)
        tree_r[i] = tree_r[i + 1] or recursive or (short and "R" in t)
        protected[i] = protected[i + 1] or target[i]
        delete[i] = delete[i + 1] or t == "-delete"
        next_sep[i] = i if seps[i] else next_sep[i + 1]
        next_term[i] = i if t in (";", "+") else next_term[i + 1]
        next_opt[i] = i if (t.startswith("-") or t in ("(", "!")) else next_opt[i + 1]
        exec_rm[i] = exec_rm[i + 1] or (
            t in _EXEC_ACTIONS and deleting[next_term[i + 1]] - deleting[i + 1] > 0)

    for i, name in enumerate(names):
        a = i + 1
        if name == "rm" and (no_preserve[a] or (r_flag[a] and f_flag[a] and protected[a])):
            return "recursive forced delete of a root, home or wildcard path"
        if name in ("chmod", "chown", "chgrp") and tree_r[a] and protected[a]:
            return "destructive system command"
        if name == "find":
            # The roots: the words before the first option, after the global
            # options. A substitution among them is stepped over (its words
            # are not find's: find $(echo -L) / -delete).
            j, skipped, roots = a, 0, 0
            while True:
                while j < n and (tokens[j] == "-D" or _FIND_GLOBAL_OPTION.fullmatch(tokens[j])):
                    j += 2 if tokens[j] == "-D" else 1
                    skipped += 1
                    if skipped > _MAX_OPTION_WORDS:
                        return REASON_SHELL_UNREADABLE
                j = min(j, n)
                rest = next_opt[j]
                roots += find_root[rest] - find_root[j]
                if rest >= n or group_end[rest] < 0:
                    break
                j = group_end[rest] + 1
                skipped += 1
                if skipped > _MAX_OPTION_WORDS:
                    return REASON_SHELL_UNREADABLE
            if roots > 0 and (delete[rest] or exec_rm[rest]):
                return "destructive system command"
        end = next_sep[a]
        if power and lexed and name in ("init", "telinit") and runlevel[end] - runlevel[a] > 0:
            return _REASON_POWER  # init --no-wall 6: options between the two
        on_disks = disks[end] - disks[a]
        if name in _DISK_TOOLS and on_disks:
            if (name in ("fdisk", "sfdisk") and other[end] - other[a] == 0
                    and listing[end] - listing[a] > 0):
                continue  # fdisk -l /dev/sda only lists the partitions
            return "destructive system command"
        if name == "dd" and dd_disk[end] - dd_disk[a] > 0:
            return "destructive system command"
        if name in ("cp", "install") and on_disks:
            source = _cp_source(tokens, a, end)
            if on_disks - (1 if source is not None and disk[source] else 0) > 0:
                return "destructive system command"
    return None


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
    client appears at all). A word after a client is read with the options of
    every client before it, in one pass (linear work on "psql psql …")."""
    sql, seen = [], False
    opts, letters = set(), set()
    for j, a in enumerate(tokens):
        base = _program(a)
        if base in _DB_CLIENT_SQL_OPTS:
            seen = True
            opts |= _DB_CLIENT_SQL_OPTS[base]
            letters = {o[1] for o in opts if len(o) == 2 and o[0] == "-"}
            continue
        if seen:
            option, has_value, value = a.partition("=")
            # A long option may be cut short (--comm, --exec, --que).
            if option not in opts and len(option) > 3 and option.startswith("--") and any(
                    o.startswith(option) for o in opts):
                option = next(o for o in sorted(opts) if o.startswith(option))
            if a in opts and j + 1 < len(tokens):
                sql.append(tokens[j + 1])
            elif option in opts and has_value:
                sql.append(value)
            elif option in opts and j + 1 < len(tokens):
                sql.append(tokens[j + 1])
            elif len(a) > 2 and a[0] == "-" and a[1] != "-":
                # A short-option cluster: the rest of the token after an SQL
                # option's letter is its SQL (-c"DROP …", -Xc"DROP …"); with
                # nothing after it, the next token is (-Xc "DROP …"). Over-
                # reading another option's value only adds text to check.
                for letter in letters:
                    k = a.find(letter, 1)
                    if k < 0:
                        continue
                    rest = a[k + 1:].lstrip("=")
                    if rest:
                        sql.append(rest)
                    elif j + 1 < len(tokens):
                        sql.append(tokens[j + 1])
    return sql, seen


def shell_block_reason(command: str) -> Optional[str]:
    """Why a remote shell command is blocked, or None."""
    return _shell_reason(command, _Budget())


# The longest command read: execute_shell's schema allows 16384 characters;
# counted here in UTF-8 bytes. A longer one is refused before any pattern.
_MAX_SHELL_BYTES = 16 * 1024
_IFS = re.compile(r"\$IFS(?![A-Za-z0-9_])|\$\{IFS[^}]*\}")
_EMPTY_PARAMETER = re.compile(r"\$[0-9@*]|\$\{[0-9@*]\}")
_WINDOWS_ESCAPES = re.compile(r"[`^]")


def _shell_reason(command: str, budget: "_Budget") -> Optional[str]:
    if not isinstance(command, str) or not command.strip():
        return None
    if _utf8_len(command) > _MAX_SHELL_BYTES:
        return REASON_SHELL_UNREADABLE
    power_words_are_text = _reads_only(command)
    text = command.translate(_LINE_BREAKS)
    roots = [text]
    if "$" in text:
        # $IFS is a space and a positional parameter is empty in a command
        # line (rm${IFS}-rf${IFS}/etc): that spelling is read as well.
        expanded = _EMPTY_PARAMETER.sub("", _IFS.sub(" ", text))
        if expanded != text:
            roots.append(expanded)
    if "`" in text or "^" in text:
        # PowerShell's ` and cmd's ^ escape the next character inside a word
        # (Stop-Comp`uter, shut^down): the text without them is read as well.
        roots.append(_WINDOWS_ESCAPES.sub("", text))
    reading = _ShellReading()
    try:
        reading.read(roots)
    except _ShellUnreadable:
        return REASON_SHELL_UNREADABLE
    # Each unique reading (words) and each unique SQL text is checked once,
    # whatever number of segments, lines and nested bodies hold it.
    clients = {}
    for views in reading.texts.values():
        for words, _ in views:
            if words not in clients:
                clients[words] = _db_client_sql(words)
    client_anywhere = any(seen for _, seen in clients.values())
    read_views: set = set()
    read_sql: set = set()
    facts = _WordFacts()

    def sql_reason(sql: str, level: str) -> Optional[str]:
        if (sql, level) in read_sql:
            return None
        read_sql.add((sql, level))
        return _sql_reason(sql, level, budget)

    for seg, views in reading.texts.items():
        for view in views:
            if view in read_views:
                continue
            read_views.add(view)
            reason = _token_reason(*view, power=not power_words_are_text, facts=facts)
            if reason:
                return reason
        if not power_words_are_text:
            for pat in _POWER_PATTERNS:
                if pat.search(seg):
                    return _REASON_POWER
        for pat in _SHELL_PATTERNS:
            if pat.search(seg):
                return "destructive system command"
        if _redirects_to_device(seg):
            return "destructive system command"
        # SQL run through a database client: psql -c "DROP …", mysql -e …,
        # and SQL piped into one (echo "DROP TABLE x" | psql).
        for words, _ in views:
            for sql in clients[words][0]:
                reason = sql_reason(sql, "sql")
                if reason:
                    return reason
            if client_anywhere:
                for word in words:
                    reason = sql_reason(word, "shared")
                    if reason:
                        return reason
    # The reading without quoting, of the command and of each nested body.
    for view in reading.unquoted.values():
        if view not in read_views:
            read_views.add(view)
            reason = _token_reason(*view, power=not power_words_are_text, facts=facts)
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
# A list item gets its key's kind ("queries": ["SELECT 1", "DELETE …"]); a
# value nested in an object gets the stronger of its own key's kind and the
# enclosing one ("sql": {"q1": "DELETE …"} is "sql").
_SQL_SHARED_KEYS = {"query", "q", "command", "script", "code"}
# A tool whose name says it runs SQL: an engine name before query or queries,
# with or without words between (postgres_query, postgres_raw_query,
# bigquery_run_readonly_query), run/execute + query, read/write/start + query.
# Read on the name as written and on its camelCase split (PostgresRawQuery
# is read as Postgres_Raw_Query).
_SQL_TOOL_NAME = re.compile(
    r"sql|statement|stmt|database|^query$|(^|[_-])(run|execute|exec)[_-]?(query|queries)|(^|[_-])db([_-]|$)"
    r"|(^|[_-])(read|write|start)[_-]?(query|queries)([_-]|$)",
    re.IGNORECASE)
_SQL_ENGINE_QUERY_NAME = re.compile(
    r"(^|[_-])(postgres|postgresql|pg|mysql|mariadb|mssql|tsql|oracle|snowflake|bigquery|bq|duckdb|redshift"
    r"|athena|clickhouse|trino|presto|databricks|sqlite|synapse|teradata|vertica|cockroach|spanner)"
    r"[_-]?(\w*[_-])?(query|queries)([_-]|$)",
    re.IGNORECASE)
# Search, natural-language and history tools are never SQL tools, whatever
# engine they name (snowflake_cortex_search_query, pg_query_history): their
# "query" is a question or a filter. Compared by whole name part.
_NOT_SQL_TOOL_WORDS = frozenset({
    "search", "semantic", "vector", "natural", "genie", "analyst", "ask", "history", "log", "logs", "list",
    "named", "saved",
})
# A name with one of these as a part says it runs SQL: the words above never
# demote it (runSQLLogQuery, MSSQLListQuery).
_SQL_NAME_PARTS = frozenset({"sql", "tsql", "mssql", "mysql", "postgresql", "sqlite"})
# AI words demote only a name that is a SQL tool by engine + query alone
# (databricks_ai_query, snowflake_cortex_complete_query); a name that says SQL
# otherwise (mssql_ai_query, execute_sql_prompt, run_query_complete) stays one.
_AI_TOOL_WORDS = frozenset({"complete", "ai", "cortex", "prompt"})
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
    if not tool_name:
        return False
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", tool_name))
    names = (tool_name, split)
    says_sql = any(_SQL_TOOL_NAME.search(n) for n in names)
    if not says_sql and not any(_SQL_ENGINE_QUERY_NAME.search(n) for n in names):
        return False
    parts = set(re.split(r"[^a-z0-9]+", split.lower()))
    if _SQL_NAME_PARTS.intersection(parts):
        return True
    return not (_NOT_SQL_TOOL_WORDS.intersection(parts) or (not says_sql and _AI_TOOL_WORDS.intersection(parts)))


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
# "call": CALL p('DR' || 'OP TABLE x') holds its statement only once joined.
_GATE_WORDS = ("drop", "truncate", "delete", "exec", "call")
# EXEC bodies are read as SQL again, at most this many levels deep; a body
# still holding SQL to run at that depth is refused as unreadable.
_MAX_EXEC_DEPTH = 2
# SQL held in literals (an EXEC body, a concatenation of an EXEC's literals,
# a procedure argument) is rechecked one item at a time, each read alone so
# nothing in one can affect another. At most this many rechecks per call
# (block_reason, sql_block_reason or shell_block_reason), and at most
# _MAX_SCAN_BYTES of text read in them, across every level and argument; a
# call that needs more is refused as unreadable.
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
    "IF", "WHILE", "DECLARE", "PRINT", "THROW", "RAISERROR", "RAISE", "WAITFOR", "RETURN", "GOTO", "BREAK",
    "CONTINUE",
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
    (never a keyword token or a statement word in _NOT_A_PROCEDURE), each
    part optionally a temporary procedure's (#run, ##run: the tokenizers give
    one or two HASH tokens right before the name), followed by an argument,
    not by "(", "=", ":" or more name."""
    def name_part(k: int) -> Optional[int]:
        """Index of the name token of a part starting at k, or None."""
        for _ in range(2):
            if (k + 1 < len(toks) and toks[k].token_type.name == "HASH"
                    and toks[k].end + 1 == toks[k + 1].start):
                k += 1
        return k if k < len(toks) and toks[k].token_type.name in ("VAR", "IDENTIFIER") else None

    i = name_part(0)
    if i is None:
        return None
    while i + 1 < len(toks) and toks[i + 1].token_type.name == "DOT":
        i += 1
        while i + 1 < len(toks) and toks[i + 1].token_type.name == "DOT":
            i += 1
        i = name_part(i + 1)
        if i is None:
            return None
    if i == 0 and toks[0].token_type.name == "VAR" and toks[0].text.upper() in _NOT_A_PROCEDURE:
        return None
    if i + 1 >= len(toks) or toks[i + 1].token_type.name not in _PROC_ARGUMENT_START:
        return None
    return i


# A procedure name no search phrase starts with: bracketed, a temporary
# procedure's (#run, ##run), sp_/xp_, or qualified (dbo.run). Matched at the
# start of the name's source text.
_NO_WORD_PROCEDURE_NAME = re.compile(r"\s*(\[|#|(sp|xp)_|(\"[^\"]*\"|`[^`]*`|[\w$@]+)\s*\.)", re.IGNORECASE)


def _literal_items(tokens: list, items: dict, level: str, text: str) -> None:
    """Text in this SQL that would itself run as SQL, added to ``items`` in
    order: the string literals of a statement with EXEC/EXECUTE (and their
    concatenation), the literal of PREPARE … FROM '…' and the statement of
    PREPARE … AS …; also the literals of a procedure call: one without EXEC,
    and CALL p('…'). At level "shared" a procedure call counts only when its
    name is one no search phrase starts with (_NO_WORD_PROCEDURE_NAME): "find
    'DELETE FROM x' in the logs" reads like a call to "find". When SQL held
    in a variable is run (EXEC(@q), EXEC sp_executesql @q, PREPARE s FROM @q),
    the text can come from any statement, so every literal of the whole text
    is added too. ``text`` is the source the tokens were read from."""
    every: list = []
    dynamic = False
    procedures = level == "sql"
    for kws, toks in _segments(tokens):
        proc = _proc_call_at_start(toks)
        if proc is not None and not procedures and not _NO_WORD_PROCEDURE_NAME.match(text, toks[0].start):
            proc = None
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
                elif head == "CALL" and (procedures or _NO_WORD_PROCEDURE_NAME.match(t.text)):
                    _add(items, inner)  # CALL p('…'): the procedure may run them
                continue
            every.append(t.text)
            if has_exec:
                parts.append(t.text)
        _add(items, parts)
    if dynamic:
        _add(items, every)


def _utf8_len(text: str) -> int:
    """UTF-8 size of a text; a lone surrogate (invalid in UTF-8, but a Python
    str can hold one) counts as its three bytes instead of raising."""
    return len(text.encode("utf-8", "surrogatepass"))


def _valid_utf8(text: str) -> bool:
    """False for a text holding a lone surrogate. At "sql" and "shared" such a
    text with a gate word is refused as unreadable; at "other" it is read as
    it is (a surrogate is one more character that keeps a text from being a
    lone DROP, like any other junk there)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class _Budget:
    """Rechecks left for one call: at most _MAX_RECHECKS items and
    _MAX_SCAN_BYTES of their text, across every level and argument."""

    def __init__(self) -> None:
        self.left = _MAX_RECHECKS
        self.bytes_left = _MAX_SCAN_BYTES


def _recheck_literals(text: str, items: dict, depth: int, seen: frozenset, budget: _Budget) -> set:
    """Read each item as SQL on its own, at level "sql". Only items with a
    gate word are read. An item whose exact text was already read at a
    shallower level (``seen``) is not read again: the tokenizers that keep an
    EXEC body raw give back the "EXEC " + body item this module made,
    unchanged. Only the exact text is skipped: a near copy (other case or
    spacing) can hold different SQL one level deeper. Past _MAX_EXEC_DEPTH,
    any item still to read makes the text unreadable, and so does a call
    that needs more than _MAX_RECHECKS reads or more than _MAX_SCAN_BYTES of
    text read in them (each item is charged its size before it is read)."""
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
    size = sum(_utf8_len(t) for t in pending)
    if size > budget.bytes_left:
        logger.info("blocklist: more than %d bytes of SQL literals to recheck in one call; refused",
                    _MAX_SCAN_BYTES)
        return {REASON_UNREADABLE}
    budget.left -= len(pending)
    budget.bytes_left -= size
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
    if _utf8_len(text) > _MAX_PARSE_BYTES:
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
    if not _valid_utf8(text):
        # A lone surrogate: an engine or driver could drop or replace it and
        # run the rest, which the tokenizers never read as it would run.
        logger.info("blocklist: %s-level text with a gate word is not valid UTF-8; refused", level)
        return [REASON_UNREADABLE]
    size = _utf8_len(text)
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
        _literal_items(tokens, items, level, text)
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


_KIND_RANK = {"other": 0, "shared": 1, "sql": 2}


def _strings(value: Any, kind_of: Callable[[str], str], kind: str = "other",
             depth: int = 0) -> Iterable[tuple]:
    """(kind, text) for every string in the arguments. A value under a key
    gets the stronger of that key's kind (``kind_of``) and the kind of the
    value it sits in, so a string nested in a SQL argument is read as SQL
    ({"sql": {"q1": "…"}}); a list item gets its list's kind. A non-empty
    value past _MAX_ARGUMENT_DEPTH yields _TOO_DEEP."""
    if isinstance(value, str):
        yield kind, value
    elif depth >= _MAX_ARGUMENT_DEPTH:
        if isinstance(value, (dict, list, tuple)) and value:
            yield kind, _TOO_DEEP
    elif isinstance(value, dict):
        for k, v in value.items():
            inner = kind_of(str(k))
            if _KIND_RANK[kind] > _KIND_RANK[inner]:
                inner = kind
            yield from _strings(v, kind_of, inner, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, kind_of, kind, depth + 1)


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
    for kind, text in _strings(args, lambda key: _key_kind(key, bare, sql_params)):
        if text is _TOO_DEEP:
            logger.info("blocklist: arguments nested past %d levels; refused", _MAX_ARGUMENT_DEPTH)
            return REASON_UNREADABLE
        reason = _sql_reason(text, kind, budget)
        if reason:
            return reason
    return None
