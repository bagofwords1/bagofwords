"""Unit tests for the python-oracledb thick-mode bootstrap and TCPS connect args."""

import ssl

import pytest

import app.data_sources.clients.oracledb_client as oc


def _client(**overrides):
    params = dict(host="dbhost", port=1521, service_name="dwh",
                  user="scott", password="tiger")
    params.update(overrides)
    return oc.OracledbClient(**params)


# ---------------------------------------------------------------------------
# init_thick_mode_if_available
# ---------------------------------------------------------------------------

def test_returns_true_when_client_libraries_load(monkeypatch):
    calls = []
    monkeypatch.setattr(oc.oracledb, "init_oracle_client", lambda: calls.append(1))
    assert oc.init_thick_mode_if_available() is True
    assert len(calls) == 1


def test_returns_false_and_stays_thin_when_libraries_missing(monkeypatch):
    def boom():
        raise Exception("DPI-1047: Cannot locate a 64-bit Oracle Client library")
    monkeypatch.setattr(oc.oracledb, "init_oracle_client", boom)
    assert oc.init_thick_mode_if_available() is False


def test_env_var_opt_out_skips_init_entirely(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("init_oracle_client must not be called when opted out")
    monkeypatch.setattr(oc.oracledb, "init_oracle_client", fail)
    monkeypatch.setenv("ORACLE_THICK_MODE", "0")
    assert oc.init_thick_mode_if_available() is False


# ---------------------------------------------------------------------------
# TCPS connect args
# ---------------------------------------------------------------------------

def test_plain_tcp_has_no_extra_connect_args():
    assert _client()._connect_args() == {}


def test_tcps_overrides_dsn_with_tcps_descriptor():
    args = _client(use_tcps=True)._connect_args()
    assert args["dsn"] == (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST=dbhost)(PORT=1521))"
        "(CONNECT_DATA=(SERVICE_NAME=dwh)))"
    )
    # verification stays on by default: no ssl relaxation args
    assert "ssl_context" not in args
    assert "ssl_server_dn_match" not in args


def test_tcps_without_verification_relaxes_ssl_in_thin_mode(monkeypatch):
    monkeypatch.setattr(oc.oracledb, "is_thin_mode", lambda: True)
    args = _client(use_tcps=True, verify_ssl=False)._connect_args()
    assert args["ssl_server_dn_match"] is False
    ctx = args["ssl_context"]
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_tcps_without_verification_omits_ssl_context_in_thick_mode(monkeypatch):
    monkeypatch.setattr(oc.oracledb, "is_thin_mode", lambda: False)
    args = _client(use_tcps=True, verify_ssl=False)._connect_args()
    assert args["ssl_server_dn_match"] is False
    assert "ssl_context" not in args
