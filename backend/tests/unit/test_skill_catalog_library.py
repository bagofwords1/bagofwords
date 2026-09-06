"""The shipped pre-built skill library must be well-formed.

`_parse_skill_file` drops a malformed file with a log line rather than raising,
so a typo in frontmatter would silently remove a skill from the catalog. These
tests are what turns that into a failing build.
"""
import pathlib

import pytest

from app.ai.skills.catalog import (
    DEFAULT_ORDER,
    LIBRARY_DIR,
    MAX_DESCRIPTION_LEN,
    VALID_CATEGORIES,
    VALID_MODES,
    _parse_skill_file,
    get_prebuilt_skill,
    list_default_skills,
    list_prebuilt_skills,
)

#: The curated opening of the catalog, in the order an admin sees it. These
#: are also the entries every new organization starts with. Anything else is
#: listed after them, alphabetically, and stays off until enabled.
CURATED_ORDER = [
    "train-agent",
    "audit-instructions",
    "complex-dashboard",
    "migrate-bi-dashboard",
    "usage-review",
    "erd-mermaid",
    "create-evals",
]


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


def test_catalog_leads_with_the_curated_entries_in_order():
    """Train agent first, then the rest of the curated set — the list an admin
    (and the catalog API) sees opens with these, in this order."""
    keys = [s.key for s in list_prebuilt_skills()]
    assert keys[: len(CURATED_ORDER)] == CURATED_ORDER

    # Everything after the curated block is the un-ordered remainder, by title.
    rest = list_prebuilt_skills()[len(CURATED_ORDER):]
    assert all(s.order == DEFAULT_ORDER for s in rest)
    assert [s.title.lower() for s in rest] == sorted(s.title.lower() for s in rest)


def test_curated_entries_are_enabled_by_default_and_nothing_else_is():
    assert [s.key for s in list_default_skills()] == CURATED_ORDER
    off = {s.key for s in list_prebuilt_skills()} - set(CURATED_ORDER)
    assert all(get_prebuilt_skill(k).default_enabled is False for k in off)


def test_explicit_order_is_reserved_for_the_curated_entries():
    """An `order` on a non-default entry would push it into the curated block
    without anyone deciding it belongs there."""
    for skill in list_prebuilt_skills():
        assert (skill.order != DEFAULT_ORDER) == skill.default_enabled, skill.key


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
        "bad_order.md": (
            "---\nkey: bad_order\ntitle: T\ndescription: Use when testing\n"
            "category: general\nversion: '1.0'\norder: first\n---\nbody\n"
        ),
        # bool is an int subclass — `order: true` must not parse as order 1.
        "bool_order.md": (
            "---\nkey: bool_order\ntitle: T\ndescription: Use when testing\n"
            "category: general\nversion: '1.0'\norder: true\n---\nbody\n"
        ),
        "bad_default.md": (
            "---\nkey: bad_default\ntitle: T\ndescription: Use when testing\n"
            "category: general\nversion: '1.0'\ndefault_enabled: yes please\n---\nbody\n"
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
    # Neither optional field declared: listed after the curated block, off.
    assert skill.order == DEFAULT_ORDER
    assert skill.default_enabled is False


def test_order_and_default_enabled_are_parsed(tmp_path):
    path = tmp_path / "curated.md"
    path.write_text(
        "---\nkey: curated\ntitle: C\ndescription: Use when testing.\n"
        "category: general\nversion: '1.0'\norder: 5\ndefault_enabled: true\n"
        "---\nThe body.\n",
        encoding="utf-8",
    )
    skill = _parse_skill_file(path)
    assert skill is not None
    assert skill.order == 5
    assert skill.default_enabled is True
    assert skill.to_dict()["order"] == 5
    assert skill.to_dict()["default_enabled"] is True
