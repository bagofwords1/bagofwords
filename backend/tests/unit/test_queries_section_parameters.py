"""<queries> advertises each query's declared parameters.

Without this the planner cannot tell that an in-report query takes VALUES —
it would have to spend a read_query call to find out, and would reach for
create_data instead of run_query. <entities> has always rendered its params
for describe_entity; this is the same affordance for the report's own queries.

Run:
    cd backend && uv run pytest tests/unit/test_queries_section_parameters.py -v
"""
from app.ai.context.sections.queries_section import QueriesSection, QueryObservation


def _section(parameters):
    return QueriesSection(items=[QueryObservation(
        query_id="q1", query_title="Revenue by region",
        row_count=3, column_names=["region", "revenue"],
        parameters=parameters,
    )]).render()


def test_declared_parameters_are_rendered_with_name_type_and_source():
    xml = _section([
        {"name": "region", "type": "string", "source": "input", "label": "Region"},
        {"name": "viewer_email", "type": "string", "source": "identity"},
    ])
    assert "<parameters>" in xml
    assert '<param name="region" type="string" source="input">' in xml
    assert "Region" in xml
    assert 'name="viewer_email"' in xml and 'source="identity"' in xml


def test_required_and_default_are_surfaced():
    xml = _section([
        {"name": "year", "type": "number", "source": "input", "required": True},
        {"name": "region", "type": "string", "source": "input", "default": "US"},
    ])
    assert 'name="year"' in xml and 'required="true"' in xml
    assert 'default="US"' in xml


def test_no_parameters_renders_no_block():
    assert "<parameters>" not in _section(None)
    assert "<parameters>" not in _section([])


def test_rows_without_a_name_are_skipped_not_fatal():
    xml = _section([{"name": "region", "type": "string"}, {"no_name": 1}])
    assert 'name="region"' in xml
    assert xml.count("<param ") == 1


def test_builder_filters_non_dict_rows_before_the_section():
    """The section models dicts; the builder is what tolerates junk on the
    stored Query.parameters JSON."""
    from app.ai.context.builders.query_context_builder import _param_dicts

    assert _param_dicts([{"name": "region"}, "junk", 7, {"no_name": 1}]) == [{"name": "region"}]
    assert _param_dicts(None) is None
    assert _param_dicts([]) is None
    assert _param_dicts("not a list") is None


def test_values_are_escaped():
    xml = _section([{"name": "region", "type": "string", "label": "A & B <x>"}])
    assert "A &amp; B &lt;x&gt;" in xml
