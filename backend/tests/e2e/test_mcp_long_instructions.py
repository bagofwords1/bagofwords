"""Over MCP, no instruction text is lost to a silent cut.

Contract:
- list_instructions may shorten text for size, but says so on every item:
  ``text`` is a prefix of the live text, ``text_truncated`` is true exactly
  when something was left out, and ``text_length`` is the full length.
- read_instruction returns any instruction in full, whatever its size: either
  whole in one call, or as an opening window that says it is partial and
  pages on (``next_offset``) until the pieces rebuild the exact text. No
  single response grows with the instruction, so an MCP client's own result
  cap never cuts one mid-text.
- read_instruction can grep a long instruction; a match deep inside a long
  line is shown, not just reported.
"""

import json
import uuid

import pytest

# The largest tool result a common MCP client keeps whole is ~25k tokens
# (Claude Code's default MAX_MCP_OUTPUT_TOKENS). One response must stay well
# under that, measured as the JSON text the client receives.
CLIENT_RESULT_BUDGET_CHARS = 60_000


@pytest.fixture
def mcp_user(enable_mcp, create_api_key, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)
    key = create_api_key(user_token=token, org_id=org_id)["key"]
    return {"token": token, "org_id": org_id, "key": key}


def _call(test_client, key, name, arguments):
    r = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    text = r.json()["result"]["content"][0]["text"]
    return json.loads(text), len(text)


def _published(create_global_instruction, mcp_user, text):
    return create_global_instruction(
        text=text, user_token=mcp_user["token"], org_id=mcp_user["org_id"], status="published",
    )["id"]


def _body(n_chars, tag):
    """A multi-line markdown-ish body of about n_chars, unique per tag."""
    lines, size, i = [], 0, 0
    while size < n_chars:
        line = f"- rule {i} for {tag}: revenue is booked net of refunds (ü€)" if i % 7 else f"## Section {i} {tag}"
        lines.append(line)
        size += len(line) + 1
        i += 1
    return "\n".join(lines)


def _read_all(test_client, key, inst_id):
    """Read an instruction over MCP the way a client should — follow
    next_offset (and next_col_offset inside a giant line) until a read is no
    longer truncated. Returns (rebuilt text, response sizes)."""
    out, size = _call(test_client, key, "read_instruction", {"instruction_id": inst_id})
    assert out["success"], out
    sizes, inst = [size], out["instruction"]
    text = inst["text"]
    for _ in range(1_000):
        if not inst.get("truncated"):
            return text, sizes
        args = {"instruction_id": inst_id, "offset": inst["next_offset"]}
        if inst.get("next_col_offset") is not None:
            args["col_offset"] = inst["next_col_offset"]
        out, size = _call(test_client, key, "read_instruction", args)
        assert out["success"], out
        sizes.append(size)
        inst = out["instruction"]
        assert inst["start_line"] == args["offset"]
        # A piece that starts mid-line continues the previous one directly.
        text += inst["text"] if inst.get("start_col") else "\n" + inst["text"]
    pytest.fail("read_instruction never reached the end of the instruction")


def _giant_line(n_chars, tail):
    return "{" + "\"k\": 1, " * (n_chars // 8) + "}" + tail


@pytest.mark.e2e
def test_list_instructions_marks_every_shortened_text(test_client, mcp_user, create_global_instruction):
    tag = uuid.uuid4().hex[:8]
    texts = {}
    for n in (30, 499, 500, 501, 2_345, 20_000):
        text = (f"[{tag}:{n}] " + _body(n, tag))[:n]
        texts[_published(create_global_instruction, mcp_user, text)] = text

    out, _ = _call(test_client, mcp_user["key"], "list_instructions", {"search": tag, "limit": 100})
    items = {i["id"]: i for i in out["instructions"] if i["id"] in texts}
    assert set(items) == set(texts)

    for inst_id, full in texts.items():
        item = items[inst_id]
        assert full.startswith(item["text"]), f"list text is not a prefix of the live text ({len(full)} chars)"
        assert item["text_length"] == len(full)
        assert item["text_truncated"] is (len(item["text"]) < len(full))
    assert any(i["text_truncated"] for i in items.values())
    assert any(not i["text_truncated"] for i in items.values())


@pytest.mark.e2e
def test_list_instructions_tells_the_client_where_the_full_text_is(test_client, mcp_user):
    r = test_client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"X-API-Key": mcp_user["key"]})
    tool = next(t for t in r.json()["result"]["tools"] if t["name"] == "list_instructions")
    assert "read_instruction" in tool["description"]


@pytest.mark.e2e
@pytest.mark.parametrize("n_chars", [800, 9_000, 45_000, 160_000])
def test_read_instruction_returns_the_whole_text_at_any_size(
    test_client, mcp_user, create_global_instruction, n_chars,
):
    full = _body(n_chars, uuid.uuid4().hex[:8])
    inst_id = _published(create_global_instruction, mcp_user, full)

    text, sizes = _read_all(test_client, mcp_user["key"], inst_id)

    assert text == full
    assert max(sizes) < CLIENT_RESULT_BUDGET_CHARS, sizes


@pytest.mark.e2e
def test_read_instruction_says_when_a_read_is_partial(test_client, mcp_user, create_global_instruction):
    full = _body(160_000, uuid.uuid4().hex[:8])
    inst_id = _published(create_global_instruction, mcp_user, full)

    out, _ = _call(test_client, mcp_user["key"], "read_instruction", {"instruction_id": inst_id})
    inst = out["instruction"]
    assert inst["truncated"] is True
    assert inst["total_chars"] == len(full)
    assert inst["total_lines"] == full.count("\n") + 1
    assert inst["outline"], "a partial read should carry an outline to navigate by"
    assert full.startswith(inst["text"])


@pytest.mark.e2e
def test_read_instruction_grep_shows_matches_anywhere_in_a_long_instruction(
    test_client, mcp_user, create_global_instruction,
):
    tag = uuid.uuid4().hex[:8]
    long_line = "x" * 3_000 + f" NEEDLE-{tag}-A use table fct_orders " + "y" * 3_000
    full = _body(120_000, tag) + "\n" + long_line + "\n" + _body(5_000, tag) + f"\nlast line NEEDLE-{tag}-B"
    inst_id = _published(create_global_instruction, mcp_user, full)

    out, size = _call(test_client, mcp_user["key"], "read_instruction",
                      {"instruction_id": inst_id, "grep_pattern": f"NEEDLE-{tag}"})
    inst = out["instruction"]
    shown = [m["line"] for m in inst["matches"]]
    assert inst["grep_total_matches"] == 2
    assert any(f"NEEDLE-{tag}-A use table fct_orders" in s for s in shown)
    assert any(f"NEEDLE-{tag}-B" in s for s in shown)
    lines = full.split("\n")
    for m in inst["matches"]:
        assert m["line"] in lines[m["line_no"] - 1]
    assert size < CLIENT_RESULT_BUDGET_CHARS


@pytest.mark.e2e
def test_read_instruction_range_read_is_verbatim(test_client, mcp_user, create_global_instruction):
    full = _body(30_000, uuid.uuid4().hex[:8])
    inst_id = _published(create_global_instruction, mcp_user, full)
    lines = full.split("\n")

    out, _ = _call(test_client, mcp_user["key"], "read_instruction",
                   {"instruction_id": inst_id, "offset": 40, "limit": 25})
    inst = out["instruction"]
    assert inst["text"] == "\n".join(lines[39:64])
    assert (inst["start_line"], inst["end_line"]) == (40, 64)


@pytest.mark.e2e
@pytest.mark.parametrize("shape", ["alone", "between_lines", "with_unicode"])
def test_read_instruction_returns_a_giant_line_whole_in_pieces(
    test_client, mcp_user, create_global_instruction, shape,
):
    """A line bigger than one read (a pasted JSON blob) is still returned in
    full: in pieces that say which line they cut and where to continue."""
    tail = f"TAIL-{uuid.uuid4().hex[:8]} use fct_orders"
    giant = _giant_line(120_000, tail)
    full = {
        "alone": giant,
        "between_lines": "# Mapping\n" + giant + "\nend of mapping",
        "with_unicode": "# מיפוי\n" + giant.replace("1, ", "€, ") + "\n" + _body(3_000, "he"),
    }[shape]
    inst_id = _published(create_global_instruction, mcp_user, full)

    text, sizes = _read_all(test_client, mcp_user["key"], inst_id)

    assert text == full
    assert max(sizes) < CLIENT_RESULT_BUDGET_CHARS, sizes

    out, _ = _call(test_client, mcp_user["key"], "read_instruction",
                   {"instruction_id": inst_id, "grep_pattern": "TAIL-"})
    assert any(tail in m["line"] for m in out["instruction"]["matches"])


@pytest.mark.e2e
def test_read_instruction_names_the_line_it_cut(test_client, mcp_user, create_global_instruction):
    giant = _giant_line(90_000, "end")
    inst_id = _published(create_global_instruction, mcp_user, "# Mapping\n" + giant + "\nafter")

    out, _ = _call(test_client, mcp_user["key"], "read_instruction", {"instruction_id": inst_id, "offset": 2})
    inst = out["instruction"]
    assert inst["truncated"] is True
    assert inst["clipped_line"] == 2 and inst["clipped_line_length"] == len(giant)
    assert inst["next_offset"] == 2 and inst["next_col_offset"] == len(inst["text"])
    assert giant.startswith(inst["text"])


@pytest.mark.e2e
def test_read_instruction_rejects_an_invalid_grep_pattern(test_client, mcp_user, create_global_instruction):
    inst_id = _published(create_global_instruction, mcp_user, _body(2_000, uuid.uuid4().hex[:8]))
    out, _ = _call(test_client, mcp_user["key"], "read_instruction",
                   {"instruction_id": inst_id, "grep_pattern": "(unclosed"})
    assert out["success"] is False
    assert out["error_message"]
