"""Unit tests for the A3 file-reference resolver's payload serializer."""
import pandas as pd

from app.services.file_reference_service import _serialize


def test_serialize_dataframe_to_csv():
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    name, content, mime = _serialize(df, "report", None)
    assert name == "report.csv" and mime == "text/csv"
    assert b"a,b" in content


def test_serialize_dict_to_json():
    name, content, mime = _serialize({"k": "v"}, "data", None)
    assert name == "data.json" and mime == "application/json"
    assert b'"k"' in content


def test_serialize_str_to_txt():
    name, content, mime = _serialize("hello world", "note", None)
    assert name == "note.txt" and mime == "text/plain" and content == b"hello world"


def test_serialize_bytes_passthrough_with_ref_mime():
    name, content, mime = _serialize(b"\x00\x01", "blob.pdf", "application/pdf")
    assert name == "blob.pdf" and mime == "application/pdf" and content == b"\x00\x01"


def test_serialize_keeps_existing_extension():
    name, _, _ = _serialize("x", "already.txt", None)
    assert name == "already.txt"  # not "already.txt.txt"
