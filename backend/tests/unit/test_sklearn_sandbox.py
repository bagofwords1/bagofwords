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


# ---------------------------------------------------------------------------
# Org-level gate: `enable_ml_training` / `ml_training_row_limit`
# ---------------------------------------------------------------------------
from app.ai.code_execution.code_execution import (  # noqa: E402
    ML_TRAINING_MODULES,
    ML_TRAINING_ROW_LIMIT_DEFAULT,
    ML_TRAINING_ROW_LIMIT_MIN,
    ml_training_settings,
)
from app.schemas.organization_settings_schema import OrganizationSettingsConfig  # noqa: E402


class _FakeSettings:
    """Minimal stand-in for OrganizationSettings.get_config."""

    def __init__(self, **values):
        self._values = values

    def get_config(self, key):
        if key in self._values:
            return type("Cfg", (), {"value": self._values[key]})()
        return None


def test_ml_settings_default_on_with_50k_row_limit():
    cfg = OrganizationSettingsConfig()
    assert cfg.enable_ml_training.value is True
    assert cfg.ml_training_row_limit.value == ML_TRAINING_ROW_LIMIT_DEFAULT
    assert ml_training_settings(None) == (True, ML_TRAINING_ROW_LIMIT_DEFAULT)


def test_ml_settings_read_from_org_and_floor_the_row_limit():
    assert ml_training_settings(_FakeSettings(enable_ml_training=False, ml_training_row_limit=20000)) == (False, 20000)
    # An edited value below the floor can't make every fit degenerate.
    assert ml_training_settings(_FakeSettings(ml_training_row_limit=5)) == (True, ML_TRAINING_ROW_LIMIT_MIN)
    # Garbage falls back to defaults rather than raising.
    assert ml_training_settings(_FakeSettings(ml_training_row_limit="lots")) == (True, ML_TRAINING_ROW_LIMIT_DEFAULT)


def test_sklearn_rejected_at_validation_when_training_disabled():
    executor = StreamingCodeExecutor(
        organization_settings=_FakeSettings(enable_ml_training=False), logger=None
    )
    with pytest.raises(UnsafePythonError, match="machine-learning training .* is disabled"):
        executor.execute_code(code=_SKLEARN_CODE, ds_clients={}, excel_files=[])
    # Same gate, called directly.
    with pytest.raises(UnsafePythonError, match="Forbidden import: 'from scipy.stats'"):
        validate_python_code(
            "def generate_df(ds_clients, excel_files):\n    from scipy.stats import zscore\n    return None\n",
            extra_forbidden_modules=ML_TRAINING_MODULES,
        )


def test_sklearn_runs_when_training_enabled_by_org():
    executor = StreamingCodeExecutor(
        organization_settings=_FakeSettings(enable_ml_training=True), logger=None
    )
    df, _, _ = executor.execute_code(code=_SKLEARN_CODE, ds_clients={}, excel_files=[])
    assert len(df) == 4


def test_disabling_ml_does_not_widen_the_static_denylist():
    # pandas-only code is unaffected by the gate, and sklearn/scipy are not in
    # the security denylist — they are gated, not forbidden.
    validate_python_code(
        "def generate_df(ds_clients, excel_files):\n    import numpy as np\n    return None\n",
        extra_forbidden_modules=ML_TRAINING_MODULES,
    )
    assert not (ML_TRAINING_MODULES & FORBIDDEN_MODULES)


def test_ml_rules_follow_the_toggle_and_row_limit():
    on = _ml_rules_section(True, 20_000)
    assert "scikit-learn IS available" in on
    assert "20,000 rows (organization limit)" in on
    assert "df.sample(n=20000, random_state=42)" in on
    off = _ml_rules_section(False, 20_000)
    assert "DISABLED for this organization" in off
    assert "scikit-learn IS available" not in off
    assert "pandas/numpy" in off


def test_planner_prompt_follows_the_ml_toggle():
    """The planner must not promise (or claim) a trained model when the org
    has training off — the sandbox would reject the imports and the coder
    would otherwise be pushed to fake a model in numpy."""
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
    from app.schemas.ai.planner import PlannerInput

    on = PromptBuilderV3._build_system(PlannerInput(user_message="train a churn model", ml_training_enabled=True))
    off = PromptBuilderV3._build_system(PlannerInput(user_message="train a churn model", ml_training_enabled=False))
    assert "scikit-learn is enabled for this org" in on
    assert "model_type" in on
    assert "Model training is DISABLED for this org" in off
    assert "never ask the coder to hand-roll one with numpy" in off
    assert "scikit-learn is enabled for this org" not in off
    # Default mirrors the org default (on).
    assert PlannerInput(user_message="x").ml_training_enabled is True
