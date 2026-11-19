"""The entity types a policy can turn on for a detector.

Only what a pattern cannot find: the pattern-shaped entities Presidio also
knows (emails, cards, IBANs…) are already covered by the built-in rules, with
one place to configure them.
"""

from __future__ import annotations

from typing import Dict, List, Optional

DETECTOR_PREFIX = "presidio."

DETECTOR_ENTITIES: List[Dict[str, str]] = [
    {"id": "PERSON", "name": "Person name", "replacement": "[REDACTED_PERSON]"},
    {"id": "LOCATION", "name": "Location", "replacement": "[REDACTED_LOCATION]"},
    {"id": "ORGANIZATION", "name": "Organization", "replacement": "[REDACTED_ORG]"},
    {"id": "NRP", "name": "Nationality, religious or political group", "replacement": "[REDACTED_GROUP]"},
]

DEFAULT_MIN_SCORE = 0.7


def detector_entity(entity_id: str) -> Optional[Dict[str, str]]:
    for e in DETECTOR_ENTITIES:
        if e["id"] == entity_id:
            return e
    return None
