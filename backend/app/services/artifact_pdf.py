"""Parse a bounded document in a short-lived process, never the API event loop."""

import asyncio
import sys
from app.services.artifact_resource_service import fail

_SCRIPT = """
import io, sys, resource
resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
if sys.platform == 'linux':
    resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))
from pypdf import PdfReader
raw = sys.stdin.buffer.read(10485761)
if len(raw) > 10485760: raise ValueError('size')
r = PdfReader(io.BytesIO(raw))
if r.is_encrypted or len(r.pages) > 50: raise ValueError('unsupported')
text = ''
for page in r.pages:
    text += (page.extract_text() or '') + '\\n'
    if len(text) > 100000: break
sys.stdout.write(text[:100000])
"""


async def extract_pdf(raw):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(raw), 12)
        if process.returncode:
            fail("VALIDATION", "Unsupported or malformed PDF")
        return out.decode("utf-8")
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
