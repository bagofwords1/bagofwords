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

Confinement: an uploaded document is content we do not control, and a
document can name other files and URLs (a linked image is fetched and drawn
into the output). The converter therefore runs with

* a scrubbed environment (nothing the API process was configured with),
* a throwaway profile whose settings refuse links to untrusted locations and
  never update links or run macros,
* and, on Linux, inside `_soffice_worker.py`: no network sockets, read access
  to the LibreOffice installation only, write access to its work directory
  only.

The last layer follows the code sandbox's policy: where the host cannot
provide it, conversion still runs with the first two (one warning per
process), unless the sandbox itself would refuse to run generated code there
(subprocess mode with ``BOW_SANDBOX_REQUIRE_LANDLOCK`` on), in which case
conversion is refused too, with a log line saying so. ``security.office_convert_confinement:
false`` turns the last layer off.

Best-effort by contract: every failure mode (binary absent, missing format
filter, timeout, corrupt input) returns None and the caller keeps its original
binary result.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

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


# Must match _soffice_worker.CONFINEMENT_UNAVAILABLE_EXIT.
CONFINEMENT_UNAVAILABLE_EXIT = 78
# soffice.bin asks to be started again (first run on a new profile). The
# `soffice` wrapper script does this itself; the confined path runs the binary.
_RESTART_EXIT = 81
_MAX_STARTS = 3
_unconfined_warned = False

# Profile settings for a converter of untrusted documents: links to anything
# outside a trusted location are not followed, links are never refreshed, and
# macros never run.
_PROFILE_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>
"""


def converter_environment(workdir: str) -> Dict[str, str]:
    """The converter's whole environment. Nothing is inherited."""
    return {
        "HOME": workdir,
        "TMPDIR": workdir,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def write_profile(profile: str) -> None:
    user = os.path.join(profile, "user")
    os.makedirs(user, exist_ok=True)
    with open(os.path.join(user, "registrymodifications.xcu"), "w", encoding="utf-8") as fh:
        fh.write(_PROFILE_SETTINGS)


def converter_args(workdir: str, src: str, profile: str) -> List[str]:
    """soffice arguments (without the program itself)."""
    return [
        "--headless",
        "--norestore",
        "--invisible",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to",
        "pdf",
        "--outdir",
        workdir,
        src,
    ]


def _confined_binary() -> Optional[str]:
    """The real LibreOffice binary next to the `soffice` wrapper script. The
    confined path execs it directly: the wrapper is a shell script that runs
    several helper programs, none of which conversion needs."""
    wrapper = shutil.which("soffice")
    if not wrapper or sys.platform != "linux":
        return None
    binary = os.path.join(os.path.dirname(os.path.realpath(wrapper)), "soffice.bin")
    return binary if os.path.isfile(binary) else None


def _confinement_enabled() -> bool:
    try:
        from app.settings.config import settings

        return bool(getattr(settings.bow_config.security, "office_convert_confinement", True))
    except Exception:
        return True


def _confinement_required() -> bool:
    """Exactly when the code sandbox itself refuses to run unconfined
    (runner.required_confinement_problem): subprocess mode with
    BOW_SANDBOX_REQUIRE_LANDLOCK on. With BOW_CODE_SANDBOX=inprocess generated
    code runs with no confinement at all, so conversion is not held to more."""
    from app.ai.code_execution.sandbox.config import MODE_SUBPROCESS, SandboxLimits, sandbox_mode

    return sandbox_mode() == MODE_SUBPROCESS and SandboxLimits.from_env().require_landlock


def _run(cmd: List[str], *, env: Dict[str, str], cwd: str, timeout: float) -> Tuple[int, bytes]:
    """Run to completion or kill the whole process group at the deadline."""
    proc = subprocess.Popen(
        cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        raise
    return proc.returncode, out or b""


def _instance_pipes() -> set:
    # LibreOffice binds one socket per profile here, whatever TMPDIR says.
    return set(glob.glob(f"/tmp/OSL_PIPE_{os.getuid()}_SingleOfficeIPC_*"))


def _run_confined(binary: str, args: List[str], *, env: Dict[str, str], workdir: str,
                  timeout: float) -> Tuple[int, bytes]:
    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_soffice_worker.py")
    cmd = [sys.executable, "-I", "-S", launcher, workdir, binary, *args]
    deadline = time.monotonic() + timeout
    rc, out = 1, b""
    for _ in range(_MAX_STARTS):
        before = _instance_pipes()
        try:
            rc, out = _run(cmd, env=env, cwd=workdir, timeout=deadline - time.monotonic())
        finally:
            # The confined process may create its instance socket but not
            # remove it, and a leftover one makes the next start fail.
            for path in _instance_pipes() - before:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if rc != _RESTART_EXIT:
            break
    return rc, out


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

    global _unconfined_warned
    with tempfile.TemporaryDirectory(prefix="bow_soffice_") as tmp:
        tmp = os.path.realpath(tmp)
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
        env = converter_environment(tmp)
        args = converter_args(tmp, src, profile)
        try:
            write_profile(profile)
            binary = _confined_binary() if _confinement_enabled() else None
            returncode, output = CONFINEMENT_UNAVAILABLE_EXIT, b""
            if binary:
                returncode, output = _run_confined(binary, args, env=env, workdir=tmp, timeout=timeout)
            if returncode == CONFINEMENT_UNAVAILABLE_EXIT:
                if _confinement_enabled() and _confinement_required():
                    logger.warning(
                        "office_to_pdf_bytes: not converting %s — this host cannot confine the "
                        "converter and BOW_SANDBOX_REQUIRE_LANDLOCK requires it. %s",
                        name, output.decode("utf-8", "replace").strip()[:300],
                    )
                    return None
                if _confinement_enabled() and not _unconfined_warned:
                    _unconfined_warned = True
                    logger.warning(
                        "office_to_pdf_bytes: filesystem/network confinement of the document "
                        "converter is unavailable on this host (%s); converting with a scrubbed "
                        "environment and a locked-down profile only.",
                        output.decode("utf-8", "replace").strip()[:300] or "unsupported platform",
                    )
                returncode, output = _run(["soffice", *args], env=env, cwd=tmp, timeout=timeout)
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
            detail = (output or b"").decode("utf-8", "replace").strip()
            logger.info(
                "office_to_pdf_bytes: no PDF produced for %s (rc=%s) %s",
                name, returncode, detail[:300],
            )
            return None
        try:
            with open(produced[0], "rb") as fh:
                pdf = fh.read()
        except OSError as e:
            logger.info("office_to_pdf_bytes: could not read converted PDF for %s: %s", name, e)
            return None

    return pdf or None
