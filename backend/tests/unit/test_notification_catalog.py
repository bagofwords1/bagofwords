"""Guardrails for the notification center.

Every notification type the backend can emit must render properly in the
inbox: it needs an icon (frontend ``utils/notifications.ts``) and, unless its
copy is free-form text authored at runtime, a ``notifications.content.<type>``
catalog entry so the row re-renders in the viewer's locale. And every review
notification must carry a link: a row the admin clicks on must take them to
the place where they can act on it.

The emitted types are discovered from the source (AST), so a new producer that
forgets the icon or the translation fails here instead of shipping a bare bell
with English text.
"""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.notification import SOURCES
from app.models.review_item import EVENT_TYPES

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "backend" / "app"
NOTIF_TS = REPO / "frontend" / "utils" / "notifications.ts"

NOTIFY_CALLS = {"notify_users", "notify_agent_managers"}
# Types whose title/body are written at runtime by an agent or a tool (the
# notify tool, check-in follow-ups). There is nothing to translate.
FREE_FORM = {"notify", "checkin_followup"}


def _const_strs(node):
    """String constants a type expression can evaluate to (`"a"`, `"a" if c else "b"`)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _const_strs(node.body) | _const_strs(node.orelse)
    return set()


def emitted_types() -> set:
    found = set(EVENT_TYPES)
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in NOTIFY_CALLS:
                    for kw in node.keywords:
                        if kw.arg == "type":
                            found |= _const_strs(kw.value)
            # The type is often picked into a local first: `ntype = "..."`.
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "ntype" for t in node.targets):
                    found |= _const_strs(node.value)
            # notify_service takes it as `notification_type` (arg default + tool payloads).
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "notification_type":
                        found |= _const_strs(v)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            args = fn.args.args + fn.args.kwonlyargs
            defaults = ([None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults)
                        + list(fn.args.kw_defaults))
            for a, d in zip(args, defaults):
                if a.arg == "notification_type" and d is not None:
                    found |= _const_strs(d)
    return found


def _ts_map_keys(name: str) -> set:
    src = NOTIF_TS.read_text(encoding="utf-8")
    m = re.search(name + r"\s*:\s*Record<string,\s*string>\s*=\s*\{(.*?)\n\}", src, re.S)
    assert m, f"{name} not found in {NOTIF_TS}"
    return set(re.findall(r"^\s*([a-z_]+)\s*:", m.group(1), re.M))


def test_discovery_finds_the_known_producers():
    # Guards the guardrail: if the AST scan stops seeing producers, the checks
    # below would pass vacuously.
    types = emitted_types()
    for t in ("share_conversation", "connection_down", "nightly_learning",
              "scheduled_run", "checkin_followup", "schema_changed"):
        assert t in types, t


def test_every_emitted_type_has_an_icon():
    icons = _ts_map_keys("TYPE_ICONS")
    missing = sorted(emitted_types() - icons)
    assert not missing, f"no TYPE_ICONS entry for: {missing}"


def test_every_source_has_an_icon():
    icons = _ts_map_keys("SOURCE_ICONS")
    missing = sorted(set(SOURCES) - icons)
    assert not missing, f"no SOURCE_ICONS entry for: {missing}"


def test_every_emitted_type_has_a_catalog_entry():
    content = json.loads((REPO / "locales" / "en.json").read_text(encoding="utf-8"))["notifications"]["content"]
    missing = sorted(t for t in emitted_types() - FREE_FORM if t not in content)
    assert not missing, f"no notifications.content entry in en.json for: {missing}"


@pytest.mark.parametrize("review_type", sorted(EVENT_TYPES))
@pytest.mark.parametrize("agent_id", [None, "ds-1"])
def test_every_review_notification_has_a_link(review_type, agent_id):
    from app.services.review_service import review_link_for
    subject = {"instruction_id": "ins-1"} if review_type == "instruction_suggestion" else {}
    item = SimpleNamespace(id="item-1", type=review_type, data_source_id=agent_id, subject_json=subject)
    link = review_link_for(item)
    assert isinstance(link, str) and link.startswith("/agents/"), link
    # The generic agent overview is exactly the dead end this guards against.
    assert link != f"/agents/{agent_id}"
