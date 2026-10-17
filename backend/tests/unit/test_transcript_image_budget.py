"""Tool images in the in-run transcript: bounded, budgeted, never re-sent.

Contracts:
1. A run that reads many scanned pages keeps images only for the results of
   the most recent planner steps, and never more than the image cap — older
   results keep their text plus a note on how to re-read the pages.
2. The budget counts what an image costs, so fit_to_budget (and therefore the
   context-overflow recovery that drives it) can actually shrink an
   image-heavy prompt.
3. The local token estimate charges non-Latin scripts what they cost.
4. Tool images reach a request through exactly one channel; user uploads keep
   theirs.

Born from a live failure: a SharePoint list fill over ~50 scanned PDFs died
mid-turn with "prompt is too long: 1001304 tokens > 1000000 maximum" because
every page image ever read was re-sent on every later planner call.
"""
from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.ai.agent_v2 import AgentV2
from app.ai.agents.planner import PlannerV2, PlannerV3
from app.ai.context.parts import (
    ToolCallPart,
    ToolResultPart,
    estimate_tokens,
    image_tokens,
)
from app.ai.context.result_parts import build_result_part
from app.ai.context.transcript import (
    IMAGE_RETENTION_STEPS,
    MAX_RETAINED_IMAGES,
    Transcript,
)
from app.schemas.ai.planner import PlannerInput


def _page(width=1210, height=1568, fmt="PNG") -> dict:
    """A rendered page as read_file attaches it (ImageInput shape)."""
    buf = io.BytesIO()
    Image.new("L", (width, height), 255).save(buf, format=fmt)
    return {
        "data": base64.b64encode(buf.getvalue()).decode(),
        "media_type": f"image/{fmt.lower()}",
        "source_type": "base64",
    }


PAGE = _page()


def _read_result(call_id: str, pages: int, text_chars: int = 600) -> ToolResultPart:
    observation = {
        "success": True,
        "summary": f"Read {pages} page(s) as images",
        "file_id": f"Case-{call_id}/scan.pdf",
        "content_type": "images",
        "content": "x" * text_chars,
        "images": [dict(PAGE) for _ in range(pages)],
    }
    return build_result_part(call_id=call_id, tool_name="read_file", observation=observation)


def _run(steps: int, pages_per_step: int, reads_per_step: int = 1, text_chars: int = 600) -> Transcript:
    t = Transcript()
    t.add_user_text("<static context>" + "s" * 2000)
    t.add_user_text("Go folder by folder, read every scan, save one row per folder.")
    for i in range(steps):
        calls, results = [], []
        for j in range(reads_per_step):
            cid = f"c{i}_{j}"
            calls.append(ToolCallPart(
                id=cid, tool_name="read_file",
                args={"file_id": f"Case-{i}/scan{j}.pdf", "page_range": "1-8"},
            ))
            results.append(_read_result(cid, pages_per_step, text_chars))
        t.add_assistant_step(calls=calls)
        t.add_tool_results(results)
    return t


def _rendered(t: Transcript):
    """(image blocks per message, tool_result bodies, estimated request cost)."""
    images_per_msg, bodies, cost = [], [], 0
    for m in t.to_model_messages():
        blocks = m.content if isinstance(m.content, list) else [{"type": "text", "text": m.content}]
        n = 0
        for b in blocks:
            if b["type"] == "image":
                n += 1
                cost += image_tokens(b["source"])
            elif b["type"] == "tool_result":
                bodies.append(b["content"])
                cost += estimate_tokens(b["content"])
            elif b["type"] == "text":
                cost += estimate_tokens(b["text"])
            elif b["type"] == "tool_use":
                cost += estimate_tokens(json.dumps(b["input"]))
        images_per_msg.append(n)
    return images_per_msg, bodies, cost


# --- 1. retention ---------------------------------------------------------

@pytest.mark.parametrize("steps,pages", [(40, 1), (50, 4), (60, 8)])
def test_long_scan_run_keeps_images_only_for_recent_steps(steps, pages):
    t = _run(steps, pages)
    per_msg, bodies, _ = _rendered(t)

    with_images = [i for i, n in enumerate(per_msg) if n]
    assert sum(per_msg) <= MAX_RETAINED_IMAGES
    # Only the newest result turns carry pictures — never anything older.
    assert len(with_images) <= IMAGE_RETENTION_STEPS
    assert all(i >= len(per_msg) - 2 * IMAGE_RETENTION_STEPS for i in with_images)
    # The newest read is always visible in full (up to the cap).
    assert per_msg[-1] == min(pages, MAX_RETAINED_IMAGES)

    # Older reads say how to get the pages back instead of looking empty.
    older = bodies[: steps - IMAGE_RETENTION_STEPS]
    assert older and all("read_file" in b and "page_range" in b for b in older)
    assert not any("page_range" in b for b in bodies[-1:])


def test_one_huge_step_is_capped_newest_kept():
    """Parallel reads in a single step can exceed the cap on their own."""
    t = _run(steps=2, pages_per_step=8, reads_per_step=5)
    per_msg, _, _ = _rendered(t)
    assert sum(per_msg) == MAX_RETAINED_IMAGES
    assert per_msg[-1] == MAX_RETAINED_IMAGES


def test_released_image_is_never_sent_again():
    t = _run(steps=IMAGE_RETENTION_STEPS + 1, pages_per_step=2)
    first_call_id = t.turns[3].parts[0].call_id
    sent_before = _rendered(t)[0]
    # Grow the transcript and render repeatedly: the aged-out result stays
    # image-free no matter how often the request is rebuilt.
    for _ in range(3):
        t.to_model_messages()
    oldest = next(p for turn in t.turns for p in turn.parts
                  if isinstance(p, ToolResultPart) and p.call_id == first_call_id)
    assert oldest.images == []
    assert _rendered(t)[0] == sent_before


# --- 2. budget ------------------------------------------------------------

def test_transcript_size_includes_image_cost():
    t = _run(steps=1, pages_per_step=0)
    base = t.tokens()
    t.add_assistant_step(calls=[ToolCallPart(id="img", tool_name="read_file", args={})])
    t.add_tool_results([_read_result("img", pages=3, text_chars=0)])
    assert t.tokens() - base >= 3 * image_tokens(PAGE)
    # ~2.4k tokens for a 1568 px portrait page.
    assert 2000 <= image_tokens(PAGE) <= 3200


@pytest.mark.parametrize("pages", [4, 8])
def test_fit_to_budget_shrinks_what_is_actually_sent(pages):
    t = _run(steps=12, pages_per_step=pages, text_chars=2000)
    _, _, before = _rendered(t)
    # Room for the newest step's pages plus text — not for the retained
    # window's worth of pages.
    budget = pages * image_tokens(PAGE) + 8000
    assert before > budget

    stats = t.fit_to_budget(budget)

    _, _, after = _rendered(t)
    assert after <= budget
    assert stats["reached"] is True
    assert stats["after"] >= after * 0.9  # the ladder sees what is sent
    # The step the model just took keeps its pages.
    assert _rendered(t)[0][-1] == pages


def test_context_overflow_recovery_brings_image_heavy_prompt_under_the_limit():
    limit = 64_000
    t = _run(steps=10, pages_per_step=8, text_chars=12_000)
    _, _, before = _rendered(t)
    assert before > limit
    agent = SimpleNamespace(
        model=SimpleNamespace(context_window_tokens=limit),
        transcript=t,
        _context_budget_factor=1.0,
        _compaction_attempted=True,  # compaction folds prior turns, not this run
    )

    import asyncio
    asyncio.run(AgentV2._handle_context_overflow(
        agent, f"prompt is too long: {before} tokens > {limit} maximum"
    ))

    _, _, after = _rendered(t)
    assert after < limit


# --- 3. estimate ----------------------------------------------------------

HEBREW = (
    "תיק מספר 2024-117: הסכם שכירות בין חברת אלון נכסים לבין משפחת כהן. "
    "תאריך חתימה 12 במרץ 2023, דמי שכירות חודשיים 8,500 ש\"ח, לשלוש שנים."
)
ENGLISH = (
    "Case number 2024-117: lease agreement between Alon Properties and the "
    "Cohen family. Signed 12 March 2023, monthly rent 8,500 ILS, three years."
)


@pytest.mark.parametrize(
    "text",
    [HEBREW, HEBREW * 7, "مرحبا بالعالم، هذا اختبار " * 5, "Привет, мир " * 9],
    ids=["hebrew", "hebrew-long", "arabic", "cyrillic"],
)
def test_non_latin_text_is_not_estimated_at_english_rates(text):
    assert estimate_tokens(text) >= 2 * (len(text) // 4)


@pytest.mark.parametrize(
    "text", [ENGLISH, ENGLISH * 13, "select * from sales where id = 1;"],
    ids=["english", "english-long", "sql"],
)
def test_english_estimate_is_unchanged(text):
    assert estimate_tokens(text) == len(text) // 4


def test_json_escaped_hebrew_is_charged_for_its_escapes():
    """Tool results are json.dumps'd with ensure_ascii, so Hebrew arrives as
    \\uXXXX escapes — pure ASCII that still costs ~3-4 tokens per letter."""
    blob = json.dumps({"rows": [{"name": "שם הלקוח", "city": "תל אביב"}] * 10})
    assert blob.isascii()
    assert estimate_tokens(blob) >= 1.5 * (len(blob) // 4)


# --- 4. one channel per image -----------------------------------------------

def _agent(planner_cls):
    return SimpleNamespace(planner=object.__new__(planner_cls))


def _planner_input(**kw):
    return PlannerInput(
        organization_name="Org", organization_ai_analyst_name="AI", timezone="UTC",
        user_message="read the scans", tool_catalog=[], mode=kw.pop("mode", "chat"), **kw,
    )


@pytest.fixture
def transcript_on(monkeypatch):
    monkeypatch.delenv("BOW_PLANNER_TRANSCRIPT", raising=False)


def test_transcript_path_sends_tool_images_once_and_keeps_uploads(transcript_on):
    upload, page = {"data": "UPLOAD"}, {"data": "PAGE"}
    images = AgentV2._planner_images(_agent(PlannerV3), _planner_input(), [upload], [page])
    assert images == [upload]


@pytest.mark.parametrize("planner_cls,mode,flag", [
    (PlannerV2, "chat", None),          # legacy planner: no transcript
    (PlannerV3, "knowledge", None),     # knowledge harness: no transcript
    (PlannerV3, "chat", "0"),           # transcript path switched off
])
def test_non_transcript_paths_still_get_tool_images(monkeypatch, planner_cls, mode, flag):
    if flag is None:
        monkeypatch.delenv("BOW_PLANNER_TRANSCRIPT", raising=False)
    else:
        monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", flag)
    upload, page = {"data": "UPLOAD"}, {"data": "PAGE"}
    images = AgentV2._planner_images(_agent(planner_cls), _planner_input(mode=mode), [upload], [page])
    assert images == [upload, page]


def test_uploads_alone_still_reach_the_planner(transcript_on):
    upload = {"data": "UPLOAD"}
    assert AgentV2._planner_images(_agent(PlannerV3), _planner_input(), [upload], []) == [upload]
    assert AgentV2._planner_images(_agent(PlannerV3), _planner_input(), [], []) is None
