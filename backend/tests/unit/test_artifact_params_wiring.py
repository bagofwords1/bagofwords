"""Params-wiring contract check on generated artifact code."""

from app.ai.tools.implementations.create_artifact import CreateArtifactTool

check = CreateArtifactTool.params_wiring_errors

DATA = {"visualizations": [
    {"id": "v1", "parameters": [
        {"name": "genre_id", "type": "number", "source": "input"},
        {"name": "assignee", "type": "string", "source": "identity"},
    ]},
    {"id": "v2", "parameters": [{"name": "genre_id", "type": "number", "source": "input"}]},
]}

WIRED = "const p = useParams();\np.setParam('genre_id', Number(v));"
LOCAL_STATE_ONLY = "const [sel, setSel] = React.useState('');\nsetSel(v);"
WRONG_NAME = "const p = useParams();\np.setParam('genre', v);"


def test_wired_code_passes():
    assert check(WIRED, DATA) == []


def test_local_state_only_fails_with_all_names():
    errs = check(LOCAL_STATE_ONLY, DATA)
    assert len(errs) == 1
    assert "genre_id" in errs[0]
    assert "useParams" in errs[0]


def test_wrong_name_fails_naming_the_missing_param():
    errs = check(WRONG_NAME, DATA)
    assert len(errs) == 1
    assert "genre_id" in errs[0]


def test_identity_params_are_exempt():
    data = {"visualizations": [{"parameters": [{"name": "email", "source": "identity"}]}]}
    assert check(LOCAL_STATE_ONLY, data) == []


def test_no_params_no_errors():
    assert check(LOCAL_STATE_ONLY, {"visualizations": [{"parameters": []}]}) == []
    assert check(LOCAL_STATE_ONLY, {}) == []
