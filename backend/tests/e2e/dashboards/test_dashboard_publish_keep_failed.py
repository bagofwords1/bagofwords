"""Publish when some charts cannot run again (pubkeep01).

Publish re-runs every copy with the publisher's credentials. Contract:
- every copy runs, also after one failed, and the failed attempt lists each
  chart that could not run, with its reason and whether it may keep its
  last data; readers keep the live release;
- asked again with `keep_failed`, the charts that still fail go live with
  the data they were published with, the others with fresh data, and the
  live release records which kept theirs;
- a chart with no data to keep is never kept: that Publish still fails.
"""
import json

import pytest

from tests.e2e.dashboards.dashboard_setup import PUBLISHED, as_, own, publish
from tests.e2e.dashboards.test_dashboard_publish import (  # noqa: F401  (fixtures)
    _copies, _finish, _status, drawn, spawned,
)

pytestmark = pytest.mark.e2e

DASH = "encrypted/dash"  # two charts
BROKEN = "def generate_df(ds_clients, excel_files):\n    raise ValueError('המקור לא זמין')\n"
LAST = {"rows": [{"חודש": "אחרון", "הכנסות": 7}], "columns": [{"field": "חודש"}, {"field": "הכנסות"}]}
FRESH = "def generate_df(ds_clients, excel_files):\n    return pd.DataFrame({'חודש': ['טרי'], 'הכנסות': [1]})\n"


def _publish(test_client, corpus, aid, **body):
    resp = test_client.post(f"/api/artifacts/{aid}/publish", json=body, headers=as_(corpus, "owner"))
    assert resp.status_code == 202, resp.text
    PUBLISHED.append(resp.json()["latest"]["id"])
    return resp.json()["latest"]["id"]


def _months(data) -> set:
    data = json.loads(data) if isinstance(data, str) else data
    return {r.get("חודש") for r in (data or {}).get("rows", [])}


def _set(corpus_rows, corpus, key, **cols):
    corpus_rows.update("steps", corpus.ids[f"encrypted/{key}/step"], **cols)


@pytest.mark.parametrize("broken", [["q_sealed"], ["q_sealed", "q_plain"]], ids=["one", "both"])
def test_a_failed_publish_lists_every_chart_that_could_not_run(corpus, corpus_rows, test_client, spawned, broken):
    aid = own(corpus_rows, corpus, DASH)
    live = publish(aid)
    for key in ("q_sealed", "q_plain"):
        _set(corpus_rows, corpus, key, code=BROKEN if key in broken else FRESH, data=LAST)

    attempt = _publish(test_client, corpus, aid)
    _finish(spawned)
    latest = _status(test_client, corpus, aid)["latest"]
    assert latest["id"] == attempt and latest["status"] == "failed"
    failures = latest["step_failures"]
    assert len(failures) == len(broken)  # every one, not just the first
    assert all(f["keepable"] and not f["kept"] and "המקור לא זמין" in f["reason"] for f in failures)
    assert _status(test_client, corpus, aid)["published"]["id"] == live


def test_publish_with_last_data_keeps_only_the_charts_that_fail(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _set(corpus_rows, corpus, "q_sealed", code=BROKEN, data=LAST)
    _set(corpus_rows, corpus, "q_plain", code=FRESH, data=LAST)

    new = _publish(test_client, corpus, aid, keep_failed=True)
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "ready", status["latest"]["error"]
    assert status["published"]["id"] == new
    (kept,) = status["published"]["step_failures"]
    assert kept["kept"] and "המקור לא זמין" in kept["reason"]
    months = sorted(_months(c["data"]) for c in _copies(corpus_rows, new))
    assert months == sorted([{"אחרון"}, {"טרי"}])  # the failed one kept its data, the other ran


def test_a_chart_with_no_data_to_keep_still_fails_the_publish(corpus, corpus_rows, test_client, spawned):
    aid = own(corpus_rows, corpus, DASH)
    live = publish(aid)
    _set(corpus_rows, corpus, "q_sealed", code=BROKEN, data={"rows": [], "columns": []})
    _set(corpus_rows, corpus, "q_plain", code=FRESH, data=LAST)

    _publish(test_client, corpus, aid, keep_failed=True)
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "failed"
    (failure,) = status["latest"]["step_failures"]
    assert not failure["keepable"] and not failure["kept"]
    assert status["published"]["id"] == live
