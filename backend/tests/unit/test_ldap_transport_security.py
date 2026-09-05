"""LDAP transport contracts; only the external ldap3 boundary is substituted."""
import ssl
from types import SimpleNamespace

import pytest

from app.ee.ldap.connection import LDAPConnectionManager
from app.settings.bow_config import LDAPConfig


@pytest.fixture
def directory(monkeypatch):
    import ldap3

    state = SimpleNamespace(binds=[], tls=[], filters=[], entries=[], fail_tls=False, pages=None)

    class Connection:
        def __init__(self, server, **kwargs):
            self.server = server
            self.secure = server.ssl
            self.closed = False
            self.entries = state.entries
            self.result = {"result": 0}
            state.connection = self
            if kwargs.get("auto_bind"):
                self.bind()

        def open(self):
            return True

        def start_tls(self):
            if state.fail_tls:
                raise RuntimeError("TLS rejected")
            self.secure = True
            return True

        def bind(self):
            state.binds.append(self.secure)
            return True

        def unbind(self):
            self.closed = True

        def search(self, **kwargs):
            state.filters.append(kwargs["search_filter"])
            if state.pages is not None:
                self.entries, self.result = state.pages.pop(0)
            return bool(self.entries)

    monkeypatch.setattr(ldap3, "Connection", Connection)
    return state


@pytest.mark.parametrize("start_tls", [False, True])
@pytest.mark.parametrize("user_bind", [False, True])
def test_every_bind_uses_verified_tls(directory, start_tls, user_bind):
    config = LDAPConfig(url="ad.example.test", use_ssl=not start_tls,
                        start_tls=start_tls, bind_dn="lookup", bind_password="synthetic")
    manager = LDAPConnectionManager(config)
    if user_bind:
        assert manager.bind_user("cn=person", "synthetic")
    else:
        manager.get_connection().unbind()
    assert directory.binds and all(directory.binds)
    assert directory.connection.server.tls.validate == ssl.CERT_REQUIRED
    assert directory.connection.closed


def test_failed_upgrade_never_sends_credentials(directory):
    directory.fail_tls = True
    manager = LDAPConnectionManager(LDAPConfig(url="ad.example.test", use_ssl=False,
        start_tls=True, bind_dn="lookup", bind_password="synthetic"))
    with pytest.raises(Exception):
        manager.get_connection()
    assert not directory.binds
    assert directory.connection.closed


def test_empty_password_never_binds(directory):
    manager = LDAPConnectionManager(LDAPConfig(url="ad.example.test"))
    assert not manager.bind_user("cn=person", "")
    assert not directory.binds


@pytest.mark.parametrize("login", ["a*)(mail=*)", "x\\y\x00@example.test"])
def test_login_filter_values_are_escaped(directory, login):
    from ldap3.utils.conv import escape_filter_chars
    manager = LDAPConnectionManager(LDAPConfig(url="ad.example.test",
        bind_dn="lookup", bind_password="synthetic"))
    manager.find_user_dn(login)
    assert f"(mail={escape_filter_chars(login)})" in directory.filters[0]


def test_ambiguous_lookup_is_not_an_identity(directory):
    directory.entries = [SimpleNamespace(entry_dn="cn=one"), SimpleNamespace(entry_dn="cn=two")]
    manager = LDAPConnectionManager(LDAPConfig(url="ad.example.test",
        bind_dn="lookup", bind_password="synthetic"))
    with pytest.raises(Exception):
        manager.find_user_dn("duplicate@example.test")


class Entry(dict):
    def __init__(self, dn, **attrs):
        super().__init__({k: SimpleNamespace(value=v) for k, v in attrs.items()})
        self.entry_dn = dn


def page(cookie, code=0):
    return {"result": code, "controls": {"1.2.840.113556.1.4.319": {"value": {"cookie": cookie}}}}


@pytest.mark.parametrize("kind", ["users", "groups"])
def test_directory_search_consumes_all_pages(directory, kind):
    entries = [Entry(f"cn=p{i}", mail=f"p{i}@example.test", displayName=f"P{i}", cn=f"P{i}", member=[]) for i in range(3)]
    directory.pages = [([e], page(str(i).encode() if i < 2 else b"")) for i, e in enumerate(entries)]
    manager = LDAPConnectionManager(LDAPConfig(url="ad.example.test", bind_dn="lookup", bind_password="synthetic", page_size=1))
    assert len(getattr(manager, f"search_{kind}")()) == len(entries)
    assert directory.connection.closed


@pytest.mark.parametrize("failure", [page(b"", 4), page(b"repeat"), {"result": 0}])
def test_incomplete_directory_snapshot_is_rejected(directory, failure):
    e = Entry("cn=one", mail="one@example.test")
    directory.pages = [([e], page(b"repeat")), ([e], failure)]
    manager = LDAPConnectionManager(LDAPConfig(url="ad.example.test", bind_dn="lookup", bind_password="synthetic"))
    with pytest.raises(Exception):
        manager.search_users()
    assert directory.connection.closed
