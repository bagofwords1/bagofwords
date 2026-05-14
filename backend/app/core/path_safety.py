"""Path traversal guards.

Single source of truth for joining a trusted base directory with untrusted
input (HTTP params, env vars, DB-stored paths) without leaving the base.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class UnsafePathError(ValueError):
    """Raised when a path operation would escape its trusted base."""


def safe_join(base: Path | str, *segments: str) -> Path:
    """Join `base` with one or more untrusted `segments` and verify the
    resolved path stays within `base`. Returns the resolved Path.
    """
    base_path = Path(base).resolve()
    candidate = base_path.joinpath(*segments).resolve()
    if not candidate.is_relative_to(base_path):
        raise UnsafePathError(f"path escapes base: {segments!r}")
    return candidate


def ensure_within(path: Path | str, allowed_bases: Iterable[Path | str]) -> Path:
    """Verify `path` resolves inside one of `allowed_bases`. Returns the
    resolved Path; raises UnsafePathError otherwise. Use this when the path
    came from somewhere semi-trusted (e.g. database) but the caller wants
    defence-in-depth before passing it to a file API.
    """
    resolved = Path(path).resolve()
    for base in allowed_bases:
        if resolved.is_relative_to(Path(base).resolve()):
            return resolved
    raise UnsafePathError(f"path {path!r} not within any allowed base")
