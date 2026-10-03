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


def test_declaration_driven_custom_controls_and_batch_setters_are_supported():
    assert check("const p = useParams(); p.declarations.map(d => p.setParams({[d.name]: value}));", DATA) == []
    assert check("const p = useParams(); p.setParams({genre_id: value});", DATA) == []


def test_legacy_edits_preserve_existing_controls_without_retrofitting_new_requirements():
    data = {"runtime": {"version": 0}, "visualizations": [{"parameters": [
        {"name": "region"}, {"name": "period"},
    ]}]}
    before = "const p = useParams(); p.setParam('region', value); title = 'Revenue';"
    after = before.replace("Revenue", "Sales")
    assert check(after, data, previous_code=before) == []
    assert check("title = 'Sales';", data, previous_code=before)
    assert check(after, {**data, "runtime": {"version": 11}}, previous_code=before)


def test_new_query_parameters_are_not_grandfathered_by_a_legacy_edit():
    data = {"runtime": {"version": 0}, "visualizations": [
        {"id": "existing", "parameters": [{"name": "region"}]},
        {"id": "added", "parameters": [{"name": "period"}]},
    ]}
    before = "const p = useParams(); p.setParam('region', value);"
    assert check(before, data, previous_code=before, previous_visualization_ids=["existing"])
    assert check(before + " p.setParam('period', range);", data,
                 previous_code=before, previous_visualization_ids=["existing"]) == []
