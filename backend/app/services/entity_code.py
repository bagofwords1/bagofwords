"""A saved query's code, independent of the agent it runs on.

Agent-generated code reaches a data source through
``ds_clients["<agent name>:<connection name>"]`` (see
``DataSourceService.construct_clients``) — a key that names ONE agent. A saved
query (Entity) is shared with several agents, so its code is stored with that
name taken out: every key is replaced by a token naming only the connection
TYPE, and each run puts the running agent back in.

    stored:          ds_clients["$agent:powerbi"]
    run from jtlv:   ds_clients["jtlv:JTLV"]
    run from jtlv2:  ds_clients["jtlv2:JTLV2"]

The running agent must have exactly one active connection of each type the
code uses — none, or two to choose between, is an error the caller surfaces.

Not every query can be written that way. ``templatize`` classifies the code:

* ``templated`` — its keys all name one agent, one connection per type: stored
  with tokens, runs on whichever agent it is run from.
* ``dynamic``   — reaches ``ds_clients`` without naming a key (a loop over
  ``.items()``): nothing to replace, runs over the running agent's clients.
* ``bound``     — names several agents (a cross-agent join), or two
  connections of one type: it needs those exact agents, so it runs with them
  and cannot be shared further.
* ``unresolved`` — names a key no current connection answers to (its agent or
  connection is gone) AND the query's agents leave more than one connection
  type it could mean: kept as-is and refused at run time. When they leave
  exactly one, the key is repaired to it (``Templated.repaired``) instead.

Pure: no database access, so the migration backfill uses it too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from app.errors import AppError, ErrorCode

TEMPLATE_PREFIX = "$agent:"
FAST_SUFFIX = "::fast"
# The BOW client is not an agent's: it is installed under this fixed key.
NEUTRAL_KEYS = frozenset({"bow"})

MODE_TEMPLATED = "templated"
MODE_DYNAMIC = "dynamic"
MODE_BOUND = "bound"
MODE_UNRESOLVED = "unresolved"
SHAREABLE_MODES = frozenset({MODE_TEMPLATED, MODE_DYNAMIC})

# `ds_clients["key"]` / `ds_clients['key']` / `ds_clients.get("key")`. Each
# quote style is its own branch so a key may contain the OTHER quote — an agent
# named `Bob's DB` is written `ds_clients["Bob's DB:pg"]`.
_KEY_ACCESS = re.compile(
    r"""\bds_clients\s*(?:\[\s*(?:"(?P<sub_d>[^"\\\n]+)"|'(?P<sub_s>[^'\\\n]+)')\s*\]"""
    r"""|\.get\(\s*(?:"(?P<get_d>[^"\\\n]+)"|'(?P<get_s>[^'\\\n]+)'))"""
)
_KEY_GROUPS = ("sub_d", "sub_s", "get_d", "get_s")


def _key_group(m: "re.Match") -> str:
    """The name of the group that matched the key."""
    return next(g for g in _KEY_GROUPS if m.group(g) is not None)


_CLIENTS_NAME = re.compile(r"\bds_clients\b")
_DEF_PARAMS = re.compile(r"\bdef\s+\w+\s*\(([^)]*)\)")


@dataclass(frozen=True)
class ConnInfo:
    id: str
    name: str
    type: str
    is_active: bool = True


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    connections: Tuple[ConnInfo, ...] = ()

    @property
    def active(self) -> List[ConnInfo]:
        return [c for c in self.connections if c.is_active]


@dataclass
class Templated:
    code: str
    mode: str
    origin_id: Optional[str] = None
    # Agents the code names by key (the one it was written for, or — for a
    # bound query — every agent it joins).
    agent_ids: Set[str] = field(default_factory=set)
    # Keys that named no existing connection and were repaired from the
    # query's agents (see _repair_type): {old key: new token}.
    repaired: Dict[str, str] = field(default_factory=dict)


def agent_info(data_source) -> AgentInfo:
    """Snapshot a DataSource ORM row (with `connections` loaded)."""
    return AgentInfo(
        id=str(data_source.id),
        name=str(data_source.name or ""),
        connections=tuple(
            ConnInfo(
                id=str(c.id), name=str(c.name or ""), type=str(c.type or ""),
                is_active=bool(getattr(c, "is_active", True)),
            )
            for c in (getattr(data_source, "connections", None) or [])
        ),
    )


def client_keys(code: str) -> Tuple[List[str], bool]:
    """Literal keys the code reads, and whether it also reaches `ds_clients`
    some other way (iteration, a computed key)."""
    code = code or ""
    keys = [m.group(_key_group(m)) for m in _KEY_ACCESS.finditer(code)]
    in_signatures = sum(len(_CLIENTS_NAME.findall(m.group(1))) for m in _DEF_PARAMS.finditer(code))
    other_uses = len(_CLIENTS_NAME.findall(code)) - in_signatures - len(keys)
    return keys, other_uses > 0


def is_template_key(key: str) -> bool:
    return key.startswith(TEMPLATE_PREFIX)


def _key_map(agent: AgentInfo) -> Dict[str, Tuple[ConnInfo, bool]]:
    """Every key `construct_clients` could hand out for this agent, mapped to
    (connection, is_fast). Inactive connections are included so a query whose
    connection is only momentarily down still classifies correctly."""
    out: Dict[str, Tuple[ConnInfo, bool]] = {}
    for c in agent.connections:
        base = f"{agent.name}:{c.name}"
        out[base] = (c, False)
        out[base + FAST_SUFFIX] = (c, True)
    active = agent.active
    alias_conn = active[0] if len(active) == 1 else (agent.connections[0] if len(agent.connections) == 1 else None)
    if alias_conn is not None:
        out.setdefault(agent.name, (alias_conn, False))
    return out


def _replace_keys(code: str, mapping: Dict[str, str]) -> str:
    import json

    def sub(m: re.Match) -> str:
        group = _key_group(m)
        new = mapping.get(m.group(group))
        if new is None:
            return m.group(0)
        start, end = m.span(group)
        quote = "'" if group.endswith("_s") else '"'
        s0 = m.start()
        text = m.group(0)
        if quote in new or "\\" in new:
            # The new key cannot sit inside the old quotes: replace the whole
            # literal with an escaped double-quoted one (valid Python).
            return text[: start - 1 - s0] + json.dumps(new, ensure_ascii=False) + text[end + 1 - s0:]
        return text[: start - s0] + new + text[end - s0:]

    return _KEY_ACCESS.sub(sub, code or "")


def _type_token_is_exact(agent: AgentInfo, conn: ConnInfo) -> bool:
    """Whether `$agent:<type>` rendered on `agent` lands on `conn` — the one
    rule every conversion below obeys. `render` picks among ACTIVE
    connections of the type, so:
    - an active connection is exact when no other ACTIVE one shares its type;
    - an inactive one only when it is the sole connection of its type (were
      another active one there, render would silently run on that one).
    """
    same_type = [c for c in agent.connections if c.type == conn.type]
    if conn.is_active:
        return len([c for c in same_type if c.is_active]) == 1
    return len(same_type) == 1


def all_client_keys(agents: Iterable[AgentInfo]) -> Set[str]:
    """Every client key any of `agents` answers to (see _key_map)."""
    out: Set[str] = set()
    for a in agents:
        out.update(_key_map(a))
    return out


def templatize(
    code: str, agents: Iterable[AgentInfo], *, repair: bool = True,
    existing_keys: Optional[Set[str]] = None,
) -> Templated:
    """Take agent names out of `code`. `agents` are the candidates its keys
    may name (the query's agents; at creation, the agent it was written in).

    A key is replaced by a type token only when that token means exactly the
    connection the key named (_type_token_is_exact); otherwise the code stays
    as it is (bound). Keys naming no connection of `agents` are repaired only
    when ALL of these hold — otherwise the query is unresolved and a person
    fixes it:
    - `repair` (False for a lookup across the whole organization);
    - the key's agent no longer exists at all: `existing_keys` (every key of
      the organization's agents) does not contain it — a key of an agent that
      merely left the query is never "repaired" onto another agent;
    - the code does not already read several agents (a join);
    - the query's agents leave exactly one connection the key can mean
      (_repair_type).
    """
    code = code or ""
    agents = list(agents)
    keys, _dynamic = client_keys(code)
    literal = [k for k in keys if k not in NEUTRAL_KEYS and not is_template_key(k)]
    has_tokens = any(is_template_key(k) for k in keys)
    if not literal:
        return Templated(code=code, mode=MODE_TEMPLATED if has_tokens else MODE_DYNAMIC)

    maps = {a.id: _key_map(a) for a in agents}
    resolved: Dict[str, Tuple[AgentInfo, ConnInfo, bool]] = {}
    for key in literal:
        for a in agents:
            hit = maps[a.id].get(key)
            if hit is not None:
                resolved[key] = (a, hit[0], hit[1])
                break
    named = {a.id for (a, _c, _f) in resolved.values()}
    unknown = [k for k in dict.fromkeys(literal) if k not in resolved]

    def unresolved() -> Templated:
        return Templated(code=code, mode=MODE_UNRESOLVED, agent_ids=named)

    repaired: Dict[str, str] = {}
    if unknown:
        if not repair or len(named) > 1:
            return unresolved()
        if existing_keys is not None and any(k in existing_keys for k in unknown):
            return unresolved()
        for key in unknown:
            ctype = _repair_type(key, agents)
            if ctype is None:
                return unresolved()
            repaired[key] = f"{TEMPLATE_PREFIX}{ctype}" + (FAST_SUFFIX if key.endswith(FAST_SUFFIX) else "")

    if len(named) > 1 or (has_tokens and named):
        return Templated(code=code, mode=MODE_BOUND, agent_ids=named)

    agent: Optional[AgentInfo] = next((a for (a, _c, _f) in resolved.values()), None)
    if agent is not None:
        conns_by_type: Dict[str, Set[str]] = {}
        for (_a, c, _f) in resolved.values():
            conns_by_type.setdefault(c.type, set()).add(c.id)
        for ids in conns_by_type.values():
            conn = next(c for c in agent.connections if c.id in ids)
            if len(ids) > 1 or not _type_token_is_exact(agent, conn):
                # Two of the agent's connections of one type, or one a type
                # token would not land on: keep the code pinned as it is.
                return unresolved() if repaired else Templated(code=code, mode=MODE_BOUND, agent_ids=named)

    mapping = dict(repaired)
    for key, (_a, c, fast) in resolved.items():
        mapping[key] = f"{TEMPLATE_PREFIX}{c.type}" + (FAST_SUFFIX if fast else "")
    if agent is None:
        wanted = {t[len(TEMPLATE_PREFIX):].removesuffix(FAST_SUFFIX) for t in repaired.values()}
        agent = next((a for a in agents if wanted <= {c.type for c in a.active}), agents[0])
    return Templated(
        code=_replace_keys(code, mapping), mode=MODE_TEMPLATED,
        origin_id=agent.id, agent_ids={agent.id}, repaired=repaired,
    )


def _repair_type(key: str, agents: List[AgentInfo]) -> Optional[str]:
    """The one connection type an unknown key can mean on the query's agents.

    First by connection name — `"<agent>:<connection>"` whose agent was
    renamed or removed still names its connection — then by type alone: when
    every active connection of the query's agents is of a single type, that
    is the only thing the key can have reached. Then the type must be exact
    on every one of those agents: an agent with two connections of it (active
    or not) could run the key on the wrong one, so the key is not repaired.
    None when it stays ambiguous — a person fixes the query instead.
    """
    if not agents:
        return None
    base = key[: -len(FAST_SUFFIX)] if key.endswith(FAST_SUFFIX) else key
    ctype: Optional[str] = None
    if ":" in base:
        conn_name = base.rsplit(":", 1)[1]
        by_name = {c.type for a in agents for c in a.connections if c.name == conn_name}
        if len(by_name) == 1:
            ctype = next(iter(by_name))
    if ctype is None:
        types = {c.type for a in agents for c in a.active}
        if not types:
            types = {c.type for a in agents for c in a.connections}
        if len(types) != 1:
            return None
        ctype = next(iter(types))
    for a in agents:
        if len([c for c in a.connections if c.type == ctype]) > 1:
            return None
    return ctype


def required_types(code: str) -> List[str]:
    """Connection types a templated query needs on the agent it runs on."""
    keys, _ = client_keys(code)
    types: List[str] = []
    for k in keys:
        if is_template_key(k):
            t = k[len(TEMPLATE_PREFIX):]
            if t.endswith(FAST_SUFFIX):
                t = t[: -len(FAST_SUFFIX)]
            if t not in types:
                types.append(t)
    return types


def connection_for_type(agent: AgentInfo, ctype: str) -> ConnInfo:
    """The one active connection of `ctype` on `agent`, or a typed error."""
    matches = [c for c in agent.active if c.type == ctype]
    if not matches:
        raise AppError.bad_request(
            ErrorCode.ENTITY_AGENT_NO_CONNECTION_TYPE,
            f'Agent "{agent.name}" has no active {ctype} connection, so this query cannot run on it.',
            agent=agent.name, type=ctype,
        )
    if len(matches) > 1:
        raise AppError.bad_request(
            ErrorCode.ENTITY_AGENT_AMBIGUOUS_CONNECTION,
            f'Agent "{agent.name}" has {len(matches)} active {ctype} connections; this query cannot tell which one to use.',
            agent=agent.name, type=ctype, count=len(matches),
        )
    return matches[0]


def render(code: str, agent: AgentInfo) -> str:
    """Put `agent` into templated code: every `$agent:<type>` key becomes that
    agent's key for its one connection of that type. Code without tokens is
    returned unchanged."""
    keys, _ = client_keys(code)
    mapping: Dict[str, str] = {}
    for k in keys:
        if not is_template_key(k) or k in mapping:
            continue
        t = k[len(TEMPLATE_PREFIX):]
        fast = t.endswith(FAST_SUFFIX)
        if fast:
            t = t[: -len(FAST_SUFFIX)]
        conn = connection_for_type(agent, t)
        mapping[k] = f"{agent.name}:{conn.name}" + (FAST_SUFFIX if fast else "")
    return _replace_keys(code, mapping) if mapping else (code or "")


def as_all_active(agent: AgentInfo) -> AgentInfo:
    """`agent` with every connection treated as active — whether it HAS the
    connections a query needs, regardless of which are up right now."""
    from dataclasses import replace
    return AgentInfo(
        id=agent.id, name=agent.name,
        connections=tuple(replace(c, is_active=True) for c in agent.connections),
    )


def check_runnable_on(code: str, mode: str, agent: AgentInfo) -> None:
    """Raise the typed error that running this query on `agent` would hit."""
    if mode == MODE_UNRESOLVED:
        raise AppError.bad_request(
            ErrorCode.ENTITY_UNRESOLVED_CODE,
            "This query's code names an agent or connection that no longer exists. Edit the query to fix it.",
        )
    if mode in SHAREABLE_MODES:
        render(code, agent)
