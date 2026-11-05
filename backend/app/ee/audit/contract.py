# Audit event contract (docs/design/audit-event-contract.md)
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# One place for the vocabulary every producer, the table, the envelope and the
# collectors share: outcomes, actor types, event categories and the rule that
# infers an outcome for call sites that predate the ``outcome`` column.

from typing import Optional

# Rows written by code that fills the contract fields carry this value in
# audit_logs.schema_version; rows written before the column existed are NULL
# and are exported as legacy (schema_version 1, outcome inferred).
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_DENIED = "denied"
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_DENIED)

ACTOR_USER = "user"
ACTOR_AGENT = "agent"
ACTOR_SYSTEM = "system"
ACTOR_SERVICE = "service"
ACTOR_UNKNOWN = "unknown"  # pre-authentication: nobody fabricated

# Prefix → category. The category is exported so a collector can route or
# alert without parsing action names.
_CATEGORY_BY_PREFIX = {
    "auth.": "authentication",
    "access.": "authorization",
    "user.": "administration",
    "member.": "administration",
    "role.": "administration",
    "organization.": "administration",
    "api_key.": "administration",
    "llm_provider.": "administration",
    "llm_model.": "administration",
    "audit_stream.": "administration",
    "audit_log.": "data_access",
    "audit.": "platform",
    "file.": "data_access",
    "query.": "data_access",
    "report.": "data_access",
    "artifact.": "data_access",
    "data_source.": "data_access",
    "connection.": "data_access",
    "tool.": "agent",
    "completion.": "agent",
    "scheduled": "background",
    "build.": "administration",
    "instruction.": "administration",
}


def category_for(action: str) -> str:
    for prefix, cat in _CATEGORY_BY_PREFIX.items():
        if action.startswith(prefix):
            return cat
    return "other"


def infer_outcome(action: str) -> str:
    """Outcome for a producer that did not state one.

    Every existing call site records an action after it happened, so the
    default is success; the two legacy shapes that record a non-success are
    recognisable from the action name. New producers pass ``outcome``
    explicitly.
    """
    if action == "access.denied":
        return OUTCOME_DENIED
    if action.endswith(("_failed", ".failed", ".error", "_error")):
        return OUTCOME_FAILURE
    return OUTCOME_SUCCESS


def normalize_outcome(outcome: Optional[str], action: str) -> str:
    if outcome in OUTCOMES:
        return outcome
    return infer_outcome(action)


def actor_type_for(user_id: Optional[str], details: Optional[dict], *, service: bool = False) -> str:
    if isinstance(details, dict) and details.get("agent_execution_id"):
        return ACTOR_AGENT
    if service:
        return ACTOR_SERVICE
    if user_id:
        return ACTOR_USER
    return ACTOR_SYSTEM


# Fields a category must carry for a record to count as complete. Checked by
# the health endpoint (malformed/missing-field counter), never enforced at
# write time: a producer with partial facts records them honestly.
REQUIRED_BY_CATEGORY = {
    "authentication": ("outcome", "ip_address", "auth_method"),
    "authorization": ("outcome", "actor", "permission"),
    "administration": ("outcome", "actor", "target"),
    "data_access": ("outcome", "actor", "target"),
    "agent": ("outcome", "actor", "execution_id"),
    "background": ("outcome",),
    "platform": ("outcome",),
}
