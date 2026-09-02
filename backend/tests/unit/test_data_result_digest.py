"""History digest shared by create_data and describe_entity results: shape,
columns, chart, viz id, the top row when data may be shown, and the values a
parameterized result ran with — so a later turn can reuse the result rather
than read or rebuild it."""
from app.ai.context.builders.message_context_builder import _digest_data_result

RESULT = {
    "data_preview": {
        "columns": [{"field": "GenreId"}, {"field": "Name"}],
        "rows": [{"GenreId": 1, "Name": "Rock"}, {"GenreId": 2, "Name": "Jazz"}],
        "row_count": 25,
    },
    "data_model": {"type": "bar_chart"},
    "created_visualization_ids": ["viz-1"],
}


def test_digest_carries_shape_columns_chart_viz_and_top_row():
    d = _digest_data_result(RESULT, True)
    assert d.startswith("25 rows × 2 cols")
    for needle in ("cols: GenreId, Name", "chart: bar_chart", "viz_id: viz-1", '"Name": "Rock"'):
        assert needle in d


def test_digest_hides_rows_when_data_may_not_be_shown():
    d = _digest_data_result(RESULT, False)
    assert "25 rows × 2 cols" in d
    assert "top row" not in d and "Rock" not in d


def test_digest_records_the_values_a_parameterized_result_ran_with():
    d = _digest_data_result(RESULT, True, applied_params={"genre": "Rock", "year": None})
    assert "params: genre='Rock', year=None" in d


def test_digest_falls_back_to_legacy_full_data_shape():
    legacy = {"data": {"columns": [{"field": "a"}], "rows": [{"a": 1}, {"a": 2}]}}
    assert _digest_data_result(legacy, True).startswith("2 rows × 1 cols")
    assert _digest_data_result({}, True) == "0 rows × 0 cols"
    assert _digest_data_result(None, True) == ""
