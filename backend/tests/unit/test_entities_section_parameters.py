"""The planner's <entities> context must expose each entity's declared
parameters so it can pass VALUES to describe_entity instead of regenerating
code."""
from app.ai.context.sections.entities_section import EntitiesSection, EntityItem


def _section(params):
    return EntitiesSection(items=[EntityItem(id="e1", type="model", title="Sales by year", parameters=params)])


def test_declared_parameters_are_rendered_with_their_contract():
    xml = _section([
        {"name": "year", "type": "number", "source": "input", "default": 2022, "label": "Invoice year"},
        {"name": "viewer_email", "source": "identity"},
        {"name": "region", "type": "string", "options": ["EMEA", "APAC"], "required": True},
    ]).render()
    assert "<parameters>" in xml
    for needle in ('name="year"', 'type="number"', 'default="2022"', "Invoice year",
                   'name="viewer_email"', 'source="identity"',
                   'name="region"', 'options="EMEA,APAC"', 'required="true"'):
        assert needle in xml, needle


def test_entities_without_parameters_render_no_parameters_block():
    assert "<parameters>" not in _section(None).render()
    assert "<parameters>" not in _section([]).render()
    assert "<parameters>" not in _section([{"type": "number"}]).render(), "nameless rows are skipped"
