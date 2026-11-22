"""Column ranking, per-table column cap and fold summary (BOW-215).

Contract:
* columns with usage rank ahead of unused ones; a failure counts less than a
  success (but more than nothing); thumbs down lowers, thumbs up raises;
* instruction-referenced and structural (key / timestamp / id) columns rank
  ahead of plain ones, and on cold start structure + catalog order decide;
* a wide table renders at most its budget of columns, and the rest are named
  in a <more_columns> summary pointing at describe_tables column search;
* the snapshot reports "N of M columns" with an honest reason.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.ai.context.column_ranking import (
    column_matcher,
    search_columns,
    curate_columns,
    fold_groups,
    promote_mentioned_columns,
    rank_table_columns,
)
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.services.column_usage_service import FAILURE_USAGE_WEIGHT

NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)


@dataclass
class Stats:
    """Shape of a ColumnStats row (what the builder hands the ranker)."""
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    weighted_usage_count: float = 0.0
    weighted_pos_feedback: float = 0.0
    weighted_neg_feedback: float = 0.0
    pos_feedback_count: int = 0
    neg_feedback_count: int = 0
    last_used_at: Optional[datetime] = None


def used(successes=0, failures=0, pos=0.0, neg=0.0, days_ago=1.0, weight=1.0):
    """Stats as the column usage service accumulates them."""
    return Stats(
        usage_count=successes + failures, success_count=successes, failure_count=failures,
        weighted_usage_count=successes * weight + failures * weight * FAILURE_USAGE_WEIGHT,
        weighted_pos_feedback=pos, weighted_neg_feedback=neg,
        last_used_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None),
    )


def wide_table(n=500, name="metrics_wide", prefix_style="snake"):
    cols = []
    for i in range(n):
        if prefix_style == "dotted":
            fam = ["svc.request", "process.cpu", "host.os"][i % 3]
            cols.append(TableColumn(name=f"{fam}.attr_{i:04d}", dtype="number"))
        elif prefix_style == "flat":
            cols.append(TableColumn(name=f"c{i:04d}", dtype="number"))
        else:
            fam = ["cpu", "mem", "net", "disk"][i % 4]
            cols.append(TableColumn(name=f"{fam}_metric_{i:04d}", dtype="number"))
    return Table(name=name, columns=cols, pks=[], fks=[])


def order(table):
    return [c.name for c in table.columns]


# ------------------------------------------------------------------ ranking
def test_used_columns_rank_ahead_of_unused_ones():
    t = wide_table()
    late = [order(t)[-1], order(t)[-50], order(t)[-300]]
    rank_table_columns(t, {c.lower(): used(successes=k + 1) for k, c in enumerate(late)}, now=NOW)
    assert order(t)[:3] == list(reversed(late))  # more uses first
    assert all(c.selection_signal == "usage" for c in t.columns[:3])
    assert t.columns[0].usage_count == 3


@pytest.mark.parametrize("n", [1, 3, 10])
def test_failure_counts_less_than_success_but_more_than_nothing(n):
    t = wide_table(50)
    s_col, f_col, unused = "cpu_metric_0040", "mem_metric_0041", "net_metric_0042"
    rank_table_columns(t, {s_col: used(successes=n), f_col: used(failures=n)}, now=NOW)
    pos = {name: i for i, name in enumerate(order(t))}
    assert pos[s_col] < pos[f_col] < pos[unused]


@pytest.mark.parametrize("weight", [0.8, 1.0, 1.5])
def test_feedback_moves_a_column_both_ways(weight):
    def score_with(**fb):
        t = wide_table(20)
        rank_table_columns(t, {"cpu_metric_0012": used(successes=2, weight=weight, **fb)}, now=NOW)
        return next(c.score for c in t.columns if c.name == "cpu_metric_0012")

    base = score_with()
    assert score_with(pos=weight) > base > score_with(neg=weight)


def test_thumbs_down_can_drop_a_column_below_a_merely_used_one():
    t = wide_table(30)
    rank_table_columns(t, {
        "cpu_metric_0020": used(successes=2, neg=3.0),
        "mem_metric_0021": used(successes=1),
    }, now=NOW)
    names = order(t)
    assert names.index("mem_metric_0021") < names.index("cpu_metric_0020")


def test_recent_usage_outranks_equally_frequent_stale_usage():
    t = wide_table(30)
    rank_table_columns(t, {"cpu_metric_0020": used(successes=3, days_ago=60),
                           "mem_metric_0021": used(successes=3, days_ago=1)}, now=NOW)
    names = order(t)
    assert names.index("mem_metric_0021") < names.index("cpu_metric_0020")


def test_instruction_references_and_structure_lead_on_cold_start():
    cols = [TableColumn(name=f"metric_{i:04d}", dtype="number") for i in range(200)]
    cols += [TableColumn(name="row_id", dtype="bigint"), TableColumn(name="event_time", dtype="timestamp"),
             TableColumn(name="account_id", dtype="bigint"), TableColumn(name="metric_rule", dtype="number")]
    t = Table(name="t", columns=cols, pks=[TableColumn(name="row_id", dtype="bigint")],
              fks=[ForeignKey(column=TableColumn(name="account_id", dtype="bigint"), references_name="accounts",
                              references_column=TableColumn(name="account_id", dtype="bigint"))])
    rank_table_columns(t, None, {"metric_rule"}, now=NOW)
    assert order(t)[:4] == ["metric_rule", "row_id", "account_id", "event_time"]
    # Then catalog order for everything without a signal.
    assert order(t)[4:7] == ["metric_0000", "metric_0001", "metric_0002"]


def test_cold_start_samples_every_column_family_instead_of_the_first_n():
    # A search index: hundreds of date-typed attributes, families that sort
    # late alphabetically. Neither may crowd the others out of the budget.
    families = ["geo.client", "host.os", "net.tcp", "process.cpu", "svc.request", "user.profile"]
    cols = [TableColumn(name="@timestamp", dtype="datetime")]
    cols += [TableColumn(name=f"{fam}.attr_{i:04d}", dtype="datetime" if i % 3 == 0 else "string")
             for fam in families for i in range(300)]
    t = Table(name="idx", columns=cols, pks=[TableColumn(name="_id", dtype="string")], fks=[])
    t.column_budget = 60
    rank_table_columns(t, None, now=NOW)
    shown = [c.name for c in curate_columns(t).shown]
    assert shown[0] == "@timestamp"
    per_family = {fam: sum(1 for n in shown if n.startswith(fam + ".")) for fam in families}
    assert min(per_family.values()) >= 9, per_family
    snap = _ctx([t]).get_usage_snapshot(top_k_per_ds=10).data_sources[0].tables_used[0]
    assert snap.column_selection_reason == "structural_only"
    assert snap.column_signal_counts.get("sample") == 59


def test_keyword_multi_field_follows_its_used_parent():
    cols = [TableColumn(name=f"f{i}", dtype="string") for i in range(100)]
    cols += [TableColumn(name="svc.tier", dtype="string (full-text; aggregate/sort on svc.tier.keyword)"),
             TableColumn(name="svc.tier.keyword", dtype="string")]
    t = Table(name="idx", columns=cols, pks=[], fks=[])
    rank_table_columns(t, {"svc.tier": used(successes=2)}, now=NOW)
    assert order(t)[:2] == ["svc.tier", "svc.tier.keyword"]


# ------------------------------------------------------------- cap and fold
@pytest.mark.parametrize("n,budget", [(5000, 60), (1600, 50), (800, 100), (61, 60)])
def test_cap_keeps_budget_columns_and_folds_the_rest(n, budget):
    t = wide_table(n)
    t.column_budget = budget
    cur = curate_columns(t)
    assert len(cur.shown) == budget
    assert len(cur.folded) == n - budget
    assert cur.total == n
    assert sum(c for _, c in cur.groups) + cur.other == n - budget


@pytest.mark.parametrize("budget", [0, -1])
def test_budget_zero_or_negative_disables_the_cap(budget):
    t = wide_table(300)
    t.column_budget = budget
    cur = curate_columns(t)
    assert not cur.capped and len(cur.shown) == 300 and cur.reason == "all_columns"


def test_narrow_tables_are_untouched():
    t = Table(name="small", columns=[TableColumn(name=c, dtype="text") for c in ("a", "b", "c")], pks=[], fks=[])
    cur = curate_columns(t)
    assert [c.name for c in cur.shown] == ["a", "b", "c"] and cur.reason == "all_columns"


def test_fold_groups_by_dotted_prefix_and_snake_prefix():
    dotted, other = fold_groups([f"svc.request.a{i}" for i in range(400)] + [f"process.cpu.b{i}" for i in range(30)] + ["x"])
    labels = dict(dotted)
    assert labels["svc.request.*"] == 400
    assert labels.get("process.*") == 30 and other == 1
    snake, other = fold_groups([f"cpu_{i}" for i in range(50)] + [f"mem_{i}" for i in range(20)] + ["a_1", "b_2"])
    assert dict(snake) == {"cpu_*": 50, "mem_*": 20} and other == 2


def test_fold_without_prefixes_says_how_many_more():
    t = wide_table(200, prefix_style="flat")
    t.column_budget = 60
    ctx = _ctx([t])
    xml = ctx.render_combined(top_k_per_ds=10, index_limit=10)
    assert "+140 more columns" in xml


def _ctx(tables):
    return TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds1", name="Wide", type="postgresql"), tables=tables)])


@pytest.mark.parametrize("style", ["snake", "dotted"])
def test_planner_render_is_bounded_and_carries_the_fold_summary(style):
    t = wide_table(5000, prefix_style=style)
    t.column_budget = 60
    used_cols = {order(t)[4321].lower(): used(successes=4)}
    rank_table_columns(t, used_cols, now=NOW)
    xml = _ctx([t]).render_combined(top_k_per_ds=10, index_limit=100)
    table_xml = re.search(r"<table\b.*?</table>", xml, re.S).group(0)
    assert table_xml.count("<column ") == 60
    assert 'cols="5000"' in table_xml and 'cols_shown="60"' in table_xml
    assert "<more_columns" in table_xml and 'count="4940"' in table_xml
    assert "describe_tables" in table_xml
    # The used column made the cut even though it sits deep in the catalog.
    assert f'name="{order(t)[0]}"' in table_xml and order(t)[0] == list(used_cols)[0]


def test_full_render_path_applies_the_same_cap():
    t = wide_table(1000)
    t.column_budget = 40
    xml = _ctx([t]).render()
    assert xml.count("<column ") == 40 and "<more_columns" in xml


def test_snapshot_reports_n_of_m_and_an_honest_reason():
    cold = wide_table(500, name="cold")
    warm = wide_table(500, name="warm")
    rank_table_columns(warm, {"cpu_metric_0400": used(successes=1)}, now=NOW)
    small = Table(name="small", columns=[TableColumn(name="a", dtype="int")], pks=[], fks=[])
    snap = _ctx([warm, cold, small]).get_usage_snapshot(top_k_per_ds=10)
    by = {t.name: t for t in snap.data_sources[0].tables_used}
    assert (by["warm"].columns_shown, by["warm"].columns_total) == (60, 500)
    assert by["warm"].column_selection_reason == "usage_ranked"
    assert by["warm"].column_signal_counts.get("usage") == 1
    assert by["warm"].shown_columns[0].name == "cpu_metric_0400" and by["warm"].shown_columns[0].usage_count == 1
    assert by["warm"].folded_groups and sum(g.count for g in by["warm"].folded_groups) + by["warm"].folded_other == 440
    assert by["cold"].column_selection_reason == "structural_only"
    assert by["small"].column_selection_reason == "all_columns" and by["small"].columns_shown == 1


# ------------------------------------------------------------ column search
@pytest.mark.parametrize("term,expected", [
    ("retry", {"svc.request.retry_budget", "retry_count"}),
    ("svc.request.*", {"svc.request.retry_budget", "svc.request.tier"}),
    ("^retry_", {"retry_count"}),
    ("RETRY_COUNT", {"retry_count"}),
    ("tenant", {"svc.request.tier"}),  # description match
])
def test_column_matcher_matches_names_and_descriptions(term, expected):
    cols = [TableColumn(name="svc.request.retry_budget", dtype="long"),
            TableColumn(name="svc.request.tier", dtype="string", description="Tenant tier of the caller"),
            TableColumn(name="retry_count", dtype="int"), TableColumn(name="host.name", dtype="string")]
    m = column_matcher([term])
    assert {c.name for c in cols if m(c)} == expected


@pytest.mark.parametrize("term,expected", [
    ("retrans*", {"net_retransmit_rate"}),
    ("*_pct", {"cpu_user_pct", "svc_error_budget_pct"}),
    ("svc_*", {"svc_error_budget_pct"}),
    ("err?r*", {"svc_error_budget_pct"}),
])
def test_wildcards_match_at_name_segment_boundaries(term, expected):
    cols = [TableColumn(name=n, dtype="real") for n in
            ("net_retransmit_rate", "cpu_user_pct", "svc_error_budget_pct", "transmit_total", "pretrans_x")]
    m = column_matcher([term])
    assert {c.name for c in cols if m(c)} == expected


@pytest.mark.parametrize("n_broad", [50, 300, 1500])
def test_narrow_matches_lead_broad_companion_patterns(n_broad):
    cols = [TableColumn(name=f"net_rx_bytes_{i:04d}", dtype="real") for i in range(n_broad)]
    cols.insert(int(n_broad * 0.9), TableColumn(name="net_retransmit_rate", dtype="real"))
    hits = search_columns(cols, ["net_*", "retrans*"])
    assert len(hits) == n_broad + 1
    assert hits[0].name == "net_retransmit_rate"


def test_columns_named_in_the_task_reach_the_coder_even_when_folded():
    t = wide_table(3000)
    t.column_budget = 50
    rank_table_columns(t, None, now=NOW)
    deep = order(t)[2500]
    assert deep not in [c.name for c in curate_columns(t).shown]
    promote_mentioned_columns(_ctx([t]), f"Average {deep} per host over the last week")
    shown = [c.name for c in curate_columns(t).shown]
    assert shown[0] == deep
