"""Layering of the agent-sandbox code: one seam, at the driver.

Moving from Linux servers to another kind of backend later must mean writing
one new driver, not changing the service, the runtime client, the tools, the
API or the models. That only holds if nothing above the driver knows what the
first driver is made of. Two rules, checked on the code itself (comments and
docstrings may explain; code may not depend):

1. Outside ``drivers/docker_ssh/``, no code imports the SSH library or names
   the pieces that driver is built from.
2. Nothing branches on a backend's type name. The driver registry maps type
   names to drivers; everything else asks a driver for its capabilities.
"""
import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SANDBOX_PACKAGE = BACKEND / "app" / "services" / "sandbox"
DRIVER_PACKAGE = SANDBOX_PACKAGE / "drivers" / "docker_ssh"
REGISTRY = SANDBOX_PACKAGE / "drivers" / "registry.py"

# What the docker-ssh driver is made of.
FORBIDDEN = re.compile(r"asyncssh|paramiko|caddy|iptables|docker|smokescreen|egress[ _-]?proxy|forward[ _-]?proxy", re.I)
TYPE_BRANCH = re.compile(r"\b(?:backend\w*\.type|backend_?type|type_name)\s*(?:==|!=|\bin\b|\bnot\s+in\b)")


def _files():
    files = [p for p in SANDBOX_PACKAGE.rglob("*.py") if DRIVER_PACKAGE not in p.parents and "sandboxd_proto" not in p.parts]
    files += [
        BACKEND / "app" / "routes" / "sandbox.py",
        BACKEND / "app" / "models" / "sandbox.py",
        BACKEND / "app" / "schemas" / "sandbox_schema.py",
    ]
    files += sorted((BACKEND / "app" / "ai" / "tools" / "implementations").glob("*sandbox*.py"))
    return [p for p in files if p.exists()]


def _code_words(path: Path):
    """Identifiers, imported module names and string literals — not comments or docstrings."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value
        elif isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.lineno, node.arg
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or ""
            for alias in node.names:
                yield node.lineno, alias.name


def _code_lines(path: Path):
    """Source lines with comments removed (good enough to spot a comparison)."""
    for number, line in enumerate(path.read_text().splitlines(), 1):
        yield number, line.split("#", 1)[0]


def test_the_scan_covers_the_sandbox_code():
    names = {p.relative_to(BACKEND).as_posix() for p in _files()}
    assert "app/services/sandbox/drivers/base.py" in names
    assert "app/services/sandbox/runtime_client.py" in names
    assert "app/routes/sandbox.py" in names
    assert not any("docker_ssh" in n for n in names)


@pytest.mark.parametrize("path", [p for p in _files() if p != REGISTRY], ids=lambda p: p.relative_to(BACKEND).as_posix())
def test_code_above_the_driver_does_not_know_what_the_driver_is_made_of(path):
    hits = [f"{path.name}:{line}: {word!r}" for line, word in _code_words(path) if FORBIDDEN.search(word)]
    assert not hits, "infrastructure named outside drivers/docker_ssh/:\n" + "\n".join(hits)


@pytest.mark.parametrize("path", [p for p in _files() if p != REGISTRY], ids=lambda p: p.relative_to(BACKEND).as_posix())
def test_nothing_branches_on_the_backend_type(path):
    hits = [f"{path.name}:{number}: {line.strip()}" for number, line in _code_lines(path) if TYPE_BRANCH.search(line)]
    assert not hits, "backend type checked outside the driver registry:\n" + "\n".join(hits)


def test_the_rules_catch_what_they_are_for(tmp_path):
    """The scanners themselves: a planted violation is found, an explanation is not."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""Mentions Docker and Caddy in a docstring, which is fine."""\n'
        "import asyncssh  # the import is not\n"
        "# a comment about iptables is fine\n"
        "def f(backend):\n"
        "    if backend.type == 'docker-ssh':\n"
        "        return 'docker run'\n"
    )
    words = [w for _, w in _code_words(sample) if FORBIDDEN.search(w)]
    assert words == ["asyncssh", "docker-ssh", "docker run"]
    assert [n for n, line in _code_lines(sample) if TYPE_BRANCH.search(line)] == [5]
