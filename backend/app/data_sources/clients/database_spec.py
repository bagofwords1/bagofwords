import fnmatch
from typing import List

from app.schemas.data_sources.configs import parse_athena_database_spec as parse_database_spec

_GLOB_CHARS = "*?["

__all__ = ["parse_database_spec", "is_glob", "is_excluded", "spec_matches", "select_databases"]


def is_glob(entry: str) -> bool:
    return any(c in entry for c in _GLOB_CHARS)


def _fold(text: str, case_sensitive: bool) -> str:
    return text if case_sensitive else text.lower()


def is_excluded(name: str, excludes: List[str], *, case_sensitive: bool) -> bool:
    candidate = _fold(name, case_sensitive)
    return any(fnmatch.fnmatchcase(candidate, _fold(e, case_sensitive)) for e in excludes)


def spec_matches(name: str, includes: List[str], excludes: List[str], *, case_sensitive: bool) -> bool:
    candidate = _fold(name, case_sensitive)
    included = any(
        fnmatch.fnmatchcase(candidate, _fold(i, case_sensitive)) if is_glob(i)
        else candidate == _fold(i, case_sensitive)
        for i in includes
    )
    return included and not is_excluded(name, excludes, case_sensitive=case_sensitive)


def select_databases(
    available: List[str], includes: List[str], excludes: List[str], *, case_sensitive: bool
) -> List[str]:
    return list(dict.fromkeys(
        name for name in available
        if spec_matches(name, includes, excludes, case_sensitive=case_sensitive)
    ))
