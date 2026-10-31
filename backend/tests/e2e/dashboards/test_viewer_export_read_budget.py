"""Exporting current viewer results must not decrypt older cached grids.

Fernet is observed at the crypto boundary without changing its behavior. No
wall-clock threshold: the number of obsolete payloads read is deterministic.
"""
from datetime import datetime, timedelta

from cryptography.fernet import Fernet

from app.ee.encryption.types import encrypt_value
from tests.e2e.dashboards.dashboard_setup import as_
from tests.e2e.dashboards.test_dashboard_exports import (
    _copy_step, _embedded, _published, _rows_by_query, no_vendor_libs,  # noqa: F401
)


def test_export_reads_only_the_latest_successful_viewer_grid(
        corpus, corpus_rows, test_client, no_vendor_libs, monkeypatch):  # noqa: F811
    aid, release = _published(corpus, corpus_rows, test_client, "user_required/dash")
    step = _copy_step(corpus_rows, release)
    tokens = {}
    # A provider/agent writes parameter-specific cached runs; no API can seed
    # that historical state without the external personal-credential provider.
    for index in range(12):
        data = {"rows": [{"sample": index, "value": n} for n in range(1000)],
                "columns": [{"field": "sample"}, {"field": "value"}]}
        encrypted = encrypt_value(data)
        tokens[encrypted["v"].encode()] = index
        corpus_rows.insert("step_user_results", step_id=step, user_id=corpus.cast.id("has_results"),
                           organization_id=corpus.cast.org_id, report_id=None, status="success",
                           params_fingerprint=str(index), executed_as="viewer", data=encrypted,
                           last_run_at=datetime(2026, 4, 1) + timedelta(minutes=index))
    decrypted = []
    original = Fernet.decrypt

    def observe(self, token, *args, **kwargs):
        raw = original(self, token, *args, **kwargs)
        key = token.encode() if isinstance(token, str) else token
        if key in tokens:
            decrypted.append(tokens[key])
        return raw

    monkeypatch.setattr(Fernet, "decrypt", observe)
    response = test_client.get(f"/api/d/{aid}/export/html", headers=as_(corpus, "has_results"))
    assert response.status_code == 200, response.text
    (rows,) = _rows_by_query(_embedded(response.text)).values()
    assert len(rows) == 1000 and all(row["sample"] == 11 for row in rows)
    assert set(decrypted) == {11}, "export should never decrypt obsolete parameter runs"
