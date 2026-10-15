"""read_file by path on SharePoint Server (2010 SOAP and modern REST).

A path the agent passes is tried in every visible library and must match
exactly one file. The error used to say "missing or ambiguous" for both
outcomes, so an agent could not tell whether to fix the path or pick a
library. Each outcome now names itself, and an ambiguous path lists the
matching files by their id (full server path), each of which reads.
"""
import pytest

from tests.unit import test_sharepoint_2010_client as legacy_mod
from tests.unit import test_sharepoint_onprem_client as modern_mod
from tests.unit.test_sharepoint_2010_client import legacy  # noqa: F401  (fixture)
from tests.unit.test_sharepoint_onprem_client import remote  # noqa: F401  (fixture)

ROOT, OTHER = modern_mod.ROOT, modern_mod.OTHER
assert (ROOT, OTHER) == (legacy_mod.ROOT, legacy_mod.OTHER)


@pytest.fixture(params=["2010", "modern"])
def server(request):
    if request.param == "2010":
        state = request.getfixturevalue("legacy")
        return state.blobs, legacy_mod.client
    state = request.getfixturevalue("remote")
    return state["blobs"], modern_mod.client


@pytest.mark.parametrize("path", ["no-such-file.pdf", "nested/missing.txt", "Documents/nope.csv"])
def test_a_path_that_matches_nothing_says_not_found(server, path):
    blobs, client = server
    with pytest.raises(ValueError) as exc:
        client().read_raw_bytes(path)
    msg = str(exc.value)
    assert "not found" in msg.lower()
    assert path in msg
    assert "ambiguous" not in msg.lower()


def test_a_path_in_several_libraries_lists_each_match_as_a_readable_id(server):
    blobs, client = server
    blobs[ROOT + "/shared.txt"] = b"from documents"
    blobs[OTHER + "/shared.txt"] = b"from policies"
    c = client()
    with pytest.raises(ValueError) as exc:
        c.read_raw_bytes("shared.txt")
    msg = str(exc.value)
    assert "not found" not in msg.lower()
    listed = [p for p in (ROOT + "/shared.txt", OTHER + "/shared.txt") if p in msg]
    assert len(listed) == 2, msg
    # What the error offers must work as the next read.
    assert {c.read_raw_bytes(p)[0] for p in listed} == {b"from documents", b"from policies"}


def test_a_unique_relative_path_still_reads(server):
    blobs, client = server
    assert client(drive_name="Documents").read_raw_bytes("revenue.csv")[0] == blobs[ROOT + "/revenue.csv"]
