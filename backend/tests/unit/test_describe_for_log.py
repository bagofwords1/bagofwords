"""describe_for_log: an exception as a log line that keeps the code structure
needed to debug it (types, frames, failed fields, constraint, status) and
never the text of a message, which can quote what a client sent."""
import secrets
import sqlite3

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors.redact import describe_for_log


def _marker():
    return f"client-content-{secrets.token_hex(6)}"


def _raised(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 - the test inspects whatever was raised
        return e
    raise AssertionError("expected an exception")


def _app_level_insert(marker):
    """Raise a real sqlite IntegrityError wrapped the way SQLAlchemy wraps it."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (title TEXT CONSTRAINT uq_title UNIQUE)")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    try:
        conn.execute("INSERT INTO t VALUES (?)", (marker,))
    except sqlite3.IntegrityError as orig:
        raise sa_exc.IntegrityError("INSERT INTO t VALUES (?)", (marker,), orig) from orig


def test_integrity_error_keeps_driver_type_and_app_frames_not_params():
    marker = _marker()
    e = _raised(lambda: _app_level_insert(marker))
    assert marker in str(e), "precondition: the DB error's text carries the bound parameter"

    line = describe_for_log(e)

    assert marker not in line
    assert line.startswith("IntegrityError")
    assert "orig=IntegrityError" in line
    # Every frame, repo-relative, so the app frame that issued the query shows.
    assert "tests/unit/test_describe_for_log.py:" in line
    assert " _app_level_insert" in line
    assert "/home/" not in line


def test_constraint_name_of_the_driver_error_is_kept():
    marker = _marker()

    class UniqueViolationError(Exception):  # asyncpg's shape: constraint_name on the error
        constraint_name = "uq_instruction_title"

    def boom():
        raise sa_exc.IntegrityError("INSERT ...", {"title": marker}, UniqueViolationError(f"dup {marker}"))

    line = describe_for_log(_raised(boom))

    assert marker not in line
    assert "orig=UniqueViolationError" in line
    assert "constraint=uq_instruction_title" in line


def test_cause_and_context_types_are_kept_without_their_messages():
    marker = _marker()

    def caused():
        try:
            raise KeyError(marker)
        except KeyError as inner:
            raise RuntimeError(f"wrapped {marker}") from inner

    def during():
        try:
            raise LookupError(marker)
        except LookupError:
            raise ValueError(f"while handling {marker}")

    caused_line = describe_for_log(_raised(caused))
    during_line = describe_for_log(_raised(during))

    assert marker not in caused_line and marker not in during_line
    assert caused_line.startswith("RuntimeError")
    assert "caused by KeyError" in caused_line
    assert during_line.startswith("ValueError")
    assert "during LookupError" in during_line


def test_validation_error_keeps_model_title_and_failed_fields_not_input():
    marker = _marker()

    class MCPCreateInstructionInput(BaseModel):
        text: str
        load_mode: int

    e = _raised(lambda: MCPCreateInstructionInput(text=123, load_mode=marker))

    line = describe_for_log(e)

    assert marker not in line
    assert "ValidationError" in line
    assert "MCPCreateInstructionInput" in line
    assert "('load_mode',):int_parsing" in line
    assert "('text',):string_type" in line
    # A library frame is named by its package path, not its install location.
    assert "pydantic/main.py:" in line
    assert "site-packages" not in line


def test_starlette_http_exception_keeps_status_not_detail():
    marker = _marker()

    def boom():
        raise StarletteHTTPException(status_code=422, detail=f"bad {marker}")

    line = describe_for_log(_raised(boom))

    assert marker not in line
    assert line.startswith("HTTPException 422")


def test_exception_whose_str_raises_is_still_described():
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("str() must not be called")

    def boom():
        raise Unprintable()

    line = describe_for_log(_raised(boom))

    assert line.startswith("Unprintable")
    assert "tests/unit/test_describe_for_log.py:" in line


def _deep_recursion_error():
    """A RecursionError whose ~1000 frames are in a file outside the backend
    and outside site-packages, the frames that cost the most to place."""
    namespace = {}
    exec(compile("def recurse(n):\n    return recurse(n + 1)\n", "/opt/elsewhere/deep.py", "exec"), namespace)
    return _raised(lambda: namespace["recurse"](0))


def test_a_deep_traceback_keeps_its_outer_and_inner_frames():
    e = _deep_recursion_error()
    assert isinstance(e, RecursionError)

    line = describe_for_log(e)

    frames = line.split(" at ", 1)[1].split(" > ")
    assert len(frames) == 41, len(frames)  # 20 outermost, "...", 20 innermost
    assert frames[20] == "..."
    assert frames[0].endswith(" _raised")
    assert all(f.endswith(" recurse") for f in frames[-20:])

