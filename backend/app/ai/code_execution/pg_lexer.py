"""Read Postgres query text the way the server's lexer does.

The read-only guard and table scope check Postgres queries with sqlglot,
which does not read every string form the way Postgres does (an escaped
quote in an E'...' string, for one). This lexer finds the literal, quoted
name, comment and statement boundaries the way Postgres's own scanner does
(src/backend/parser/scan.l, with standard_conforming_strings on) and gives
back one canonical text:

  * every comment replaced by one space, keeping the line breaks inside it;
  * every string literal (standard, E'...', dollar-quoted) written as a
    standard '...' literal with the same value: quotes doubled, backslashes
    as they are. An E-string's \\n and \\r escapes become real line breaks
    inside that literal, so the canonical text can have more lines than the
    original;
  * one space before such a literal where writing it right after the code
    before it would change how it reads: after another literal (the two
    would join into one) or after a standalone `u&` (it would become a
    U&'...' string). The canonical text is a fixed point of this lexer;
  * N'...', B'...' and X'...' literals, quoted names and all other code as
    written.

That one text is what sqlglot checks and what the connection sends, so the
checks and the server read the same thing. Forms that are rare and that
readers disagree on are refused rather than modelled: a string continued on
the next line (it keeps the first piece's escape rules), U&'...' and U&"..."
(their own escape syntax), a NUL anywhere (the client library stops reading
at it), anything unterminated, E-string escapes that make a byte >= 0x80 or
a NUL, bit or hex strings with other digits, `{#` (a comment to sqlglot,
code to Postgres), and a prefixed string glued to a number (read one way by
Postgres 15, another by older servers).

Postgres reads every character >= 0x80 as a name character, a name start
included; sqlglot reads some of them otherwise (a space ends a name, a
digit starts a number, some letters upper-case to a keyword). So every
unquoted name holding such a character must be exactly one name to
sqlglot's tokenizer, and no sqlglot keyword may start with it (or the text
after it could join it into one); every dollar-quote tag holding one must
be read by sqlglot as the same delimiter (defence in depth: dollar strings
are rewritten as standard literals, so sqlglot never reads a tag).
Otherwise the text is refused.
Names and tags in ASCII are read alike (a '$' inside a name included: both
read it as a name character) and skip this check. When sqlglot's tokenizer
fails on a name or tag other than by reporting a token error, the text
could not be checked and is refused as such.

`mode="redshift"` also refuses E-strings and any backslash in a literal
value: Redshift reads backslashes in plain strings as escapes. It checks
names and tags against sqlglot's Redshift tokenizer.

Pure: no I/O, apart from a warning logged when the tokenizer fails.
"""

import logging
import re
from dataclasses import dataclass

from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType
from sqlglot.trie import TrieResult, in_trie

from app.ai.code_execution.sql_guard import ReadOnlyViolation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PgLexResult:
    # The canonical text: what is checked and what is sent.
    text: str
    # `text` with every literal's body emptied (prefixes and quoted names
    # kept). For keyword scans only, never sent.
    code_only: str
    # Non-empty segments between top-level semicolons.
    statements: int
    # Whether any literal's value holds a backslash.
    has_backslash_literal: bool


_MODES = ("postgres", "redshift")
_NUL = chr(0)
_BACKSLASH = chr(92)
# Postgres 15 whitespace; anything else outside a literal or comment is code.
_WHITESPACE = frozenset(" \t\n\r\f")
_NEWLINES = frozenset("\n\r")
# What may stand between two literals for them to count as one continued
# string: whitespace (vertical tab too, which newer servers may read as
# whitespace) and comments, with a line break somewhere.
_GAP = _WHITESPACE | {chr(11)}
_DIGITS = frozenset("0123456789")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_OCTAL_DIGITS = frozenset("01234567")
# A dollar-quote delimiter: $$ or $tag$, the tag a name without '$'.
_DOLLAR_DELIMITER = re.compile(r"\$(?:[A-Za-z_\x80-\U0010FFFF][A-Za-z0-9_\x80-\U0010FFFF]*)?\$")
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PARAMETER = re.compile(r"\$[0-9]+")
# Right after a number or a parameter: a literal prefix and its quote.
_PREFIXED_QUOTE = re.compile(r"[bBeEnNxX]'|[uU]&['\"]")
_BIT_BODY = re.compile(r"[01]*")
_HEX_BODY = re.compile(r"[0-9A-Fa-f]*")
_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _is_name_start(c: str) -> bool:
    return c == "_" or ("a" <= c <= "z") or ("A" <= c <= "Z") or c >= "\x80"


def _is_name_part(c: str) -> bool:
    return _is_name_start(c) or c in _DIGITS or c == "$"


class _SqlglotReading:
    """How sqlglot's tokenizer for the mode reads one name or dollar tag.
    Built on first use, so text in ASCII never builds it; each name or tag
    is read once, in time linear in its length."""

    def __init__(self, mode: str):
        self._mode = mode
        self._tokenizer = None

    def _tokens(self, text: str):
        if self._tokenizer is None:
            from sqlglot.dialects.dialect import Dialect

            self._tokenizer = Dialect.get_or_raise(self._mode).tokenizer()
        try:
            return self._tokenizer.tokenize(text)
        except Exception as exc:
            # The type only: the text is the query's.
            if isinstance(exc, TokenError):
                # The expected path: sqlglot reads no token there (a name
                # holding a space), so the name reads differently.
                logger.debug("pg_lexer.sqlglot_tokenizer_failed error=%s", type(exc).__name__)
                return None
            logger.warning("pg_lexer.sqlglot_tokenizer_failed error=%s", type(exc).__name__)
            raise ReadOnlyViolation(
                "The query could not be checked: the query checks could not read one of its names "
                "or dollar-quote tags. Quote the name, or use an ASCII name or tag."
            ) from None

    def one_name(self, name: str) -> bool:
        tokens = self._tokens(name)
        if tokens is None or len(tokens) != 1:
            return False
        token = tokens[0]
        if token.token_type is not TokenType.VAR or token.text != name:
            return False
        # The keywords sqlglot can read across the end of a name (multi-word
        # ones and those holding punctuation): none may start with it.
        return in_trie(self._tokenizer._KEYWORD_TRIE, name.upper())[0] is TrieResult.FAILED

    def same_dollar_tag(self, delimiter: str) -> bool:
        tokens = self._tokens(delimiter + "v" + delimiter)
        return (tokens is not None and len(tokens) == 1
                and tokens[0].token_type is TokenType.HEREDOC_STRING and tokens[0].text == "v")


def _check_name(name: str, reading: _SqlglotReading) -> None:
    if not name.isascii() and not reading.one_name(name):
        raise ReadOnlyViolation(
            "A name holds a character outside ASCII that the query checks read differently from the "
            "server (a space such as a no-break space, a digit at its start, or a letter read as a "
            "keyword). Use a plain space, or quote the name."
        )


def _check_dollar_tag(delimiter: str, reading: _SqlglotReading) -> None:
    """Defence in depth: the canonical text writes every dollar string as a
    standard literal, so sqlglot never reads the tag itself; this can only
    refuse a text the checks would have read correctly."""
    if not delimiter.isascii() and not reading.same_dollar_tag(delimiter):
        raise ReadOnlyViolation(
            "A dollar-quote tag holds a character outside ASCII that the query checks read differently "
            "from the server. Use an ASCII tag such as $q$, or a standard string."
        )


def _unterminated(what: str) -> ReadOnlyViolation:
    return ReadOnlyViolation(f"The query has an unterminated {what}. Close it, or remove it.")


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _standard_body(sql: str, start: int) -> tuple[str, int]:
    """The value of the standard literal whose opening quote is at `start`,
    and where it ends (exclusive)."""
    parts = []
    i = start + 1
    while True:
        end = sql.find("'", i)
        if end < 0:
            raise _unterminated("string literal")
        parts.append(sql[i:end])
        if sql.startswith("'", end + 1):
            parts.append("'")
            i = end + 2
            continue
        return "".join(parts), end + 1


def _escape_value(sql: str, at: int) -> tuple[str, int]:
    """One E-string escape starting at the backslash at `at`: its character
    and where it ends."""
    if at + 1 >= len(sql):
        raise _unterminated("string literal")
    c = sql[at + 1]
    if c in _OCTAL_DIGITS:
        end = at + 2
        while end < len(sql) and end < at + 4 and sql[end] in _OCTAL_DIGITS:
            end += 1
        return _byte(int(sql[at + 1:end], 8)), end
    if c == "x" and at + 2 < len(sql) and sql[at + 2] in _HEX_DIGITS:
        end = at + 3
        if end < len(sql) and sql[end] in _HEX_DIGITS:
            end += 1
        return _byte(int(sql[at + 2:end], 16)), end
    if c in "uU":
        width = 4 if c == "u" else 8
        digits = sql[at + 2:at + 2 + width]
        if len(digits) < width or any(d not in _HEX_DIGITS for d in digits):
            raise ReadOnlyViolation(
                f"The query has an invalid Unicode escape in an E'...' string (\\{c} needs {width} hex digits)."
            )
        code = int(digits, 16)
        if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            raise ReadOnlyViolation(
                "The query has a Unicode escape for NUL, a surrogate or a value past U+10FFFF in an "
                "E'...' string. Write the character itself in a standard string."
            )
        return chr(code), at + 2 + width
    return _SIMPLE_ESCAPES.get(c, c), at + 2


def _byte(value: int) -> str:
    if value == 0 or value >= 0x80:
        raise ReadOnlyViolation(
            "The query has an octal or hex escape for NUL or a byte of 0x80 or more in an E'...' string. "
            "Write the character itself in a standard string."
        )
    return chr(value)


def _e_string_body(sql: str, start: int) -> tuple[str, int]:
    """The value of the E-string whose opening quote is at `start`."""
    parts = []
    i = start + 1
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            if sql.startswith("'", i + 1):
                parts.append("'")
                i += 2
                continue
            return "".join(parts), i + 1
        if c == _BACKSLASH:
            value, i = _escape_value(sql, i)
            parts.append(value)
            continue
        # Plain characters up to the next quote or backslash, in one step.
        end = i + 1
        while end < n and sql[end] not in ("'", _BACKSLASH):
            end += 1
        parts.append(sql[i:end])
        i = end
    raise _unterminated("string literal")


def _block_comment_end(sql: str, start: int) -> int:
    """Where the (nested) comment opening at `start` ends (exclusive)."""
    depth = 0
    i = start
    n = len(sql)
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise _unterminated("/* comment")


def lex_postgres(sql: str, *, mode: str = "postgres") -> PgLexResult:
    """Read `sql` as Postgres does; return its canonical text, or raise
    ReadOnlyViolation for a form that is refused."""
    if mode not in _MODES:
        raise ValueError(f"unknown lexer mode {mode!r}")
    redshift = mode == "redshift"
    reading = _SqlglotReading(mode)
    if _NUL in sql:
        raise ReadOnlyViolation("The query holds a NUL character, after which the server would not read it. Remove it.")

    text: list[str] = []
    code: list[str] = []
    statements = 0
    content = False  # the current statement has something besides whitespace and comments
    backslash = False
    # After a literal, until anything but whitespace or a comment: whether a
    # line break was seen (a literal there would continue the first one).
    after_literal = False
    gap_newline = False

    def literal(prefix: str, value: str, written: str) -> None:
        nonlocal content, backslash, after_literal, gap_newline
        if after_literal and gap_newline:
            raise ReadOnlyViolation(
                "The query continues a string literal on the next line ('a'<newline>'b'), which the server "
                "reads with the first piece's escape rules. Write it as one literal, or join the pieces with ||."
            )
        if _BACKSLASH in value:
            if redshift:
                raise ReadOnlyViolation(
                    "A string literal holds a backslash, which Redshift reads as an escape. "
                    "Remove the backslash, or match it with a wildcard."
                )
            backslash = True
        if written.startswith("'") and text and (
            text[-1].endswith("'") or (text[-1] == "&" and len(text) > 1 and text[-2] in ("u", "U"))
        ):
            # Written right there, the literal would join the one before it,
            # or turn a standalone `u&` into a U&'...' string.
            text.append(" ")
            code.append(" ")
        text.append(written)
        code.append(prefix + "''")
        content = True
        after_literal, gap_newline = True, False

    def other(piece: str) -> None:
        nonlocal content, after_literal
        text.append(piece)
        code.append(piece)
        content = True
        after_literal = False

    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c in _GAP:
            end = i + 1
            while end < n and sql[end] in _GAP:
                end += 1
            piece = sql[i:end]
            text.append(piece)
            code.append(piece)
            if any(ch not in _WHITESPACE for ch in piece):
                content = True  # a vertical tab is not whitespace to Postgres 15
            if after_literal and any(ch in _NEWLINES for ch in piece):
                gap_newline = True
            i = end
        elif c == "-" and nxt == "-":
            end = i + 2
            while end < n and sql[end] not in _NEWLINES:
                end += 1
            text.append(" ")
            code.append(" ")
            i = end
        elif c == "/" and nxt == "*":
            end = _block_comment_end(sql, i)
            breaks = "".join(ch for ch in sql[i:end] if ch in _NEWLINES)
            text.append(" " + breaks)
            code.append(" " + breaks)
            if after_literal and breaks:
                gap_newline = True
            i = end
        elif c == "'":
            value, end = _standard_body(sql, i)
            literal("", value, _quoted(value))
            i = end
        elif c in "eE" and nxt == "'":
            if redshift:
                raise ReadOnlyViolation("E'...' strings are not allowed on Redshift. Use a standard string.")
            value, end = _e_string_body(sql, i + 1)
            literal("", value, _quoted(value))
            i = end
        elif c in "nN" and nxt == "'":
            value, end = _standard_body(sql, i + 1)
            literal(c, value, sql[i:end])
            i = end
        elif c in "bBxX" and nxt == "'":
            end = sql.find("'", i + 2)
            if end < 0:
                raise _unterminated("bit or hex string")
            body = sql[i + 2:end]
            if not (_BIT_BODY if c in "bB" else _HEX_BODY).fullmatch(body):
                kind = "bit string B'...' may hold only 0 and 1" if c in "bB" else "hex string X'...' may hold only hex digits"
                raise ReadOnlyViolation(f"A {kind}.")
            literal(c, body, sql[i:end + 1])
            i = end + 1
        elif c in "uU" and nxt == "&" and sql[i + 2:i + 3] in ("'", '"'):
            raise ReadOnlyViolation(
                "U&'...' strings and U&\"...\" names are not allowed (they have their own escape syntax). "
                "Write the characters themselves."
            )
        elif c == '"':
            end = i + 1
            while True:
                end = sql.find('"', end)
                if end < 0:
                    raise _unterminated("quoted name")
                if sql.startswith('"', end + 1):
                    end += 2
                    continue
                break
            other(sql[i:end + 1])
            i = end + 1
        elif c == "$":
            delimiter = _DOLLAR_DELIMITER.match(sql, i)
            if delimiter:
                tag = delimiter.group()
                _check_dollar_tag(tag, reading)
                end = sql.find(tag, delimiter.end())
                if end < 0:
                    raise _unterminated("dollar-quoted string")
                value = sql[delimiter.end():end]
                literal("", value, _quoted(value))
                i = end + len(tag)
                continue
            parameter = _PARAMETER.match(sql, i)
            end = parameter.end() if parameter else i + 1
            other(sql[i:end])
            i = end
            if parameter:
                _refuse_glued(sql, i)
        elif _is_name_start(c):
            end = i + 1
            while end < n and _is_name_part(sql[end]):
                end += 1
            name = sql[i:end]
            _check_name(name, reading)
            other(name)
            i = end
        elif c in _DIGITS or (c == "." and nxt in _DIGITS):
            end = _NUMBER.match(sql, i).end()
            other(sql[i:end])
            i = end
            _refuse_glued(sql, i)
        elif c == "{" and nxt == "#":
            raise ReadOnlyViolation("The query has '{#', which some readers take as the start of a comment. Remove it.")
        elif c == ";":
            if content:
                statements += 1
            content = False
            text.append(c)
            code.append(c)
            after_literal = False
            i += 1
        else:
            other(c)
            i += 1
    if content:
        statements += 1
    return PgLexResult(
        text="".join(text),
        code_only="".join(code),
        statements=statements,
        has_backslash_literal=backslash,
    )


def _refuse_glued(sql: str, at: int) -> None:
    """A prefixed string right after a number or parameter: Postgres 15
    refuses the junk after the number, older servers read a string there."""
    if _PREFIXED_QUOTE.match(sql, at):
        raise ReadOnlyViolation("A string literal is written right after a number. Put a space or an operator between them.")
