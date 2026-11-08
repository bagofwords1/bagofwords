"""A card's thumbnail URL loads (dashboards list).

A page's thumbnail is a file under uploads/thumbnails; a deck's is its first
slide preview under uploads/pptx_previews. Either way the `thumbnail_url` a
card carries must load for the reader it was listed to -- the draft's for
the dashboard's editors, the live release's for everyone once published.

The deck's preview file and its path on the version are written directly:
only a real pptx render produces them, and these tests have no renderer.
"""
import shutil
import uuid

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, uploads_dir

DECK = "slides_previews/deck"
PAGE = "thumbnail/dash"
PNG = b"\x89PNG\r\n\x1a\n deck slide"


def _cards(test_client, corpus, person, **params) -> dict:
    resp = test_client.get("/api/artifacts", params={"limit": 100, **params}, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return {d["id"]: d for d in resp.json()["dashboards"]}


def _image(test_client, corpus, person, url):
    assert url, "the card has no thumbnail"
    return test_client.get(f"/api{url}", headers=as_(corpus, person))


@pytest.fixture
def rendered_deck(corpus, corpus_rows):
    """The corpus deck as a render leaves it: two slide previews on disk, the
    first one its thumbnail. The folder name varies per test (rule 3), so no
    URL can be built from a fixed name."""
    folder = f"deck-{uuid.uuid4().hex[:8]}"
    previews = uploads_dir() / "pptx_previews" / folder
    previews.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in (1, 2):
        (previews / f"slide-0{n}.png").write_bytes(PNG + bytes([n]))
        paths.append(f"pptx_previews/{folder}/slide-0{n}.png")
    corpus_rows.update("artifact_versions", corpus.ids[DECK + "/v1"], thumbnail_path=paths[0], content={
        "code": "from pptx import Presentation", "runtime_version": 1,
        "visualization_ids": [corpus.ids["slides_previews/q_sales/viz"]], "preview_images": paths})
    try:
        yield own(corpus_rows, corpus, DECK)
    finally:
        shutil.rmtree(previews, ignore_errors=True)


@pytest.mark.e2e
def test_a_decks_card_shows_its_first_slide(corpus, test_client, rendered_deck, corpus_rows):
    deck = rendered_deck

    draft = _image(test_client, corpus, "owner", _cards(test_client, corpus, "owner", filter="my")[deck]["thumbnail_url"])
    assert draft.status_code == 200, draft.text
    assert draft.headers["content-type"] == "image/png" and draft.content == PNG + b"\x01"

    publish(deck)
    test_client.put(f"/api/artifacts/{deck}/sharing", json={"visibility": "internal"}, headers=as_(corpus, "owner"))
    for person, tab in (("owner", "my"), ("member", "shared")):
        card = _cards(test_client, corpus, person, filter=tab)[deck]
        assert card["published"] is True
        live = _image(test_client, corpus, person, card["thumbnail_url"])
        assert live.status_code == 200, (person, live.text)
        assert live.headers["content-type"] == "image/png" and live.content == PNG + b"\x01"


@pytest.mark.e2e
def test_a_pages_card_thumbnail_still_loads(corpus, corpus_rows, test_client):
    page = own(corpus_rows, corpus, PAGE)
    resp = _image(test_client, corpus, "owner", _cards(test_client, corpus, "owner", filter="my")[page]["thumbnail_url"])
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
