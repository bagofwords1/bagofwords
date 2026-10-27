#!/usr/bin/env python3
"""Minimal fake ServiceNow instance for the file-tool sandbox loops.

Serves just what ``ServiceNowClient`` needs for a KB article with an attached
Word document:

- ``GET /api/now/table/<table>``            Table API rows (kb_knowledge + probes)
- ``GET /api/now/attachment``               attachment metadata listing (sys_attachment)
- ``GET /api/now/attachment/<sys_id>``      one attachment's metadata
- ``GET /api/now/attachment/<sys_id>/file`` the binary

The attachment is a real .docx built at startup: two pages of procedure text
plus an embedded screenshot. The screenshot carries a code
(``SCREEN_CODE``) that appears NOWHERE in the document text — a model can only
report it if the page image actually reached it.

Run:  cd backend && uv run python ../tools/agent/fake_servicenow.py
Env:  FAKE_SN_PORT (default 9311)
Any basic-auth credentials are accepted.
"""
import io
import os
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageDraw, ImageFont

SCREEN_CODE = "ZEBRA-7731"
KB_SYS_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
ATT_SYS_ID = "f0e1d2c3b4a5968778695a4b3c2d1e0f"
FILE_NAME = "KB - (004) Digital.docx"

app = FastAPI()


def _screenshot_png() -> bytes:
    img = Image.new("RGB", (1200, 700), "#f1f5f9")
    d = ImageDraw.Draw(img)
    try:
        big = ImageFont.load_default(size=72)
        mid = ImageFont.load_default(size=40)
    except TypeError:  # Pillow < 10.1
        big = mid = ImageFont.load_default()
    d.rectangle([0, 0, 1200, 90], fill="#1e3a8a")
    d.text((30, 20), "SquaredUp - Transfers dashboard", fill="white", font=mid)
    d.rectangle([60, 160, 1140, 420], outline="#dc2626", width=8)
    d.text((100, 200), "ROUTING CODE:", fill="#111827", font=big)
    d.text((100, 300), SCREEN_CODE, fill="#dc2626", font=big)
    d.text((60, 500), "APPD node availability: 3/4 nodes up", fill="#111827", font=mid)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _docx() -> bytes:
    """A real Office Open XML document: text paragraphs + one inline picture."""
    png = _screenshot_png()
    paras = [
        "Purpose: work instruction for investigating faults in the new transfers service.",
        "Step 1: check the APPD bridge and the infrastructure map in SquaredUp.",
        "Step 2: check the STAR and CGW servers.",
        "Step 3: look for HTTP 500 errors on the UI server and review All Nodes Availability.",
        "The screenshot below shows the dashboard to open, including the routing code to "
        "quote when escalating to the network team.",
    ]
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paras
    )
    cx, cy = 5486400, 3200400  # 6in x 3.5in in EMU
    drawing = (
        '<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="1" name="Picture 1"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:nvPicPr><pic:cNvPr id="0" name="screenshot.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="rIdImg1"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    )
    tail = '<w:p><w:r><w:t>End of procedure.</w:t></w:r></w:p>'
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f'<w:body>{body}{drawing}{tail}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdImg1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/image1.png"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/media/image1.png", png)
    return buf.getvalue()


DOCX = _docx()
ATTACHMENT = {
    "sys_id": ATT_SYS_ID,
    "file_name": FILE_NAME,
    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "size_bytes": str(len(DOCX)),
    "table_name": "kb_knowledge",
    "table_sys_id": KB_SYS_ID,
    "sys_updated_on": "2026-10-20 09:00:00",
}
KB_ROW = {
    "sys_id": KB_SYS_ID,
    "number": "KB0013962",
    "short_description": "Investigating faults in the new transfers service",
    "workflow_state": "published",
    "sys_updated_on": "2026-10-20 09:00:00",
}
STATS = {"attachment_downloads": 0, "table_reads": 0}


@app.get("/api/now/table/{table}")
async def table(table: str):
    STATS["table_reads"] += 1
    if table == "kb_knowledge":
        return {"result": [KB_ROW]}
    if table == "sys_user":
        return {"result": [{"sys_id": "0" * 32, "user_name": "integration"}]}
    if table == "sys_dictionary":
        return {"result": [{"element": "number"}]}
    return {"result": []}


@app.get("/api/now/attachment")
async def attachments(request: Request):
    q = request.query_params.get("sysparm_query", "")
    if "kb_knowledge" in q or FILE_NAME in q or KB_SYS_ID in q:
        return {"result": [ATTACHMENT]}
    return {"result": []}


@app.get("/api/now/attachment/{sys_id}")
async def attachment_meta(sys_id: str):
    if sys_id == ATT_SYS_ID:
        return {"result": ATTACHMENT}
    return JSONResponse({"error": {"message": "not found"}}, status_code=404)


@app.get("/api/now/attachment/{sys_id}/file")
async def attachment_file(sys_id: str):
    if sys_id != ATT_SYS_ID:
        return JSONResponse({"error": {"message": "not found"}}, status_code=404)
    STATS["attachment_downloads"] += 1
    return Response(DOCX, media_type=ATTACHMENT["content_type"])


@app.get("/stats")
async def stats():
    return STATS


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FAKE_SN_PORT", "9311")), log_level="warning")
