"""Unit tests for the python-oracledb thick-mode startup bootstrap."""

import pytest

import app.data_sources.clients.oracledb_client as oc


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
