"""Feedback loop — a legacy-encoded filename must never reach an INSERT.

Observed live on a Hebrew mortgage share (cp862 / DOS-Hebrew names, the
zip-and-SMB era encoding). Reading a scanned PDF off it ended the whole turn
with:

    invalid input for query argument $1: '\\udc84\\udc8b\\udc90...'
    ('utf-8' codec can't encode characters in position 0-5: surrogates not
    allowed)
    ... This Session's transaction has been rolled back due to a previous
    exception during flush.

Two defects chained:

1. `network_dir_client` recovers legacy names for LISTINGS (`_entry`,
   `_rel_id`) but handed back `path.name` RAW from `read_file`/
   `read_raw_bytes` — the surrogateescape'd directory-entry bytes. That name
   became the attached File's `filename` and its `path`, and Postgres columns
   are UTF-8: asyncpg rejects lone surrogates outright. Note the source_ref in
   the live traceback was clean Hebrew while the filename was mangled —
   exactly this split.
2. The rejection happens during flush, so the `AsyncSession` was left needing
   a rollback nobody issued. Every later statement in the turn then died with
   "transaction has been rolled back" — one un-storable filename took down the
   entire run, not just its own attach.

These tests FAIL on main by design and flip with the fix.
"""
from __future__ import annotations

import os

import pytest

from app.data_sources.clients._file_source_common import storage_safe_name
from app.data_sources.clients.network_dir_client import NetworkDirClient

CP862_NAME = "הכנסות לווים + דפי חשבון בנק.pdf"   # DOS-Hebrew on disk
JUNK_PDF = b"%PDF-1.4\n(scanned: no text layer)\n%%EOF\n"


def _has_surrogates(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s or "")


def _pg_storable(s: str) -> bool:
    """What asyncpg does to every string parameter before it hits the wire."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in s


@pytest.fixture()
def cp862_tree(tmp_path):
    """A share holding one cp862-named PDF, written via BYTES so the on-disk
    name really is non-UTF-8 — as a Windows/DOS-era tool would leave it."""
    with open(os.path.join(os.fsencode(str(tmp_path)), CP862_NAME.encode("cp862")), "wb") as fh:
        fh.write(JUNK_PDF)
    return tmp_path


# ─── 1. the name must leave the connector recovered ────────────────────────


class TestConnectorNamesAreRecovered:
    def test_premise_raw_dir_entry_carries_surrogates(self, cp862_tree):
        """Guard the premise: without recovery the OS layer really does hand
        Python a lone-surrogate name (this is what used to be persisted)."""
        raw = os.listdir(str(cp862_tree))[0]
        assert _has_surrogates(raw) and not _pg_storable(raw)

    def test_read_file_bytes_carry_a_storable_name(self, cp862_tree):
        """A scanned PDF falls through to NamedBytes — whose `.name` becomes
        the attached file's filename."""
        payload = NetworkDirClient(root_path=str(cp862_tree)).read_file(CP862_NAME)
        assert isinstance(payload, bytes)
        assert bytes(payload) == JUNK_PDF
        assert payload.name == CP862_NAME
        assert _pg_storable(payload.name)

    def test_read_raw_bytes_returns_a_storable_name(self, cp862_tree):
        """attach_file's path to the ORIGINAL bytes — same requirement."""
        content, name, mime = NetworkDirClient(root_path=str(cp862_tree)).read_raw_bytes(CP862_NAME)
        assert content == JUNK_PDF
        assert name == CP862_NAME and _pg_storable(name)
        assert mime == "application/pdf"   # guessed off the recovered name

    def test_listing_and_read_agree_on_the_name(self, cp862_tree):
        """The id the model was given must round-trip to the same display
        name it gets back — otherwise the cache key and the filename drift."""
        c = NetworkDirClient(root_path=str(cp862_tree))
        entry = c.list_files()[0]
        assert entry["name"] == c.read_raw_bytes(entry["id"])[1]


# ─── 2. the persistence boundary refuses to pass surrogates through ────────


class TestStorageSafeName:
    def test_recovers_legacy_bytes_rather_than_mangling_them(self):
        surrogate_form = CP862_NAME.encode("cp862").decode("utf-8", "surrogateescape")
        assert _has_surrogates(surrogate_form)          # premise
        assert storage_safe_name(surrogate_form) == CP862_NAME

    def test_unknown_encoding_still_becomes_storable(self):
        """No charset explains these bytes; the result is degraded, but it must
        never be un-storable — a lost name beats a dead transaction."""
        mystery = b"report \x90\x81\x8d\xff\xfe final.pdf".decode("utf-8", "surrogateescape")
        out = storage_safe_name(mystery)
        assert _pg_storable(out) and not _has_surrogates(out)

    def test_strips_nul(self):
        assert _pg_storable(storage_safe_name("bad\x00name.pdf"))

    def test_clean_names_pass_through_untouched(self):
        for name in ("quarterly report.xlsx", "חוזה חדש.pdf", "café menu.txt"):
            assert storage_safe_name(name) == name

    def test_empty_input_is_safe(self):
        assert storage_safe_name("") == ""


