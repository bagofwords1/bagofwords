"""Saved-query code is stored without an agent name and rendered per agent."""
import uuid

import pytest

from app.errors import AppError
from app.services.entity_code import (
    MODE_BOUND, MODE_DYNAMIC, MODE_TEMPLATED, MODE_UNRESOLVED,
    AgentInfo, ConnInfo, client_keys, render, required_types, templatize,
)


def _agent(name=None, *conns):
    name = name or f"agent_{uuid.uuid4().hex[:5]}"
    return AgentInfo(id=str(uuid.uuid4()), name=name, connections=tuple(conns))


def _conn(ctype, name=None, active=True):
    return ConnInfo(id=str(uuid.uuid4()), name=name or f"{ctype}-{uuid.uuid4().hex[:4]}", type=ctype, is_active=active)


def _code(*keys, getter=False):
    lines = ["def generate_df(ds_clients, excel_files):", "    frames = []"]
    for k in keys:
        access = f"ds_clients.get({k!r})" if getter else f"ds_clients[{k!r}]"
        lines.append(f"    frames.append({access}.execute_query('SELECT 1'))")
    lines.append("    return frames[0]")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("key_form", ["full", "alias", "fast", "get"])
def test_one_agent_code_round_trips_to_any_agent_of_the_same_type(key_form):
    ctype = f"t{uuid.uuid4().hex[:4]}"
    origin, other = _agent(None, _conn(ctype)), _agent(None, _conn(ctype))
    oc = origin.connections[0]
    key = {
        "full": f"{origin.name}:{oc.name}",
        "alias": origin.name,
        "fast": f"{origin.name}:{oc.name}::fast",
        "get": f"{origin.name}:{oc.name}",
    }[key_form]
    t = templatize(_code(key, getter=key_form == "get"), [origin, other])

    assert t.mode == MODE_TEMPLATED
    assert t.origin_id == origin.id
    assert origin.name not in t.code
    assert required_types(t.code) == [ctype]

    for agent in (origin, other):
        keys, _ = client_keys(render(t.code, agent))
        expected = f"{agent.name}:{agent.connections[0].name}" + ("::fast" if key_form == "fast" else "")
        assert keys == [expected]


def test_code_that_iterates_clients_is_dynamic_and_untouched():
    code = "def generate_df(ds_clients, excel_files):\n    return [c for c in ds_clients.values()]\n"
    t = templatize(code, [_agent(None, _conn("pg"))])
    assert t.mode == MODE_DYNAMIC
    assert t.code == code


def test_bow_client_is_not_an_agent():
    code = _code("bow")
    t = templatize(code, [_agent(None, _conn("pg"))])
    assert t.mode == MODE_DYNAMIC
    assert t.code == code


def test_code_joining_two_agents_is_bound():
    a, b = _agent(None, _conn("pg")), _agent(None, _conn("pg"))
    code = _code(f"{a.name}:{a.connections[0].name}", f"{b.name}:{b.connections[0].name}")
    t = templatize(code, [a, b])
    assert t.mode == MODE_BOUND
    assert t.agent_ids == {a.id, b.id}
    assert t.code == code


def test_two_connections_of_one_type_on_the_origin_is_bound():
    agent = _agent(None, _conn("pg"), _conn("pg"))
    code = _code(f"{agent.name}:{agent.connections[0].name}")
    assert templatize(code, [agent]).mode == MODE_BOUND


def test_two_connection_types_on_one_agent_template_separately():
    agent = _agent(None, _conn("pg"), _conn("pbi"))
    code = _code(*[f"{agent.name}:{c.name}" for c in agent.connections])
    t = templatize(code, [agent])
    assert t.mode == MODE_TEMPLATED
    assert set(required_types(t.code)) == {"pg", "pbi"}


def test_key_naming_a_missing_agent_with_no_agents_to_repair_from_is_unresolved():
    code = _code(f"gone_{uuid.uuid4().hex[:4]}")
    t = templatize(code, [])
    assert t.mode == MODE_UNRESOLVED
    assert t.code == code


def test_templating_is_idempotent():
    agent = _agent(None, _conn("pg"))
    once = templatize(_code(agent.name), [agent])
    twice = templatize(once.code, [agent])
    assert twice.code == once.code
    assert twice.mode == MODE_TEMPLATED


@pytest.mark.parametrize("conns,code", [
    ((), "entity.agent_no_connection_type"),
    (("pg", "pg"), "entity.agent_ambiguous_connection"),
    (("pbi",), "entity.agent_no_connection_type"),
])
def test_rendering_on_an_agent_without_exactly_one_matching_connection_fails(conns, code):
    origin = _agent(None, _conn("pg"))
    t = templatize(_code(origin.name), [origin])
    target = _agent(None, *[_conn(c) for c in conns])
    with pytest.raises(AppError) as err:
        render(t.code, target)
    assert err.value.error_code == code


def test_inactive_connection_does_not_count_when_rendering():
    origin = _agent(None, _conn("pg"))
    t = templatize(_code(origin.name), [origin])
    target = _agent(None, _conn("pg", active=False), _conn("pg"))
    keys, _ = client_keys(render(t.code, target))
    assert keys == [f"{target.name}:{target.connections[1].name}"]


def test_agent_name_with_colon_resolves():
    origin = _agent(f"sales:{uuid.uuid4().hex[:3]}", _conn("pg"))
    t = templatize(_code(f"{origin.name}:{origin.connections[0].name}"), [origin])
    assert t.mode == MODE_TEMPLATED


# ── Repairing a key that names nothing (deleted / renamed / hand-edited agent) ──

def test_unknown_key_is_repaired_when_the_querys_agents_share_one_type():
    ctype = f"t{uuid.uuid4().hex[:4]}"
    a, b = _agent(None, _conn(ctype)), _agent(None, _conn(ctype))
    stale = f"gone_{uuid.uuid4().hex[:4]}"
    t = templatize(_code(stale), [a, b])
    assert t.mode == MODE_TEMPLATED
    assert t.repaired == {stale: f"$agent:{ctype}"}
    assert t.origin_id == a.id
    for agent in (a, b):
        keys, _ = client_keys(render(t.code, agent))
        assert keys == [f"{agent.name}:{agent.connections[0].name}"]


def test_unknown_key_is_not_guessed_when_the_agents_mix_types():
    a, b = _agent(None, _conn("pbi")), _agent(None, _conn("pg"))
    t = templatize(_code(f"gone_{uuid.uuid4().hex[:4]}"), [a, b])
    assert t.mode == MODE_UNRESOLVED
    assert not t.repaired


def test_unknown_agent_with_a_known_connection_name_is_repaired_by_that_connection():
    """A renamed agent's old key still names its connection."""
    pbi, pg = _conn("pbi"), _conn("pg")
    agent = _agent(None, pbi, pg)
    t = templatize(_code(f"old_name:{pg.name}"), [agent])
    assert t.mode == MODE_TEMPLATED
    assert required_types(t.code) == ["pg"]


def test_unknown_key_beside_a_known_one_takes_the_type_of_the_querys_agents():
    ctype = f"t{uuid.uuid4().hex[:4]}"
    agent = _agent(None, _conn(ctype))
    t = templatize(_code(agent.name, f"gone_{uuid.uuid4().hex[:4]}"), [agent])
    assert t.mode == MODE_TEMPLATED
    assert required_types(t.code) == [ctype]


def test_no_repair_against_agents_that_are_not_the_querys_own():
    agent = _agent(None, _conn("pg"))
    t = templatize(_code(f"gone_{uuid.uuid4().hex[:4]}"), [agent], repair=False)
    assert t.mode == MODE_UNRESOLVED


def test_repaired_fast_key_keeps_its_fast_client():
    ctype = f"t{uuid.uuid4().hex[:4]}"
    agent = _agent(None, _conn(ctype))
    t = templatize(_code(f"gone:{uuid.uuid4().hex[:4]}::fast"), [agent])
    assert t.mode == MODE_TEMPLATED
    keys, _ = client_keys(render(t.code, agent))
    assert keys == [f"{agent.name}:{agent.connections[0].name}::fast"]


def test_agent_name_with_an_apostrophe_is_recognised_and_rendered():
    ctype = f"t{uuid.uuid4().hex[:4]}"
    origin = _agent(f"Bob's {uuid.uuid4().hex[:3]}", _conn(ctype))
    t = templatize(_code(f"{origin.name}:{origin.connections[0].name}"), [origin])
    assert t.mode == MODE_TEMPLATED
    assert origin.name not in t.code

    target = _agent(f"Ann's {uuid.uuid4().hex[:3]}", _conn(ctype))
    rendered = render(t.code, target)
    keys, _ = client_keys(rendered)
    assert keys == [f"{target.name}:{target.connections[0].name}"]
    compile(rendered, "<entity>", "exec")  # still valid Python


def test_rendering_into_single_quotes_escapes_an_apostrophe():
    ctype = f"t{uuid.uuid4().hex[:4]}"
    origin = _agent(None, _conn(ctype))
    code = f"def generate_df(ds_clients, excel_files):\n    return ds_clients['{origin.name}'].execute_query('SELECT 1')\n"
    t = templatize(code, [origin])
    target = _agent(f"O'Neil {uuid.uuid4().hex[:3]}", _conn(ctype))
    rendered = render(t.code, target)
    compile(rendered, "<entity>", "exec")
    keys, _ = client_keys(rendered)
    assert keys == [f"{target.name}:{target.connections[0].name}"]


def test_an_inactive_connection_of_the_same_type_does_not_make_the_code_ambiguous():
    """templatize must agree with render, which only picks active connections."""
    live = _conn("pg")
    agent = _agent(None, live, _conn("pg", active=False))
    t = templatize(_code(f"{agent.name}:{live.name}"), [agent])
    assert t.mode == MODE_TEMPLATED
    keys, _ = client_keys(render(t.code, agent))
    assert keys == [f"{agent.name}:{live.name}"]


def test_code_naming_an_inactive_connection_is_never_moved_to_another_one():
    """`sales:prod` (inactive) beside `staging` (active, same type): a type
    token would run on staging. The key stays pinned to prod instead."""
    prod = _conn("pg", name="prod", active=False)
    staging = _conn("pg", name="staging")
    agent = _agent(None, prod, staging)
    code = _code(f"{agent.name}:prod")
    t = templatize(code, [agent])
    assert t.mode == MODE_BOUND
    assert t.code == code


def test_the_only_connection_of_its_type_templates_even_while_inactive():
    """A connection that is merely down right now is still the only one the
    key can mean."""
    only = _conn("pg", active=False)
    agent = _agent(None, only)
    t = templatize(_code(f"{agent.name}:{only.name}"), [agent])
    assert t.mode == MODE_TEMPLATED


# ── Review round 4: repairs never guess a connection ──────────────────────────

def test_a_stale_key_does_not_let_an_inactive_connection_move_to_another():
    prod = _conn("pg", name="prod", active=False)
    agent = _agent("sales", prod, _conn("pg", name="staging"))
    code = _code("sales:prod", f"gone_{uuid.uuid4().hex[:4]}:prod")
    t = templatize(code, [agent])
    assert t.mode != MODE_TEMPLATED
    assert t.code == code


def test_a_renamed_agents_key_is_not_repaired_onto_a_sibling_connection():
    """sales_old was renamed to sales; its key still names `prod`, which is
    inactive beside an active `staging` of the same type."""
    agent = _agent("sales", _conn("pg", name="prod", active=False), _conn("pg", name="staging"))
    code = _code("sales_old:prod")
    t = templatize(code, [agent])
    assert t.mode == MODE_UNRESOLVED
    assert t.code == code


def test_a_key_of_an_agent_that_still_exists_is_never_repaired():
    """Removing B from a join must not turn B's key into A's (a self-join)."""
    from app.services.entity_code import all_client_keys

    a = _agent(None, _conn("pg"))
    b = _agent(None, _conn("pg"))
    code = _code(f"{a.name}:{a.connections[0].name}", f"{b.name}:{b.connections[0].name}")
    t = templatize(code, [a], existing_keys=all_client_keys([a, b]))
    assert t.mode == MODE_UNRESOLVED
    assert t.code == code


def test_a_key_of_a_deleted_agent_is_still_repaired():
    a = _agent(None, _conn("pg"))
    t = templatize(_code(f"deleted_{uuid.uuid4().hex[:4]}:pg-1"), [a], existing_keys={f"{a.name}:{a.connections[0].name}"})
    assert t.mode == MODE_TEMPLATED
    assert t.repaired
