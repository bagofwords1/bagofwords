"""classify_schema_change: which list edits bump the schema version."""
import pytest

from app.services.agent_lists.schema_change import classify_schema_change


def f(fid, name=None, t="string", required=False, enum=None, description=""):
    return {"id": fid, "name": name or fid, "type": t, "required": required, "enum": enum,
            "description": description, "unit": None, "method": "extract"}


BASE = [f("a"), f("b", t="enum", enum=["x", "y"]), f("c", t="number", required=True)]


@pytest.mark.parametrize("new,new_key,expected", [
    (BASE, None, "none"),
    (BASE + [f("d")], None, "additive"),                                   # new optional field
    ([f("a", description="changed"), BASE[1], BASE[2]], None, "additive"),  # description edit
    ([f("a", name="renamed"), BASE[1], BASE[2]], None, "additive"),        # rename (same id)
    ([f("b", t="enum", enum=["x", "y", "z"]), BASE[0], BASE[2]], None, "additive"),  # enum value added + reorder
    ([BASE[0], BASE[1], f("c", t="number", required=False)], None, "additive"),       # became optional
    (BASE + [f("d", required=True)], None, "breaking"),                   # new required field
    ([BASE[0], BASE[1]], None, "breaking"),                                # removed field
    ([f("a", t="number"), BASE[1], BASE[2]], None, "breaking"),            # type change
    ([f("a", required=True), BASE[1], BASE[2]], None, "breaking"),         # became required
    ([BASE[0], f("b", t="enum", enum=["x"]), BASE[2]], None, "breaking"),  # enum value removed
    (BASE, "a", "breaking"),                                               # key field changed
])
def test_classification(new, new_key, expected):
    assert classify_schema_change(BASE, None, new, new_key) == expected
