"""Built-in PII detection rules shipped with Bag of Words.

Each rule is a logical entity (email, credit card, ...) with *multiple* regex
patterns so the different real-world shapes of the same entity match under one
enable switch and one replacement token. These live in code (not the DB) so the
patterns can be improved without a migration; an organization's settings only
store per-rule overrides (enable/replacement) keyed by ``id``.

Keep patterns conservative — a false positive here silently rewrites a prompt,
which can break generated SQL/code. Prefer precise, well-anchored patterns over
greedy ones.
"""

from __future__ import annotations

from typing import Any, Dict, List


# NOTE: patterns are plain strings compiled with re.IGNORECASE by the redactor.
# Order matters only for display; detection is order-independent (any match wins).
BUILTIN_PII_RULES: List[Dict[str, Any]] = [
    {
        "id": "email",
        "name": "Email address",
        "replacement": "[REDACTED_EMAIL]",
        "patterns": [
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        ],
    },
    {
        "id": "credit_card",
        "name": "Credit card number",
        "replacement": "[REDACTED_CC]",
        "patterns": [
            # 16-digit (Visa/MC/Discover) with optional space/dash separators
            r"\b(?:\d[ -]*?){13,16}\b",
            # Amex 15-digit 4-6-5 grouping
            r"\b3[47]\d{2}[ -]?\d{6}[ -]?\d{5}\b",
        ],
    },
    {
        "id": "us_ssn",
        "name": "US Social Security Number",
        "replacement": "[REDACTED_SSN]",
        "patterns": [
            r"\b\d{3}-\d{2}-\d{4}\b",
            r"\b\d{3}\s\d{2}\s\d{4}\b",
        ],
    },
    {
        "id": "phone",
        "name": "Phone number",
        "replacement": "[REDACTED_PHONE]",
        "patterns": [
            # North American: (123) 456-7890, 123-456-7890, 123.456.7890
            r"\b(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]?\d{3}[ .\-]?\d{4}\b",
            # International E.164-ish: +NN NNNNNNN...
            r"\+\d{1,3}[ .\-]?\(?\d{1,4}\)?(?:[ .\-]?\d{2,4}){2,4}",
        ],
    },
    {
        "id": "ipv4",
        "name": "IP address (IPv4)",
        "replacement": "[REDACTED_IP]",
        "patterns": [
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
        ],
    },
    {
        "id": "iban",
        "name": "IBAN (bank account)",
        "replacement": "[REDACTED_IBAN]",
        "patterns": [
            r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b",
        ],
    },
    {
        "id": "aws_access_key",
        "name": "AWS access key ID",
        "replacement": "[REDACTED_AWS_KEY]",
        "patterns": [
            r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b",
        ],
    },
]


def builtin_rule_ids() -> List[str]:
    return [r["id"] for r in BUILTIN_PII_RULES]
