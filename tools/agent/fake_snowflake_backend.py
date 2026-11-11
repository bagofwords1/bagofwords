"""Boot the real BOW backend against a fake Snowflake account.

Only Snowflake's driver is replaced (tests/fixtures/fake_snowflake.py):
SnowflakeClient, the pool, pandas, the agent and the code sandbox are the real
code. Statements run on the SQLite file $WH_DB; every statement is appended to
$WH_HISTORY (jsonl: query_id, query_text, query_tag, session_id, error), which
is what a Snowflake admin would read in QUERY_HISTORY.

    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db BOW_ENCRYPTION_KEY=<fernet key> \
    WH_DB=/abs/warehouse.db WH_HISTORY=/abs/wh_history.jsonl \
    .venv/bin/python ../tools/agent/fake_snowflake_backend.py

See docs/feedback-loops/snowflake-query-tag.md.
"""
import json
import os
import sys
BACKEND = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend"))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from tests.fixtures.fake_snowflake import FakeSnowflakeServer
from app.data_sources.clients import snowflake_client
from app.ai.prompt_formatters import Table, TableColumn

WH_DB = os.environ["WH_DB"]
WH_HISTORY = os.environ["WH_HISTORY"]

class DumpingServer(FakeSnowflakeServer):
    def _record(self, session, statement, error=None):
        qid = super()._record(session, statement, error)
        with open(WH_HISTORY, "a") as f:
            f.write(json.dumps(self.history[-1]) + "\n")
        return qid

server = DumpingServer(WH_DB, pool_size=2)

class _MP:  # minimal monkeypatch stand-in
    def setattr(self, obj, name, value):
        setattr(obj, name, value)
server.install(_MP())

# Catalog discovery: Snowflake's INFORMATION_SCHEMA does not exist on SQLite,
# so list the fake warehouse's tables through the client's own connection
# (still tagged, still in the history).
def _get_tables(self):
    out = []
    with self.connect() as conn:
        names = [r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for n in names:
            cols = [TableColumn(name=r[1], dtype=r[2] or "TEXT")
                    for r in conn.exec_driver_sql(f"PRAGMA table_info('{n}')")]
            out.append(Table(name=n, columns=cols, pks=[], fks=[], metadata_json={}))
    return out
snowflake_client.SnowflakeClient.get_tables = _get_tables

import uvicorn
import main
uvicorn.run(main.app, host="0.0.0.0", port=8000, log_level="info")
