"""Vision images must never breach a provider's per-image byte cap.

Bedrock Converse 400s the ENTIRE request when one image exceeds ~3.75 MB raw
(seen in the field: an 18 MB lossless PNG of a scanned ID page), killing the
planner turn. Normalization happens at the render source (PDF page rasterizing,
picture reads) and again as a safety net at the client boundary.
"""

import base64
import io
import os

import pytest
from PIL import Image

from app.ai.llm.image_utils import (
    PROVIDER_SAFE_IMAGE_BYTES,
    VISION_MAX_EDGE_PX,
    VISION_PNG_BYTE_THRESHOLD,
    normalize_image_bytes,
    normalize_image_input,
    sniff_image_mime,
)
from app.ai.llm.types import ImageInput
from app.ai.tools.implementations._file_tool_common import (
    render_file_images,
    render_pdf_pages_images,
)


def _noise_image(w, h):
    """Incompressible RGB noise — the worst case for any codec, standing in
    for a dense color scan."""
    return Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))


def _png_bytes(im):
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _flat_png(w=64, h=48, color=(200, 10, 10)):
    return _png_bytes(Image.new("RGB", (w, h), color))


# --- sniff_image_mime -------------------------------------------------------

def test_sniff_image_mime():
    assert sniff_image_mime(_flat_png()) == "image/png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    assert sniff_image_mime(buf.getvalue()) == "image/jpeg"
    assert sniff_image_mime(b"not an image", default="image/gif") == "image/gif"


# --- normalize_image_bytes --------------------------------------------------

def test_small_png_passes_through_untouched():
    raw = _flat_png()
    out, mime = normalize_image_bytes(raw)
    assert out is raw and mime == "image/png"


def test_heavy_png_is_reencoded_jpeg_under_caps():
    raw = _png_bytes(_noise_image(2200, 2200))
    assert len(raw) > PROVIDER_SAFE_IMAGE_BYTES  # premise: this WOULD 400 Bedrock
    out, mime = normalize_image_bytes(raw)
    assert mime == "image/jpeg"
    assert len(out) < PROVIDER_SAFE_IMAGE_BYTES
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= VISION_MAX_EDGE_PX


def test_oversized_dimensions_downscaled_even_when_bytes_small():
    # 4000px flat-color PNG is tiny in bytes but far past the useful edge —
    # the provider downscales it server-side anyway, so we do it here.
    raw = _png_bytes(Image.new("RGB", (4000, 500), (0, 80, 200)))
    out, mime = normalize_image_bytes(raw)
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= VISION_MAX_EDGE_PX
    assert mime in ("image/png", "image/jpeg")


def test_moderate_jpeg_passes_through_without_requality():
    buf = io.BytesIO()
    _noise_image(1200, 900).save(buf, format="JPEG", quality=90)
    raw = buf.getvalue()
    assert VISION_PNG_BYTE_THRESHOLD < len(raw) <= PROVIDER_SAFE_IMAGE_BYTES
    out, mime = normalize_image_bytes(raw)
    # Already lossy and within provider limits — re-encoding would only degrade.
    assert out is raw and mime == "image/jpeg"


def test_undecodable_bytes_pass_through():
    raw = b"\x00\x01garbage"
    out, mime = normalize_image_bytes(raw, "image/png")
    assert out is raw and mime == "image/png"


# --- normalize_image_input (client-boundary safety net) ---------------------

def test_image_input_within_limit_untouched():
    img = ImageInput(data=base64.b64encode(_flat_png()).decode(), media_type="image/png")
    assert normalize_image_input(img) is img


def test_image_input_url_untouched():
    img = ImageInput(data="https://example.com/x.png", source_type="url")
    assert normalize_image_input(img) is img


def test_oversized_image_input_shrunk():
    raw = _png_bytes(_noise_image(2200, 2200))
    assert len(raw) > PROVIDER_SAFE_IMAGE_BYTES
    img = ImageInput(data=base64.b64encode(raw).decode(), media_type="image/png")
    out = normalize_image_input(img)
    decoded = base64.b64decode(out.data)
    assert len(decoded) < PROVIDER_SAFE_IMAGE_BYTES
    assert out.media_type == sniff_image_mime(decoded)


# --- render paths -----------------------------------------------------------

def _scan_like_pdf_bytes(pages=1, px=1700):
    """A PDF whose pages are photographic noise — a stand-in for a scanned
    document. PIL embeds the page image; pypdfium2 rasterizes it back."""
    imgs = [_noise_image(px, px) for _ in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:], resolution=72)
    return buf.getvalue()


def test_render_pdf_pages_images_stays_under_provider_cap():
    images, total = render_pdf_pages_images(_scan_like_pdf_bytes(), 1, 1, dpi=150)
    assert total == 1 and len(images) == 1
    data, mime = images[0]
    assert mime in ("image/png", "image/jpeg")
    assert sniff_image_mime(data) == mime
    assert len(data) < PROVIDER_SAFE_IMAGE_BYTES
    im = Image.open(io.BytesIO(data))
    assert max(im.size) <= VISION_MAX_EDGE_PX


def test_render_file_images_picture_normalized():
    raw = _png_bytes(_noise_image(2200, 2200))
    images, total = render_file_images("scan.png", raw)
    assert total == 1 and len(images) == 1
    data, mime = images[0]
    assert mime == "image/jpeg"
    assert len(data) < PROVIDER_SAFE_IMAGE_BYTES


def test_render_file_images_small_picture_kept_verbatim():
    raw = _flat_png()
    images, total = render_file_images("logo.png", raw)
    assert total == 1
    data, mime = images[0]
    assert data == raw and mime == "image/png"


def test_render_file_images_corrupt_picture_yields_nothing():
    images, total = render_file_images("broken.png", b"\x89PNG but not really")
    assert images == [] and total == 0


# --- transcript replay ravel -------------------------------------------------

def test_transcript_hoists_observation_shaped_images_in_canonical_source():
    # Observation images use ImageInput shape (data/media_type/source_type);
    # replayed via the transcript they must become {"type": "base64", ...} —
    # Anthropic 400s on a source without "type" ("source.type: Field required").
    from app.ai.context.transcript import Transcript
    from app.ai.context.parts import ToolCallPart, ToolResultPart

    t = Transcript()
    t.add_assistant_step(text=None, calls=[ToolCallPart(id="c1", tool_name="read_file", args={})])
    t.add_tool_results([ToolResultPart(
        call_id="c1", tool_name="read_file", content="ok",
        images=[{"data": "IMGB64", "media_type": "image/jpeg", "source_type": "base64"}],
    )])
    msgs = t.to_model_messages()
    img_blocks = [b for m in msgs if isinstance(m.content, list)
                  for b in m.content if b.get("type") == "image"]
    assert len(img_blocks) == 1
    src = img_blocks[0]["source"]
    assert src == {"type": "base64", "media_type": "image/jpeg", "data": "IMGB64"}

    # Anthropic requires ALL tool_results at the head of the user message —
    # a multi-result turn must render [tr, tr, img, img], never [tr, img, tr].
    tm = Transcript()
    tm.add_assistant_step(text=None, calls=[
        ToolCallPart(id="m1", tool_name="read_file", args={}),
        ToolCallPart(id="m2", tool_name="read_file", args={}),
    ])
    tm.add_tool_results([
        ToolResultPart(call_id="m1", tool_name="read_file", content="a",
                       images=[{"data": "A", "media_type": "image/png", "source_type": "base64"}]),
        ToolResultPart(call_id="m2", tool_name="read_file", content="b",
                       images=[{"data": "B", "media_type": "image/png", "source_type": "base64"}]),
    ])
    result_turn = [m for m in tm.to_model_messages() if isinstance(m.content, list)
                   and any(b.get("type") == "tool_result" for b in m.content)][-1]
    types = [b["type"] for b in result_turn.content]
    assert types == ["tool_result", "tool_result", "image", "image"]

    # Already-canonical sources pass through untouched.
    t2 = Transcript()
    t2.add_assistant_step(text=None, calls=[ToolCallPart(id="c2", tool_name="x", args={})])
    canonical = {"type": "base64", "media_type": "image/png", "data": "ZZ"}
    t2.add_tool_results([ToolResultPart(call_id="c2", tool_name="x", content="ok", images=[canonical])])
    msgs2 = t2.to_model_messages()
    img2 = [b for m in msgs2 if isinstance(m.content, list)
            for b in m.content if b.get("type") == "image"]
    assert img2[0]["source"] is canonical


# --- anthropic images-param fold ---------------------------------------------

def test_anthropic_fold_places_images_after_tool_results():
    from app.ai.llm.clients.anthropic_client import Anthropic as AnthropicClient

    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
        {"type": "text", "text": "per-turn head"},
    ]}]
    AnthropicClient._fold_images(msgs, [ImageInput(data=base64.b64encode(_flat_png()).decode())])
    types = [b["type"] for b in msgs[0]["content"]]
    assert types == ["tool_result", "tool_result", "image", "text"]


def test_anthropic_fold_plain_text_last_message():
    from app.ai.llm.clients.anthropic_client import Anthropic as AnthropicClient

    msgs = [{"role": "user", "content": "look at this"}]
    AnthropicClient._fold_images(msgs, [ImageInput(data=base64.b64encode(_flat_png()).decode())])
    types = [b["type"] for b in msgs[0]["content"]]
    assert types == ["image", "text"]


# --- bedrock client boundary -------------------------------------------------

def test_bedrock_image_block_shrinks_oversized_image(monkeypatch):
    from app.ai.llm.clients.bedrock_client import BedrockClient

    raw = _png_bytes(_noise_image(2200, 2200))
    assert len(raw) > PROVIDER_SAFE_IMAGE_BYTES
    img = ImageInput(data=base64.b64encode(raw).decode(), media_type="image/png")
    block = BedrockClient._image_block(img)
    sent = block["image"]["source"]["bytes"]
    assert len(sent) < PROVIDER_SAFE_IMAGE_BYTES
    # Declared format must match the (possibly re-encoded) bytes.
    assert block["image"]["format"] == ("jpeg" if sniff_image_mime(sent) == "image/jpeg" else "png")
