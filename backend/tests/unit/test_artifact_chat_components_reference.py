"""The authoring reference teaches the chat building blocks first.

Generated chat apps copy what the reference shows. Its CHAT BUILDING BLOCKS part
must name only sandbox globals that exist (``useConversation``, ``ChatMessage``,
``ChatResult`` in ``artifact-globals.js``), document the hook's return keys and
the note labels exactly as the globals define them, and come before the
low-level ``bow.conversations`` lines, which stay for what the blocks cannot do.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.ai.agents.planner.artifact_sdk_reference import ARTIFACT_SDK_REFERENCE as REF

ROOT = Path(__file__).resolve().parents[3]
GLOBALS = (ROOT / "frontend" / "public" / "libs" / "artifact-globals.js").read_text(encoding="utf-8")
BLOCKS = ("useConversation", "ChatMessage", "ChatResult")


def _blocks_part() -> str:
    assert "CHAT BUILDING BLOCKS" in REF and "LOW LEVEL" in REF
    return REF[REF.index("CHAT BUILDING BLOCKS"):REF.index("LOW LEVEL")]


def _keys(braced: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_]\w*)\s*(?=[,:}])", braced))


def test_every_building_block_the_reference_names_is_a_sandbox_global():
    part = _blocks_part()
    named = set(re.findall(r"\b(use[A-Z]\w*|Chat[A-Z]\w*)\b", part))
    assert set(BLOCKS) <= named
    for name in named:
        assert re.search(rf"^\s*window\.{re.escape(name)} = ", GLOBALS, re.M), f"{name} is not a sandbox global"


def test_the_documented_hook_return_keys_are_the_hooks_return_keys():
    part = _blocks_part()
    documented = re.search(r"//\s*(\{messages,[^}]*\})", part)
    assert documented, "the useConversation return keys are documented"
    hook = GLOBALS[GLOBALS.index("function useConversation("):]
    returned = re.search(r"return \{([^}]*)\};", hook)
    assert returned
    assert _keys(documented.group(1)) == _keys("{" + returned.group(1) + "}")


def test_the_documented_labels_are_the_globals_labels():
    part = _blocks_part()
    documented = re.search(r"labels=\{\{(.*?)\}\}", part, re.S)
    assert documented, "the note labels are documented"
    defaults = re.search(r"var _CHAT_LABELS = \{(.*?)\};", GLOBALS, re.S)
    assert defaults
    keys = lambda s: set(re.findall(r"([A-Za-z_]\w*)\s*:\s*'", s))  # noqa: E731
    assert keys(documented.group(1)) == keys(defaults.group(1)) != set()


def test_the_blocks_come_first_and_the_low_level_api_stays():
    conversations = REF[REF.index("CONVERSATIONS:"):]
    assert "CHAT BUILDING BLOCKS" in conversations and "LOW LEVEL" in conversations
    blocks, low = conversations.index("CHAT BUILDING BLOCKS"), conversations.index("LOW LEVEL")
    assert blocks < low < conversations.index("bow.conversations.send(")
    assert re.search(r"do not write[^.]*yourself", _blocks_part())
    # Paid answers start only on a user action, in both layers.
    assert "on a user action" in _blocks_part()
    assert "Send only on a user action" in conversations[low:]
    # The low-level result event shape and the results_incomplete re-read are still documented.
    assert re.search(r"\bresult \{id,block,title,columns:\[\{field,headerName,type\}\],rows", conversations)
    assert re.search(r"results\??:\[\.\.\.\]", conversations)
    assert "chart is null" in conversations and "never invent a chart" in conversations
    assert re.search(r"completed \{status, results_incomplete\?:true\}: when results_incomplete,[^;]*"
                     r"re-read messages\(\) and render its results for that message", conversations)


def test_results_render_through_chat_result_not_a_hand_rolled_copy():
    assert "function resultOption" not in REF and "ResultView" not in REF
    assert "<ChatResult result={r}" in REF


def test_the_reference_stays_within_its_size_budget():
    assert len(REF) <= 10500


def test_apps_read_host_context_from_artifact_data_during_render():
    # Finding 12: host_context arrives in ARTIFACT_DATA pushes, which re-render
    # useArtifactData(); bow.context carries only the server /context payload.
    assert "useArtifactData().host_context" in REF
    assert "never once on mount" in REF
    for sentence in re.split(r"(?<=[.;])\s", REF):
        assert not ("bow.context.subscribe" in sentence and "host_context" in sentence), sentence


def test_the_use_conversation_signature_says_to_omit_the_title():
    # Finding 13: the hook names a new conversation after its first question
    # (``o.title || text.slice(0, 80)``); a copied fixed title names them all alike.
    assert "o.title || text.slice(0, 80)" in GLOBALS
    signature = next(line for line in REF.splitlines() if "useConversation({" in line)
    assert "omit title" in signature
    assert "named after its first question" in signature
    assert not re.search(r"useConversation\(\{[^}]*title\s*:", REF), "no example passes a fixed title"
