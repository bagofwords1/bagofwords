"""write_csv input tolerance for the ``user_prompt`` field.

Planners that only see the tool's prose description (no JSON schema) routinely
rename the field ("prompt") or omit it in favor of ``title`` +
``source_file_ids``. Each such call used to burn a validation strike — two of
them killed the whole run with "user_prompt: Field required". The input model
now recovers the intent from common aliases and falls back to the title.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.tools.schemas.write_csv import WriteCsvInput


def test_user_prompt_passes_through():
    data = WriteCsvInput(user_prompt="Parse the logs", title="Errors")
    assert data.user_prompt == "Parse the logs"


@pytest.mark.parametrize("alias", ["prompt", "instruction", "description", "task"])
def test_common_aliases_accepted(alias):
    data = WriteCsvInput(**{alias: "Parse the logs"})
    assert data.user_prompt == "Parse the logs"


def test_title_only_call_falls_back_to_title():
    data = WriteCsvInput(title="Software Houses Income Tax 2025")
    assert data.user_prompt == "Software Houses Income Tax 2025"
    assert data.title == "Software Houses Income Tax 2025"


def test_explicit_user_prompt_wins_over_alias_and_title():
    data = WriteCsvInput(user_prompt="real", prompt="alias", title="title")
    assert data.user_prompt == "real"


def test_blank_alias_is_skipped():
    data = WriteCsvInput(prompt="   ", title="Fallback Title")
    assert data.user_prompt == "Fallback Title"


def test_still_required_when_nothing_usable():
    with pytest.raises(ValidationError):
        WriteCsvInput(source_file_ids=["abc"])


def test_json_schema_still_advertises_user_prompt_required():
    schema = WriteCsvInput.model_json_schema()
    assert "user_prompt" in schema.get("required", [])
