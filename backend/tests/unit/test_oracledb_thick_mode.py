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


# ---------------------------------------------------------------------------
# Coder-facing description
# ---------------------------------------------------------------------------

def test_description_warns_about_charset_mismatch():
    """The description feeds the coder's <connection_clients> prompt, so it must
    teach how to avoid the recurring ORA-12704 character set mismatch."""
    desc = _client().description
    assert "ORA-12704" in desc
    assert "character set mismatch" in desc.lower()
    # Names the offending types and the concrete fix so the model can act on it.
    assert "NVARCHAR2" in desc
    assert "VARCHAR2" in desc
    assert "TO_CHAR" in desc


# ---------------------------------------------------------------------------
# Optional SDU (Oracle Net packet size)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unset", [None, ""])
def test_connections_without_sdu_are_unchanged(unset):
    # Every connection saved before the field existed (no key at all) or with
    # the field left blank must connect exactly as before: no dsn override.
    assert _client(sdu=unset)._connect_args() == {}
    assert _client()._connect_args() == {}


@pytest.mark.parametrize("sdu", [512, 1400, 8192])
def test_sdu_is_requested_in_the_connect_descriptor(sdu):
    dsn = _client(sdu=sdu)._connect_args()["dsn"]
    assert f"(SDU={sdu})" in dsn
    assert "(PROTOCOL=TCP)" in dsn
    assert "(HOST=dbhost)(PORT=1521)" in dsn
    assert "(SERVICE_NAME=dwh)" in dsn


def test_sdu_combines_with_tcps():
    dsn = _client(sdu=1400, use_tcps=True)._connect_args()["dsn"]
    assert "(SDU=1400)" in dsn and "(PROTOCOL=TCPS)" in dsn


def test_oracle_config_sdu_is_optional_and_blank_means_default():
    from app.schemas.data_sources.configs import OracleConfig
    base = dict(host="h", service_name="s")
    assert OracleConfig(**base).sdu is None            # legacy stored config
    assert OracleConfig(**base, sdu="").sdu is None    # cleared form input
    assert OracleConfig(**base, sdu=None).sdu is None
    assert OracleConfig(**base, sdu=1400).sdu == 1400


@pytest.mark.parametrize("bad", [0, 100, 10_000_000])
def test_oracle_config_rejects_out_of_range_sdu(bad):
    from pydantic import ValidationError
    from app.schemas.data_sources.configs import OracleConfig
    with pytest.raises(ValidationError):
        OracleConfig(host="h", service_name="s", sdu=bad)
