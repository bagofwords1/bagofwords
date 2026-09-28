"""Reasoning parameters for OpenAI-shaped Chat Completions requests.

Shared by the OpenAI-compatible and Azure Chat Completions clients so both
translate the requested effort the same way:

* clamp it to what the model accepts (Chat Completions has no "max"),
* merge the admin's raw per-level request fields on top,
* and, when the endpoint rejects function tools combined with a reasoning
  effort ("Function tools with reasoning_effort are not supported for <model>
  in /v1/chat/completions"), retry once with "none" so the run keeps working.
  Which models do this differs between OpenAI and Azure deployments of the
  same model (verified live), so the retry reacts to the error instead of
  guessing.
"""
import logging
from typing import Any, Optional, Sequence

from app.ai.llm.reasoning import (
    chat_completions_efforts,
    clamp_effort,
    client_mode,
    client_reasons,
    efforts_for_client,
    merge_raw_params,
    raw_params_for,
    selected_effort,
)

logger = logging.getLogger(__name__)

_TOOLS_EFFORT_REJECTED = "function tools with reasoning_effort"


def apply_chat_reasoning(client, model_id: str, request_kwargs: dict[str, Any], thinking: Optional[dict]) -> Optional[Sequence[str]]:
    """Set reasoning_effort / raw fields on a Chat Completions request.

    Returns the accepted efforts (for the tools retry), or None when unknown.
    """
    if not client_reasons(client, model_id):
        return None
    efforts = chat_completions_efforts(efforts_for_client(client, model_id))
    requested = selected_effort(thinking)
    effort = clamp_effort(requested, efforts)
    if effort and client_mode(client) != "custom":
        request_kwargs["reasoning_effort"] = effort
    request_kwargs.pop("temperature", None)
    if requested:
        merge_raw_params(request_kwargs, raw_params_for(client, requested, effort))
    return efforts


async def create_chat_stream(async_client, request_kwargs: dict[str, Any], efforts: Optional[Sequence[str]]):
    try:
        return await async_client.chat.completions.create(**request_kwargs)
    except Exception as exc:  # openai.BadRequestError; kept generic for proxies/SDK versions
        can_retry = (
            request_kwargs.get("tools")
            and _TOOLS_EFFORT_REJECTED in str(exc).lower()
            and request_kwargs.get("reasoning_effort") != "none"
            and (efforts is None or "none" in efforts or not efforts)
        )
        if not can_retry:
            raise
        logger.warning(
            "model=%s rejected reasoning_effort=%s with tools on Chat Completions; "
            "retrying with 'none'. Use the Responses API to honor the effort.",
            request_kwargs.get("model"), request_kwargs.get("reasoning_effort"),
        )
        retry = dict(request_kwargs)
        retry["reasoning_effort"] = "none"
        return await async_client.chat.completions.create(**retry)
