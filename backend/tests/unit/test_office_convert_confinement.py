"""Office documents are converted to PDF by a converter that is boxed in.

The contract: converting an uploaded document can only draw on the document
itself. The converter does not get the API process's environment, does not
follow the document's links to other files, and still converts ordinary
documents. Rows that need LibreOffice are skipped where it is not installed.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest

from app.data_sources.clients import _office_convert as oc

needs_soffice = pytest.mark.skipif(not oc.soffice_available(), reason="LibreOffice is not installed")


def _odt(body: str) -> bytes:
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.2">'
        f"<office:body><office:text>{body}</office:text></office:body></office:document-content>"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
        z.writestr("META-INF/manifest.xml", manifest)
    return buf.getvalue()


def _image_count(pdf: bytes) -> int:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    doc = pdfium.PdfDocument(pdf)
    try:
        return sum(
            1 for page in doc for obj in page.get_objects(max_depth=4) if obj.type == raw.FPDF_PAGEOBJ_IMAGE
        )
    finally:
        doc.close()


def _text(pdf: bytes) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        return "\n".join(page.get_textpage().get_text_range() for page in doc)
    finally:
        doc.close()


def test_converter_environment_is_built_from_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("BOW_ENCRYPTION_KEY", "must-not-travel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-travel")
    env = oc.converter_environment(str(tmp_path))
    assert "must-not-travel" not in env.values()
    assert not [k for k in env if k.startswith(("BOW_", "AWS_"))]
    assert env["HOME"] == str(tmp_path) and env["TMPDIR"] == str(tmp_path)


def test_every_conversion_runs_on_its_own_throwaway_profile(tmp_path):
    work = str(tmp_path)
    profile = os.path.join(work, "profile")
    args = oc.converter_args(work, os.path.join(work, "source.docx"), profile)
    assert f"-env:UserInstallation=file://{profile}" in args
    assert args[args.index("--outdir") + 1] == work
    oc.write_profile(profile)
    settings = open(os.path.join(profile, "user", "registrymodifications.xcu"), encoding="utf-8").read()
    assert "BlockUntrustedRefererLinks" in settings and "DisableMacrosExecution" in settings


@needs_soffice
def test_an_ordinary_document_still_converts():
    pdf = oc.office_to_pdf_bytes(_odt("<text:p>Quarterly numbers look fine</text:p>"), "report.odt")
    assert pdf and pdf.startswith(b"%PDF-")
    assert "Quarterly numbers look fine" in _text(pdf)


@needs_soffice
def test_a_document_cannot_pull_another_file_into_its_pdf(tmp_path):
    from PIL import Image

    outside = tmp_path / "elsewhere.png"
    Image.new("RGB", (48, 48), (200, 0, 0)).save(outside)
    linked = _odt(
        "<text:p>Has a linked picture"
        '<draw:frame draw:name="p" text:anchor-type="as-char" svg:width="2cm" svg:height="2cm">'
        f'<draw:image xlink:href="file://{outside}" xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/>'
        "</draw:frame></text:p>"
    )
    pdf = oc.office_to_pdf_bytes(linked, "linked.odt")
    assert pdf and "Has a linked picture" in _text(pdf)
    assert _image_count(pdf) == 0


@needs_soffice
def test_conversion_leaves_nothing_behind(tmp_path, monkeypatch):
    import glob
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    pipes = set(glob.glob("/tmp/OSL_PIPE_*"))
    assert oc.office_to_pdf_bytes(_odt("<text:p>tidy</text:p>"), "tidy.odt")
    assert os.listdir(tmp_path) == []
    assert set(glob.glob("/tmp/OSL_PIPE_*")) <= pipes


# (BOW_CODE_SANDBOX, BOW_SANDBOX_REQUIRE_LANDLOCK, ENVIRONMENT) -> converts without confinement?
# The same table the code sandbox uses to decide whether generated code may
# run on a host without Landlock.
DECISIONS = [
    ("subprocess", "1", "production", False),
    ("subprocess", None, "production", False),   # production default: required
    ("subprocess", "0", "production", True),
    ("subprocess", None, "development", True),   # development default: not required
    ("subprocess", "1", "development", False),
    ("inprocess", "1", "production", True),      # the sandbox itself is off
    ("inprocess", None, "production", True),
    ("inprocess", "0", "development", True),
]


@pytest.mark.parametrize("mode, require, environment, converts", DECISIONS)
@pytest.mark.parametrize("launcher_output", [b"landlock unavailable", b"cannot install syscall restrictions"])
def test_when_the_host_cannot_confine_the_converter(monkeypatch, caplog, mode, require, environment, converts, launcher_output):
    from app.ai.code_execution.sandbox.runner import required_confinement_problem  # noqa: F401 - same policy source

    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    monkeypatch.setenv("ENVIRONMENT", environment)
    if require is None:
        monkeypatch.delenv("BOW_SANDBOX_REQUIRE_LANDLOCK", raising=False)
    else:
        monkeypatch.setenv("BOW_SANDBOX_REQUIRE_LANDLOCK", require)
    monkeypatch.setattr(oc, "soffice_available", lambda: True)
    monkeypatch.setattr(oc, "_confined_binary", lambda: "/opt/office/soffice.bin")
    monkeypatch.setattr(oc, "_unconfined_warned", False)
    monkeypatch.setattr(oc, "_run_confined", lambda *a, **k: (oc.CONFINEMENT_UNAVAILABLE_EXIT, launcher_output))
    ran = []

    def fallback(cmd, *, env, cwd, timeout):
        ran.append((cmd, env))
        with open(os.path.join(cwd, "source.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 converted")
        return 0, b""

    monkeypatch.setattr(oc, "_run", fallback)
    with caplog.at_level("WARNING"):
        first = oc.office_to_pdf_bytes(_odt("<text:p>x</text:p>"), "x.odt")
        second = oc.office_to_pdf_bytes(_odt("<text:p>y</text:p>"), "y.odt")
    notes = [r.getMessage() for r in caplog.records if "office_to_pdf_bytes" in r.getMessage()]
    if converts:
        assert first == second == b"%PDF-1.4 converted"
        cmd, env = ran[0]
        # The fallback is still the scrubbed, locked-down converter.
        assert cmd[0] == "soffice" and any(a.startswith("-env:UserInstallation=") for a in cmd)
        assert env == oc.converter_environment(env["HOME"])
        assert len(notes) == 1  # one warning per process, not per document
    else:
        assert first is None and second is None and ran == []
        assert len(notes) == 2 and all("not converting" in n for n in notes)


def test_a_host_that_can_confine_never_uses_the_fallback(monkeypatch):
    monkeypatch.setattr(oc, "soffice_available", lambda: True)
    monkeypatch.setattr(oc, "_confined_binary", lambda: "/opt/office/soffice.bin")

    def confined(binary, args, *, env, workdir, timeout):
        with open(os.path.join(workdir, "source.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 confined")
        return 0, b""

    monkeypatch.setattr(oc, "_run_confined", confined)
    monkeypatch.setattr(oc, "_run", lambda *a, **k: pytest.fail("unconfined converter was started"))
    assert oc.office_to_pdf_bytes(_odt("<text:p>x</text:p>"), "x.odt") == b"%PDF-1.4 confined"
