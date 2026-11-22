"""Every event class a tool references must be bound in its module.

A tool that yields an event class it never imported only fails when that
branch runs, as a NameError the agent then surfaces to the user (e.g.
create_data's "BOW data requires authorized training mode" guard raised
``name 'ToolErrorEvent' is not defined``). Check every tool module statically
so a rare error branch can't ship broken.
"""
import ast
import builtins
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "app" / "ai" / "tools" / "implementations"


def _module_bindings(tree: ast.Module) -> set[str]:
    names: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


@pytest.mark.parametrize("path", sorted(TOOLS_DIR.rglob("*.py")), ids=lambda p: p.name)
def test_referenced_event_classes_are_bound(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = _module_bindings(tree)
    missing = sorted({
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id.endswith("Event")
        and node.id not in bound
    })
    assert not missing, f"{path.name} references undefined event classes: {missing}"
