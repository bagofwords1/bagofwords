"""LibreOffice-backed conversion of Office documents to PDF.

Why this exists: PDFs that can't be extracted as text have a vision fallback —
rasterize the pages and let the model look at them. docx/pptx had none, because
their page layout doesn't exist until a layout engine has run. A Word file
wrapping scanned images therefore dead-ended as an opaque "binary" read: no
text, nothing to rasterize, and (before this) not even attachable.

LibreOffice is already in the runtime image — it backs the pptx slide previews
in `pptx_executor` — so converting to PDF and handing the result to the
existing pypdfium2 rasterizer gives docx/pptx the same fallback PDFs have,
with no new Python dependency.

Note on packaging: `soffice` on PATH is NOT sufficient. The binary and UNO
framework ship in `libreoffice-core`, but each format's import filter ships
with its application module — Writer's WordprocessingML filter
(`libwriterfilterlo.so`) is in `libreoffice-writer`, Impress's in
`libreoffice-impress`. With the wrong module set, type detection succeeds and
loading then fails with "source file could not be loaded". The Dockerfile
installs both.

Best-effort by contract: every failure mode (binary absent, missing format
filter, timeout, corrupt input) returns None and the caller keeps its original
binary result.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

# Formats worth routing through LibreOffice. Deliberately limited to the
# document types the read path can otherwise do nothing with — PDFs and
# pictures already rasterize directly, and tabular formats render as text.
CONVERTIBLE_EXTS = {"docx", "doc", "pptx", "ppt", "odt", "odp", "rtf"}

# LibreOffice cold-starts in seconds and is slower on a big document; this is a
# backstop against a wedged process, not an expected duration.
DEFAULT_TIMEOUT_SECONDS = 90


def _ext(name: str) -> str:
    leaf = str(name or "").rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""


def soffice_available() -> bool:
    return shutil.which("soffice") is not None


def office_to_pdf_bytes(
    data: bytes,
    name: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[bytes]:
    """Convert an Office document to PDF bytes, or None if it can't be done.

    Blocking (spawns a subprocess) — call it from a worker thread, never
    directly on the event loop.
    """
    ext = _ext(name)
    if ext not in CONVERTIBLE_EXTS:
        return None
    if not data:
        return None
    if not soffice_available():
        logger.info(
            "office_to_pdf_bytes: soffice not on PATH — cannot render %s for vision", name
        )
        return None

    with tempfile.TemporaryDirectory(prefix="bow_soffice_") as tmp:
        src = os.path.join(tmp, f"source.{ext}")
        try:
            with open(src, "wb") as fh:
                fh.write(data)
        except OSError as e:
            logger.info("office_to_pdf_bytes: could not stage %s: %s", name, e)
            return None

        # A per-call user profile. LibreOffice serializes on a shared profile
        # directory, so concurrent conversions with the default profile either
        # block each other or fail outright — this makes parallel reads safe.
        # It lives inside the temp dir, so cleanup is automatic.
        profile = os.path.join(tmp, "profile")
        cmd = [
            "soffice",
            "--headless",
            "--norestore",
            "--invisible",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            tmp,
            src,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.info("office_to_pdf_bytes: timed out after %ss on %s", timeout, name)
            return None
        except (OSError, ValueError) as e:
            logger.info("office_to_pdf_bytes: could not run soffice for %s: %s", name, e)
            return None

        # soffice exits 0 even when it refuses the input (the "source file
        # could not be loaded" case), so the produced file is the real check.
        produced = glob.glob(os.path.join(tmp, "*.pdf"))
        if not produced:
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            logger.info(
                "office_to_pdf_bytes: no PDF produced for %s (rc=%s) %s",
                name, proc.returncode, detail[:300],
            )
            return None
        try:
            with open(produced[0], "rb") as fh:
                pdf = fh.read()
        except OSError as e:
            logger.info("office_to_pdf_bytes: could not read converted PDF for %s: %s", name, e)
            return None

    return pdf or None
