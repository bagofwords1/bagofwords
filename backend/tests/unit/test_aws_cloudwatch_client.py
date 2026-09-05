"""Unit tests for AwsCloudWatchClient.

Covers the connector's contract without a live AWS account:
- boto3 session construction per auth variant (static keys, STS assume-role,
  default chain)
- get_schemas() shaping: log groups and metrics as two prefixed table
  namespaces, JSON field sampling, the sampling cap, dimension unioning
- the Logs Insights async lifecycle (start -> poll -> complete), and that a
  query that overruns is cancelled rather than abandoned
- execute_query() envelope dispatch and DataFrame shaping
- the hard CloudWatch limits the client has to enforce client-side
- test_connection() success / per-IAM-action failure
- registry wiring (resolve_client_class)

The boto3 boundary is faked: `boto3.Session` is monkeypatched to hand back
scripted `logs` / `cloudwatch` clients, so no AWS call leaves the process.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.data_sources.clients.aws_cloudwatch_client import (
    LOG_PREFIX,
    MAX_LOG_GROUPS_PER_QUERY,
    METRIC_PREFIX,
    AwsCloudWatchClient,
)


# ---------- fake boto3 plumbing ---------- #


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        if callable(self._pages):
            return self._pages(**kwargs)
        return list(self._pages)


class _FakeLogs:
    """Scripted CloudWatch Logs client.

    `query_script` maps a queryId to the list of statuses returned by
    successive get_query_results calls, so a test can drive the poll loop
    through Scheduled/Running before it settles.
    """

    def __init__(self, log_groups=None, rows=None, statuses=None, start_error=None):
        self.log_groups = log_groups or []
        self.rows = rows if rows is not None else []
        self.statuses = list(statuses or ["Complete"])
        self.start_error = start_error
        self.started = []
        self.stopped = []
        self._polls = 0

    def get_paginator(self, name):
        assert name == "describe_log_groups"

        def _pages(**kwargs):
            prefix = kwargs.get("logGroupNamePrefix")
            groups = [
                g for g in self.log_groups
                if not prefix or g["logGroupName"].startswith(prefix)
            ]
            return [{"logGroups": groups}]

        return _Paginator(_pages)

    def describe_log_groups(self, **kwargs):
        return {"logGroups": self.log_groups}

    def start_query(self, **kwargs):
        if self.start_error:
            raise self.start_error
        self.started.append(kwargs)
        return {"queryId": f"q{len(self.started)}"}

    def get_query_results(self, queryId):
        status = self.statuses[min(self._polls, len(self.statuses) - 1)]
        self._polls += 1
        payload = {"status": status, "results": []}
        if status == "Complete":
            payload["results"] = [
                [{"field": k, "value": v} for k, v in row.items()] for row in self.rows
            ]
        return payload

    def stop_query(self, queryId):
        self.stopped.append(queryId)
        return {"success": True}


class _FakeCloudWatch:
    def __init__(self, metrics=None, series=None, list_error=None):
        self.metrics = metrics or []
        self.series = series or []
        self.list_error = list_error
        self.data_calls = []

    def get_paginator(self, name):
        assert name == "list_metrics"

        def _pages(**kwargs):
            ns = kwargs.get("Namespace")
            mn = kwargs.get("MetricName")
            sel = [m for m in self.metrics
                   if (not ns or m["Namespace"] == ns) and (not mn or m["MetricName"] == mn)]
            return [{"Metrics": sel}]

        return _Paginator(_pages)

    def list_metrics(self, **kwargs):
        if self.list_error:
            raise self.list_error
        return {"Metrics": self.metrics}

    def get_metric_data(self, **kwargs):
        self.data_calls.append(kwargs)
        return {"MetricDataResults": self.series}


class _FakeSession:
    def __init__(self, logs, cw, record=None, **kwargs):
        self._logs = logs
        self._cw = cw
        self.kwargs = kwargs
        if record is not None:
            record.append(kwargs)

    def client(self, name, **kwargs):
        return {"logs": self._logs, "cloudwatch": self._cw}[name]


@pytest.fixture
def patch_boto(monkeypatch):
    """Install fake logs/cloudwatch clients and record how the session was built."""

    def _install(logs=None, cw=None, sts_creds=None):
        logs = logs if logs is not None else _FakeLogs()
        cw = cw if cw is not None else _FakeCloudWatch()
        sessions: list = []
        sts_calls: list = []

        class _FakeSts:
            def assume_role(self, **kwargs):
                sts_calls.append(kwargs)
                return {"Credentials": sts_creds or {
                    "AccessKeyId": "ASIA-TEMP",
                    "SecretAccessKey": "temp-secret",
                    "SessionToken": "temp-token",
                }}

        import boto3

        monkeypatch.setattr(
            boto3, "Session",
            lambda **kw: _FakeSession(logs, cw, record=sessions, **kw),
        )
        monkeypatch.setattr(boto3, "client", lambda name, **kw: _FakeSts())
        return {"logs": logs, "cw": cw, "sessions": sessions, "sts": sts_calls}

    return _install


def _client(**overrides):
    params = dict(region="eu-west-1", access_key="AKIA-TEST", secret_key="secret")
    params.update(overrides)
    return AwsCloudWatchClient(**params)


# ---------- auth / session construction ---------- #


def test_static_keys_are_passed_to_the_session(patch_boto):
    ctx = patch_boto()
    client = _client(session_token="tok")
    client.test_connection()

    built = ctx["sessions"][0]
    assert built["aws_access_key_id"] == "AKIA-TEST"
    assert built["aws_secret_access_key"] == "secret"
    assert built["aws_session_token"] == "tok"
    assert built["region_name"] == "eu-west-1"
    assert ctx["sts"] == []  # no assume-role on the static-key path


def test_assume_role_exchanges_for_temporary_credentials(patch_boto):
    ctx = patch_boto()
    client = _client(role_arn="arn:aws:iam::1234:role/observability")
    client.test_connection()

    assert ctx["sts"][0]["RoleArn"] == "arn:aws:iam::1234:role/observability"
    built = ctx["sessions"][0]
    # The session must use the STS-issued credentials, not the configured ones.
    assert built["aws_access_key_id"] == "ASIA-TEMP"
    assert built["aws_session_token"] == "temp-token"


def test_default_chain_passes_no_credentials(patch_boto):
    ctx = patch_boto()
    AwsCloudWatchClient(region="us-east-1").test_connection()

    built = ctx["sessions"][0]
    assert built == {"region_name": "us-east-1"}


# ---------- discovery ---------- #


LOG_GROUPS = [
    {"logGroupName": "/aws/lambda/checkout", "storedBytes": 900, "retentionInDays": 30},
    {"logGroupName": "/aws/lambda/billing", "storedBytes": 500},
    {"logGroupName": "/ecs/worker", "storedBytes": 100},
]


def test_log_groups_become_prefixed_tables_with_builtin_columns(patch_boto):
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS))
    tables = _client(max_sampled_log_groups=0).get_schemas()

    assert {t.name for t in tables} == {
        f"{LOG_PREFIX}/aws/lambda/checkout",
        f"{LOG_PREFIX}/aws/lambda/billing",
        f"{LOG_PREFIX}/ecs/worker",
    }
    for table in tables:
        names = [c.name for c in table.columns]
        assert {"@timestamp", "@message", "@logStream"} <= set(names)
        assert table.metadata_json["source"] == "cloudwatch_logs"
        # The bare AWS name must survive in metadata so queries can use it.
        assert table.metadata_json["log_group"] == table.name[len(LOG_PREFIX):]


def test_prefix_scopes_discovery_server_side(patch_boto):
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS))
    tables = _client(log_group_prefix="/aws/lambda/", max_sampled_log_groups=0).get_schemas()

    assert {t.name for t in tables} == {
        f"{LOG_PREFIX}/aws/lambda/checkout",
        f"{LOG_PREFIX}/aws/lambda/billing",
    }


def test_json_message_bodies_contribute_sampled_columns(patch_boto):
    rows = [{"@timestamp": "2026-01-01 00:00:00.000",
             "@message": '{"level":"error","latency_ms":42,"ok":false,"ctx":{"a":1}}'}]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS[:1], rows=rows))

    table = _client().get_schemas()[0]
    by_name = {c.name: c.dtype for c in table.columns}
    assert by_name["level"] == "string"
    assert by_name["latency_ms"] == "number"
    assert by_name["ok"] == "boolean"
    assert by_name["ctx"] == "json"
    assert table.metadata_json["field_sampled"] is True


def test_non_json_messages_leave_the_table_thin(patch_boto):
    rows = [{"@timestamp": "2026-01-01 00:00:00.000", "@message": "plain text line"}]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS[:1], rows=rows))

    table = _client().get_schemas()[0]
    assert [c.name for c in table.columns] == [
        "@timestamp", "@message", "@logStream", "@ingestionTime"
    ]


def test_sampling_cap_ranks_by_stored_bytes(patch_boto):
    rows = [{"@timestamp": "t", "@message": '{"field_a":1}'}]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=rows))

    tables = {t.name: t for t in _client(max_sampled_log_groups=1).get_schemas()}
    # Only the largest group is sampled; the rest stay thin but still listed.
    assert tables[f"{LOG_PREFIX}/aws/lambda/checkout"].metadata_json["field_sampled"] is True
    assert tables[f"{LOG_PREFIX}/aws/lambda/billing"].metadata_json["field_sampled"] is False
    assert tables[f"{LOG_PREFIX}/ecs/worker"].metadata_json["field_sampled"] is False


def test_field_sampling_failure_degrades_to_thin_instead_of_failing_discovery(patch_boto):
    logs = _FakeLogs(log_groups=LOG_GROUPS[:1])
    logs.start_error = RuntimeError("AccessDeniedException: StartQuery")
    patch_boto(logs=logs)

    tables = _client().get_schemas()
    assert len(tables) == 1
    assert [c.name for c in tables[0].columns] == [
        "@timestamp", "@message", "@logStream", "@ingestionTime"
    ]


METRICS = [
    # The same metric arrives once per dimension-set variant — this is what the
    # real ListMetrics does, and the reason tables union their dimensions.
    {"Namespace": "AWS/RDS", "MetricName": "VolumeReadIOPs",
     "Dimensions": [{"Name": "DBClusterIdentifier", "Value": "c1"}]},
    {"Namespace": "AWS/RDS", "MetricName": "VolumeReadIOPs",
     "Dimensions": [{"Name": "DbClusterIdentifier", "Value": "c1"},
                    {"Name": "EngineName", "Value": "aurora"}]},
    {"Namespace": "AWS/RDS", "MetricName": "VolumeReadIOPs", "Dimensions": []},
    {"Namespace": "AWS/RDS", "MetricName": "CPUUtilization",
     "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "i1"}]},
]


def test_metric_variants_collapse_into_one_table_with_unioned_dimensions(patch_boto):
    patch_boto(cw=_FakeCloudWatch(metrics=METRICS))
    tables = _client(metric_namespaces="AWS/RDS").get_schemas()

    by_name = {t.name: t for t in tables}
    assert set(by_name) == {
        f"{METRIC_PREFIX}AWS/RDS/VolumeReadIOPs",
        f"{METRIC_PREFIX}AWS/RDS/CPUUtilization",
    }
    read = by_name[f"{METRIC_PREFIX}AWS/RDS/VolumeReadIOPs"]
    names = [c.name for c in read.columns]
    assert {"DBClusterIdentifier", "DbClusterIdentifier", "EngineName"} <= set(names)
    assert names[-2:] == ["timestamp", "value"]


def test_metrics_are_skipped_when_no_namespace_is_configured(patch_boto):
    patch_boto(cw=_FakeCloudWatch(metrics=METRICS))
    tables = _client().get_schemas()

    assert not [t for t in tables if t.name.startswith(METRIC_PREFIX)]


def test_get_schemas_reports_progress(patch_boto):
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS))
    seen: list = []
    _client(max_sampled_log_groups=0).get_schemas(
        progress_callback=lambda done, total, msg: seen.append((done, total))
    )
    assert seen[-1] == (3, 3)


# ---------- Logs Insights lifecycle ---------- #


def test_poll_loop_waits_for_the_query_to_settle(patch_boto):
    ctx = patch_boto(logs=_FakeLogs(
        log_groups=LOG_GROUPS,
        rows=[{"@timestamp": "2026-01-01 00:00:00.000", "@message": "hi"}],
        statuses=["Scheduled", "Running", "Complete"],
    ))
    df = _client().execute_query({"insights": "fields @message", "log_groups": ["/ecs/worker"]})

    assert len(df) == 1
    assert ctx["logs"].stopped == []  # a query that completes is never cancelled


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "Timeout"])
def test_terminal_failure_statuses_raise(patch_boto, status):
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, statuses=[status]))
    with pytest.raises(RuntimeError, match=status):
        _client().execute_query({"insights": "fields @message", "log_groups": ["/ecs/worker"]})


def test_overrunning_query_is_cancelled_not_abandoned(patch_boto, monkeypatch):
    # An abandoned Insights query keeps scanning, and keeps billing.
    monkeypatch.setattr(
        "app.data_sources.clients.aws_cloudwatch_client.QUERY_TIMEOUT_SECONDS", 0
    )
    ctx = patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, statuses=["Running"]))

    with pytest.raises(TimeoutError):
        _client().execute_query({"insights": "fields @message", "log_groups": ["/ecs/worker"]})
    assert ctx["logs"].stopped == ["q1"]


def test_stop_query_failure_does_not_mask_the_timeout(patch_boto, monkeypatch):
    # StopQuery raises if the query ended between the timeout and the cancel;
    # the caller must still see the TimeoutError.
    monkeypatch.setattr(
        "app.data_sources.clients.aws_cloudwatch_client.QUERY_TIMEOUT_SECONDS", 0
    )
    logs = _FakeLogs(log_groups=LOG_GROUPS, statuses=["Running"])
    logs.stop_query = lambda queryId: (_ for _ in ()).throw(RuntimeError("already ended"))
    patch_boto(logs=logs)

    with pytest.raises(TimeoutError):
        _client().execute_query({"insights": "fields @message", "log_groups": ["/ecs/worker"]})


# ---------- query dispatch and shaping ---------- #


def test_insights_rows_become_a_dataframe_without_the_ptr_cursor(patch_boto):
    rows = [{"@timestamp": "2026-01-01 00:00:00.000", "@message": "a", "@ptr": "opaque"}]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=rows))

    df = _client().execute_query({"insights": "fields @message", "log_groups": ["/ecs/worker"]})
    assert "@ptr" not in df.columns
    assert pd.api.types.is_datetime64_any_dtype(df["@timestamp"])


def test_ptr_is_kept_when_explicitly_selected(patch_boto):
    rows = [{"@message": "a", "@ptr": "opaque"}]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=rows))

    df = _client().execute_query(
        {"insights": "fields @message, @ptr", "log_groups": ["/ecs/worker"]}
    )
    assert "@ptr" in df.columns


def test_stats_output_is_typed_for_charting(patch_boto):
    # `stats count() by bin(...)` returns everything as strings; a time bucket
    # that stays a string plots as a category instead of a time axis.
    rows = [
        {"bucket": "2026-01-01 00:30:00.000", "events": "43"},
        {"bucket": "2026-01-01 01:00:00.000", "events": "60"},
    ]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=rows))

    df = _client().execute_query(
        {"insights": "stats count() as events by bin(30m) as bucket",
         "log_groups": ["/ecs/worker"]}
    )
    assert pd.api.types.is_numeric_dtype(df["events"])
    assert pd.api.types.is_datetime64_any_dtype(df["bucket"])


def test_free_text_columns_are_left_alone(patch_boto):
    rows = [{"msg": "timeout"}, {"msg": "refused"}]
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=rows))

    df = _client().execute_query({"insights": "fields msg", "log_groups": ["/ecs/worker"]})
    assert df["msg"].tolist() == ["timeout", "refused"]


def test_metric_result_carries_the_dimension_columns_the_catalog_advertises(patch_boto):
    """The schema lists a metric's dimensions as columns, so the query result
    must have them — otherwise generated code does df['InstanceId'] and dies
    with a KeyError against a table the catalog said had that column."""
    metrics = [{"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-aaa"}]}]
    series = [{"Id": "q0", "Label": "CPUUtilization",
               "Timestamps": [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)],
               "Values": [7.5]}]
    patch_boto(cw=_FakeCloudWatch(metrics=metrics, series=series))

    df = _client().execute_query({"metric": "AWS/EC2/CPUUtilization"})

    catalog_columns = {c.name for c in
                       _client(metric_namespaces="AWS/EC2").get_schemas()[0].columns}
    assert "InstanceId" in df.columns
    assert "InstanceId" in catalog_columns
    assert df["InstanceId"].tolist() == ["i-aaa"]


def test_a_dimensionless_query_fans_out_over_the_metrics_real_dimensions(patch_boto):
    """CloudWatch returns NOTHING for a metric queried without its dimensions.
    Asking for a metric with no dimensions must still return that metric's
    data, one series per resource — not an empty frame."""
    metrics = [
        {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
         "Dimensions": [{"Name": "InstanceId", "Value": "i-aaa"}]},
        {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
         "Dimensions": [{"Name": "InstanceId", "Value": "i-bbb"}]},
    ]
    ts = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]
    series = [
        {"Id": "q0", "Label": "CPUUtilization", "Timestamps": ts, "Values": [1.0]},
        {"Id": "q1", "Label": "CPUUtilization", "Timestamps": ts, "Values": [2.0]},
    ]
    ctx = patch_boto(cw=_FakeCloudWatch(metrics=metrics, series=series))

    df = _client().execute_query({"metric": "AWS/EC2/CPUUtilization"})

    sent = ctx["cw"].data_calls[0]["MetricDataQueries"]
    assert len(sent) == 2, "should issue one query per published dimension set"
    assert sorted(df["InstanceId"].tolist()) == ["i-aaa", "i-bbb"]
    assert sorted(df["value"].tolist()) == [1.0, 2.0]


def test_explicit_dimensions_are_not_fanned_out(patch_boto):
    metrics = [
        {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
         "Dimensions": [{"Name": "InstanceId", "Value": "i-aaa"}]},
        {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
         "Dimensions": [{"Name": "InstanceId", "Value": "i-bbb"}]},
    ]
    ctx = patch_boto(cw=_FakeCloudWatch(metrics=metrics, series=[]))
    _client().execute_query(
        {"metric": "AWS/EC2/CPUUtilization", "dimensions": {"InstanceId": "i-bbb"}}
    )
    sent = ctx["cw"].data_calls[0]["MetricDataQueries"]
    assert len(sent) == 1
    assert sent[0]["MetricStat"]["Metric"]["Dimensions"] == [
        {"Name": "InstanceId", "Value": "i-bbb"}
    ]


def test_dimension_resolution_failure_still_returns_a_usable_frame(patch_boto):
    cw = _FakeCloudWatch(series=[])
    cw.get_paginator = lambda name: (_ for _ in ()).throw(RuntimeError("AccessDenied"))
    patch_boto(cw=cw)

    df = _client().execute_query({"metric": "AWS/EC2/CPUUtilization"})
    assert list(df.columns)[-3:] == ["series", "timestamp", "value"]


def test_metric_envelope_returns_a_tidy_series(patch_boto):
    series = [{
        "Id": "q0",
        "Label": "CPUUtilization",
        "Timestamps": [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                       dt.datetime(2026, 1, 1, 1, tzinfo=dt.timezone.utc)],
        "Values": [3.5, 4.25],
    }]
    ctx = patch_boto(cw=_FakeCloudWatch(series=series))

    df = _client().execute_query({
        "metric": "AWS/EC2/CPUUtilization", "stat": "avg", "period": 300,
        "dimensions": {"InstanceId": "i-0abc"}, "start": "-24h",
    })

    assert list(df.columns) == ["InstanceId", "series", "timestamp", "value"]
    assert df["value"].tolist() == [3.5, 4.25]
    stat = ctx["cw"].data_calls[0]["MetricDataQueries"][0]["MetricStat"]
    assert stat["Stat"] == "Average"  # friendly spelling normalized
    assert stat["Metric"]["Namespace"] == "AWS/EC2"
    assert stat["Metric"]["MetricName"] == "CPUUtilization"
    assert stat["Metric"]["Dimensions"] == [{"Name": "InstanceId", "Value": "i-0abc"}]


@pytest.mark.parametrize(
    "given,expected",
    [("avg", "Average"), ("Sum", "Sum"), ("max", "Maximum"),
     ("count", "SampleCount"), ("p95", "p95"), ("p99.9", "p99.9")],
)
def test_stat_aliases(given, expected):
    assert AwsCloudWatchClient._normalize_stat(given) == expected


def test_metric_math_is_sent_as_an_expression(patch_boto):
    ctx = patch_boto(cw=_FakeCloudWatch(series=[]))
    _client().execute_query({
        "metric_math": 'SELECT AVG(CPUUtilization) FROM "AWS/EC2" GROUP BY InstanceId',
        "start": "-6h",
    })
    query = ctx["cw"].data_calls[0]["MetricDataQueries"][0]
    assert query["Expression"].startswith("SELECT AVG")
    assert "MetricStat" not in query


def test_bare_string_is_treated_as_an_insights_query(patch_boto):
    ctx = patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=[{"n": "1"}]))
    _client().execute_query("stats count() as n", log_groups=["/ecs/worker"])

    assert ctx["logs"].started[0]["queryString"] == "stats count() as n"


def test_catalog_table_names_are_accepted_wherever_aws_names_are(patch_boto):
    # The agent sees `log_group::/ecs/worker` in the schema, so it will send it.
    ctx = patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=[{"n": "1"}]))
    _client().execute_query(
        {"insights": "stats count() as n", "log_groups": [f"{LOG_PREFIX}/ecs/worker"]}
    )
    assert ctx["logs"].started[0]["logGroupNames"] == ["/ecs/worker"]


def test_metric_table_name_prefix_is_accepted(patch_boto):
    ctx = patch_boto(cw=_FakeCloudWatch(series=[]))
    _client().execute_query({"metric": f"{METRIC_PREFIX}AWS/EC2/CPUUtilization"})

    metric = ctx["cw"].data_calls[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]
    assert metric["Namespace"] == "AWS/EC2"
    assert metric["MetricName"] == "CPUUtilization"


def test_query_alias_is_available(patch_boto):
    # The base class aliases .query() -> .execute_query(); model-generated code
    # reaches for the shorter name.
    patch_boto(cw=_FakeCloudWatch(series=[]))
    assert _client().query({"metric": "AWS/EC2/CPUUtilization"}).empty


# ---------- limits and validation ---------- #


def test_log_group_cap_is_enforced_before_the_api_call(patch_boto):
    ctx = patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS))
    too_many = [f"/g/{i}" for i in range(MAX_LOG_GROUPS_PER_QUERY + 1)]

    with pytest.raises(ValueError, match=str(MAX_LOG_GROUPS_PER_QUERY)):
        _client().execute_query({"insights": "fields @message", "log_groups": too_many})
    assert ctx["logs"].started == []  # rejected client-side, no wasted API call


def test_logs_query_without_log_groups_is_rejected(patch_boto):
    patch_boto()
    with pytest.raises(ValueError, match="log group"):
        _client().execute_query({"insights": "fields @message"})


def test_empty_envelope_is_rejected(patch_boto):
    patch_boto()
    with pytest.raises(ValueError, match="must specify one of"):
        _client().execute_query({})


def test_metric_without_a_namespace_is_rejected(patch_boto):
    patch_boto()
    with pytest.raises(ValueError, match="Namespace"):
        _client().execute_query({"metric": "CPUUtilization"})


def test_a_connection_error_is_not_disguised_as_a_query_error(patch_boto, monkeypatch):
    # The reverse also matters: a bad query must not be reported as a
    # connection failure, which is why connect() only wraps session setup.
    import boto3

    monkeypatch.setattr(boto3, "Session", lambda **kw: (_ for _ in ()).throw(
        RuntimeError("Unable to locate credentials")))
    with pytest.raises(RuntimeError, match="Error connecting to AWS CloudWatch"):
        _client().execute_query({"insights": "fields @message", "log_groups": ["/a"]})


@pytest.mark.parametrize("bad", ["yesterdayish", "", "-1 fortnight"])
def test_unparseable_times_are_rejected_with_guidance(patch_boto, bad):
    patch_boto()
    if bad == "":
        pytest.skip("empty string means 'now', which is a valid default")
    with pytest.raises(ValueError, match="Unparseable time"):
        _client().execute_query(
            {"insights": "fields @message", "log_groups": ["/a"], "start": bad}
        )


@pytest.mark.parametrize("expr,seconds", [("-30s", 30), ("-15m", 900), ("-2h", 7200),
                                          ("-7d", 604800), ("-1w", 604800)])
def test_relative_offsets_resolve_against_now(expr, seconds):
    import time

    now = int(time.time())
    resolved = AwsCloudWatchClient._to_epoch(expr, default=0)
    assert abs((now - resolved) - seconds) <= 2


def test_absolute_and_epoch_times_are_accepted():
    assert AwsCloudWatchClient._to_epoch(1767225600, default=0) == 1767225600
    iso = AwsCloudWatchClient._to_epoch("2026-01-01T00:00:00Z", default=0)
    assert iso == 1767225600


def test_inverted_window_is_rejected(patch_boto):
    patch_boto()
    with pytest.raises(ValueError, match="window is empty"):
        _client().execute_query({
            "insights": "fields @message", "log_groups": ["/a"],
            "start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z",
        })


def test_row_limit_is_clamped_to_the_api_maximum(patch_boto):
    ctx = patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=[{"n": "1"}]))
    _client().execute_query(
        {"insights": "fields @message", "log_groups": ["/a"], "limit": 999_999}
    )
    assert "| limit 10000" in ctx["logs"].started[0]["queryString"]


def test_an_explicit_limit_in_the_query_is_not_duplicated(patch_boto):
    ctx = patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS, rows=[{"n": "1"}]))
    _client().execute_query(
        {"insights": "fields @message | limit 5", "log_groups": ["/a"], "limit": 100}
    )
    assert ctx["logs"].started[0]["queryString"].count("limit") == 1


# ---------- connection test ---------- #


def test_test_connection_reports_what_it_found(patch_boto):
    patch_boto(logs=_FakeLogs(log_groups=LOG_GROUPS))
    result = _client().test_connection()

    assert result["success"] is True
    assert "3" in result["message"]


def test_test_connection_names_the_failing_iam_action(patch_boto):
    # An admin whose policy is missing one permission needs to know which.
    logs = _FakeLogs()
    logs.get_paginator = lambda name: (_ for _ in ()).throw(
        RuntimeError("AccessDeniedException"))
    patch_boto(logs=logs)

    result = _client().test_connection()
    assert result["success"] is False
    assert "DescribeLogGroups" in result["message"]


def test_test_connection_names_the_failing_metrics_action(patch_boto):
    patch_boto(cw=_FakeCloudWatch(list_error=RuntimeError("AccessDeniedException")))

    result = _client().test_connection()
    assert result["success"] is False
    assert "ListMetrics" in result["message"]


def test_test_connection_warns_when_the_connection_would_index_nothing(patch_boto):
    patch_boto(logs=_FakeLogs(log_groups=[]))
    result = _client().test_connection()

    assert result["success"] is True
    assert "index nothing" in result["message"]


# ---------- registry wiring ---------- #


def test_registry_resolves_the_client():
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    assert resolve_client_class("aws_cloudwatch") is AwsCloudWatchClient
    entry = REGISTRY["aws_cloudwatch"]
    assert set(entry.credentials_auth.by_auth) == {"aws_keys", "aws_role", "aws_default"}
    assert entry.category == "infra"


def test_client_accepts_every_field_the_form_can_submit():
    """Connection.get_client() splats config + credentials together, so the
    constructor has to take the union of every auth variant's fields."""
    from app.schemas.data_source_registry import REGISTRY

    entry = REGISTRY["aws_cloudwatch"]
    fields = set(entry.config_schema.model_fields)
    for variant in entry.credentials_auth.by_auth.values():
        fields |= set(variant.schema.model_fields)

    AwsCloudWatchClient(**{f: None for f in fields if f != "region"}, region="eu-west-1")


def test_relative_date_hint_is_declared():
    # Without it the coder agent freezes literal dates into saved queries.
    assert "CloudWatch" in AwsCloudWatchClient.relative_date_hint
