"""Expected access matrix for the MCP *builder* tools (instructions + schema).

Written before the tools existed, from the web app's own rules, so the MCP
surface is held to the same contract as the REST routes:

- Instruction write (edit / delete): ``manage_instructions`` on EVERY agent the
  instruction is attached to; a scope-less (global) instruction needs it at the
  ORG level. Same rule as ``_require_instruction_authority`` in
  ``app/routes/instruction.py``.
- Instruction scope change: the new scope needs the same authority — an agent
  manager can't widen a rule to another agent or promote it to global.
- Schema read: ``view_schema`` on the agent (implicit on any grant and on public
  agents). Readers see only the agent's active tables.
- Schema write (activate / deactivate tables): ``manage`` on the agent, the
  same gate as ``PUT /data_sources/{id}/update_tables_status``.
- An org-level ``manage_instructions`` admin manages instructions everywhere but
  does NOT get schema read/write on private agents they hold no grant on.
- Everything is org-scoped: a key from another org can't touch any of it.

The cast (personas) and targets are built by ``mcp_acl_world.build_world``.
"""

# ── Cast ────────────────────────────────────────────────────────────────
ORG_ADMIN = "org_admin"                    # full_admin_access (bootstrap owner)
INSTRUCTION_ADMIN = "instruction_admin"    # custom org role: manage_instructions only
AGENT_OWNER = "agent_owner"                # role create_data_source; created agent OWNED
AGENT_MANAGER = "agent_manager"            # resource grant `manage` on ALPHA
AGENT_INSTR_MANAGER = "agent_instruction_manager"  # grant `manage_instructions` on ALPHA
AGENT_EVALS_MANAGER = "agent_evals_manager"        # grant `manage_evals` on ALPHA
AGENT_VIEWER = "agent_viewer"              # plain member of ALPHA (view only)
PLAIN_MEMBER = "plain_member"              # org member, no grants (sees PUB only)
CONNECTION_MANAGER = "connection_manager"  # `manage_data_sources` on ALPHA's connection
GROUP_MANAGER = "group_manager"            # in group "alpha-stewards" holding `manage` on ALPHA
OUTSIDER = "outsider"                      # admin of a different org

IN_ORG = [
    ORG_ADMIN, INSTRUCTION_ADMIN, AGENT_OWNER, AGENT_MANAGER, AGENT_INSTR_MANAGER,
    AGENT_EVALS_MANAGER, AGENT_VIEWER, PLAIN_MEMBER, CONNECTION_MANAGER, GROUP_MANAGER,
]
ALL = IN_ORG + [OUTSIDER]

# Who holds `manage` (agent-manager tier) on ALPHA, by any route.
ALPHA_MANAGERS = {AGENT_MANAGER, CONNECTION_MANAGER, GROUP_MANAGER}
# Who holds `manage_instructions` on ALPHA.
ALPHA_INSTRUCTION_WRITERS = ALPHA_MANAGERS | {AGENT_INSTR_MANAGER}
# Who can see ALPHA at all.
ALPHA_VIEWERS = ALPHA_INSTRUCTION_WRITERS | {AGENT_EVALS_MANAGER, AGENT_VIEWER}
ORG_INSTRUCTION_ADMINS = {ORG_ADMIN, INSTRUCTION_ADMIN}

# ── Tool catalog visibility (tools/list) ────────────────────────────────
# A builder tool is listed only for someone who could use it somewhere.
INSTRUCTION_WRITE_TOOLS = ("create_instruction", "edit_instruction", "delete_instruction")
SCHEMA_WRITE_TOOLS = ("edit_schema",)
READ_TOOLS = ("list_instructions", "read_instruction", "describe_tables")

TOOL_VISIBLE_TO = {
    **{t: ORG_INSTRUCTION_ADMINS | ALPHA_INSTRUCTION_WRITERS | {AGENT_OWNER}
       for t in INSTRUCTION_WRITE_TOOLS},
    **{t: {ORG_ADMIN, AGENT_OWNER} | ALPHA_MANAGERS for t in SCHEMA_WRITE_TOOLS},
    **{t: set(IN_ORG) for t in READ_TOOLS},
}

# ── Instruction targets ─────────────────────────────────────────────────
# key → which agents it's attached to (resolved by the world builder)
INSTRUCTION_TARGETS = {
    "inst_alpha": ["ALPHA"],
    "inst_beta": ["BETA"],
    "inst_pub": ["PUB"],
    "inst_owned": ["OWNED"],
    "inst_multi": ["ALPHA", "BETA"],   # authority needed on EVERY attached agent
    "inst_global": [],                 # org-wide: org-level authority only
}

# Edit / delete of a published instruction.
INSTRUCTION_WRITE_ALLOWED = {
    "inst_alpha": ORG_INSTRUCTION_ADMINS | ALPHA_INSTRUCTION_WRITERS,
    "inst_beta": ORG_INSTRUCTION_ADMINS,
    "inst_pub": ORG_INSTRUCTION_ADMINS,
    "inst_owned": ORG_INSTRUCTION_ADMINS | {AGENT_OWNER},
    "inst_multi": ORG_INSTRUCTION_ADMINS,
    "inst_global": ORG_INSTRUCTION_ADMINS,
}

# Read of a published instruction (must agree with REST GET /instructions/{id}).
# Public-agent and global rules are readable by every member; agent rules by
# whoever can see the agent.
INSTRUCTION_READ_MUST_ALLOW = {
    "inst_alpha": {ORG_ADMIN} | ALPHA_VIEWERS,
    "inst_pub": set(IN_ORG),
    "inst_global": set(IN_ORG),
    "inst_owned": {AGENT_OWNER},
}
INSTRUCTION_READ_MUST_DENY = {
    "inst_alpha": {PLAIN_MEMBER, AGENT_OWNER, OUTSIDER},
    "inst_beta": {PLAIN_MEMBER, AGENT_OWNER, OUTSIDER} | ALPHA_VIEWERS,
    "inst_owned": {PLAIN_MEMBER, OUTSIDER} | ALPHA_VIEWERS,
    "inst_pub": {OUTSIDER},
    "inst_global": {OUTSIDER},
}

# Scope changes made through edit_instruction(data_source_ids=...):
# (persona, instruction, new scope) → allowed?
SCOPE_CHANGES = [
    (AGENT_MANAGER, "inst_alpha", [], False),               # promote to global
    (AGENT_MANAGER, "inst_alpha", ["ALPHA", "BETA"], False),  # widen to unmanaged agent
    (AGENT_INSTR_MANAGER, "inst_alpha", ["BETA"], False),   # move to unmanaged agent
    (AGENT_OWNER, "inst_owned", ["OWNED", "ALPHA"], False),
    (AGENT_OWNER, "inst_owned", ["OWNED"], True),           # no-op re-scope is fine
    (INSTRUCTION_ADMIN, "inst_alpha", ["ALPHA", "BETA"], True),
    (ORG_ADMIN, "inst_beta", [], True),
]

# create_instruction: scope → (personas that may create, personas whose
# creation must go live immediately). Everyone else is refused.
CREATE_ALLOWED = {
    ("ALPHA",): (ORG_INSTRUCTION_ADMINS | ALPHA_INSTRUCTION_WRITERS,
                 ORG_INSTRUCTION_ADMINS | ALPHA_INSTRUCTION_WRITERS),
    ("BETA",): (ORG_INSTRUCTION_ADMINS, ORG_INSTRUCTION_ADMINS),
    ("OWNED",): (ORG_INSTRUCTION_ADMINS | {AGENT_OWNER}, ORG_INSTRUCTION_ADMINS | {AGENT_OWNER}),
    # Global: org admins publish; agent-level writers may only *suggest*
    # (lands pending admin review, never live).
    (): (ORG_INSTRUCTION_ADMINS | ALPHA_INSTRUCTION_WRITERS | {AGENT_OWNER},
         ORG_INSTRUCTION_ADMINS),
}

# ── Schema targets ──────────────────────────────────────────────────────
SCHEMA_READ_ALLOWED = {
    "ALPHA": {ORG_ADMIN} | ALPHA_VIEWERS,
    "BETA": {ORG_ADMIN},
    "PUB": set(IN_ORG),
    "OWNED": {ORG_ADMIN, AGENT_OWNER},
}
SCHEMA_WRITE_ALLOWED = {
    "ALPHA": {ORG_ADMIN} | ALPHA_MANAGERS,
    "BETA": {ORG_ADMIN},
    "PUB": {ORG_ADMIN},
    "OWNED": {ORG_ADMIN, AGENT_OWNER},
}
# Who sees tables an agent manager deselected (to everyone else the agent IS
# its active tables).
SEES_INACTIVE_TABLES = {
    "ALPHA": {ORG_ADMIN} | ALPHA_MANAGERS,
}
