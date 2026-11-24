"""PII policies per agent: the policy follows the agents a call involves.

Design: ``docs/design/pii-agent-policies.md``. The promises, each checked
through public surfaces (the policy config functions and the compiled
redactor), never private helpers:

* **Strictest wins.** An LLM call is redacted with the merge of the
  provider's policy and the policy of every agent in the call's scope. The
  merge applies to the whole prompt: one agent's policy also covers the other
  agents' data in the same chat.
* **Only tighter.** For every input policy, whatever it hides the merge hides
  too, with an action at least as strong, and whatever it blocks the merge
  blocks too. An agent policy overrides an Exempt provider.
* **Default agent policy.** Agents without their own assignment use the org's
  ``default_agent_policy``. ``none`` (explicit on an agent) means "no agent
  policy" and only the provider's policy applies.
* **Unknown scope fails closed.** A call that doesn't say which agents it
  involves gets every agent policy the org could apply.
* **Unchanged for everyone else.** An org with no agent assignments and no
  default agent policy redacts exactly as before, whatever the scope.
"""

import random

import pytest

from app.ai.llm.pii import policy as pol
from app.ai.llm.pii.redactor import VALID_ACTIONS, PiiPromptBlockedError
from app.ai.llm.pii.vault import PiiVault
from app.schemas.organization_settings_schema import PiiProtectionConfig

EMAIL = "jane.roe@contoso.com"
PHONE = "415-555-0134"
TEXT = f"Contact {EMAIL} or call {PHONE} about the Q3 numbers."

ALL_BUILTINS_OFF = {rid: {"enabled": False} for rid in pol.BUILTIN_RULE_IDS}


def _policy(name, **rules):
    """A policy with every built-in rule off except those given as
    ``rule_id=action``."""
    overrides = dict(ALL_BUILTINS_OFF)
    for rid, action in rules.items():
        overrides[rid] = {"enabled": True, "action": action}
    return {"name": name, "mode": "replace", "builtin_overrides": overrides}


POLICIES = {
    "default": _policy("Default"),
    "emails": _policy("Emails", email="replace"),
    "phones": _policy("Phones", phone="replace"),
    "emails_block": _policy("Emails (block)", email="block"),
    "emails_mask": _policy("Emails (mask)", email="mask"),
    "emails_pseudo": _policy("Emails (pseudonymize)", email="pseudonymize"),
}

A, B, C = "agent-a", "agent-b", "agent-c"
PROV, EXEMPT_PROV = "prov-public", "prov-vpc"
UNKNOWN = None  # the call carries no agent scope


def _config(*, agents=None, default_agent="none", provider=None, enabled=True):
    provider_policies = {EXEMPT_PROV: "exempt"}
    if provider:
        provider_policies[PROV] = provider
    return pol.compile_config({
        "enabled": enabled,
        "policies": POLICIES,
        "provider_policies": provider_policies,
        "agent_policies": agents or {},
        "default_agent_policy": default_agent,
    })


def _send(compiled, provider, scope, text=TEXT):
    """What the provider receives: ``("sent", text)`` or ``("blocked", rules)``."""
    redactor = compiled.for_call(provider, scope)
    if redactor is None:
        return "sent", text
    try:
        out, _ = redactor.apply(text, PiiVault.ephemeral() if redactor.uses_vault else None)
        return "sent", out
    except PiiPromptBlockedError as e:
        return "blocked", sorted(e.rule_names)


RAW = "raw"
EMAIL_HIDDEN = "email hidden"
PHONE_HIDDEN = "phone hidden"
BOTH_HIDDEN = "both hidden"
BLOCKED = "blocked"


def _outcome(result):
    kind, payload = result
    if kind == "blocked":
        return BLOCKED
    email_raw, phone_raw = EMAIL in payload, PHONE in payload
    if email_raw and phone_raw:
        return RAW
    if not email_raw and not phone_raw:
        return BOTH_HIDDEN
    return EMAIL_HIDDEN if not email_raw else PHONE_HIDDEN


# --- the permutation matrix -------------------------------------------------
# (id, config kwargs, provider, agents in scope, expected outcome)
MATRIX = [
    # one agent with a policy covers the whole chat
    ("A-pii + B-none -> pii", dict(agents={A: "emails"}), PROV, [A, B], EMAIL_HIDDEN),
    ("A-pii alone -> pii", dict(agents={A: "emails"}), PROV, [A], EMAIL_HIDDEN),
    ("B-none alone -> raw", dict(agents={A: "emails"}), PROV, [B], RAW),
    ("no agents in chat -> raw", dict(agents={A: "emails"}), PROV, [], RAW),
    ("order of agents does not matter", dict(agents={A: "emails"}), PROV, [B, A], EMAIL_HIDDEN),
    # both agents have policies: union of rules
    ("A-emails + B-phones -> both", dict(agents={A: "emails", B: "phones"}), PROV, [A, B], BOTH_HIDDEN),
    ("A-emails + B-emails -> emails", dict(agents={A: "emails", B: "emails"}), PROV, [A, B], EMAIL_HIDDEN),
    ("three agents, one policy each", dict(agents={A: "emails", B: "phones", C: "default"}), PROV, [A, B, C], BOTH_HIDDEN),
    # same rule, different actions: strongest wins
    ("A-block + B-replace -> blocked", dict(agents={A: "emails_block", B: "emails"}), PROV, [A, B], BLOCKED),
    ("A-block + B-none -> blocked", dict(agents={A: "emails_block"}), PROV, [A, B], BLOCKED),
    ("A-mask + B-replace -> replaced", dict(agents={A: "emails_mask", B: "emails"}), PROV, [A, B], EMAIL_HIDDEN),
    ("A-pseudo + B-replace -> replaced", dict(agents={A: "emails_pseudo", B: "emails"}), PROV, [A, B], EMAIL_HIDDEN),
    # the provider's policy is part of the merge
    ("provider emails + A-phones -> both", dict(agents={A: "phones"}, provider="emails"), PROV, [A], BOTH_HIDDEN),
    ("provider emails + no agent policy -> emails", dict(provider="emails"), PROV, [A], EMAIL_HIDDEN),
    ("provider block + A-replace -> blocked", dict(agents={A: "emails"}, provider="emails_block"), PROV, [A], BLOCKED),
    ("provider replace + A-block -> blocked", dict(agents={A: "emails_block"}, provider="emails"), PROV, [A], BLOCKED),
    # an agent policy overrides an Exempt provider
    ("exempt provider + A-pii -> pii", dict(agents={A: "emails"}), EXEMPT_PROV, [A], EMAIL_HIDDEN),
    ("exempt provider + A-pii + B -> pii", dict(agents={A: "emails"}), EXEMPT_PROV, [A, B], EMAIL_HIDDEN),
    ("exempt provider + B-none -> raw", dict(agents={A: "emails"}), EXEMPT_PROV, [B], RAW),
    # default agent policy
    ("default agent pii, A unassigned -> pii", dict(default_agent="emails"), PROV, [A], EMAIL_HIDDEN),
    ("default agent pii, no agents -> raw", dict(default_agent="emails"), PROV, [], RAW),
    ("default agent pii, A explicit none -> raw", dict(default_agent="emails", agents={A: "none"}), PROV, [A], RAW),
    ("default agent pii, A none + B inherits -> pii", dict(default_agent="emails", agents={A: "none"}), PROV, [A, B], EMAIL_HIDDEN),
    ("default emails, A explicit phones -> phones only", dict(default_agent="emails", agents={A: "phones"}), PROV, [A], PHONE_HIDDEN),
    ("default emails, A phones + B inherits -> both", dict(default_agent="emails", agents={A: "phones"}), PROV, [A, B], BOTH_HIDDEN),
    ("default agent pii + exempt provider -> pii", dict(default_agent="emails"), EXEMPT_PROV, [A], EMAIL_HIDDEN),
    # unknown scope fails closed
    ("unknown scope, A has pii -> pii", dict(agents={A: "emails"}), PROV, UNKNOWN, EMAIL_HIDDEN),
    ("unknown scope, A emails + B phones -> both", dict(agents={A: "emails", B: "phones"}), PROV, UNKNOWN, BOTH_HIDDEN),
    ("unknown scope, default agent pii -> pii", dict(default_agent="emails"), PROV, UNKNOWN, EMAIL_HIDDEN),
    ("unknown scope, exempt provider, A pii -> pii", dict(agents={A: "emails"}), EXEMPT_PROV, UNKNOWN, EMAIL_HIDDEN),
    ("unknown scope, no agent policies -> provider only", dict(provider="emails"), PROV, UNKNOWN, EMAIL_HIDDEN),
    ("unknown scope, nothing configured -> raw", dict(), PROV, UNKNOWN, RAW),
    ("unknown scope, all agents none -> raw", dict(agents={A: "none"}), PROV, UNKNOWN, RAW),
    # org switch off wins over everything
    ("PII off, A pii -> raw", dict(agents={A: "emails_block"}, enabled=False), PROV, [A], RAW),
    ("PII off, unknown scope -> raw", dict(default_agent="emails", enabled=False), PROV, UNKNOWN, RAW),
]


@pytest.mark.parametrize(
    "cfg,provider,scope,expected",
    [m[1:] for m in MATRIX],
    ids=[m[0] for m in MATRIX],
)
def test_permutation_matrix(cfg, provider, scope, expected):
    assert _outcome(_send(_config(**cfg), provider, scope)) == expected


def test_strongest_action_is_the_one_applied():
    """Not only hidden: hidden the strongest way. mask < pseudonymize < replace."""
    compiled = _config(agents={A: "emails_mask", B: "emails_pseudo"})
    kind, out = _send(compiled, PROV, [A, B])
    assert kind == "sent" and EMAIL not in out and "<" in out and "****" not in out  # pseudonymized, not masked
    kind, out = _send(_config(agents={A: "emails_pseudo", B: "emails"}), PROV, [A, B])
    assert kind == "sent" and "[REDACTED_EMAIL]" in out


def test_blocked_call_names_the_merged_policy():
    compiled = _config(agents={A: "emails_block", B: "phones"}, provider="emails")
    with pytest.raises(PiiPromptBlockedError) as err:
        compiled.for_call(PROV, [A, B]).apply(TEXT)
    for name in ("Emails", "Emails (block)", "Phones"):
        assert name in err.value.policy_name


def test_merged_policy_is_cached_per_policy_set():
    compiled = _config(agents={A: "emails", B: "phones", C: "phones"})
    assert compiled.for_call(PROV, [A, B]) is compiled.for_call(PROV, [B, A])
    # C uses the same policy as B: same policy set, same redactor
    assert compiled.for_call(PROV, [A, C]) is compiled.for_call(PROV, [A, B])


def test_single_policy_reuses_the_policy_itself():
    """No merge when only one policy applies: the call gets that policy's own
    redactor, so a single-policy call is exactly today's behavior."""
    compiled = _config(agents={A: "emails"})
    assert compiled.for_call(PROV, [A]) is compiled.policies["emails"]
    assert compiled.for_call(PROV, [B]) is compiled.for_provider(PROV)


# --- no agent configuration: identical to provider-only ---------------------

@pytest.mark.parametrize("scope", [[], [A], [A, B], UNKNOWN], ids=["none", "one", "two", "unknown"])
@pytest.mark.parametrize("provider_policy", [None, "emails", "emails_block", "phones"])
def test_without_agent_config_scope_changes_nothing(scope, provider_policy):
    compiled = _config(provider=provider_policy)
    assert _send(compiled, PROV, scope) == _send(compiled, PROV, [])
    assert compiled.for_call(PROV, scope) is compiled.for_provider(PROV)


# --- merge invariant: never weaker than any input ---------------------------

STRENGTH = {"mask": 1, "pseudonymize": 2, "replace": 3, "block": 4}


def _rule_states(cfg, policy_id):
    """rule id -> action for every built-in rule the policy turns on."""
    p = cfg["policies"][policy_id]
    out = {}
    for rid in pol.BUILTIN_RULE_IDS:
        ov = p["builtin_overrides"].get(rid, {})
        if ov.get("enabled", True):
            out[rid] = ov.get("action") or p["mode"]
    return out


@pytest.mark.parametrize("seed", range(40))
def test_merge_is_never_weaker_than_any_input(seed):
    rng = random.Random(seed)
    rule_ids = sorted(pol.BUILTIN_RULE_IDS)
    policies = {"default": {"name": "Default", "mode": rng.choice(["replace", "block"])}}
    for i in range(rng.randint(2, 4)):
        policies[f"p{i}"] = {
            "name": f"P{i}",
            "mode": rng.choice(["replace", "block"]),
            "redact_context": rng.random() < 0.3,
            "on_detector_error": rng.choice(["block", "regex_only"]),
            "builtin_overrides": {
                rid: {"enabled": rng.random() < 0.5, "action": rng.choice(VALID_ACTIONS)} for rid in rule_ids
            },
        }
    cfg = pol.normalize({"enabled": True, "policies": policies})
    ids = list(cfg["policies"])
    rng.shuffle(ids)
    chosen = ids[: rng.randint(2, len(ids))]
    merged = pol.merge_policies(cfg, chosen)
    merged_cfg = {**cfg, "policies": {**cfg["policies"], "__merged__": merged}}
    merged_states = _rule_states(merged_cfg, "__merged__")
    for pid in chosen:
        for rid, action in _rule_states(cfg, pid).items():
            assert rid in merged_states, f"{rid} on in {pid} but off in merge"
            assert STRENGTH[merged_states[rid]] >= STRENGTH[action]
        if cfg["policies"][pid]["redact_context"]:
            assert merged["redact_context"] is True
        if cfg["policies"][pid]["on_detector_error"] == "block":
            assert merged["on_detector_error"] == "block"
    # and never stricter than needed: a rule no input turns on stays off
    for rid in rule_ids:
        if not any(rid in _rule_states(cfg, pid) for pid in chosen):
            assert rid not in merged_states


def test_merge_keeps_the_lowest_detector_threshold():
    cfg = pol.normalize({"enabled": True, "policies": {
        "default": {"name": "Default"},
        "a": {"name": "A", "detector_overrides": {"PERSON": {"enabled": True, "min_score": 0.8}}},
        "b": {"name": "B", "detector_overrides": {"PERSON": {"enabled": True, "min_score": 0.4}}},
        "c": {"name": "C"},
    }})
    merged = pol.merge_policies(cfg, ["a", "b", "c"])
    assert merged["detector_overrides"]["PERSON"]["enabled"] is True
    assert merged["detector_overrides"]["PERSON"]["min_score"] == 0.4


# --- config: normalize, validate, save, audit -------------------------------

def test_normalize_drops_bad_agent_assignments():
    cfg = pol.normalize({
        "enabled": True,
        "policies": POLICIES,
        "agent_policies": {A: "emails", B: "exempt", C: "deleted-policy", "": "emails", "d": 7, "e": "none"},
        "default_agent_policy": "deleted-policy",
    })
    assert cfg["agent_policies"] == {A: "emails", "e": "none"}
    assert cfg["default_agent_policy"] == "none"


def test_deleting_a_policy_returns_its_agents_to_the_default():
    stored = pol.normalize({"enabled": True, "policies": POLICIES, "agent_policies": {A: "phones", B: "emails"},
                            "default_agent_policy": "phones"})
    remaining = {k: v for k, v in POLICIES.items() if k != "phones"}
    after = pol.apply_update(stored, {"policies": remaining})
    assert after["agent_policies"] == {B: "emails"}
    assert after["default_agent_policy"] == "none"


def test_old_configs_read_unchanged():
    cfg = pol.normalize({"enabled": True, "policies": POLICIES})
    assert cfg["agent_policies"] == {}
    assert cfg["default_agent_policy"] == "none"


@pytest.mark.parametrize("update", [
    {"agent_policies": {A: "exempt"}},
    {"agent_policies": {A: "nope"}},
    {"agent_policies": [A]},
    {"agent_policies": {A: 3}},
    {"default_agent_policy": "exempt"},
    {"default_agent_policy": "nope"},
    {"default_agent_policy": 3},
])
def test_invalid_agent_writes_are_rejected(update):
    with pytest.raises(pol.PolicyValidationError):
        pol.validate_update(update, {"enabled": True, "policies": POLICIES})


def test_none_is_reserved_as_a_policy_id():
    with pytest.raises(pol.PolicyValidationError):
        pol.validate_update({"policies": {**POLICIES, "none": {"name": "None"}}}, {})


def test_agent_settings_round_trip_through_the_schema():
    stored = PiiProtectionConfig(**{
        "enabled": True, "policies": POLICIES,
        "agent_policies": {A: "emails", B: "none"}, "default_agent_policy": "phones",
    }).model_dump()
    assert stored["agent_policies"] == {A: "emails", B: "none"}
    assert stored["default_agent_policy"] == "phones"
    again = pol.normalize(stored)
    assert again["agent_policies"] == stored["agent_policies"]


def test_agent_changes_are_audited():
    old = pol.normalize({"enabled": True, "policies": POLICIES, "agent_policies": {A: "emails"}})
    new = pol.normalize({"enabled": True, "policies": POLICIES, "agent_policies": {A: "phones", B: "none"},
                         "default_agent_policy": "emails"})
    entries = {e["action"]: e["details"] for e in pol.diff_for_audit(old, new)}
    changes = entries["pii_protection.agent_policies_updated"]["changes"]
    assert {"agent_id": A, "from": "emails", "to": "phones"} in changes
    assert {"agent_id": B, "from": "inherit", "to": "none"} in changes
    assert entries["pii_protection.default_agent_policy_updated"] == {"from": "none", "to": "emails"}
