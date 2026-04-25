"""Unit tests for the deterministic helpers in the eval-draft path.

Pure-Python: no DB, no LLM. Covers the data-source extractor and the
classifier JSON parser (the LLM call itself is mocked so we can verify
the prompt-shape robustness without paying for an inference).
"""
from unittest.mock import MagicMock, patch
import pytest


class _ToolExec:
    def __init__(self, arguments_json):
        self.arguments_json = arguments_json


def test_extract_data_source_ids_from_tables_by_source_list():
    from app.services.completion_feedback_service import CompletionFeedbackService

    tes = [
        _ToolExec({"tables_by_source": [
            {"data_source_id": "ds-a", "tables": ["t1"]},
            {"data_source_id": "ds-b", "tables": ["t2"]},
        ]}),
    ]
    assert CompletionFeedbackService._extract_data_source_ids(tes) == ["ds-a", "ds-b"]


def test_extract_data_source_ids_from_legacy_dict():
    from app.services.completion_feedback_service import CompletionFeedbackService

    tes = [_ToolExec({"tables_by_source": {"ds-x": ["t"], "ds-y": ["t"]}})]
    assert CompletionFeedbackService._extract_data_source_ids(tes) == ["ds-x", "ds-y"]


def test_extract_data_source_ids_falls_back_to_top_level_array():
    from app.services.completion_feedback_service import CompletionFeedbackService

    tes = [_ToolExec({"data_source_ids": ["ds-1", "ds-2"]})]
    assert CompletionFeedbackService._extract_data_source_ids(tes) == ["ds-1", "ds-2"]


def test_extract_data_source_ids_dedups_across_executions():
    from app.services.completion_feedback_service import CompletionFeedbackService

    tes = [
        _ToolExec({"tables_by_source": [{"data_source_id": "ds-a", "tables": []}]}),
        _ToolExec({"tables_by_source": [{"data_source_id": "ds-a", "tables": []}]}),
        _ToolExec({"data_source_ids": ["ds-b"]}),
    ]
    assert CompletionFeedbackService._extract_data_source_ids(tes) == ["ds-a", "ds-b"]


def test_extract_data_source_ids_handles_missing_keys():
    from app.services.completion_feedback_service import CompletionFeedbackService

    tes = [_ToolExec({}), _ToolExec({"unrelated": "x"})]
    assert CompletionFeedbackService._extract_data_source_ids(tes) == []


@pytest.mark.asyncio
async def test_classify_duplicate_parses_clean_json():
    from app.services.completion_feedback_service import CompletionFeedbackService

    svc = CompletionFeedbackService()
    fake_llm = MagicMock()
    fake_llm.inference.return_value = (
        '{"duplicate": true, "matched_id": "case-123", "reason": "same metric"}'
    )

    with patch("app.services.completion_feedback_service.LLMService") as LLMServiceMock, \
         patch("app.ai.llm.LLM") as LLMClassMock:
        LLMServiceMock.return_value.get_default_model = MagicMock()
        async def _gd(*args, **kwargs):
            return MagicMock()
        LLMServiceMock.return_value.get_default_model = _gd
        LLMClassMock.return_value = fake_llm

        result = await svc._classify_duplicate(
            db=MagicMock(), organization=MagicMock(), user=MagicMock(),
            new_prompt="how many users last 30d?", new_tools=["create_data"],
            candidates=[{"id": "case-123", "prompt": "users 30d", "tools": ["create_data"]}],
        )
    assert result == {
        "duplicate": True,
        "matched_id": "case-123",
        "reason": "same metric",
    }


@pytest.mark.asyncio
async def test_classify_duplicate_handles_markdown_fence():
    from app.services.completion_feedback_service import CompletionFeedbackService

    svc = CompletionFeedbackService()
    fake_llm = MagicMock()
    fake_llm.inference.return_value = (
        "```json\n"
        '{"duplicate": false, "matched_id": null, "reason": "different metric"}\n'
        "```"
    )

    with patch("app.services.completion_feedback_service.LLMService") as LLMServiceMock, \
         patch("app.ai.llm.LLM") as LLMClassMock:
        async def _gd(*args, **kwargs):
            return MagicMock()
        LLMServiceMock.return_value.get_default_model = _gd
        LLMClassMock.return_value = fake_llm

        result = await svc._classify_duplicate(
            db=MagicMock(), organization=MagicMock(), user=MagicMock(),
            new_prompt="x", new_tools=[], candidates=[{"id": "c", "prompt": "y", "tools": []}],
        )
    assert result is not None
    assert result["duplicate"] is False
    assert result["matched_id"] is None


@pytest.mark.asyncio
async def test_classify_duplicate_handles_garbage_response():
    from app.services.completion_feedback_service import CompletionFeedbackService

    svc = CompletionFeedbackService()
    fake_llm = MagicMock()
    fake_llm.inference.return_value = "I don't know, sorry"

    with patch("app.services.completion_feedback_service.LLMService") as LLMServiceMock, \
         patch("app.ai.llm.LLM") as LLMClassMock:
        async def _gd(*args, **kwargs):
            return MagicMock()
        LLMServiceMock.return_value.get_default_model = _gd
        LLMClassMock.return_value = fake_llm

        result = await svc._classify_duplicate(
            db=MagicMock(), organization=MagicMock(), user=MagicMock(),
            new_prompt="x", new_tools=[], candidates=[{"id": "c", "prompt": "y", "tools": []}],
        )
    # Garbage response → return None (caller treats None as "not a duplicate").
    assert result is None
