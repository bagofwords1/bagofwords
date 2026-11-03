"""nats-accounts editing for the Data Tunnels wizard (design A11).

The wizard adds and removes one NATS user per agent in the `TUNNEL` account's
users array. These cover the contract on the text itself — no cluster: the
user lands inside the right account whatever the file's layout, a second add
is a no-op, a remove takes exactly that user, and an unrecognised file is
refused rather than guessed at.
"""
import pytest

from app.services.tunnel_provisioning_service import (
    ProvisioningError,
    _agent_user_block,
    agent_cn,
    insert_user_block,
    remove_user_block,
)


def _accounts(tunnel_users: str = "", trailing_account: bool = False, indent: str = "  ") -> str:
    """A nats-accounts file in the sandbox layout. `trailing_account` puts
    another account *after* TUNNEL so TUNNEL's `]` is not the last in the file."""
    i = indent
    out = (
        "accounts {\n"
        f"{i}SYS {{\n{i}{i}users = [\n{i}{i}{i}{{ user: sys, password: x }}\n{i}{i}]\n{i}}}\n"
        f"{i}TUNNEL {{\n{i}{i}users = [\n{tunnel_users}{i}{i}]\n{i}}}\n"
    )
    if trailing_account:
        out += f"{i}OTHER {{\n{i}{i}users = [\n{i}{i}]\n{i}}}\n"
    return out + "}\n"


def _tunnel_users(content: str) -> str:
    start = content.index("TUNNEL")
    return content[start:content.index("}", content.index("]", start))]


@pytest.mark.parametrize("trailing_account", [False, True])
@pytest.mark.parametrize("indent", ["  ", "    ", "\t"])
def test_user_is_added_inside_the_tunnel_account(trailing_account, indent):
    content = _accounts(trailing_account=trailing_account, indent=indent)
    block = _agent_user_block("org-1", "nyc-01")

    new = insert_user_block(content, block, "TUNNEL")

    cn = agent_cn("org-1", "nyc-01")
    assert f'user: "CN={cn}"' in _tunnel_users(new)          # inside TUNNEL…
    assert cn not in new[: new.index("TUNNEL")]              # …not in SYS
    if trailing_account:
        assert cn not in new[new.index("OTHER"):]            # …not in OTHER
    assert new.count("accounts {") == 1 and new.rstrip().endswith("}")


def test_remove_takes_exactly_that_user():
    content = _accounts()
    for agent in ("nyc-01", "tokyo-02"):
        content = insert_user_block(content, _agent_user_block("org-1", agent), "TUNNEL")

    new = remove_user_block(content, agent_cn("org-1", "nyc-01"))

    assert agent_cn("org-1", "nyc-01") not in new
    assert f'user: "CN={agent_cn("org-1", "tokyo-02")}"' in new
    assert "tunnel.org-1.tokyo-02.>" in new                   # its permissions intact
    assert new.count("{") == new.count("}")                   # still balanced


def test_remove_of_an_absent_user_is_a_no_op():
    content = _accounts()
    assert remove_user_block(content, agent_cn("org-1", "ghost")) == content


def test_add_then_remove_round_trips_to_the_original():
    content = _accounts(trailing_account=True)
    block = _agent_user_block("org-1", "nyc-01")
    assert remove_user_block(insert_user_block(content, block, "TUNNEL"),
                               agent_cn("org-1", "nyc-01")) == content


@pytest.mark.parametrize("content", [
    "accounts {\n  SYS {\n    users = [\n    ]\n  }\n}\n",     # no TUNNEL account
    "accounts {\n  TUNNEL {\n    jetstream: enabled\n  }\n}\n", # TUNNEL without users
    "",                                                           # empty
])
def test_unrecognised_layout_is_refused_not_guessed(content):
    with pytest.raises(ProvisioningError):
        insert_user_block(content, _agent_user_block("org-1", "nyc-01"), "TUNNEL")
