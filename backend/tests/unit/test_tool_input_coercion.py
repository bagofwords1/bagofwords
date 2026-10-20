"""List-typed data-tool arguments that arrive as JSON text are parsed
(app/ai/tools/schemas/_coerce.py); other bad shapes still fail validation."""
import pytest
from pydantic import ValidationError

from app.ai.tools.schemas.create_data import CreateDataInput
from app.ai.tools.schemas.inspect_data import InspectDataInput
from app.ai.tools.schemas.write_csv import WriteCsvInput

TBS = '[{"data_source_id": "ds-1", "tables": ["orders", "deploys"]}]'


def test_create_data_parses_json_text_lists():
    m = CreateDataInput(user_prompt="paid orders by week", title="t", interpreted_prompt="p", tables_by_source=TBS, source_file_ids='["f1"]')
    assert [t.data_source_id for t in m.tables_by_source] == ["ds-1"]
    assert m.tables_by_source[0].tables == ["orders", "deploys"]
    assert m.source_file_ids == ["f1"]


def test_inspect_and_write_csv_parse_json_text_lists():
    assert InspectDataInput(user_prompt="x", tables_by_source=TBS).tables_by_source[0]["tables"] == ["orders", "deploys"]
    assert WriteCsvInput(user_prompt="x", tables_by_source=TBS).tables_by_source[0]["data_source_id"] == "ds-1"


def test_real_lists_and_none_pass_through_and_garbage_still_fails():
    m = CreateDataInput(user_prompt="x", title="t", interpreted_prompt="p", tables_by_source=[{"data_source_id": "ds-1", "tables": ["orders"]}])
    assert m.tables_by_source[0].tables == ["orders"]
    assert CreateDataInput(user_prompt="x", title="t", interpreted_prompt="p").tables_by_source is None
    with pytest.raises(ValidationError):
        CreateDataInput(user_prompt="x", title="t", interpreted_prompt="p", tables_by_source="orders, deploys")
    with pytest.raises(ValidationError):
        CreateDataInput(user_prompt="x", title="t", interpreted_prompt="p", tables_by_source='{"data_source_id": "ds-1"}')
