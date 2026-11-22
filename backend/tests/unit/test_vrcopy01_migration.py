"""vrcopy01 -- a step the upgrade copied takes back only its own source's time.

dashfc08 copied each published conversation's default steps into a release
and stamped every copy with the time of the upgrade; nothing it wrote names
the step a copy came from. vrcopy01 gives a copy the time its source last
changed, and only where the source is certain. A copy given another step's
time would serve readers a result its own step had outdated, so the promise
tested here is one-sided: whatever happened to the conversation before and
after the upgrade, a copy's time is either left as it is or set to the time
its true source had when it was copied -- and only ever moved earlier.

The examples name the cases; the seeded histories below them look for the
ones nobody named. Both drive the migration's upgrade() on an in-memory
SQLite schema holding the columns it reads (the release path is covered on
both databases in tests/e2e/dashboards/test_reader_result_freshness.py).

Run:
    cd backend
    TESTING=true uv run pytest tests/unit/test_vrcopy01_migration.py
"""
import importlib.util
import json
import logging
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

LOG = "alembic.runtime.migration"
UPGRADED = datetime(2026, 6, 1, 12, 0)  # when dashfc08 made the copies
ROWS, OTHER_ROWS = '{"rows": [1]}', '{"rows": [2]}'


def _load_migration():
    path = (Path(__file__).resolve().parents[2]
            / "alembic" / "versions" / "vrcopy01_copied_steps_keep_their_change_time.py")
    spec = importlib.util.spec_from_file_location("vrcopy01", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(metadata, without=()):
    def table(name, *columns):
        if name not in without:
            sa.Table(name, metadata, sa.Column("id", sa.String(36), primary_key=True), *columns)

    def times():
        return (sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime),
                sa.Column("deleted_at", sa.DateTime))

    table("widgets", sa.Column("report_id", sa.String(36)))
    table("queries", sa.Column("title", sa.String, nullable=False), sa.Column("report_id", sa.String(36)),
          sa.Column("artifact_release_id", sa.String(36)), sa.Column("widget_id", sa.String(36)),
          sa.Column("default_step_id", sa.String(36)), *times())
    table("steps", sa.Column("title", sa.String, nullable=False), sa.Column("data", sa.Text),
          sa.Column("query_id", sa.String(36)), sa.Column("widget_id", sa.String(36)), *times())
    table("artifacts", sa.Column("report_id", sa.String(36)), sa.Column("legacy_report_id", sa.String(36)))
    table("artifact_versions", sa.Column("report_id", sa.String(36)))
    table("artifact_releases", sa.Column("artifact_id", sa.String(36)),
          sa.Column("source_version_id", sa.String(36)))
    if "dashfc08_undo" not in without:
        sa.Table("dashfc08_undo", metadata, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
                 sa.Column("table_name", sa.String(64)), sa.Column("row_id", sa.String(255)),
                 sa.Column("action", sa.String(16)))


class Installation:
    """A database as dashfc08 left it: conversations, and the release copies
    of the ones it published. `source_time` holds, for every step copy, the
    time its source had when it was copied (None: it had none)."""

    def __init__(self, new_id=lambda: str(uuid.uuid4()), without=()):
        self.engine = sa.create_engine("sqlite://")
        self.meta = sa.MetaData()
        _schema(self.meta, without)
        self.meta.create_all(self.engine)
        self.new_id = new_id
        self.source_time: dict[str, datetime | None] = {}

    def add(self, table, **values) -> str:
        values.setdefault("id", self.new_id())
        with self.engine.begin() as conn:
            conn.execute(self.meta.tables[table].insert().values(**values))
        return values["id"]

    def set(self, table, row_id, **values):
        tbl = self.meta.tables[table]
        with self.engine.begin() as conn:
            assert conn.execute(tbl.update().where(tbl.c.id == row_id).values(**values)).rowcount == 1

    def get(self, table, row_id):
        tbl = self.meta.tables[table]
        with self.engine.connect() as conn:
            return conn.execute(sa.select(tbl).where(tbl.c.id == row_id)).mappings().first()

    def remove(self, table, row_id):
        tbl = self.meta.tables[table]
        with self.engine.begin() as conn:
            conn.execute(tbl.delete().where(tbl.c.id == row_id))

    # the conversation
    def query(self, report, title) -> str:
        widget = self.add("widgets", report_id=report)
        return self.add("queries", title=title, report_id=report, widget_id=widget)

    def run(self, query, title, data, at, *, shown=True, by_widget_only=False) -> str:
        """A run of the query: a new step, which the query shows from then
        on (`shown`; a failed run is kept beside the one it shows). A step
        of the app's first versions names its widget, not its query."""
        step = self.add("steps", title=title, data=data, widget_id=self.get("queries", query)["widget_id"],
                        query_id=None if by_widget_only else query, created_at=at, updated_at=at)
        if shown:
            self.set("queries", query, default_step_id=step)
        return step

    # dashfc08
    def publish(self, report, queries) -> dict:
        """The conversation's release, with a copy of each of `queries` and
        of the step it shows. The copied steps, by source query."""
        artifact = self.add("artifacts", report_id=report, legacy_report_id=report)
        version = self.add("artifact_versions", report_id=report)
        release = self.add("artifact_releases", artifact_id=artifact, source_version_id=version)
        copies = {}
        for query in queries:
            q = self.get("queries", query)
            source = self.get("steps", q["default_step_id"])
            new_query = self.add("queries", title=q["title"], artifact_release_id=release, deleted_at=q["deleted_at"],
                                 created_at=UPGRADED, updated_at=UPGRADED)
            copy = self.add("steps", title=source["title"], data=source["data"], query_id=new_query,
                            deleted_at=source["deleted_at"], created_at=UPGRADED, updated_at=UPGRADED)
            self.set("queries", new_query, default_step_id=copy)
            for table, row in (("queries", new_query), ("steps", copy)):
                self.add("dashfc08_undo", id=None, table_name=table, row_id=row, action="insert")
            self.source_time[copy] = source["updated_at"]
            copies[query] = copy
        self.released = {"artifact": artifact, "version": version, "release": release}
        return copies

    def time_of(self, step):
        row = self.get("steps", step)
        return row and row["updated_at"]

    # vrcopy01
    def upgrade(self) -> list:
        """Run the migration; the records it logged (alembic logs its own there)."""
        records = []
        handler = logging.Handler(logging.INFO)
        handler.emit = records.append
        logger = logging.getLogger(LOG)
        level, disabled = logger.level, logger.disabled
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.disabled = False
        try:
            migration = _load_migration()
            with self.engine.begin() as conn:
                migration.op = Operations(MigrationContext.configure(conn))
                migration.upgrade()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.disabled = disabled
        return [r for r in records if r.getMessage().startswith("vrcopy01")]

    def upgrade_summary(self) -> dict:
        """What the migration says it read and did."""
        (summary,) = [r.getMessage() for r in self.upgrade() if r.levelno == logging.INFO]
        counts = json.loads(summary.split(":", 1)[1])
        assert counts["restored"] + sum(counts["left_as_they_are"].values()) == counts["copies_read"]
        return counts


def _left(counts) -> dict:
    """The reasons copies were left as they are, without the empty ones."""
    return {k: v for k, v in counts["left_as_they_are"].items() if v}


CHANGED = UPGRADED - timedelta(days=30)   # when the source last changed
EARLIER = UPGRADED - timedelta(days=60)
LATER = UPGRADED + timedelta(days=3)


def _sales(data=ROWS, at=CHANGED):
    db = Installation()
    sales = db.query("conversation", "sales")
    source = db.run(sales, "sales", data, at)
    return db, sales, source


def test_a_copy_takes_back_the_time_its_source_last_changed_and_the_log_says_so():
    """A copy of a query that was run more than once, a copy refreshed since
    the upgrade and a copy that is gone: the first takes its source's time,
    and the log counts each one under what was done with it. Running the
    migration again changes nothing, and counts the restored copy as one."""
    db = Installation()
    sales = db.query("conversation", "sales")
    db.run(sales, "sales", OTHER_ROWS, EARLIER)
    db.run(sales, "sales", ROWS, CHANGED)
    costs = db.query("conversation", "costs")
    db.run(costs, "costs", ROWS, EARLIER)
    stock = db.query("conversation", "stock")
    db.run(stock, "stock", ROWS, EARLIER)
    copies = db.publish("conversation", [sales, costs, stock])
    db.set("steps", copies[costs], data=OTHER_ROWS, updated_at=LATER)
    db.remove("steps", copies[stock])

    first = db.upgrade_summary()

    assert db.time_of(copies[sales]) == CHANGED
    assert db.time_of(copies[costs]) == LATER
    assert (first["copies_read"], first["restored"]) == (3, 1)
    assert _left(first) == {"copy_refreshed": 1, "rows_gone": 1}

    again = db.upgrade_summary()

    assert db.time_of(copies[sales]) == CHANGED
    assert (again["copies_read"], again["restored"]) == (3, 0)
    assert _left(again) == {"already_restored": 1, "copy_refreshed": 1, "rows_gone": 1}


def test_a_copy_is_left_once_its_source_moved_on_and_only_a_twin_that_was_never_copied_matches():
    """Another query of the conversation showed a step of the same title and
    data; the dashboard did not show it, so the release has no copy of it.
    After the upgrade the copied query was run again under another title:
    the step it was copied from is kept, no longer shown. The twin is the
    only step still shown under the copy's title -- and not its source."""
    for twin_query_title in ("sales", "sales by month"):
        db, sales, source = _sales()
        twin = db.query("conversation", twin_query_title)
        db.run(twin, "sales", ROWS, EARLIER)
        (copy,) = db.publish("conversation", [sales]).values()
        db.run(sales, "sales by quarter", ROWS, LATER)
        assert db.get("queries", sales)["default_step_id"] != source

        counts = db.upgrade_summary()

        assert db.time_of(copy) == UPGRADED, twin_query_title
        assert counts["restored"] == 0


def test_a_twin_is_a_twin_whatever_is_known_of_its_time():
    """The twin's time was lost (the column allows it): it is still a step
    the copy may have come from."""
    db, sales, _ = _sales()
    twin = db.query("conversation", "sales by month")
    lost = db.run(twin, "sales", ROWS, EARLIER)
    db.set("steps", lost, updated_at=None)
    (copy,) = db.publish("conversation", [sales]).values()

    counts = db.upgrade_summary()

    assert db.time_of(copy) == UPGRADED
    assert _left(counts) == {"ambiguous_source": 1}


def test_a_step_of_the_title_anywhere_else_in_the_conversation_is_a_twin():
    """Not only a step of another query: one left on a widget that has no
    query, and one a query shows though it is kept elsewhere (the first
    forks shared the step they were made from)."""
    def on_a_widget_alone(db):
        db.add("steps", title="sales", data=ROWS, widget_id=db.add("widgets", report_id="conversation"),
               created_at=EARLIER, updated_at=EARLIER)

    def shown_by_a_fork(db):
        fork = db.query("conversation", "sales by month")
        db.set("queries", fork, default_step_id=db.add(
            "steps", title="sales", data=ROWS, created_at=EARLIER, updated_at=EARLIER))

    for another_step in (on_a_widget_alone, shown_by_a_fork):
        db, sales, _ = _sales()
        another_step(db)
        (copy,) = db.publish("conversation", [sales]).values()

        counts = db.upgrade_summary()

        assert db.time_of(copy) == UPGRADED, another_step.__name__
        assert _left(counts) == {"ambiguous_source": 1}


def test_two_copies_of_one_title_are_left_though_only_one_source_is_still_there():
    """Both twins were copied; one was then removed by hand (the app marks
    a deletion, it removes nothing). Its copy must not take the time of the
    twin that is left."""
    db, sales, source = _sales()
    twin = db.query("conversation", "sales")
    db.run(twin, "sales", ROWS, EARLIER)
    copies = db.publish("conversation", [sales, twin])
    db.remove("steps", source)
    db.remove("queries", sales)

    counts = db.upgrade_summary()

    assert [db.time_of(c) for c in copies.values()] == [UPGRADED, UPGRADED]
    assert _left(counts) == {"ambiguous_source": 2}


def test_a_source_whose_time_is_not_known_gives_none():
    db, sales, source = _sales()
    db.set("steps", source, updated_at=None)
    (copy,) = db.publish("conversation", [sales]).values()

    counts = db.upgrade_summary()

    assert db.time_of(copy) == UPGRADED
    assert _left(counts) == {"source_time_unknown": 1}


def test_a_source_that_holds_no_data_is_unchanged_while_it_still_holds_none():
    db, sales, _ = _sales(data=None)
    (copy,) = db.publish("conversation", [sales]).values()

    assert db.upgrade_summary()["restored"] == 1
    assert db.time_of(copy) == CHANGED


def test_runs_of_one_query_kept_by_its_widget_are_one_query():
    """The app's first versions tied a step to its widget, not its query:
    the query's older runs are still its own, not twins."""
    db = Installation()
    sales = db.query("conversation", "sales")
    db.run(sales, "sales", OTHER_ROWS, EARLIER, by_widget_only=True)
    db.run(sales, "sales", ROWS, CHANGED, by_widget_only=True)
    (copy,) = db.publish("conversation", [sales]).values()

    assert db.upgrade_summary()["restored"] == 1
    assert db.time_of(copy) == CHANGED


@pytest.mark.parametrize("missing", ["dashfc08_undo", "artifact_versions"])
def test_a_database_without_the_tables_it_reads_is_left_alone_with_a_warning(missing):
    db = Installation(without=(missing,))

    records = db.upgrade()

    assert [r.levelno for r in records] == [logging.WARNING]
    assert missing in records[0].getMessage()


# ── seeded histories ─────────────────────────────────────────────────────
#
# Small conversations where everything collides: a handful of queries share
# two titles and two results, some shown on the dashboard and some not, each
# run once or several times; after the upgrade they are run again (under the
# same title or another), edited, deleted, lose their time, and the copies
# are refreshed or removed. The generator copies the steps itself, as
# dashfc08 does, so it knows what the migration cannot read: the step each
# copy came from, and the time it had then.
#
# What the app never does is left out on purpose, because no column would
# survive it: a query or a step of a conversation is never removed (they are
# marked deleted), and a query's title and its steps' titles are never both
# rewritten in place (the app rewrites neither: a new title is a new run).

TITLES = ("sales", "costs")
RESULTS = (ROWS, ROWS, ROWS, OTHER_ROWS, OTHER_ROWS, None)


class _History:
    def __init__(self, seed):
        self.rng = rng = random.Random(seed)
        self.db = Installation(new_id=lambda: str(uuid.UUID(int=rng.getrandbits(128))))
        self.minutes = rng.sample(range(1, 200_000), 400)
        self.sure: set[str] = set()   # copies whose source nothing puts in doubt
        self.copies: list[str] = []
        for _ in range(rng.randint(1, 2)):
            self._conversation()

    def _before(self):
        return UPGRADED - timedelta(minutes=self.minutes.pop())

    def _after(self):
        return UPGRADED + timedelta(minutes=self.minutes.pop())

    def _conversation(self):
        rng, db = self.rng, self.db
        report = db.new_id()
        queries, steps, owner = [], [], {}
        for _ in range(rng.randint(1, 4)):
            title = rng.choice(TITLES)
            query = db.query(report, rng.choice([title, title, rng.choice(TITLES)]))
            by_widget_only = rng.random() < 0.15
            runs = [db.run(query, title if rng.random() < 0.8 else rng.choice(TITLES), rng.choice(RESULTS),
                           self._before(), by_widget_only=by_widget_only)
                    for _ in range(rng.randint(1, 3))]
            if rng.random() < 0.15:  # the last run failed: the query shows an earlier one
                db.set("queries", query, default_step_id=rng.choice(runs))
            if rng.random() < 0.05:
                db.set("steps", rng.choice(runs), updated_at=None)
            if rng.random() < 0.1:
                db.set("queries", query, deleted_at=self._before())
            queries.append(query)
            steps += runs
            owner.update(dict.fromkeys(runs, query))
        if rng.random() < 0.1:  # a step left on a widget that has no query
            steps.append(db.add("steps", title=rng.choice(TITLES), data=rng.choice(RESULTS),
                                widget_id=db.add("widgets", report_id=report),
                                created_at=self._before(), updated_at=self._before()))

        shown = [q for q in queries if rng.random() < (0.15 if db.get("queries", q)["deleted_at"] else 0.6)]
        copies = db.publish(report, shown or queries[:1])
        self.copies += copies.values()
        # One query, and every step the conversation holds is a run of it.
        plain = len(queries) == 1 and set(steps) == set(owner)

        # How the release still names its conversation.
        released, link = db.released, rng.random()
        elsewhere = rng.choice([None, db.new_id()])
        if link < 0.6:
            db.set("artifacts", released["artifact"], report_id=rng.choice([report, elsewhere]),
                   legacy_report_id=rng.choice([report, None]))
        elif link < 0.8:  # the version is gone: the conversation dashfc08 recorded
            db.set("artifact_releases", released["release"], source_version_id=None)
            db.set("artifacts", released["artifact"], report_id=rng.choice([report, elsewhere]))
        else:             # ... or, where it recorded none, the dashboard's own
            db.set("artifact_releases", released["release"], source_version_id=None)
            db.set("artifacts", released["artifact"], legacy_report_id=None)

        retitled_queries, retitled_steps_of = set(), set()
        changes = rng.randint(0, 4)
        for _ in range(changes):
            query, step = rng.choice(queries), rng.choice(steps)
            what = rng.choice(("run", "run", "run", "edit", "edit", "lose time", "delete", "retitle query",
                               "retitle step", "refresh copy", "remove copy"))
            if what == "run":
                shows = db.get("steps", db.get("queries", query)["default_step_id"])
                steps.append(db.run(
                    query, shows["title"] if rng.random() < 0.5 else rng.choice(TITLES),
                    shows["data"] if rng.random() < 0.6 else rng.choice(RESULTS), self._after(),
                    shown=rng.random() < 0.85))
                owner[steps[-1]] = query
            elif what == "edit":
                db.set("steps", step, data=rng.choice(RESULTS), updated_at=self._after())
            elif what == "lose time":
                db.set("steps", step, updated_at=None)
            elif what == "delete":
                table, row = rng.choice([("queries", query), ("steps", step)])
                db.set(table, row, deleted_at=self._after())
            elif what == "retitle query" and query not in retitled_steps_of:
                retitled_queries.add(query)
                db.set("queries", query, title=rng.choice(TITLES))
            elif what == "retitle step" and owner.get(step) not in retitled_queries:
                retitled_steps_of.add(owner.get(step))
                db.set("steps", step, title=rng.choice(TITLES))
            elif what == "refresh copy" and (kept := [c for c in copies.values() if db.get("steps", c)]):
                db.set("steps", rng.choice(kept), data=rng.choice(RESULTS), updated_at=self._after())
            elif what == "remove copy":
                db.remove("steps", rng.choice(list(copies.values())))
        if plain and not changes:
            self.sure |= {c for c in copies.values() if db.source_time[c] is not None}

    def faults(self) -> list:
        """Run the migration twice; every copy whose time broke the promise."""
        db = self.db
        was = {c: db.time_of(c) for c in self.copies}
        counts = db.upgrade_summary()
        now = {c: db.time_of(c) for c in self.copies}
        out = []
        for c in self.copies:
            true = db.source_time[c]
            if now[c] != was[c] and not (true is not None and now[c] == true and now[c] < was[c]):
                out.append(f"copy {c[:8]}: {was[c]} -> {now[c]}, its source's time was {true}")
            if c in self.sure and now[c] != true:
                out.append(f"copy {c[:8]}: left at {now[c]}, its only possible source's time was {true}")
        self.restored = sum(now[c] != was[c] for c in self.copies)
        if (counts["copies_read"], counts["restored"]) != (len(self.copies), self.restored):
            out.append(f"logged {counts}, read {len(self.copies)} and restored {self.restored}")
        again = db.upgrade_summary()
        if again["restored"] or {c: db.time_of(c) for c in self.copies} != now:
            out.append(f"a second run changed copies: {again}")
        return out


SEEDS_PER_BLOCK = 100


@pytest.mark.parametrize("block", range(5))
def test_no_history_gives_a_copy_any_time_but_its_own_sources(block):
    """Over 500 seeded histories: a copy's time is left as it is, or moved
    earlier to the time its true source had when it was copied -- never to
    another step's; a copy with one possible source is restored; the log
    counts what was done; and a second run does nothing."""
    broken, restored = {}, 0
    for seed in range(block * SEEDS_PER_BLOCK, (block + 1) * SEEDS_PER_BLOCK):
        history = _History(seed)
        if faults := history.faults():
            broken[seed] = faults
        restored += history.restored
    assert not broken, (f"{len(broken)} of {SEEDS_PER_BLOCK} histories {sorted(broken)}; "
                        f"seed {min(broken)}: {broken[min(broken)]}")
    assert restored > 0  # the histories do reach the path that restores
