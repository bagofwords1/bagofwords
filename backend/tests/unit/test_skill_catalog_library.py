"""The shipped pre-built skill library must be well-formed.

`_parse_skill_file` drops a malformed file with a log line rather than raising,
so a typo in frontmatter would silently remove a skill from the catalog. These
tests are what turns that into a failing build.
"""
import pathlib

import pytest

from app.ai.skills.catalog import (
    LIBRARY_DIR,
    MAX_DESCRIPTION_LEN,
    VALID_CATEGORIES,
    VALID_MODES,
    _parse_skill_file,
    get_prebuilt_skill,
    list_prebuilt_skills,
)


def _library_files():
    return sorted(LIBRARY_DIR.glob("*.md"))


def test_library_is_not_empty():
    assert _library_files(), f"no skill files found in {LIBRARY_DIR}"


def test_every_shipped_file_parses():
    """A file that fails to parse is dropped silently at runtime."""
    unparsed = [p.name for p in _library_files() if _parse_skill_file(p) is None]
    assert unparsed == [], f"malformed skill files: {unparsed}"


def test_catalog_exposes_every_file():
    assert len(list_prebuilt_skills()) == len(_library_files())


def test_keys_are_unique_and_addressable():
    skills = list_prebuilt_skills()
    keys = [s.key for s in skills]
    assert len(keys) == len(set(keys))
    for key in keys:
        assert get_prebuilt_skill(key) is not None


def test_unknown_key_returns_none():
    assert get_prebuilt_skill("no-such-skill") is None
    assert get_prebuilt_skill("") is None


@pytest.mark.parametrize("path", _library_files(), ids=lambda p: p.stem)
def test_entry_invariants(path: pathlib.Path):
    """Every entry must satisfy what the catalog and the prompt rely on."""
    skill = _parse_skill_file(path)
    assert skill is not None

    # The key addresses the entry in the API path and stamps catalog_key.
    assert skill.key == path.stem
    assert skill.category in VALID_CATEGORIES
    assert set(skill.modes) <= VALID_MODES

    # The description is the ONE line the planner sees in <available_skills>;
    # over the cap it gets truncated mid-sentence.
    assert 0 < len(skill.description) <= MAX_DESCRIPTION_LEN

    # A skill is discovered by its description, so it has to read as a trigger
    # ("Use when...") rather than as a topic label.
    assert skill.description.lower().startswith("use "), skill.description

    # A body that fits in the catalog line teaches the planner nothing it did
    # not already have from the description.
    assert len(skill.body) > 500
    assert skill.title.strip() == skill.title


def test_malformed_files_are_rejected(tmp_path):
    """Guards the parser itself — each of these must be dropped, not accepted."""
    cases = {
        "no_frontmatter.md": "Just a body with no header",
        "unterminated.md": "---\nkey: unterminated\ntitle: T\n",
        "bad_category.md": (
            "---\nkey: bad_category\ntitle: T\ndescription: Use when testing\n"
            "category: nonsense\nversion: '1.0'\n---\nbody\n"
        ),
        "bad_mode.md": (
            "---\nkey: bad_mode\ntitle: T\ndescription: Use when testing\n"
            "category: general\nversion: '1.0'\nmodes: [nonsense]\n---\nbody\n"
        ),
        "missing_field.md": (
            "---\nkey: missing_field\ntitle: T\ncategory: general\nversion: '1.0'\n---\nbody\n"
        ),
        "key_mismatch.md": (
            "---\nkey: something_else\ntitle: T\ndescription: Use when testing\n"
            "category: general\nversion: '1.0'\n---\nbody\n"
        ),
        "empty_body.md": (
            "---\nkey: empty_body\ntitle: T\ndescription: Use when testing\n"
            "category: general\nversion: '1.0'\n---\n\n"
        ),
        "long_description.md": (
            "---\nkey: long_description\ntitle: T\ndescription: "
            + "x" * (MAX_DESCRIPTION_LEN + 1)
            + "\ncategory: general\nversion: '1.0'\n---\nbody\n"
        ),
    }
    for name, content in cases.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        assert _parse_skill_file(path) is None, f"{name} should have been rejected"


def test_wellformed_file_is_accepted(tmp_path):
    """The negative cases above are only meaningful if the positive one passes."""
    path = tmp_path / "good_one.md"
    path.write_text(
        "---\nkey: good_one\ntitle: A good one\ndescription: Use when testing.\n"
        "category: general\nversion: '2.1'\nmodes: [training]\ntags: [a, b]\n"
        "---\nThe body.\n",
        encoding="utf-8",
    )
    skill = _parse_skill_file(path)
    assert skill is not None
    assert (skill.key, skill.version, skill.category) == ("good_one", "2.1", "general")
    assert skill.modes == ("training",)
    assert skill.tags == ("a", "b")
    assert skill.body == "The body."
