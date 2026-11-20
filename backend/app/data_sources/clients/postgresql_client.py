from app.data_sources.clients.progress import discovery_progress, discovery_phase
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import make_reporter, IndexingCancelled

import logging
import re
import unicodedata

import pandas as pd
import sqlalchemy
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INTRANS

from app.data_sources.clients._exact_sql import read_sql_exact
from app.data_sources.clients._pg_session import (
    STANDARD_STRINGS_ON_LOCAL,
    confirm_pg_session,
    pg_session_needs_switch,
    prepare_pg_session,
    verify_pg_session_after,
)
from app.data_sources.engine_pool import get_engine, pg_one_message
from app.data_sources.fk_reflection import attach_foreign_keys
from app.data_sources.query_cancellation import track
from sqlalchemy import text
from contextlib import contextmanager
from typing import List, Generator, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from app.data_sources.routines import build_routine_tables, postgres_variants, with_routines
from app.data_sources.clients._sa_url import (
    POSTGRES_DENIED_OPTIONS, POSTGRES_PATH_OPTIONS, build_url, connect_with_legacy_decoding,
    effective_url, render, split_database_options,
)

logger = logging.getLogger(__name__)

# The schema rule (`checked_search_path`): an item is sent as written when it
# is a double-quoted name (U&"..." too), a single-quoted string, digits, or a
# plain name. Inside quotes: any character but a control character or a
# backslash, the quote itself doubled.
_QUOTED_NAME = re.compile(r'(?:[Uu]&)?"(?:[^"\\\x00-\x1f\x7f]|"")+"')
_QUOTED_TEXT = re.compile(r"'(?:[^'\\\x00-\x1f\x7f]|'')*'")
_DIGITS = re.compile(r"[0-9]+")
_ASCII_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_ASCII_NAME_REST = _ASCII_NAME_START | frozenset("0123456789$")
_SCHEMA_RULE = (
    "The connection's schema setting has an entry that cannot be sent as written. Write each schema as "
    "a plain name (letters, digits, _ or $), in double quotes, or in single quotes, without semicolons, "
    "comments or backslashes."
)

# The one message's prefix: it ends with a line break, so the query starts
# on a line of its own.
_BEGIN_READ_ONLY = "BEGIN READ ONLY;"
_ERROR_LINE = re.compile(r"LINE (\d+): ")


def _schema_setting_error(e: Exception) -> RuntimeError:
    """The error for a schema setting the server could not apply: the type
    of `e` only, since its text holds the schema names."""
    return RuntimeError(
        "The connection's schema setting could not be applied on the server "
        f"(SET search_path failed: {type(e).__name__}). Check the schema names in the "
        "connection settings; write a name that needs quoting in double quotes."
    )


def _name_char(c: str, ascii_allowed: frozenset) -> bool:
    # Postgres reads every non-ASCII character as an identifier character;
    # whitespace, control and format characters are refused here all the same.
    if c in ascii_allowed:
        return True
    return ord(c) > 127 and not c.isspace() and unicodedata.category(c) not in ("Cc", "Cf")


def _plain_name(item: str) -> bool:
    """A name Postgres reads as one unquoted identifier: an ASCII letter,
    `_` or a non-ASCII character, then those, digits or `$`. A non-ASCII
    character counts unless it is whitespace, a control character or a
    format character."""
    if not item or not _name_char(item[0], _ASCII_NAME_START):
        return False
    return all(_name_char(c, _ASCII_NAME_REST) for c in item[1:])


def schema_items(schema) -> List[str]:
    """The connection's `schema` setting as the client reads it: split on
    commas, trimmed, empty and repeated items dropped, case kept."""
    items: List[str] = []
    if isinstance(schema, str):
        for item in (s.strip() for s in schema.split(",")):
            if item and item not in items:
                items.append(item)
    return items


def schema_item_problem(item: str) -> Optional[str]:
    """None when `checked_search_path` accepts `item`, else the kind of rule
    it breaks, for reports that must not show the item itself."""
    if (_QUOTED_NAME.fullmatch(item) or _QUOTED_TEXT.fullmatch(item) or _DIGITS.fullmatch(item)
            or _plain_name(item)):
        return None
    if ";" in item:
        return "semicolon"
    if "--" in item or "/*" in item:
        return "comment"
    if "\\" in item:
        return "backslash"
    if any(unicodedata.category(c) == "Cc" for c in item):
        return "control character"
    if any(c.isspace() or unicodedata.category(c) == "Cf" for c in item):
        return "whitespace or format character"
    if item[:1] in ("'", '"') or item[:3].upper() == 'U&"':
        return "unbalanced quotes"
    return "unquoted punctuation"


def checked_search_path(schemas: List[str]) -> str:
    """The `search_path` value for the configured schema items, each as
    written, or `ValueError` naming the setting (never its value) when an
    item is none of: a double-quoted name (`"..."`, `U&"..."`), a
    single-quoted string, digits, or a plain name. Nothing is quoted or
    lower-cased here: the server reads each item as it always has. The
    same rule is applied when a connection is saved.

    Some items the server accepts are refused all the same: a tab or other
    control character inside quotes, a backslash inside single quotes, and
    numbers that are not plain digits (`1.5`, `-1`)."""
    if any(schema_item_problem(item) for item in schemas):
        raise ValueError(_SCHEMA_RULE)
    return ", ".join(schemas)


def _prefix(begin: bool, search_path: Optional[str]) -> str:
    parts = [_BEGIN_READ_ONLY] if begin else []
    if search_path:
        parts.append(f"SET LOCAL search_path TO {search_path};")
    return " ".join(parts) + "\n" if parts else ""


def _count_lines_from_the_query(message: str, prefix_lines: int) -> str:
    """`message` with its first `LINE n:` counted from the query's first
    line (line 1 for a position inside the prefix), and the caret under it
    moved with the shorter label."""
    lines = message.split("\n")
    for i, line in enumerate(lines):
        found = _ERROR_LINE.match(line)
        if not found:
            continue
        label = f"LINE {max(1, int(found.group(1)) - prefix_lines)}: "
        lines[i] = label + line[found.end():]
        shift = found.end() - len(label)
        if shift and i + 1 < len(lines) and lines[i + 1].lstrip(" ") == "^":
            lines[i + 1] = lines[i + 1][shift:]
        break
    return "\n".join(lines)


def _name_the_checked_query(error: sqlalchemy.exc.DBAPIError, prefix: str, sql: str) -> None:
    """Make `error` read as if `sql` had been sent alone: its statement is
    `sql`, and a position the server reported counts from `sql`'s first line,
    in the error's text and in the driver error it wraps. The driver
    error's `pgerror` and `diag.statement_position` are left as the server
    sent them, counted from the whole message."""
    error.statement = sql
    prefix_lines = prefix.count("\n")
    orig = getattr(error, "orig", None)
    if not prefix_lines or getattr(getattr(orig, "diag", None), "statement_position", None) is None:
        return
    for err in (error, orig):
        if err.args and isinstance(err.args[0], str):
            err.args = (_count_lines_from_the_query(err.args[0], prefix_lines), *err.args[1:])


def _in_the_prefix(error: sqlalchemy.exc.DBAPIError, prefix: str) -> bool:
    """Whether the server put `error` at a position inside `prefix`."""
    position = getattr(getattr(getattr(error, "orig", None), "diag", None), "statement_position", None)
    try:
        return position is not None and int(position) <= len(prefix)
    except ValueError:
        return False


def _no_statement(sql: str) -> bool:
    """Whether `sql` holds no statement at all (empty, blank, only
    comments or semicolons)."""
    from app.ai.code_execution.pg_lexer import lex_postgres

    try:
        return lex_postgres(sql).statements == 0
    except Exception:
        return False


def _drop(conn) -> None:
    try:
        conn.invalidate()
    except Exception as e:
        logger.warning("postgresql.invalidate_failed error=%s", type(e).__name__)


class PostgresqlClient(DataSourceClient):

    # Postgres with standard_conforming_strings on (the default since 9.1): ''
    # only (sqlglot postgres).
    sql_literal_style = "standard"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (PostgreSQL): CURRENT_DATE, CURRENT_DATE - INTERVAL '7 days', date_trunc('month', CURRENT_DATE); the clock is the DB server's."

    # Schemas the crawl skips unless `include_system_schemas` is set. These hold
    # Postgres's own catalog (pg_catalog) and the SQL-standard views over it
    # (information_schema); BOW's schema context normally stands in for them.
    SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")

    def __init__(self, host, port, database, user, password="", schema=None, sslmode="prefer",
                 index_routines: bool = False, include_system_schemas: bool = False):
        self.host = host
        # Opt-in routine indexing (see app.data_sources.routines).
        self.index_routines = bool(index_routines)
        # Opt-in crawl of pg_catalog / information_schema tables and views, for
        # agents that monitor the database itself (pg_stat_activity, pg_locks,
        # pg_stat_user_tables, ...). Off, those schemas never appear in the
        # catalog, so there is nothing for an admin to activate later.
        self.include_system_schemas = bool(include_system_schemas)
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.sslmode = sslmode
        # Optional schema or comma-separated list of schemas
        self.schema = schema
        # Case kept as provided; Postgres lowercases unquoted names.
        self._schemas = schema_items(self.schema)

    def _system_schema_filter(self, column: str) -> List[str]:
        """WHERE clauses that hide system schemas, or none when they are opted in."""
        if self.include_system_schemas:
            return []
        quoted = ", ".join(f"'{name}'" for name in self.SYSTEM_SCHEMAS)
        return [f"{column} NOT IN ({quoted})"]

    @cached_property
    def _sa_url(self):
        # "mydb?options=-csearch_path%3Dx": libpq options after the name keep
        # working, minus the ones that redirect the connection or read files.
        database, options = split_database_options(
            self.database, kind="PostgreSQL", host=self.host,
            denied=POSTGRES_DENIED_OPTIONS, path_options=POSTGRES_PATH_OPTIONS,
        )
        return build_url(
            "postgresql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database,
            query=options,
        )

    @property
    def pg_uri(self):
        """Identity string (pool key, dialect detection) of the URL the
        engine is built from."""
        return render(effective_url(self._sa_url))

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a Postgres db.

        `read_only=True` is for reads of agent and custom-table SQL: the
        server refuses any write that the single checked statement makes in
        the connection's transaction (the guard's single-statement check is
        what keeps a text from holding more than one), and the transaction
        always ends in a rollback, never a commit, so what a read could
        still change inside it (a large object, say) does not last. Only the
        transaction is read-only; the pooled session keeps no read-only
        default: on checkin the connection returns to the session default
        (engine_pool), so the next user's plain transaction starts the way
        the server's own default says, on a read replica too.

        The schema setting is sent as written and the schema rule
        (`checked_search_path`) is not applied here, as before the rule
        existed: indexing, discovery and the connection test use this path,
        and a setting saved before the rule must keep working for them until
        someone edits it. A new or changed setting is checked when the
        connection is saved; the agent read applies the rule itself, and
        scripts/scan_postgres_schema_settings.py lists the saved settings it
        refuses.
        """
        conn = None
        try:
            conn = connect_with_legacy_decoding(
                self._sa_url,
                lambda url: get_engine(url, connect_args={"sslmode": self.sslmode}).connect(),
            )
            if read_only:
                # Before the first statement: it applies from the BEGIN.
                conn.execution_options(postgresql_readonly=True)
            # Set search_path if schemas are provided. Sent as written, as the
            # one-message read sends it: no bind markers, no `%` handling.
            if self._schemas:
                search_path = ", ".join(self._schemas)
                try:
                    conn.exec_driver_sql(f"SET search_path TO {search_path}",
                                         execution_options={"no_parameters": True})
                except Exception as e:
                    # The type only: the error text holds the schema names.
                    logger.warning("postgresql.schema_setting_failed setting=schema error=%s", type(e).__name__)
                    try:
                        conn.rollback()
                    except Exception as rollback_error:
                        # The connection is closed below either way; the
                        # schema error stays the one raised.
                        logger.warning("postgresql.schema_setting_rollback_failed error=%s",
                                       type(rollback_error).__name__)
                    raise _schema_setting_error(e) from e
        except Exception as e:
            if conn is not None:
                conn.close()
            raise RuntimeError(f"{e}")
        # The yield is deliberately OUTSIDE the try/except above. With it
        # inside, this contextmanager caught whatever the *caller* raised in
        # its `with client.connect()` body and re-raised it as a bare
        # RuntimeError, erasing the type: an extraction abort came back
        # indistinguishable from a connection failure. The except clause is
        # meant to wrap connect-time errors, and now only does.
        try:
            with track(self, conn):
                yield conn
        finally:
            if read_only:
                try:
                    conn.rollback()
                except Exception:
                    # A session that cannot roll back is not reused; the
                    # server ends its open transaction without committing.
                    logger.warning("postgresql.read_only_rollback_failed; the connection is dropped", exc_info=True)
                    conn.invalidate()
            conn.close()
        # NB: no engine.dispose() — the engine is pooled and shared
        # (engine_pool). conn.close() above returns the connection.

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame.

        `sql` is sent exactly as given (the read-only guard returned it), in
        a read-only transaction that ends in a rollback, on a session that
        reads literals as the guard did.

        On an engine built for it (`engine_pool.pg_one_message`, the
        default) that is one message, then the ROLLBACK: `BEGIN READ ONLY;`,
        `SET LOCAL search_path TO ...;` when schemas are pinned, a line
        break, then `sql`. The connection is in psycopg2's local autocommit
        mode for it, so the driver adds no BEGIN of its own. Two round trips
        on a reused connection. A server reporting `standard_conforming_strings`
        off first gets `BEGIN READ ONLY; SET LOCAL standard_conforming_strings
        = on` as a message of its own (the server parses a whole message
        before it runs any of it), and the second message has no BEGIN.

        * The schema items must pass `checked_search_path`, or nothing is
          sent. One that passes but the server cannot apply (a reserved
          word, say) fails with the same error as on `connect()`, and since
          the server refuses that whole message, every read on that source
          drops its connection and reconnects until the setting is fixed.
        * A connection that is idle once a message was sent is dropped,
          not rolled back and pooled. BOW's read-only transaction is open
          before the query's message runs (its BEGIN opens that message, or
          the switch message before it), so idle after a send means one of
          two things: the query ended BOW's transaction (a COMMIT the guard
          missed), or the server refused the whole message before running
          any of it (a syntax error anywhere in it). The two look the same
          from here, so both drop the connection; a syntax error costs a
          reconnect on the next read, and so does a send the driver
          refuses before it reaches the server. A read that succeeded that
          way returns no rows and raises. This finds such a query after the
          fact; it does not undo it: what ran after its COMMIT ran in
          transactions of its own and could commit (a `set_config(...,
          false)`, say), and only the dropped connection keeps that from
          the next read. Behind transaction-mode PgBouncer the server
          connection goes back to PgBouncer's pool when that message ends
          with the connection idle, so a session change can reach other
          clients; there only the guard's single-statement rule protects.
        * A query that ends BOW's transaction and opens another one
          (`COMMIT ... BEGIN`, `COMMIT AND CHAIN`) ends in a transaction:
          it passes the check after the read, the ROLLBACK ends the second
          transaction, and the connection is pooled, so a session change
          committed before the second BEGIN stays on it. Only the guard's
          single-statement rule covers that. Neither does a ROLLBACK undo
          session state that lives outside transactions (session advisory
          locks, the `setseed` state, `currval`), as on the `connect()`
          read; the guard refuses session advisory locks.
        * An error that leaves the transaction failed (a missing column,
          division by zero, a cancel) is rolled back, and the connection
          goes back to the pool.
        * On every exit: the ROLLBACK unless the connection is idle already
          or dropped, sent while autocommit is still on (a failed ROLLBACK
          drops the connection and never hides the result or the error);
          then the connection is idle or dropped; then the flag goes back
          off unless a drop was tried. A drop that fails leaves the flag
          on, so the pool's reset listener drops the connection.
        * Errors name `sql`, not the message, and a `LINE n` the server
          reports counts from `sql`'s first line. The driver error's
          `pgerror` and `diag.statement_position` still count from the
          whole message, as do the server's own records (its statement
          log, `pg_stat_activity.query`, `current_query()`).

        Otherwise it is the read-only `connect()` read."""
        try:
            try:
                engine = get_engine(effective_url(self._sa_url), connect_args={"sslmode": self.sslmode})
            except Exception as e:
                raise RuntimeError(f"{e}")
            if pg_one_message(engine):
                return self._read_in_one_message(sql)
            with self.connect(read_only=True) as conn:
                prepare_pg_session(conn, sql)
                df = read_sql_exact(sql, conn)
                verify_pg_session_after(conn)
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def _read_in_one_message(self, sql: str) -> pd.DataFrame:
        """`execute_query` on an engine built for the one-message read."""
        try:
            search_path = checked_search_path(self._schemas) if self._schemas else None
            # As `connect()` opens it: credentials saved percent-encoded fall
            # back to their decoded form.
            conn = connect_with_legacy_decoding(
                self._sa_url,
                lambda url: get_engine(url, connect_args={"sslmode": self.sslmode}).connect(),
            )
        except Exception as e:
            raise RuntimeError(f"{e}")
        raw = conn.connection.dbapi_connection
        try:
            if raw.get_transaction_status() != TRANSACTION_STATUS_IDLE:
                raise RuntimeError("The pooled connection was handed out inside a transaction.")
            if raw.readonly is not None:
                # Free while autocommit is off; with it on, psycopg2 would
                # send a SET.
                raw.readonly = None
            # Local: psycopg2 sends nothing while the read-only flag is at
            # its default.
            raw.autocommit = True
        except Exception as e:
            _drop(conn)
            conn.close()
            raise RuntimeError(f"{e}")
        sent = False
        try:
            with track(self, conn):
                begin = True
                if pg_session_needs_switch(conn, sql):
                    sent = True
                    try:
                        conn.exec_driver_sql(f"{_BEGIN_READ_ONLY} {STANDARD_STRINGS_ON_LOCAL}",
                                             execution_options={"no_parameters": True})
                    except sqlalchemy.exc.DBAPIError as e:
                        e.statement = sql
                        raise
                    begin = False
                    confirm_pg_session(conn, sql)
                prefix = _prefix(begin, search_path)
                sent = True
                df = self._read_message(conn, prefix, sql, bool(search_path))
                if raw.get_transaction_status() != TRANSACTION_STATUS_INTRANS:
                    # `_end_one_message` drops the connection.
                    raise RuntimeError("The query ended the read-only transaction it ran in, so its rows "
                                       "are not returned. A query may not end or start transactions.")
                # Before the ROLLBACK: after it, a server whose setting is off
                # reports off again.
                verify_pg_session_after(conn)
                return df
        finally:
            self._end_one_message(conn, raw, sent)

    @staticmethod
    def _read_message(conn, prefix: str, sql: str, schemas_in_prefix: bool) -> pd.DataFrame:
        try:
            return read_sql_exact(prefix + sql, conn)
        except sqlalchemy.exc.DBAPIError as e:
            if schemas_in_prefix and _in_the_prefix(e, prefix):
                # The type only, as on `connect()`: the text holds the names.
                logger.warning("postgresql.schema_setting_failed setting=schema error=%s", type(e).__name__)
                raise _schema_setting_error(e) from e
            _name_the_checked_query(e, prefix, sql)
            raise
        except sqlalchemy.exc.ResourceClosedError:
            # The prefix's own result: `sql` holds no statement. Sent alone,
            # the driver refuses it as an empty query; that error, as before.
            if prefix and _no_statement(sql):
                return read_sql_exact(sql, conn)
            raise

    @staticmethod
    def _end_one_message(conn, raw, sent: bool) -> None:
        """Idle after a send: dropped. Otherwise the ROLLBACK unless idle,
        then idle or dropped. Then the flag off, unless a drop was tried:
        if the drop itself failed, the flag left on makes the pool's reset
        listener drop the connection at checkin. Never raises an
        `Exception`."""
        dropping = False
        try:
            status = None if conn.invalidated else raw.get_transaction_status()
            if sent and status == TRANSACTION_STATUS_IDLE:
                # BOW's read-only transaction was open before the query's
                # message ran: the query ended it, or the server refused the
                # whole message.
                logger.warning("postgresql.read_transaction_ended_by_the_query_or_refused; "
                               "the connection is dropped")
                dropping = True
                _drop(conn)
            elif status is not None and status != TRANSACTION_STATUS_IDLE:
                try:
                    # While autocommit is on: with it off, psycopg2 would wrap
                    # the ROLLBACK in a BEGIN of its own.
                    conn.exec_driver_sql("ROLLBACK")
                except Exception:
                    logger.warning("postgresql.read_rollback_failed; the connection is dropped", exc_info=True)
                    dropping = True
                    _drop(conn)
                if not dropping and not conn.invalidated \
                        and raw.get_transaction_status() != TRANSACTION_STATUS_IDLE:
                    logger.warning("postgresql.read_not_idle_after_rollback; the connection is dropped")
                    dropping = True
                    _drop(conn)
            if not dropping and not conn.invalidated and not raw.closed:
                raw.autocommit = False
        except Exception:
            logger.warning("postgresql.read_end_failed; the connection is dropped", exc_info=True)
            _drop(conn)
        finally:
            conn.close()

    def get_tables(self, progress_callback=None) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched(progress_callback) if progress_callback else self._get_tables_enriched()
        except IndexingCancelled:
            raise
        except Exception:
            make_reporter(progress_callback).phase("metadata_fallback")
            return self._get_tables_basic(progress_callback) if progress_callback else self._get_tables_basic()

    def _get_tables_enriched(self, progress_callback=None) -> List[Table]:
        """Get tables with column/table comments via pg_description. May fail on some configurations."""
        discovery_phase('reading_columns')
        reporter = make_reporter(progress_callback)
        reporter.phase("reading_columns")
        with self.connect() as conn:
            params = {"database": self.database}
            where_clauses = [
                "c.table_catalog = :database",
                *self._system_schema_filter("c.table_schema"),
            ]
            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    params[key] = sch
                    in_keys.append(f":{key}")
                where_clauses.append(f"c.table_schema IN ({', '.join(in_keys)})")

            where_sql = " WHERE " + " AND ".join(where_clauses)
            sql = text(f"""
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    col_desc.description AS column_comment,
                    tbl_desc.description AS table_comment
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_statio_all_tables st
                    ON c.table_schema = st.schemaname AND c.table_name = st.relname
                LEFT JOIN pg_catalog.pg_description col_desc
                    ON col_desc.objoid = st.relid AND col_desc.objsubid = c.ordinal_position
                LEFT JOIN pg_catalog.pg_description tbl_desc
                    ON tbl_desc.objoid = st.relid AND tbl_desc.objsubid = 0
                {where_sql}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)
            result = conn.execute(sql, params).fetchall()

            tables = {}
            reporter.phase("processing_columns", total=len(result))
            for row in result:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                reporter.tick(f"{fqn}.{column_name}")
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment
                ))

            # Materialized views are not exposed via information_schema, so fetch
            # them separately from pg_catalog and merge into the result.
            reporter.phase("materialized_views")
            self._append_materialized_views(conn, tables, with_comments=True)
            reporter.phase("relationships", total=len(tables))
            self._attach_foreign_keys(conn, tables)
            reporter.done()
            return list(tables.values())

    def _append_materialized_views(self, conn, tables: dict, with_comments: bool) -> None:
        """Append materialized view columns (and optional comments) to `tables`.

        Materialized views (pg_class.relkind = 'm') are a Postgres-specific
        object and are not present in information_schema, so the standard table
        queries miss them. This reads them directly from pg_catalog.
        """
        params = {}
        where_clauses = [
            "c.relkind = 'm'",
            *self._system_schema_filter("n.nspname"),
        ]
        if self._schemas:
            in_keys = []
            for idx, sch in enumerate(self._schemas):
                key = f"mv_s{idx}"
                params[key] = sch
                in_keys.append(f":{key}")
            where_clauses.append(f"n.nspname IN ({', '.join(in_keys)})")
        where_sql = " WHERE " + " AND ".join(where_clauses)

        if with_comments:
            select_extra = (
                ",\n                col_desc.description AS column_comment,"
                "\n                tbl_desc.description AS table_comment"
            )
            join_extra = """
            LEFT JOIN pg_catalog.pg_description col_desc
                ON col_desc.objoid = c.oid AND col_desc.objsubid = a.attnum
            LEFT JOIN pg_catalog.pg_description tbl_desc
                ON tbl_desc.objoid = c.oid AND tbl_desc.objsubid = 0"""
        else:
            select_extra = ""
            join_extra = ""

        sql = text(f"""
            SELECT
                n.nspname AS table_schema,
                c.relname AS table_name,
                a.attname AS column_name,
                format_type(a.atttypid, a.atttypmod) AS data_type{select_extra}
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n
                ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute a
                ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped{join_extra}
            {where_sql}
            ORDER BY n.nspname, c.relname, a.attnum
        """)
        result = conn.execute(sql, params).fetchall()

        for row in result:
            if with_comments:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
            else:
                table_schema, table_name, column_name, data_type = row
                col_comment = tbl_comment = None
            key = (table_schema, table_name)
            fqn = f"{table_schema}.{table_name}"
            if key not in tables:
                tables[key] = Table(
                    name=fqn,
                    description=tbl_comment,
                    columns=[],
                    pks=[],
                    fks=[],
                    metadata_json={"schema": table_schema, "is_materialized_view": True}
                )
            tables[key].columns.append(TableColumn(
                name=column_name,
                dtype=data_type,
                description=col_comment
            ))

    def _get_tables_basic(self, progress_callback=None) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        discovery_phase('metadata_fallback')
        reporter = make_reporter(progress_callback)
        reporter.phase("reading_columns")
        try:
            with self.connect() as conn:
                params = {"database": self.database}
                where_clauses = [
                    "table_catalog = :database",
                    *self._system_schema_filter("table_schema"),
                ]
                if self._schemas:
                    in_keys = []
                    for idx, sch in enumerate(self._schemas):
                        key = f"s{idx}"
                        params[key] = sch
                        in_keys.append(f":{key}")
                    where_clauses.append(f"table_schema IN ({', '.join(in_keys)})")

                where_sql = " WHERE " + " AND ".join(where_clauses)
                sql = text(f"""
                    SELECT table_schema, table_name, column_name, data_type
                    FROM information_schema.columns
                    {where_sql}
                    ORDER BY table_schema, table_name, ordinal_position
                """)
                result = conn.execute(sql, params).fetchall()

                tables = {}
                reporter.phase("processing_columns", total=len(result))
                for row in result:
                    table_schema, table_name, column_name, data_type = row
                    key = (table_schema, table_name)
                    fqn = f"{table_schema}.{table_name}"
                    reporter.tick(f"{fqn}.{column_name}")
                    if key not in tables:
                        tables[key] = Table(
                            name=fqn, columns=[], pks=[], fks=[], metadata_json={"schema": table_schema}
                        )
                    tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

                # Materialized views are absent from information_schema; merge them in.
                reporter.phase("materialized_views")
                self._append_materialized_views(conn, tables, with_comments=False)
                reporter.phase("relationships", total=len(tables))
                self._attach_foreign_keys(conn, tables)
                reporter.done()
                return list(tables.values())
        except IndexingCancelled:
            raise
        except Exception as e:
            self.mark_discovery_incomplete("postgresql catalog")
            print(f"Error retrieving tables: {e}")
            return []

    def _attach_foreign_keys(self, conn, tables) -> None:
        """Populate `fks` on the tables just collected.

        `name_fn` mirrors the `f"{table_schema}.{table_name}"` this client uses
        for `Table.name` — the two must agree or downstream resolution, which is
        an exact string match, drops every edge without complaining.
        """
        attach_foreign_keys(
            conn,
            tables,
            self._schemas or None,
            lambda schema, table: f"{schema}.{table}",
        )

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        """Tables, views and materialized views, then stored procedures and
        functions (see app.data_sources.routines)."""
        tables = self.get_tables(progress_callback) if progress_callback else self.get_tables()
        return with_routines(tables, lambda: self.get_routines(t.name for t in tables), source="postgresql",
                             client=self, enabled=self.index_routines)

    def get_routines(self, table_names=()) -> List[Table]:
        """Stored procedures and functions as schema entries, named like tables."""
        with self.connect() as conn:
            variants = postgres_variants(conn, self._schemas or None)
        return build_routine_tables(variants, table_names, lambda schema, name: f"{schema}.{name}")

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to PostgreSQL and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to PostgreSQL"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries.
        
        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```


        """
        description = f"Postgresql database at {self.host}:{self.port}/{self.database}\n\n"
        description += system_prompt

        return description
