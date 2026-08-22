"""ParamSpec — the single declaration shape for query parameters.

One schema, used everywhere a parameter appears: Query.parameters storage,
create_data tool input, /queries/{id}/run request validation, and the
artifact payload. Params are always scalar-ish values (string/number/date/
date_range/id/list) — never dataframes; composition stays in code via
load_step/load_entity.

Three source modes with different trust semantics:
  - input: viewer-editable. Value comes from the request.
  - identity: LOCKED to the viewer ("identity-strict"). The server resolves
    the value from the session identity via the binding expression; any
    client-supplied value is rejected. Only the audited "View as" flow may
    substitute another member's identity.
  - input_identity_default: an ordinary input param whose default is the
    viewer's identity value — opens on "you", overridable by anyone with
    access. It never claims to be access control.
"""
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

ParamType = Literal["string", "number", "date", "date_range", "id", "list"]
ParamSource = Literal["input", "identity", "input_identity_default"]

# Identity binding expressions an identity-sourced param may use. Resolved
# server-side via rls_identity_service.resolve_identity — never client input.
IDENTITY_BINDINGS = (
    "viewer.email",
    "viewer.user_id",
    "viewer.name",
    "viewer.groups",
)
IDENTITY_ATTR_PREFIX = "viewer.profile_attributes."

_NAME_RE = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"


class ParamOptionsSource(BaseModel):
    """Another query's column as this param's option list (filter space).

    The choices come from that query's current default-step result; the bound
    value is still a scalar and is validated server-side regardless of what
    the UI showed.
    """
    query_id: str
    value_column: str
    label_column: Optional[str] = None


class ParamSpec(BaseModel):
    name: str = Field(..., pattern=_NAME_RE)
    type: ParamType = "string"
    label: Optional[str] = None
    description: Optional[str] = None
    # None default + required=False → the "All" path: code sees params.get(name) is None
    # and skips the predicate.
    default: Optional[Any] = None
    required: bool = False
    source: ParamSource = "input"
    # Identity binding (identity / input_identity_default only), e.g.
    # "viewer.email", "viewer.profile_attributes.department", "viewer.groups".
    identity_binding: Optional[str] = None
    options: Optional[List[Any]] = None
    options_source: Optional[ParamOptionsSource] = None
    # When true, submitted values must be in options/options_source values.
    strict_options: bool = False

    @field_validator("identity_binding")
    @classmethod
    def _validate_binding(cls, v):
        if v is None:
            return v
        if v in IDENTITY_BINDINGS or v.startswith(IDENTITY_ATTR_PREFIX):
            return v
        raise ValueError(
            f"identity_binding must be one of {IDENTITY_BINDINGS} or start with '{IDENTITY_ATTR_PREFIX}'"
        )

    def model_post_init(self, __context) -> None:
        if self.source in ("identity", "input_identity_default") and not self.identity_binding:
            # Sensible default: bind the viewer's email.
            self.identity_binding = "viewer.email"


def parse_param_specs(raw: Optional[list]) -> List[ParamSpec]:
    """Parse the JSON list stored on Query.parameters, skipping garbage rows
    rather than failing the whole query read."""
    specs: List[ParamSpec] = []
    for item in raw or []:
        try:
            specs.append(ParamSpec.model_validate(item))
        except Exception:
            continue
    return specs
