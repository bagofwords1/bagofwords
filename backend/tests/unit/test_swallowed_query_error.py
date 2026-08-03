"""Generated code must not turn a failed query into a successful empty answer.

The contract: when `execute_query` raises and the returned frame is empty, the
execution fails loudly and carries the underlying error, so the retry loop can
fix the code. When either half is absent — a query that legitimately returned no
rows, or a failure the code genuinely recovered from — execution succeeds.

Everything here goes through the public `execute_code` surface; the guard's
internals are deliberately not asserted on.
"""
import pandas as pd
import pytest

from app.ai.code_execution.code_execution import (
    StreamingCodeExecutor,
    SwallowedQueryError,
)
from app.errors.codes import ErrorCode


class _StubClient:
    """Stands in for a real driver: execute_query takes a query string only."""

    def __init__(self, result=None, raises=None):
        self._result = pd.DataFrame({"n": [7]}) if result is None else result
        self._raises = raises
        self._bow_connection_id = None

    def execute_query(self, query):
        if self._raises:
            raise self._raises
        return self._result


def _run(code, client, **kwargs):
    return StreamingCodeExecutor(organization_settings=None).execute_code(
        code=code, ds_clients={"main": client}, excel_files=[], **kwargs
    )


# ---------------------------------------------------------------------------
# A swallowed failure becomes a real failure
# ---------------------------------------------------------------------------

# Distinct failure modes, none of which should reach the caller as "0 rows".
# Values deliberately differ from the incident that prompted this guard.
SWALLOWED_FAILURES = [
    pytest.param(
        """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    try:
        return ds_clients["main"].execute_query("SELECT 1", scope=["a"])
    except Exception:
        return pd.DataFrame(columns=["c"])
""",
        _StubClient(),
        "unexpected keyword argument",
        id="unsupported_kwarg",
    ),
    pytest.param(
        """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    try:
        return ds_clients["main"].execute_query("SELECT * FROM missing_view")
    except Exception:
        return pd.DataFrame(columns=["c"])
""",
        _StubClient(raises=RuntimeError("Invalid object name 'missing_view'")),
        "missing_view",
        id="bad_object_name",
    ),
    pytest.param(
        """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    frames = []
    for q in ("SELECT a FROM t1", "SELECT b FROM t2"):
        try:
            frames.append(ds_clients["main"].execute_query(q))
        except Exception:
            pass
    return pd.concat(frames) if frames else pd.DataFrame(columns=["a"])
""",
        _StubClient(raises=ValueError("permission denied for relation t1")),
        "permission denied",
        id="loop_swallowing_every_query",
    ),
]


@pytest.mark.parametrize("code,client,expected_fragment", SWALLOWED_FAILURES)
def test_swallowed_query_failure_raises(code, client, expected_fragment):
    with pytest.raises(SwallowedQueryError) as excinfo:
        _run(code, client)

    err = excinfo.value
    assert err.error_code == ErrorCode.QUERY_FAILED_SILENTLY.value
    # The underlying driver error must survive onto the exception: the retry
    # loop feeds it back to codegen, and without it the model cannot fix the call.
    assert any(expected_fragment in e for e in err.errors)


def test_underlying_error_reaches_the_retry_loop_message():
    """The retry loop stringifies the exception, so the cause must be in there."""
    code = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    try:
        return ds_clients["main"].execute_query("SELECT 1")
    except Exception:
        return pd.DataFrame()
"""
    with pytest.raises(SwallowedQueryError) as excinfo:
        _run(code, _StubClient(raises=RuntimeError("login timeout expired")))
    assert "login timeout expired" in str(excinfo.value)


def test_stdout_error_print_is_not_what_we_rely_on():
    """The guard fires even when the code swallows silently, printing nothing."""
    code = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    try:
        return ds_clients["main"].execute_query("SELECT 1")
    except Exception:
        return pd.DataFrame(columns=["x"])
"""
    with pytest.raises(SwallowedQueryError):
        _run(code, _StubClient(raises=RuntimeError("boom")))


# ---------------------------------------------------------------------------
# Legitimate outcomes stay untouched
# ---------------------------------------------------------------------------

def test_empty_result_without_query_failure_succeeds():
    """Zero rows is a valid answer when nothing failed."""
    df, _log, _q = _run(
        """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("SELECT * FROM t WHERE 1=0")
""",
        _StubClient(result=pd.DataFrame(columns=["a", "b"])),
    )
    assert df.empty
    assert list(df.columns) == ["a", "b"]


def test_recovered_failure_returning_rows_succeeds():
    """A failure the code genuinely recovered from is not a swallow."""

    class _FailsFirst(_StubClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def execute_query(self, query):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient reset")
            return pd.DataFrame({"n": [1, 2, 3]})

    df, _log, _q = _run(
        """
def generate_df(ds_clients, excel_files):
    client = ds_clients["main"]
    try:
        return client.execute_query("SELECT primary_source")
    except Exception:
        return client.execute_query("SELECT fallback_source")
""",
        _FailsFirst(),
    )
    assert len(df) == 3


def test_successful_query_succeeds():
    """Baseline: the happy path is unchanged."""
    df, _log, _q = _run(
        """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("SELECT COUNT(*) AS n FROM t")
""",
        _StubClient(result=pd.DataFrame({"n": [42]})),
    )
    assert df.iloc[0]["n"] == 42


def test_unhandled_query_failure_still_propagates_unchanged():
    """Code that does not swallow keeps surfacing the driver's own exception."""
    code = """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("SELECT 1")
"""
    with pytest.raises(Exception) as excinfo:
        _run(code, _StubClient(raises=RuntimeError("connection refused")))
    assert not isinstance(excinfo.value, SwallowedQueryError)
    assert "connection refused" in str(excinfo.value)


@pytest.mark.parametrize(
    "returned",
    ["None", "'a string'", "{'k': 1}"],
    ids=["none", "string", "dict"],
)
def test_non_dataframe_results_are_left_to_the_caller(returned):
    """The guard only judges DataFrames; other shapes are the caller's problem."""
    code = f"""
def generate_df(ds_clients, excel_files):
    try:
        ds_clients["main"].execute_query("SELECT 1")
    except Exception:
        pass
    return {returned}
"""
    result, _log, _q = _run(code, _StubClient(raises=RuntimeError("boom")))
    assert not isinstance(result, pd.DataFrame)
