"""Failure text sent back to a chat channel when a run errors.

When an agent run triggered from Slack / WhatsApp / Google Chat fails (an LLM
provider error, a quota block, a context overflow, a crash), the user who asked
is otherwise left with a silent 👀 reaction forever. The channel delivery hook
sends them the SAME failure text BOW shows in its own web chat on an LLM error
(``frontend/pages/reports/[id]/index.vue``): the classifier's ``summary`` joined
to the provider's raw ``provider_message`` (``app/ai/llm/errors.py``), deduped.

Parity with the product is deliberate: the web and the channel show the same
words, and the provider's own text is always accurate — even for an error the
classifier itself could only mark ``unknown`` (e.g. a broken key rejected
locally as an illegal header, which surfaces as "Connection error."). A friendly
localized bucket would risk mislabeling that as a transient network blip and
telling the user "try again in a moment", which is wrong. So we show the raw
text, exactly like the web, rather than paraphrasing it.
"""
from __future__ import annotations

from typing import Optional

# A run can fail with no classified error at all (the background recovery path
# stores a bare ``{'error': True}``) and no content — then the channel still
# needs to say *something* rather than nothing.
_FALLBACK = "The run failed while answering your question. Please try again."


def bow_error_message(err, content: Optional[str] = None) -> str:
    """The channel failure line, mirroring BOW's web display of an LLM error.

    ``err`` is ``completion.completion['error']`` — the classified dict, or the
    background path's bare ``True``. Formats ``{summary}: {provider_message}``,
    collapsing to a single part when they're equal or one is missing, exactly as
    the web does. Falls back to the completion's ``content`` and then a minimal
    line, so the channel always hears something.
    """
    summary = ""
    provider_message = ""
    if isinstance(err, dict):
        summary = (err.get("summary") or "").strip()
        provider_message = (
            err.get("provider_message")
            or err.get("raw_tail")
            or err.get("message")
            or ""
        ).strip()

    if provider_message:
        if summary and summary != provider_message:
            return f"{summary}: {provider_message}"
        return provider_message
    if summary:
        return summary
    return (content or "").strip() or _FALLBACK
