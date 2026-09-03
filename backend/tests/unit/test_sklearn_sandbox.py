"""scikit-learn inside the code sandbox.

The sandbox is a denylist, so `import sklearn` was never *rejected* — it was
simply not installed, and generated code that reached for it died at runtime
with ModuleNotFoundError. These tests pin the contract now that it is a
declared dependency: sklearn code validates, runs end to end through the real
executor, returns a tidy DataFrame, and the two escape hatches a model
library invites (joblib/pickle serialization to disk) stay closed. The prompt
half of the contract — telling the coder sklearn exists and how to use it
within the executor's constraints — is checked against the prompt text.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.ai.agents.coder.coder import _ml_rules_section
from app.ai.code_execution.code_execution import (
    FORBIDDEN_MODULES,
    StreamingCodeExecutor,
    UnsafePythonError,
    validate_python_code,
)

_SKLEARN_CODE = '''
def generate_df(ds_clients, excel_files):
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.model_selection import train_test_split

    rng = np.random.RandomState(42)
    n = 400
    df = pd.DataFrame({
        "tenure": rng.randint(1, 72, n),
        "monthly_charges": rng.uniform(20, 120, n),
        "support_calls": rng.randint(0, 8, n),
        "noise": rng.normal(size=n),
    })
    # churn depends on tenure + support_calls only; `noise` should rank last.
    df["churn"] = ((df["support_calls"] > 4) | (df["tenure"] < 12)).astype(int)

    features = ["tenure", "monthly_charges", "support_calls", "noise"]
    X_train, X_test, y_train, y_test = train_test_split(
        df[features], df["churn"], test_size=0.3, random_state=42
    )
    model = RandomForestClassifier(n_estimators=50, random_state=42, n_jobs=1)
    model.fit(X_train, y_train)
    perm = permutation_importance(
        model, X_test, y_test, n_repeats=5, random_state=42, n_jobs=1
    )
    out = pd.DataFrame({
        "feature": features,
        "importance": perm.importances_mean,
        "model": type(model).__name__,
    })
    out["importance_share"] = out["importance"] / out["importance"].sum()
    out = out.sort_values("importance", ascending=False).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    print("Final df Info:", out.info())
    return out
'''


def test_sklearn_imports_pass_validation():
    validate_python_code(_SKLEARN_CODE)  # must not raise


def test_sklearn_model_trains_end_to_end_in_executor():
    executor = StreamingCodeExecutor(organization_settings=None, logger=None)
    df, output_log, queries = executor.execute_code(
        code=_SKLEARN_CODE, ds_clients={}, excel_files=[]
    )
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["feature", "importance", "model", "importance_share", "rank"]
    assert len(df) == 4
    assert df["model"].iloc[0] == "RandomForestClassifier"
    # Deterministic (random_state everywhere) and the signal features rank
    # above pure noise.
    assert df["feature"].tolist()[-1] == "noise"
    assert set(df["feature"].head(2)) == {"tenure", "support_calls"}
    assert abs(df["importance_share"].sum() - 1.0) < 1e-9
    # Output contract: primitives only, no estimator objects leaked.
    assert all(dtype.kind in "ifOb" for dtype in df.dtypes)
    assert "Final df Info:" in output_log
    assert queries == []


def test_sklearn_execution_is_deterministic_across_reruns():
    executor = StreamingCodeExecutor(organization_settings=None, logger=None)
    first, _, _ = executor.execute_code(code=_SKLEARN_CODE, ds_clients={}, excel_files=[])
    second, _, _ = executor.execute_code(code=_SKLEARN_CODE, ds_clients={}, excel_files=[])
    pd.testing.assert_frame_equal(first, second)


@pytest.mark.parametrize(
    "code",
    [
        "import joblib\ndef generate_df(ds_clients, excel_files):\n    return None\n",
        "from joblib import dump\ndef generate_df(ds_clients, excel_files):\n    return None\n",
        "def generate_df(ds_clients, excel_files):\n    import joblib\n    joblib.dump({}, '/tmp/x.pkl')\n",
        "def generate_df(ds_clients, excel_files):\n    import pickle\n    return pickle.dumps({})\n",
    ],
)
def test_model_serialization_libraries_stay_forbidden(code):
    assert "joblib" in FORBIDDEN_MODULES
    with pytest.raises(UnsafePythonError, match="Forbidden import"):
        validate_python_code(code)


def test_ml_rules_tell_the_coder_what_the_executor_enforces():
    rules = _ml_rules_section()
    # Availability, and the neighbours that are NOT installed.
    assert "scikit-learn IS available" in rules
    for missing in ("statsmodels", "prophet", "xgboost", "shap"):
        assert missing in rules
    # The executor's constraints on training code.
    assert "random_state=42" in rules           # reruns are verbatim
    assert "n_jobs=1" in rules                  # shared worker pool
    assert "permutation_importance" in rules    # preferred importance method
    assert "tidy DataFrame" in rules            # estimator never persisted
    # Sandbox idioms that sklearn code otherwise trips over.
    assert "type(model).__name__" in rules
    assert "joblib" in rules
