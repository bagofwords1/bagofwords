"""Reactive DB-error remediation hints in the code-execution retry loop.

Some database errors are too cryptic for the coder to fix from the raw
message alone, so it re-rolls the same broken query every retry. The
executor augments known error signatures with an actionable hint before
feeding them back. This is the reactive companion to the proactive guidance
carried in each client's `description`.

Born as the reproduction for: Oracle DWH queries looping on
"ORA-12704: character set mismatch" across attempts 1-3.
"""

import asyncio

import pandas as pd

from app.ai.code_execution.code_execution import (
    StreamingCodeExecutor,
    augment_db_error_hint,
)
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest


# ---------------------------------------------------------------------------
# augment_db_error_hint (pure helper)
# ---------------------------------------------------------------------------

class TestAugmentDbErrorHint:
    def test_ora_12704_gets_charset_hint(self):
        err = "Execution error: (oracledb.exceptions.DatabaseError) ORA-12704: character set mismatch"
        out = augment_db_error_hint(err)
        assert out.startswith(err), "original error text must be preserved verbatim"
        assert "ORA-12704" in out
        assert "TO_CHAR" in out
        assert "NVARCHAR2" in out and "VARCHAR2" in out
        assert "UNION" in out

    def test_signature_match_is_case_insensitive(self):
        out = augment_db_error_hint("boom ora-12704 character set mismatch")
        assert "TO_CHAR" in out

    def test_unknown_error_is_returned_unchanged(self):
        err = "Execution error: ORA-00942: table or view does not exist"
        assert augment_db_error_hint(err) == err

    def test_non_string_input_is_passed_through(self):
        assert augment_db_error_hint(None) is None
        assert augment_db_error_hint("") == ""


# ---------------------------------------------------------------------------
# End-to-end: the hint must reach the coder's retry feedback
# ---------------------------------------------------------------------------

class _RaisingClient:
    """Minimal ds_client whose execute_query always raises ORA-12704."""

    description = "oracle stub"

    def execute_query(self, query, *args, **kwargs):
        raise RuntimeError(
            "(oracledb.exceptions.DatabaseError) ORA-12704: character set mismatch"
        )


ORA_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    key = list(ds_clients.keys())[0]\n"
    "    return ds_clients[key].execute_query('SELECT 1 FROM DUAL')\n"
)

CLEAN_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'a': [1]})\n"
)


def _capturing_coder(code_sequence):
    calls = []

    async def _gen(**kwargs):
        calls.append(kwargs)
        i = min(len(calls) - 1, len(code_sequence) - 1)
        return code_sequence[i]

    return _gen, calls


def _drive(executor, generator_fn, ds_clients, retries=2):
    events = []

    async def go():
        async for ev in executor.generate_and_execute_stream_v2(
            request=CodeGenRequest(
                context=CodeGenContext(user_prompt="x", schemas_excerpt=""),
                retries=retries,
            ),
            ds_clients=ds_clients,
            excel_files=[],
            code_generator_fn=generator_fn,
        ):
            events.append(ev)

    asyncio.run(go())
    return events


class TestHintReachesRetryFeedback:
    def test_ora_12704_hint_is_fed_back_to_the_coder(self):
        """After an ORA-12704 failure, the 2nd codegen call must see the hint
        alongside the failing code — not just the bare error."""
        gen, calls = _capturing_coder([ORA_CODE, CLEAN_CODE])
        ds_clients = {"DWH:oracle-1": _RaisingClient()}
        events = _drive(StreamingCodeExecutor(), gen, ds_clients, retries=2)

        assert len(calls) == 2, "an ORA-12704 failure must consume a retry"
        feedback = calls[1]["code_and_error_messages"]
        assert feedback, "previous attempt must be fed back to the coder"
        _, prev_error = feedback[-1]
        assert "ORA-12704" in prev_error
        assert "TO_CHAR" in prev_error, "the actionable hint must ride the retry feedback"

        # And the run recovers on the clean retry.
        done = [e for e in events if e["type"] == "done"][-1]["payload"]
        df = done["df"]
        assert isinstance(df, pd.DataFrame) and list(df.columns) == ["a"]
