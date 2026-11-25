"""Built-in PII detection rules shipped with BOW.

Each rule is a logical entity (email, credit card, ...) with *multiple* regex
patterns so the different real-world shapes of the same entity match under one
enable switch and one replacement token. These live in code (not the DB) so the
patterns can be improved without a migration; an organization's settings only
store per-rule overrides (enable/replacement) keyed by ``id``.

Optional keys: ``enabled_by_default`` (False for rules added after policies
existed: they are off until turned on) and ``checksum`` / ``context`` (see
``checks.py``).

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
    # ---- Added later: OFF until a policy turns them on (``enabled_by_default``),
    # so existing policies never start redacting more on an upgrade.
    {
        "id": "ipv6",
        "name": "IP address (IPv6)",
        "replacement": "[REDACTED_IP]",
        "enabled_by_default": False,
        "patterns": [
            # Full form, and compressed forms with "::" (at least one group on a side).
            r"(?<![:\w])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![:\w])",
            r"(?<![:\w])(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}:){0,5}[0-9a-f]{1,4}(?![:\w])",
            r"(?<![:\w])(?:[0-9a-f]{1,4}:){1,7}:(?![:\w])",
            r"(?<![:\w])::(?:[0-9a-f]{1,4}:){0,6}[0-9a-f]{1,4}(?![:\w])",
        ],
    },
    {
        "id": "mac_address",
        "name": "MAC address",
        "replacement": "[REDACTED_MAC]",
        "enabled_by_default": False,
        "patterns": [
            r"\b(?:[0-9a-f]{2}([:-]))(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}\b",
            r"\b[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\b",
        ],
    },
    {
        "id": "il_id",
        "name": "Israeli ID number (Teudat Zehut)",
        "replacement": "[REDACTED_ID]",
        "enabled_by_default": False,
        # 9 digits passing the ID check digit (about 1 in 10 random 9-digit numbers do).
        "patterns": [r"(?<![\d-])\d{9}(?![\d-])"],
        "checksum": "il_id",
    },
    {
        "id": "jwt",
        "name": "JSON Web Token (JWT)",
        "replacement": "[REDACTED_TOKEN]",
        "enabled_by_default": False,
        "patterns": [r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{10,}"],
    },
    {
        "id": "private_key",
        "name": "Private key (PEM)",
        "replacement": "[REDACTED_PRIVATE_KEY]",
        "enabled_by_default": False,
        "patterns": [
            r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]{1,10000}?-----END (?:[A-Z]+ )*PRIVATE KEY-----",
        ],
    },
    {
        "id": "api_key",
        "name": "API key / token (GitHub, Slack, Stripe, Google, OpenAI, Anthropic)",
        "replacement": "[REDACTED_API_KEY]",
        "enabled_by_default": False,
        "patterns": [
            r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
            r"\bgithub_pat_[A-Za-z0-9_]{22,}\b",
            r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
            r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b",
            r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])",
            r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}",
        ],
    },
    {
        "id": "url_credentials",
        "name": "Password in a URL",
        "replacement": "[REDACTED_URL_CREDENTIALS]",
        "enabled_by_default": False,
        # scheme://user:password@ — the host and path after it stay.
        "patterns": [r"\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@]{1,200}:[^\s@/]{1,200}@"],
    },
]


def builtin_rule_ids() -> List[str]:
    return [r["id"] for r in BUILTIN_PII_RULES]
