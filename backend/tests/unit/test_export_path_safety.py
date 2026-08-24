"""Unit tests for the path guard the export downloads rely on.

Pure logic — no DB, no HTTP. `safe_join` is what keeps
`/artifacts/{id}/export/pdf` and `/r/{id}/export_pptx` from serving a file
outside their export tree; the second of those is reachable without a login,
so the guard is the last thing standing between a share link and the
filesystem. It had no coverage before those routes started depending on it.
"""

import pytest

from app.core.path_safety import UnsafePathError, ensure_within, safe_join


@pytest.fixture
def export_root(tmp_path):
    """Stands in for ReportPdfService.UPLOADS_DIR / PPTX_DIR."""
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "deck.pptx").write_bytes(b"x")
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "id_rsa").write_bytes(b"x")
    return root


def test_joins_a_plain_filename(export_root):
    assert safe_join(export_root, "deck.pptx") == (export_root / "deck.pptx").resolve()


def test_rejects_parent_traversal(export_root):
    with pytest.raises(UnsafePathError):
        safe_join(export_root, "../secrets/id_rsa")


def test_rejects_traversal_buried_mid_path(export_root):
    with pytest.raises(UnsafePathError):
        safe_join(export_root, "a/../../secrets/id_rsa")


def test_rejects_absolute_segment(export_root):
    # pathlib lets an absolute segment discard the base entirely, which is the
    # exact trap this helper exists to close.
    with pytest.raises(UnsafePathError):
        safe_join(export_root, "/etc/passwd")


def test_basename_first_is_what_the_routes_actually_pass(export_root):
    """Both export routes call os.path.basename before joining.

    Together the two steps mean a stored path is reduced to a bare name and
    then confirmed to land inside the root — a DB row holding an absolute
    path to somewhere else cannot escape.
    """
    import os

    stored = "/var/lib/other-tenant/deck.pptx"
    joined = safe_join(export_root, os.path.basename(stored))
    assert joined == (export_root / "deck.pptx").resolve()
    assert joined.is_file()


def test_ensure_within_accepts_a_path_inside_the_root(export_root):
    assert ensure_within(export_root / "deck.pptx", [export_root])


def test_ensure_within_rejects_a_sibling_directory(export_root):
    with pytest.raises(UnsafePathError):
        ensure_within(export_root.parent / "secrets" / "id_rsa", [export_root])


def test_a_root_prefix_match_is_not_enough(tmp_path):
    """`/uploads-evil` must not pass as being inside `/uploads`."""
    root = tmp_path / "uploads"
    root.mkdir()
    evil = tmp_path / "uploads-evil"
    evil.mkdir()
    (evil / "f.pdf").write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        ensure_within(evil / "f.pdf", [root])
