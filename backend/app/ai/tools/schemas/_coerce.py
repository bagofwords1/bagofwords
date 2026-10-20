"""Argument coercions shared by the data tools' input models.

Small planner models sometimes hand a list-typed argument over as the JSON
text of that list (``"[{\\"data_source_id\\": ...}]"`` instead of the list).
Pydantic then refuses it with "Input should be a valid list", the model
retries with the same shape, and a sub-agent on the investigate profile
ends its run blocked without ever querying (seen live on Haiku 5.5,
docs/feedback-loops/sub-agents.md). Parsing that one shape is cheap and
unambiguous; anything else still goes through normal validation.
"""
import json
from typing import Any


def coerce_json_list(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return value
            if isinstance(parsed, list):
                return parsed
    return value
