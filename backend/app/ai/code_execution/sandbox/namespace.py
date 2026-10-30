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


class ListsHelper:
    """`lists` in generated code: submit rows to the conversation's Agent Lists.

    Rows are validated on submit (an invalid value raises right away) and
    saved only after the run succeeds. `submit` returns counts, never rows.
    """

    __slots__ = ("_submit", "_available")

    def __init__(self, submit: Callable, available: Callable):
        self._submit = submit
        self._available = available

    def submit(self, list_name, records):
        """Queue `records` (a list of dicts keyed by field name) for list `list_name` (slug or name)."""
        return self._submit(str(list_name), list(records))

    def available(self):
        """The lists this run may write to: [{slug, name, key, fields}]."""
        return self._available()

    def __repr__(self):
        return "<lists helper: submit(list, records), available()>"


class BrowserHelper:
    """`browser` in generated code: one headless page, driven step by step.

    Every call runs on the trusted side under the browser connection's URL
    allowlist and the org's per-run page limit. Selectors are CSS (or
    Playwright text selectors such as "text=Next").
    """

    __slots__ = ("_call",)

    def __init__(self, call: Callable):
        self._call = call

    def goto(self, url, timeout=None):
        return self._call("goto", [str(url)], {"timeout": timeout})

    def click(self, selector, timeout=None):
        return self._call("click", [str(selector)], {"timeout": timeout})

    def fill(self, selector, text, timeout=None):
        return self._call("fill", [str(selector), str(text)], {"timeout": timeout})

    def press(self, key, selector=None, timeout=None):
        return self._call("press", [str(key)], {"selector": selector, "timeout": timeout})

    def select(self, selector, value, timeout=None):
        return self._call("select", [str(selector), str(value)], {"timeout": timeout})

    def scroll(self, pixels=2000):
        return self._call("scroll", [int(pixels)], {})

    def wait_for(self, selector, timeout=None, state="visible"):
        return self._call("wait_for", [str(selector)], {"timeout": timeout, "state": state})

    def text(self, selector=None):
        return self._call("text", [], {"selector": selector})

    def html(self, selector=None):
        return self._call("html", [], {"selector": selector})

    def query_all(self, selector, attrs=None):
        return self._call("query_all", [str(selector)], {"attrs": list(attrs or [])})

    def url(self):
        return self._call("url", [], {})

    def __repr__(self):
        return "<browser helper: goto, click, fill, press, select, scroll, wait_for, text, html, query_all, url>"


def invoke_generate_df(
    fn: Callable, wrapped_clients: Dict, excel_files: List,
    http_client: Any,
    load_step: Optional[Callable] = None, load_entity: Optional[Callable] = None,
    params: Optional[Dict] = None,
    lists: Any = None, browser: Any = None,
):
    """Call generate_df, binding injectables by parameter name.

    `ds_clients` and `excel_files` are always passed positionally. Any of
    `calendar_date_bounds`, `http`, `load_step`, `load_entity`, `params`, `lists`, `browser` are passed by keyword only
    when the function declares a parameter of that name — so legacy two-arg
    `(ds_clients, excel_files)` and three-arg `(…, http)` signatures keep
    working unchanged. `params` is always a dict (possibly empty) when the
    function asks for it, so `params.get(...)` never explodes.
    """
    from app.ai.code_execution.query_params import calendar_date_bounds

    injectables = {
        "calendar_date_bounds": calendar_date_bounds,
        "http": http_client,
        "load_step": load_step,
        "load_entity": load_entity,
        "params": dict(params or {}),
        "lists": lists,
        "browser": browser,
    }
    try:
        names = set(inspect.signature(fn).parameters.keys())
    except (TypeError, ValueError):
        names = set()
    kwargs = {k: v for k, v in injectables.items() if k in names}
    return fn(wrapped_clients, excel_files, **kwargs)
