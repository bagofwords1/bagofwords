"""Pure rules behind the run permission mode and the built-in blocklist."""
import time

import pytest
import sqlglot

from app.services import tool_permission_mode as pm
from app.services.tool_blocklist import block_reason, shell_block_reason, sql_block_reason

# The refusal reasons, spelled out so a changed string fails here.
DROP = "drops a database, schema or table"
TRUNCATE = "truncates a table"
DELETE = "deletes every row of a table (no WHERE)"
UNREADABLE = "could not read SQL safely"
# sqlglot's default dialect plus the four the blocklist reads with.
FIVE_DIALECTS = ("", "tsql", "postgres", "mysql", "snowflake")


def _every_tokenizer_raises(text):
    for d in FIVE_DIALECTS:
        try:
            sqlglot.tokenize(text, read=d)
        except Exception:
            continue
        return False
    return True


# ── blocklist ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -fr ~", "sudo rm -rf /*", "rm --recursive --force $HOME",
    "ls && rm -rf /", "cd /tmp; rm -Rf *", "rm --no-preserve-root -rf /",
    "mkfs.ext4 /dev/sdb1", "dd if=/dev/zero of=/dev/sda bs=1M", "shutdown -h now",
    "sudo reboot", ":(){ :|:& };:", "echo x > /dev/sda",
    "Format-Volume -DriveLetter D", "Remove-Item -Recurse -Force C:\\",
    "systemctl reboot", "/sbin/shutdown -r now", "sudo -n reboot", "sh -c 'rm -rf /'",
    "bash -lc \"cd / && rm -rf *\"", "echo `rm -rf ~`", "x=$(rm -rf /)",
    # Wrapper options that take a value, and trailing slashes.
    "sudo -u root rm -rf /", "timeout 10 rm -rf /", "nice -n 5 rm -rf /", "env X=1 rm -rf /",
    "sudo -u postgres timeout 60s rm -fr /var/", "rm -rf /etc/", "rm -rf /home/", "rm -rf ${HOME}/",
    "find / -print0 | xargs -0 rm -rf /", "sudo -u root shutdown -h now", "timeout 5 reboot",
    # Wildcards under protected dirs; nested shells behind flags; su -c.
    "rm -rf /home/*", "rm -rf /var/*", "rm -rf ~/*/", "bash -l -c \"sudo reboot\"", "sh -x -c 'reboot'",
    "sudo --user root reboot", "sudo --user=root reboot", "su -c \"shutdown -h now\"", "su root -c 'rm -rf /'",
    "init 0", "sudo init 6", "telinit 0",
    # SQL through a database client.
    "sudo -u postgres psql -c \"DROP DATABASE analytics\"", "psql -c \"DROP TABLE orders\"",
    "mysql -e \"DROP DATABASE shop\"", "psql -c \"TRUNCATE orders\"", "echo \"DROP TABLE x;\" | psql",
    "psql --command=\"DELETE FROM orders\"", "sqlcmd -Q \"DELETE orders\"",
])
def test_destructive_shell_commands_are_blocked(command):
    assert shell_block_reason(command)


@pytest.mark.parametrize("command", [
    "ls -la", "rm -rf ./build", "rm -rf /tmp/run-123", "rm old.log", "cat /etc/hostname",
    "echo shutdown", "grep -r reboot /var/log", "df -h", "dd if=a.img of=b.img",
    "dd if=/dev/sda of=/dev/null bs=1M count=1", "Get-Date -Format H:mm", "systemctl status nginx",
    "sudo -u postgres psql -c 'select 1'", "timeout 10 ls /", "rm -rf /tmp/build/", "env X=1 echo reboot",
    "rm -rf ./build/*", "rm -rf /var/tmp/cache/*", "su - postgres -c 'psql -l'", "bash -lc 'ls /home'",
    "psql -c \"select count(*) from orders\"", "grep -r 'drop table' /var/log/app.log",
    "psql -c \"DELETE FROM orders WHERE id = 3\"",
])
def test_ordinary_shell_commands_are_not_blocked(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("sql", [
    "DROP TABLE users", "drop database prod;", "select 1; DROP SCHEMA s",
    "TRUNCATE TABLE orders", "truncate orders", "DELETE FROM orders",
    "/* cleanup */ delete from orders;", "-- x\nDROP TABLE t",
    "SELECT 1 DROP TABLE x", "select 1\nGO\ndrop table x", "insert into a values (1); truncate b",
    "TRUNCATE TABLE orders CASCADE", "truncate table a, b", "TRUNCATE ONLY t RESTART IDENTITY",
    "DELETE orders", "delete from only orders",
    "DELETE FROM orders RETURNING *", 'DELETE FROM "Order Items"', 'TRUNCATE TABLE "Order Items"',
    "EXEC('DROP TABLE x')", "delete from [dbo].[Order Items]",
    "DELETE FROM orders o", "DELETE FROM orders AS o", "EXECUTE sp_executesql N'TRUNCATE TABLE t'",
])
def test_destructive_sql_is_blocked(sql):
    assert sql_block_reason(sql)


@pytest.mark.parametrize("text", [
    "SELECT * FROM orders", "DELETE FROM orders WHERE id = 3",
    "Explain TRUNCATE vs DELETE", "update t set a = 1 where id = 2", "",
])
def test_ordinary_text_and_sql_are_not_blocked(text):
    assert sql_block_reason(text) is None


def test_prose_about_drop_table_is_refused_in_sql_but_not_in_search_text():
    # Accepted false refusal (D1): a SQL argument is read as SQL, so DROP TABLE
    # anywhere in it is refused. The same text in a search or prose key passes.
    assert sql_block_reason("how do I drop table x?") == DROP
    assert sql_block_reason("how do I drop table x?", sql_context="shared") is None
    assert sql_block_reason("how do I drop table x?", sql_context="other") is None


@pytest.mark.parametrize("key,text", [
    ("subject", "Delete from inbox"), ("title", "x; truncate the axis"), ("body", "truncate orders"),
    # Search tools share keys like query/q with SQL tools: two-word phrases pass.
    ("query", "delete account"), ("query", "delete user"), ("q", "truncate text"),
    ("command", "delete branch"),
    # Prose that mentions a DROP under a non-SQL key.
    ("title", "Drop table orders_tmp after migration"), ("text", "Drop schema staging once the backfill is done"),
])
def test_prose_outside_sql_arguments_is_not_blocked(key, text):
    assert block_reason(tool_name="send_message", arguments={key: text}) is None


@pytest.mark.parametrize("key,text", [
    ("sql", "DELETE orders"), ("sqlQuery", "DELETE FROM orders"), ("sql_statement", "truncate orders"),
    ("query", "DELETE FROM orders"), ("query", "TRUNCATE TABLE orders"), ("q", "DROP TABLE orders"),
])
def test_sql_in_sql_and_shared_arguments_is_blocked(key, text):
    assert block_reason(tool_name="run", arguments={key: text})


@pytest.mark.parametrize("tool", ["execute_sql", "run_query", "query", "postgres__query", "db_execute"])
@pytest.mark.parametrize("text", ["TRUNCATE orders", "truncate public.orders restart identity", "DELETE orders"])
def test_shared_key_on_a_sql_tool_gets_the_full_sql_rules(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text})


@pytest.mark.parametrize("tool", ["search_docs", "query_knowledge_base", "search"])
def test_shared_key_on_a_search_tool_stays_lenient(tool):
    assert block_reason(tool_name=tool, arguments={"query": "delete account"}) is None


def test_reading_history_that_mentions_drop_is_not_blocked():
    q = ("SELECT query_text FROM snowflake.account_usage.query_history "
         "WHERE query_text ILIKE 'drop table orders%'")
    assert block_reason(tool_name="execute_sql", arguments={"query": q}) is None
    assert block_reason(tool_name="execute_sql", arguments={"query": "SELECT 'x; DROP TABLE y' AS s"}) is None


def test_block_reason_reads_nested_arguments_and_tool_names():
    assert block_reason(tool_name="run_query", arguments={"opts": {"sql": ["select 1", "DROP TABLE t"]}})
    assert block_reason(tool_name="delete_all_rows", arguments={})
    assert block_reason(tool_name="drop_table", arguments={})
    assert block_reason(tool_name="get_table", arguments={"q": "x"}) is None
    for name in ("purge_all_data", "drop_table_if_exists", "db.drop_table", "srv__wipe_database"):
        assert block_reason(tool_name=name, arguments={}), name
    assert block_reason(tool_name="run", arguments={"sql": "DELETE FROM orders"})
    # Shell patterns only apply to the remote shell tool.
    assert block_reason(tool_name="search", arguments={"command": "rm -rf /"}) is None
    assert block_reason(tool_name="execute_shell", arguments={"command": "rm -rf /"}, remote_shell=True)


# ── blocklist: SQL read with the parser and the token scan ─────────────────

@pytest.mark.parametrize("sql", [
    # CTE deletes.
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "WITH x AS (SELECT 1) DELETE FROM t",
    # Quoting and modifiers.
    "DELETE FROM [dbo].[t] AS x", 'DELETE FROM "s"."t"', "DELETE FROM `db`.`t`", "DELETE LOW_PRIORITY FROM t",
    "DELETE @tv", "DELETE FROM t OUTPUT deleted.*",
    # EXEC bodies; the last one fails to parse, so only the token check sees it.
    "EXEC ('DROP TABLE x')", "EXECUTE('TRUNCATE TABLE t')", "EXEC sp_executesql N'DELETE FROM t'",
    "EXEC('EXEC(''DROP TABLE x'')')", "SELECT 1 EXEC('DROP TABLE x')",
    # Batches.
    "SELECT 1 DELETE FROM t", "select 1\ngo\ndelete from t", "SELECT 1\nGO\nTRUNCATE TABLE t",
    "UPDATE t SET a = 1 WHERE id = 2 TRUNCATE TABLE x",
    # Statement starters a regex start-list missed.
    "SET NOCOUNT ON DROP TABLE x", "IF 1=1 DROP TABLE x", "(SELECT 1) DROP TABLE x",
    # MySQL escaped quote.
    "SELECT 'it\\'s'; DROP TABLE x",
    # Procedural and trigger bodies start statements after THEN, AS, BEGIN, DO.
    "DO $$ BEGIN IF true THEN DELETE FROM t; END IF; END $$", "BEGIN IF (TRUE) THEN DELETE FROM t; END IF; END",
    "CREATE TRIGGER tr ON orders AFTER INSERT AS DELETE FROM orders", "CREATE PROCEDURE p AS DELETE FROM t\nGO\nEXEC p",
    "DO $$ BEGIN TRUNCATE t; END $$", "DO $body$ BEGIN TRUNCATE t; END $body$",
    "CREATE PROCEDURE p AS BEGIN TRUNCATE TABLE t END", "IF 1=1 BEGIN TRUNCATE t END",
    "CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO DELETE FROM t", "WHILE 1=1 BEGIN DELETE TOP (10) FROM t END",
    # Parentheses do not hide a missing WHERE.
    "DELETE FROM t USING (SELECT id FROM u WHERE x = 1) s",
    # Statements after SET … ON (T-SQL needs no separator there).
    "SET NOCOUNT ON\nDELETE FROM t", "SET XACT_ABORT ON DELETE FROM t", "SET IDENTITY_INSERT t ON DELETE FROM t",
    # BEFORE and AFTER can be T-SQL table names.
    "SELECT * FROM after DELETE FROM t", "SELECT * FROM before DELETE FROM t",
    # The trigger event is exempt, the body is not.
    "CREATE TRIGGER tr ON t FOR DELETE AS DELETE FROM u",
    # DROP with modifiers.
    "DROP TEMPORARY TABLE t", "DROP DYNAMIC TABLE t", "DROP ICEBERG TABLE t", "DROP EXTERNAL TABLE t",
    "DROP TRANSIENT TABLE t", "DROP FOREIGN TABLE t",
    # MySQL executable comments run their body.
    "/*!50001 DROP TABLE x */", "SELECT 1 /*! TRUNCATE TABLE t */", "SELECT 1; /*!40101 DELETE FROM t */",
    # Dollar-quoted bodies without spaces.
    "DO $$BEGIN TRUNCATE t; END$$", "DO $tag$BEGIN TRUNCATE t; END$tag$",
    "CREATE FUNCTION f() RETURNS void AS $$DELETE FROM t$$ LANGUAGE sql",
    # Beside the `-` and NOT follower exemptions: a comment is never a `-` token.
    "DELETE--x\nFROM t", "DELETE -- note\nFROM t", "SELECT 1 DELETE--x\nFROM t",
    "CREATE TRIGGER tr ON t AFTER DELETE NOT FOR REPLICATION AS DELETE FROM u",
])
def test_destructive_sql_spellings_are_refused(sql):
    assert sql_block_reason(sql), sql


@pytest.mark.parametrize("sql", [
    # DDL that only looks destructive.
    "ALTER TABLE t DROP COLUMN c", "ALTER TABLE t DROP CONSTRAINT fk",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE CASCADE)",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE SET NULL)",
    # Comments and literals.
    "SELECT * FROM logs -- drop table x", "SELECT 1 /* DROP TABLE x */", "SELECT 'it''s drop table'",
    "SELECT 'it\\'s a drop table'", "INSERT INTO audit VALUES ('delete from orders')",
    # Privileges, MERGE and triggers.
    "GRANT SELECT, DELETE ON t TO r", "REVOKE DELETE ON t FROM r",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "CREATE TRIGGER tr AFTER DELETE ON t FOR EACH ROW EXECUTE FUNCTION f()",
    # Scoped deletes and functions.
    "DELETE FROM t WHERE id IN (SELECT id FROM u)", "DELETE t FROM t JOIN u ON t.a = u.a WHERE u.x = 1",
    "SELECT TRUNCATE(price, 2) FROM t", "SELECT CASE WHEN a THEN TRUNCATE(price, 2) ELSE 0 END FROM t",
    # Parenthesized subqueries before WHERE.
    "DELETE FROM t USING (SELECT id FROM u) s WHERE t.id = s.id",
    "DELETE t FROM t JOIN (SELECT a FROM u) x ON t.a = x.a WHERE x.a > 1",
    # MERGE clauses after THEN DELETE.
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED AND s.x = 1 THEN DELETE "
    "WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id)",
    "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN DELETE OUTPUT deleted.*;",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE RETURNING t.*",
    # Identifiers.
    "SELECT a AS truncate, b FROM t", "SELECT x.delete FROM t x", 'SELECT "delete", [truncate] FROM t',
    # No gate word, even when malformed.
    "SELECT 'unterminated",
    # Literals are never keywords.
    "SELECT * FROM t WHERE op = 'DELETE' AND ts > x", "SELECT CASE WHEN a THEN 'truncate' ELSE 'keep' END FROM t",
    "SELECT N'DELETE FROM t' AS s",
    # FK actions, rules and trigger events.
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE NO ACTION)",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE SET DEFAULT)",
    "CREATE TABLE c (id int REFERENCES p(id) ON DELETE RESTRICT)",
    "CREATE RULE r AS ON DELETE TO t DO INSTEAD NOTHING", "CREATE TRIGGER tr ON t FOR DELETE AS SELECT 1",
    "CREATE TRIGGER tr ON t AFTER DELETE AS SELECT 1",
    "CREATE TRIGGER tr AFTER UPDATE OF c OR DELETE ON t FOR EACH ROW EXECUTE FUNCTION f()",
    "SET NOCOUNT ON;\nDELETE FROM t WHERE id = 1",
    # T-SQL table hints.
    "DELETE FROM t WITH (ROWLOCK) WHERE id = 1",
    "DELETE TOP (1000) FROM t WITH (TABLOCK) WHERE created < '2020-01-01'",
    # The DROP look-ahead stops at ON, punctuation and other kinds.
    "ALTER TABLE t DROP COLUMN c, DROP COLUMN d", "DROP INDEX ix ON t", "DROP MATERIALIZED VIEW v",
    "GRANT DROP ON TABLE t TO u",
    # A MySQL executable comment without a destructive statement.
    "SELECT 1 /*!40101 DELETE FROM t WHERE id = 1 */",
    # File paths and trigger options after DELETE (`-` and NOT followers).
    "source ./delete-old-rows.sql", "\\i delete-old-rows.sql",
    "CREATE TRIGGER tr ON t AFTER DELETE NOT FOR REPLICATION AS SELECT 1",
    "CREATE TRIGGER tr ON t FOR DELETE NOT FOR REPLICATION AS SELECT 1",
    # Not a NOT test: the `,` before DELETE already exempts it.
    "CREATE TRIGGER tr ON t AFTER INSERT, DELETE NOT FOR REPLICATION AS SELECT 1",
])
def test_ordinary_sql_spellings_are_not_refused(sql):
    assert sql_block_reason(sql) is None, sql


@pytest.mark.parametrize("text", [
    "delete account", "delete user", "truncate text", "delete branch", "DELETE FROM orders WHERE id = 1",
    "delete from my history the old items", "truncate table names longer than 20 chars",
    "drop the table from the deck",
])
def test_search_phrases_pass_on_a_search_tool(text):
    assert block_reason(tool_name="search_docs", arguments={"query": text}) is None


@pytest.mark.parametrize("text", [
    "SELECT 1; DROP TABLE x",
    "Delete from inbox",  # known limit, accepted
    "x; DROP SCHEMA s",
    "DELETE FROM t USING u",  # only the parse tree sees it
])
def test_sql_in_a_search_tool_query_is_refused(text):
    assert block_reason(tool_name="search_docs", arguments={"query": text})


# Parity: shared-key texts that no SQL tokenizer can read keep today's result.
# The recorded values are the results of the module at 3875d2971 (probe r5 §1).
# Do not edit them to make this test pass: a change here is a behaviour change.
_PARITY_SHARED = [
    ("DROP TABLE x; /*", DROP), ("DROP TABLE x; /*/", DROP), ("DROP TABLE x; SELECT 'a", DROP),
    ("DROP TABLE x; /* a */ /*", DROP), ("x; SELECT '/*'; DROP TABLE x; SELECT 'a", DROP),
    ("x /* /* */ ; DROP TABLE y; SELECT 'a", DROP), ("x; EXEC('DROP TABLE y'); SELECT 'a", DROP),
    ("x\ngo\nDROP TABLE y; /*", DROP), ("DROP TABLE x -- note\n; /*", DROP),
    ("DELETE FROM orders; /*", DELETE), ('DELETE FROM "Order Items"; /*', DELETE),
    ("DELETE FROM `my-project.ds.t`; /*", DELETE), ("DELETE FROM orders o; /*", DELETE),
    ("x; EXEC('DELETE orders'); SELECT 'a", DELETE), ("Delete from inbox; it's urgent", DELETE),
    ('TRUNCATE TABLE "Order Items"; SELECT \'a', TRUNCATE), ("x; EXEC('TRUNCATE orders'); SELECT 'a", TRUNCATE),
    ("x; /* DROP TABLE y", None), ("why did revenue drop? what's the cause", None),
    ("why did revenue drop? /* what's next", None), ("delete from my history the old item's", None),
    ("truncate table names that aren't used", None), ("don't drop the table", None),
    ("DELETE FROM orders WHERE id = 1; /*", None),
]


@pytest.mark.parametrize("text,today", _PARITY_SHARED)
def test_unreadable_shared_text_keeps_todays_result(text, today):
    assert _every_tokenizer_raises(text)
    assert sql_block_reason(text, sql_context="shared") == today
    for tool in ("search_docs", "query_knowledge_base"):
        assert block_reason(tool_name=tool, arguments={"query": text}) == today, tool
    # Deliberate change (Nir: "Tool schema + names"): on a tool named for
    # running SQL (write_query, read_query) this is SQL, and unreadable SQL with
    # a gate word is refused. At 3875d2971 these two gave the shared result.
    for tool in ("write_query", "read_query"):
        assert block_reason(tool_name=tool, arguments={"query": text}) == UNREADABLE, tool


def test_unreadable_shared_text_without_its_gate_words_passes():
    # EXEC with no drop/truncate/delete fails the shared gate, so it never reaches
    # the parity fallback; today passes it too.
    text = "x; EXEC('SELECT 1'); SELECT 'a"
    assert _every_tokenizer_raises(text)
    assert sql_block_reason(text, sql_context="shared") is None


@pytest.mark.parametrize("text,today", [
    ("DROP TABLE [it's], `a\"b`", DROP), ("DROP TABLE x; /*", None), ("DROP TABLE x; SELECT 'a", None),
])
def test_unreadable_other_text_keeps_todays_result(text, today):
    # Recorded from the module at 3875d2971 (probe r5 §3 and probe c).
    assert _every_tokenizer_raises(text)
    assert sql_block_reason(text, sql_context="other") == today
    assert block_reason(tool_name="save_note", arguments={"title": text}) == today


def test_a_single_drop_over_the_parse_limit_keeps_todays_result_at_other():
    # Readable, but over 200 KB, so the parser is skipped and today's rule decides.
    text = "DROP TABLE " + ", ".join(f"t{i}" for i in range(30_000))
    assert len(text.encode()) == 228_899
    assert sql_block_reason(text, sql_context="other") == DROP
    assert block_reason(tool_name="save_note", arguments={"title": text}) == DROP


def test_an_unreadable_other_text_over_the_scan_cap_still_gets_todays_rule():
    # Level `other` has no scan cap: the parity fallback runs without tokenizing.
    big = "DROP TABLE [it's], " + ", ".join(f"t{i}" for i in range(150_000))
    assert len(big.encode()) > 1024 * 1024
    assert sql_block_reason(big, sql_context="other") == DROP
    assert sql_block_reason(big + "; /*", sql_context="other") is None


def test_many_dead_comment_openers_are_checked_in_linear_time():
    text = "DROP TABLE x; " + "/* " * 300_000
    assert len(text) == 900_014
    assert block_reason(tool_name="search_docs", arguments={"query": text}) == DROP


def test_promotion_follows_todays_sql_start_rule():
    # re.IGNORECASE reads the dotted capital I as "i", so today's rule promotes
    # this text (and refused it); every tokenizer raises, so it is unreadable SQL.
    assert block_reason(tool_name="search_docs", arguments={"query": "İNSERT x; TRUNCATE orders; SELECT 'a"})


@pytest.mark.parametrize("tool", ["write_query", "read_query", "run_snowflake_query"])
@pytest.mark.parametrize("text", [
    "INSERT INTO a VALUES (1); TRUNCATE b", "EXEC('TRUNCATE orders')", "SELECT 1; DELETE orders",
    "SELECT 1 DROP TABLE x", "select 1\nGO\ndrop table x",
])
def test_shared_key_sql_on_a_sql_tool_the_name_rule_misses_is_refused(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text})


def test_other_context_refuses_only_a_lone_drop():
    assert sql_block_reason("Drop table orders_tmp after migration", sql_context="other") is None
    assert sql_block_reason("drop table x; select 1", sql_context="other") is None
    assert sql_block_reason("DROP TABLE x", sql_context="other") == DROP


def test_size_limits():
    big_select = "SELECT 1;" * 23_000
    assert len(big_select) > 200 * 1024
    assert sql_block_reason(big_select + " DROP TABLE x") == DROP  # parser skipped, tokens decide
    assert sql_block_reason("SELECT a FROM t;" * 14_000) is None
    # Accepted false refusal (D8): over the 1 MB scan cap with a gate word.
    assert sql_block_reason("SELECT 1; " * 110_000 + "delete") == UNREADABLE


def test_execute_bodies_are_read_when_the_parser_is_skipped():
    # Every tokenizer reads EXECUTE as a command and its rest as one raw string;
    # over 200 KB only the token check runs, and it must still read the body.
    big_select = "SELECT 1;" * 23_000
    assert sql_block_reason(big_select + " EXECUTE('TRUNCATE TABLE t')") == TRUNCATE
    assert sql_block_reason(big_select + " EXECUTE sp_executesql N'DELETE FROM t'") == DELETE
    assert sql_block_reason(big_select + " EXECUTE('SELECT 1')") is None


def test_unreadable_sql_with_a_gate_word_is_refused():
    assert sql_block_reason("DELETE FROM t WHERE a = 'unterminated") == UNREADABLE


def test_a_parser_crash_leaves_the_token_check_to_decide():
    # Deep nesting makes the parser raise RecursionError, not a sqlglot error.
    assert sql_block_reason("(" * 5000 + "SELECT 1" + ")" * 5000 + "; DROP TABLE x") == DROP
    assert sql_block_reason("SELECT 1 DROP TABLE x") == DROP


@pytest.mark.parametrize("command,today", [
    # Parity: the unreadable shell token gets today's result (probe r5 §2).
    ("echo \"DROP TABLE x; SELECT 'a\" | psql", DROP), ('echo "DROP TABLE x; /*" | psql', DROP),
    ('echo "DROP TABLE x; /*/" | psql', DROP), ('echo "DELETE FROM orders; /*" | psql', DELETE),
    ("echo 'DELETE FROM \"Order Items\"; /*' | psql", DELETE),
    ("echo 'DELETE FROM `my-project.ds.t`; /*' | bq query --nouse_legacy_sql", DELETE),
    ("echo \"TRUNCATE TABLE orders; SELECT 'a\" | psql", TRUNCATE),
    ("echo 'TRUNCATE TABLE \"Order Items\"; SELECT '\"'\"'a' | psql", TRUNCATE),
    ('echo "x; /* DROP TABLE y" | psql', None),
])
def test_unreadable_sql_piped_into_a_db_client_keeps_todays_result(command, today):
    assert shell_block_reason(command) == today


@pytest.mark.parametrize("command", [
    'psql -c "WITH d AS (DELETE FROM t RETURNING 1) SELECT 1"',
    'echo "SELECT 1; TRUNCATE orders" | psql',  # promoted, as today
])
def test_more_sql_through_a_db_client_is_refused(command):
    assert shell_block_reason(command)


@pytest.mark.parametrize("command", [
    'mysql -e "SELECT 1; GRANT SELECT, DELETE ON t TO r"', "psql -d db -f delete.sql",
    'psql -c "select 1" > truncate.log', 'echo "it\'s done" | psql -c "select 1"',
    'psql -c "select 1" && echo "delete step done"', "psql -d db -f ./delete-old-rows.sh",
    './delete-old-rows.sh && psql -c "select 1"', 'mysql -e "source ./delete-old-rows.sql"',
    'psql -c "\\i delete-old-rows.sql"',
    # Known limits, as today: a heredoc body and unquoted words are separate
    # shell tokens, so no single token holds the statement. Named in the PR.
    "psql <<'SQL'\nDROP TABLE x;\nSQL", "echo DROP TABLE x | psql",
])
def test_ordinary_db_client_commands_pass(command):
    assert shell_block_reason(command) is None


# Accepted false refusals: each is refused on purpose, so a later change shows
# up as a deliberate one. "Today" is the module at 3875d2971 (probe r5 §5).
@pytest.mark.parametrize("sql", [
    "how do I drop table x?",  # D1, new
    "SELECT [drop table] FROM t",  # catalog: `[` is punctuation outside T-SQL, new
    "CREATE TRIGGER tr ON t AFTER DELETE WITH APPEND AS SELECT 1",  # catalog: WITH is no follower, new
    "SELECT [delete] FROM t",  # catalog: only tsql reads [delete] as a name, new
    "drop the temp table",  # catalog: modifier look-ahead, new
    "/*! drop table */",  # catalog: the /*! copy is read in every dialect, new
    "SELECT a AS delete FROM t",  # catalog: AS DELETE FROM is also a procedure body, new
    "SELECT 1 # drop table x",  # catalog: # is a comment only in MySQL, as today
    "EXEC sp_log 'drop table happened'",  # catalog: EXEC literals are rechecked, as today
])
def test_accepted_false_refusals_in_sql(sql):
    assert sql_block_reason(sql)


@pytest.mark.parametrize("text", [
    "update on the delete account flow",  # D10, new
    "with delete pending, then archive",  # D10, new
    "Delete from inbox",  # catalog, as today
])
def test_accepted_false_refusals_on_a_search_tool(text):
    assert block_reason(tool_name="search_docs", arguments={"query": text})


def test_promoted_unreadable_search_text_is_refused_as_unreadable():
    # Accepted false refusal (D10, new): promoted to SQL, and no tokenizer reads it.
    assert block_reason(tool_name="search_docs", arguments={"query": "Select John's tickets marked delete"}) == UNREADABLE


@pytest.mark.parametrize("command", [
    'psql -f x.sql && echo "Begin delete of old rows"',  # D10, new
    'psql -c "select 1" && echo "Update done - delete step next"',  # D10, new
])
def test_accepted_false_refusals_beside_a_db_client(command):
    assert shell_block_reason(command)


def test_nested_deletes_are_checked_in_linear_time():
    # Accepted exception to tests/AGENTS.md rule 8: this reads the clock, but it
    # asserts only a ratio of ratios taken in one process with a lenient bound.
    # The baseline is the same five-dialect tokenizing the check itself does.
    def text(k):
        return "DELETE FROM t USING (" * k + "SELECT 1" + ") s WHERE 1" * k

    small, large = text(7000), text(28000)
    assert (len(small.encode()), len(large.encode())) == (224_008, 896_008)

    def baseline(t):
        start = time.perf_counter()
        for d in FIVE_DIALECTS:
            sqlglot.tokenize(t, read=d)
        return time.perf_counter() - start

    def check(t):
        start = time.perf_counter()
        assert sql_block_reason(t, sql_context="sql") is None
        return time.perf_counter() - start

    base_small = baseline(small)
    check_small = check(small)
    base_large = baseline(large)
    check_large = check(large)
    assert check_large / check_small <= 2 * (base_large / base_small)


# ── blocklist: EXEC rechecks, analysis caps and dynamic SQL ────────────────

def _read_once(text):
    """Seconds to read ``text`` once in each of the five dialects, as the
    check itself does: tokenize, and parse up to the 200 KB parse limit. The
    least work any check of the text does."""
    from app.ai.code_execution.sql_parser import parse_guard_sql

    parse = len(text.encode()) <= 200 * 1024
    start = time.perf_counter()
    for d in FIVE_DIALECTS:
        sqlglot.tokenize(text, read=d)
        if parse:
            try:
                parse_guard_sql(text, d)
            except Exception:
                pass
    return time.perf_counter() - start


def _many_literals(k):
    return "EXEC p " + ",".join("'drop%d'" % i for i in range(k))


@pytest.mark.parametrize("text", [
    pytest.param(_many_literals(20_000), id="many-literals"),  # 228,896 bytes
    pytest.param("".join("EXEC p%d; " % i for i in range(8_000)), id="many-exec-statements"),
])
def test_many_exec_literals_cost_about_one_read_of_the_text(text):
    # Accepted exception to tests/AGENTS.md rule 8: this reads the clock, but it
    # compares two timings taken in one process with a lenient bound. Literals
    # are rechecked in one batch, not one sqlglot read each (about 1.2 ms per
    # literal: 18 s for the 229 KB row, 15 times one read, before the batching).
    base = _read_once(text)
    start = time.perf_counter()
    assert block_reason(tool_name="search_docs", arguments={"query": text}) is None
    assert time.perf_counter() - start <= 4 * base


def test_many_distinct_exec_literals_are_checked_in_linear_time():
    # Same accepted exception as above: a ratio of ratios in one process.
    small, large = _many_literals(4_000), _many_literals(16_000)
    base_small, base_large = _read_once(small), _read_once(large)

    def check(t):
        start = time.perf_counter()
        assert sql_block_reason(t) is None
        return time.perf_counter() - start

    check_small, check_large = check(small), check(large)
    assert check_large / check_small <= 2 * (base_large / base_small)


@pytest.mark.parametrize("sql", [
    # Each literal is its own statement in the batched recheck, and split
    # strings are still joined.
    "EXEC p 'x', 'DROP TABLE orders'", "EXEC('DR' + 'OP TABLE orders')",
    "EXEC sp_executesql N'DELETE FROM t WHERE n = ''a''', N'TRUNCATE TABLE u'",
    "EXEC p 'it''s'; EXEC('DROP TABLE orders')",
    # A literal that opens a quote, name or comment is never joined to the
    # next one, where it would hide it.
    "EXEC p 'drop \"'; EXEC('DROP TABLE orders'); EXEC q '\" drop'",
    "EXEC p 'drop /*'; EXEC('DROP TABLE orders'); EXEC q 'drop */'",
    "EXEC p 'drop ['; EXEC('DROP TABLE orders'); EXEC q 'drop ]'",
    "EXEC p 'drop $$'; EXEC('DROP TABLE orders'); EXEC q 'drop $$'",
    # Dynamic SQL whose text sits in another statement.
    "PREPARE s FROM 'DROP TABLE orders'; EXECUTE s;",
    "DECLARE @q NVARCHAR(99) = 'DROP TABLE orders'; EXEC(@q)",
    "DECLARE @q NVARCHAR(99); SET @q = 'TRUNCATE TABLE orders'; EXEC sp_executesql @q",
    "DECLARE @a NVARCHAR(9) = 'DR'; DECLARE @b NVARCHAR(99) = 'OP TABLE orders'; EXEC(@a + @b)",
    "SET @q = 'DELETE FROM orders'; PREPARE s FROM @q; EXECUTE s",
    "PREPARE s AS DELETE FROM orders",
    # Every tokenizer keeps the rest of an EXECUTE statement as one raw string;
    # a T-SQL batch needs no separator, so the rest is read as SQL too.
    "EXECUTE p DROP TABLE orders", "EXECUTE('SELECT 1') DROP TABLE orders",
])
def test_exec_literals_and_dynamic_sql_are_read(sql):
    assert sql_block_reason(sql), sql


@pytest.mark.parametrize("sql", [
    "EXEC p 'hello'", "EXEC p0; EXEC p1; EXEC p2", "EXEC p 'deleted rows', 'x'",
    "INSERT INTO log VALUES ('delete from orders'); EXEC p",
    "DECLARE @n INT = 1; EXEC p @n", "PREPARE s FROM 'SELECT * FROM orders'; EXECUTE s",
    "DECLARE @q NVARCHAR(99) = 'SELECT 1'; EXEC(@q)",
    "EXEC('EXEC(''EXEC(''''SELECT 1'''')'')')",
    # Procedure names that hold a gate word are read once, not refused as
    # nested past the recheck depth.
    "EXECUTE dbo.delete_old_rows", "EXEC dbo.delete_old_rows; EXEC dbo.drop_stale_rows",
    "EXECUTE dbo.delete_old_rows; EXECUTE dbo.drop_stale_rows; EXECUTE dbo.truncate_log_rows",
])
def test_ordinary_exec_and_prepared_statements_pass(sql):
    assert sql_block_reason(sql) is None, sql


def test_more_quoted_exec_literals_than_the_recheck_bound_are_refused():
    # Accepted false refusal (a cost bound, like D8): a literal with a quote is
    # rechecked alone, at most 64 per level; past that the text is refused.
    def text(k):
        return "EXEC p " + ", ".join("'drop \"t%d\"'" % i for i in range(k))

    assert sql_block_reason(text(60)) is None  # 60 literals and their concatenation
    assert sql_block_reason(text(70)) == UNREADABLE


def test_exec_nested_past_the_recheck_depth_is_refused():
    # Three levels deep: the third body is past the recheck depth, so the
    # check cannot read it and refuses instead of passing it.
    assert block_reason(tool_name="run", arguments={"sql": "EXEC('EXEC(''EXEC(''''DROP TABLE orders'''')'')')"})


def test_arguments_nested_past_the_walk_depth_are_refused():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"sql": "DROP TABLE orders"}}}}}}}
    assert block_reason(tool_name="run_query", arguments=deep) == DROP
    assert block_reason(tool_name="run_query", arguments={"sql": [[[[[["DROP TABLE orders"]]]]]]}) == DROP
    # Past the walk's depth cap: refused as unreadable, never skipped.
    value: object = "x"
    for _ in range(80):
        value = [value]
    assert block_reason(tool_name="search_docs", arguments={"query": value}) == UNREADABLE
    # A deep but ordinary search body still passes.
    es = {"query": {"bool": {"must": [{"bool": {"should": [{"match": {"title": "delete account"}}]}}]}}}
    assert block_reason(tool_name="search_docs", arguments=es) is None


def _nest(command, shell, n):
    import shlex

    for _ in range(n):
        command = f"{shell} -c {shlex.quote(command)}"
    return command


@pytest.mark.parametrize("n", [8, 9, 12])
def test_shell_nested_past_the_read_depth_is_refused(n):
    assert shell_block_reason(_nest("rm -rf /", "sh", n))
    assert shell_block_reason(_nest("ls -la", "bash", n)) == "could not read the command safely"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_ordinary_nested_shells_pass(n):
    assert shell_block_reason(_nest("ls -la /tmp", "sh", n)) is None


# ── blocklist: which arguments hold SQL ─────────────────────────────────────

_SQL_TOOLS = ["postgres_query", "run_snowflake_query", "write_query", "read_query", "bigquery_query",
              "duckdb_query", "start_query_execution"]


@pytest.mark.parametrize("tool", _SQL_TOOLS)
@pytest.mark.parametrize("text", ["TRUNCATE orders", "DELETE orders"])
def test_a_tool_named_for_a_sql_engine_gets_the_full_sql_rules(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}), (tool, text)


@pytest.mark.parametrize("tool,arguments", [
    ("execute_sql", {"query_text": "DELETE FROM orders"}),
    ("start_query_execution", {"QueryString": "DELETE FROM orders"}),
    ("run_query", {"queries": ["SELECT 1", "DELETE FROM orders"]}),
    # Spellings only the "sql" rules refuse (the shared rules pass them).
    ("execute_sql", {"query_text": "DELETE orders"}),
    ("start_query_execution", {"QueryString": "TRUNCATE orders"}),
    ("run_query", {"queries": ["SELECT 1", "DELETE orders"]}),
    ("postgres_query", {"sql_query": "SELECT 1", "statements": ["SELECT 1", "TRUNCATE orders"]}),
    # Other text on a SQL tool gets the shared rules.
    ("postgres_query", {"database": "x; DROP SCHEMA s"}),
])
def test_sql_keys_on_a_sql_tool_get_the_sql_rules(tool, arguments):
    assert block_reason(tool_name=tool, arguments=arguments), (tool, arguments)


@pytest.mark.parametrize("tool", ["search_docs", "query_knowledge_base", "search", "search_query",
                                  "query_docs", "semantic_search"])
@pytest.mark.parametrize("text", ["delete account", "truncate text", "delete branch"])
def test_search_tools_stay_lenient_under_the_wider_name_rule(tool, text):
    assert block_reason(tool_name=tool, arguments={"query": text}) is None, tool
    assert block_reason(tool_name=tool, arguments={"queries": [text]}) is None, tool


@pytest.mark.parametrize("description", [
    "The SQL query to run", "SQL statement", "A T-SQL batch", "Statement to execute against the warehouse",
])
def test_a_parameter_described_as_sql_gets_the_sql_rules(description):
    schema = {"type": "object", "properties": {"text": {"type": "string", "description": description}}}
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM orders"}, input_schema=schema) == DELETE
    nested = {"type": "object", "properties": {"opts": {"type": "object", "properties": {
        "body": {"type": "array", "items": {"type": "string"}, "title": description}}}}}
    assert block_reason(tool_name="lookup", arguments={"opts": {"body": ["SELECT 1", "TRUNCATE orders"]}},
                        input_schema=nested) == TRUNCATE


@pytest.mark.parametrize("schema", [
    None, {}, {"type": "object"}, {"properties": "junk"},
    {"type": "object", "properties": {"text": {"type": "string", "description": "Search text"}}},
    {"type": "object", "properties": {"text": {"type": "string", "description": "Bank statement id"}}},
])
def test_a_parameter_not_described_as_sql_keeps_its_key_rule(schema):
    assert block_reason(tool_name="lookup", arguments={"text": "DELETE FROM orders"}, input_schema=schema) is None


# ── small-model judge ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '{"approve": "false", "confidence": 0.99}', '{"approve": "no", "confidence": 0.99}',
    '{"approve": 0, "confidence": 0.99}', '{"approve": "true", "confidence": 0.99}',
    '{"approve": 1, "confidence": 0.99}', '{"confidence": 0.99}',
])
def test_a_judge_answer_without_a_real_true_never_allows(raw):
    from app.ai.classifiers.tool_call_judge import ToolCallVerdict

    v = ToolCallVerdict.parse(raw)
    assert v.approve is False
    assert pm.verdict_from_small_model(v.approve, v.confidence, _settings()) == "ask"


def test_a_judge_answer_with_true_approves():
    from app.ai.classifiers.tool_call_judge import ToolCallVerdict

    v = ToolCallVerdict.parse('```json\n{"approve": true, "confidence": 0.95, "reason": "read only"}\n```')
    assert (v.approve, v.confidence) == (True, 0.95)
    assert pm.verdict_from_small_model(v.approve, v.confidence, _settings()) == "allow"


# ── mode resolution ─────────────────────────────────────────────────────────

def _settings(**kw):
    return pm.PermissionSettings(**{**pm.normalize_settings({}), **kw})


def test_allow_all_is_refused_unless_explicitly_permitted():
    assert pm.effective_mode("allow_all", _settings(allow_all_enabled=True)) == "ask"


def test_report_mode_wins_over_the_org_default():
    s = _settings(default_mode="auto")
    assert pm.effective_mode(None, s) == "auto"
    assert pm.effective_mode("ask", s) == "ask"


@pytest.mark.parametrize("switch,permitted,expect", [
    (True, True, "allow_all"), (False, True, "ask"), (True, False, "ask"),
])
def test_allow_all_needs_switch_and_a_watched_owner_run(switch, permitted, expect):
    s = _settings(allow_all_enabled=switch)
    assert pm.effective_mode("allow_all", s, allow_all_permitted=permitted) == expect


def test_strictest_agent_cap_applies():
    s = _settings(allow_all_enabled=True)
    assert pm.effective_mode("allow_all", s, agent_caps=["auto"], allow_all_permitted=True) == "auto"
    assert pm.effective_mode("allow_all", s, agent_caps=["auto", "ask", None], allow_all_permitted=True) == "ask"
    assert pm.effective_mode("ask", s, agent_caps=["auto"]) == "ask"  # a cap never loosens


@pytest.mark.parametrize("p,expect", [
    (0.99, "allow"), (0.85, "allow"), (0.5, "ask"), (0.3, "refuse"), (0.0, "refuse"),
    (None, "ask"), (float("nan"), "ask"), ("junk", "ask"),
])
def test_decision_model_probability_maps_to_three_outcomes(p, expect):
    assert pm.verdict_from_probability(p, _settings()) == expect


@pytest.mark.parametrize("approve,conf,expect", [
    (True, 0.9, "allow"), (False, 0.9, "refuse"), (True, 0.5, "ask"), (False, 0.0, "ask"),
])
def test_small_model_acts_only_on_a_confident_answer(approve, conf, expect):
    assert pm.verdict_from_small_model(approve, conf, _settings()) == expect


def test_an_unreadable_stored_setting_falls_back_to_the_defaults_and_is_logged(caplog):
    class Broken:
        def get_config(self, key):
            raise ValueError("corrupt JSON")

    with caplog.at_level("WARNING", logger="app.services.tool_permission_mode"):
        s = pm.permission_settings(Broken())
    assert (s.default_mode, s.allow_all_enabled) == ("ask", False)
    assert any("corrupt JSON" in r.getMessage() for r in caplog.records)


def test_settings_normalize_bad_values_to_safe_defaults():
    s = pm.normalize_settings({
        "default_mode": "allow_all", "allow_all_enabled": "yes", "unattended": "sure",
        "allow_threshold": 0.4, "refuse_threshold": 0.9, "junk": 1,
    })
    assert s["default_mode"] == "ask"          # allow_all needs a real True switch
    assert s["allow_all_enabled"] is False
    assert s["unattended"] == "refuse"
    assert s["allow_threshold"] >= pm.MIN_ALLOW_THRESHOLD
    assert s["refuse_threshold"] <= s["allow_threshold"]
    enabled = pm.normalize_settings({"default_mode": "allow_all", "allow_all_enabled": True})
    assert enabled["default_mode"] == "allow_all"  # the default while the switch is on
    assert "junk" not in s
    assert pm.normalize_settings({"judge_decision_model_id": "small"})["judge_decision_model_id"] == "small"


@pytest.mark.parametrize("command,windows,expect", [
    ("ls -la /var/log", False, True), ("df -h", False, True),
    ("ps aux", False, False), ("ps eww", False, False),  # can print other processes' environment
    ("Get-ChildItem Env:", True, False), ("Get-Item Env:AWS_SECRET_ACCESS_KEY", True, False),
    ("ls; rm x", False, False), ("ls $(whoami)", False, False), ("rm x", False, False),
    ("ls *", False, False), ("ls > f", False, False),
    # Reads contents (could print a secret) or can also set something: judge.
    ("cat ~/.ssh/id_rsa", False, False), ("tail -F app.log", False, False),
    ("date -s 2020-01-01", False, False), ("hostname newname", False, False),
    ("Get-ChildItem C:\\data", True, True), ("dir C:\\", True, True),
    ("Remove-Item C:\\x", True, False), ("Get-Content .env", True, False),
    ("ipconfig /release", True, False),
])
def test_read_only_shell_rule(command, windows, expect):
    assert pm.is_read_only_shell_command(command, windows=windows) is expect


def test_webhook_turn_counts_as_unattended():
    from types import SimpleNamespace

    from app.services.tool_policy_service import ToolPolicyService

    user = SimpleNamespace(id="u")
    blank = {"webhook_id": None, "trigger_source": None, "scheduled_prompt_id": None, "started_via": None}
    for marker in ("webhook_id", "trigger_source", "scheduled_prompt_id"):
        head = SimpleNamespace(**{**blank, marker: "x"})
        assert not ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    # A run started with an API key: nobody is in the loop to answer.
    head = SimpleNamespace(**{**blank, "started_via": "api_key"})
    assert not ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    assert ToolPolicyService.is_machine_run({"user": user, "head_completion": head})
    head = SimpleNamespace(**blank)
    assert ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    assert not ToolPolicyService.is_machine_run({"user": user, "head_completion": head})
