"""Behavioral invariants for instruction tracked-change hunks."""

from app.services.text_hunks import rebased_hunks_against_main


def _long_instruction() -> str:
    # Deliberately repetitive and comfortably beyond MAX_DIFF_TOKENS: this is
    # the shape that previously triggered the whole-document fallback.
    return "\n\n".join(
        f"## Rule {index}\n```python\nvalue = {index}\n```\nKeep this rule unchanged."
        for index in range(700)
    )


def test_large_instruction_keeps_a_small_change_small_and_stable_after_main_moves():
    base = _long_instruction()
    needle = "## Rule 350\n"
    proposed = base.replace(needle, needle + "hello world\n\n", 1)

    initial = rebased_hunks_against_main(base, proposed, base)
    assert len(initial) == 1
    assert initial[0]["before"] == ""
    assert initial[0]["after"] == "hello world\n\n"

    # An unrelated main edit must not turn the same suggestion into a new
    # whole-document action or change the review identity.
    moved_main = base + "\n\nA later independent rule."
    moved = rebased_hunks_against_main(base, proposed, moved_main)
    assert len(moved) == 1
    assert moved[0]["key"] == initial[0]["key"]
    assert moved[0]["before"] == ""
    assert moved[0]["after"] == "hello world\n\n"
