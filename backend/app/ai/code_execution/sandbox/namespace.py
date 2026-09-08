"""Helpers that build the namespace generated code runs in.

Shared by the sandbox child and the (debug-only) in-process path, so it must
not import anything beyond the stdlib and pandas: the child imports this
before dropping privileges and must stay small and side-effect free.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional


# Formats with no reader inside the sandbox — read_file handles them instead.
READ_TEXT_REFUSES = {"pdf", "docx", "pptx", "xlsx", "xls", "png", "jpg", "jpeg", "gif", "webp"}

# A single text read is capped so one call can't blow out memory or the frame
# built from it. Callers that need more should page with read_file.
READ_TEXT_MAX_CHARS = 5_000_000


class SandboxFile:
    """Plain attribute bag standing in for the `File` ORM row inside the
    sandbox. Generated code reads `.path` / `.filename` / `.content_type`;
    it gets the same dot-access surface without an ORM instance (and its
    session, engine and credentials) ever crossing the boundary."""

    def __init__(self, attrs: Dict[str, Any]):
        for k, v in attrs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        name = getattr(self, "filename", None) or getattr(self, "path", "?")
        return f"File({name!r})"


_SIMPLE_TYPES = (str, int, float, bool, type(None))


def file_to_attrs(file_obj: Any) -> Dict[str, Any]:
    """Snapshot the plain attributes of a File row (or any duck-typed stand-in)."""
    attrs: Dict[str, Any] = {}
    table = getattr(file_obj, "__table__", None)
    if table is not None:
        for col in table.columns:
            try:
                val = getattr(file_obj, col.name)
            except Exception:
                continue
            attrs[col.name] = val if isinstance(val, _SIMPLE_TYPES) else str(val)
    else:
        try:
            raw = vars(file_obj)
        except TypeError:
            raw = {}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if isinstance(v, _SIMPLE_TYPES):
                attrs[k] = v
        for k in ("path", "filename", "content_type", "id"):
            if k not in attrs and hasattr(file_obj, k):
                v = getattr(file_obj, k)
                if isinstance(v, _SIMPLE_TYPES):
                    attrs[k] = v
    if "path" in attrs and attrs["path"] is not None:
        attrs["path"] = str(attrs["path"])
    return attrs


def build_read_text(excel_files) -> Callable[..., str]:
    """`read_text(file_or_path)` for generated code, scoped to `excel_files`.

    The sandbox forbids `open`, so this is the only text reader — and it stays
    safe by resolving only against the files this run was handed. An arbitrary
    path is refused rather than read, which is the property that made banning
    `open` worth doing in the first place.
    """
    allowed = {}
    for f in (excel_files or []):
        path = getattr(f, "path", None)
        if path:
            allowed[str(path)] = f

    def read_text(file_or_path, encoding: str = "utf-8") -> str:
        path = getattr(file_or_path, "path", None) or str(file_or_path or "")
        if path not in allowed:
            raise ValueError(
                f"read_text: {path!r} is not one of this run's files. Pass an "
                "entry from `excel_files`, e.g. read_text(excel_files[0])."
            )
        name = str(getattr(allowed[path], "filename", "") or path)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in READ_TEXT_REFUSES:
            raise ValueError(
                f"read_text: {name} is a {ext} file, not text. Read it with the "
                "read_file tool instead — it has a proper extractor for this format."
            )
        with open(path, "r", encoding=encoding, errors="replace") as fh:
            content = fh.read(READ_TEXT_MAX_CHARS + 1)
        if len(content) > READ_TEXT_MAX_CHARS:
            return (
                content[:READ_TEXT_MAX_CHARS]
                + f"\n[TRUNCATED at {READ_TEXT_MAX_CHARS} chars — page the rest with the read_file tool]"
            )
        return content

    return read_text


def build_loadable_closures(loadables: Optional[Dict], *, enable_load_step: bool = True):
    """Build pure-lookup `load_step` / `load_entity` over a resolved registry.

    The registry maps the exact literal ref used in the code to a
    DataFrame. A miss raises a clear error naming what's available — it
    only fires for dynamic (non-literal) refs that bypassed pre-resolution.

    When `enable_load_step` is False the `load_step` closure is a defensive
    stub that always raises — the feature is advertised nowhere in that
    case, so any call is a stray one and should fail clearly (and feed the
    retry loop) rather than silently succeed. `load_entity` is unaffected.
    """
    reg = loadables or {}
    steps = reg.get("steps") or {}
    entities = reg.get("entities") or {}

    def load_step(id_or_name):
        if not enable_load_step:
            raise RuntimeError(
                "load_step is disabled for this organization. "
                "Do not call load_step; query the data source instead."
            )
        key = str(id_or_name)
        if key in steps:
            return steps[key].copy()
        raise KeyError(
            f"load_step({key!r}) is not available. "
            f"Loadable steps: {list(steps.keys())}. "
            f"Use a string-literal id or name so it can be pre-loaded."
        )

    def load_entity(id_or_name):
        key = str(id_or_name)
        if key in entities:
            return entities[key].copy()
        raise KeyError(
            f"load_entity({key!r}) is not available. "
            f"Loadable entities: {list(entities.keys())}. "
            f"Use a string-literal id or name so it can be pre-loaded."
        )

    return load_step, load_entity


def invoke_generate_df(
    fn: Callable, wrapped_clients: Dict, excel_files: List,
    http_client: Any,
    load_step: Optional[Callable] = None, load_entity: Optional[Callable] = None,
    params: Optional[Dict] = None,
):
    """Call generate_df, binding injectables by parameter name.

    `ds_clients` and `excel_files` are always passed positionally. Any of
    `http`, `load_step`, `load_entity`, `params` are passed by keyword only
    when the function declares a parameter of that name — so legacy two-arg
    `(ds_clients, excel_files)` and three-arg `(…, http)` signatures keep
    working unchanged. `params` is always a dict (possibly empty) when the
    function asks for it, so `params.get(...)` never explodes.
    """
    injectables = {
        "http": http_client,
        "load_step": load_step,
        "load_entity": load_entity,
        "params": dict(params or {}),
    }
    try:
        names = set(inspect.signature(fn).parameters.keys())
    except (TypeError, ValueError):
        names = set()
    kwargs = {k: v for k, v in injectables.items() if k in names}
    return fn(wrapped_clients, excel_files, **kwargs)
