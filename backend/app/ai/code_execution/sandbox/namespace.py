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

    def query_all(self, selector, attrs=None, fields=None):
        """One dict per element matching `selector`: {"text", <attr>...,
        <field>...}. `fields` reads inside each element: {"name": "css"} is
        the first match's text, "css@href" an attribute, ["css"] a list of
        every match's text."""
        return self._call("query_all", [str(selector)], {"attrs": list(attrs or []), "fields": dict(fields or {})})

    def url(self):
        return self._call("url", [], {})

    def __repr__(self):
        return "<browser helper: goto, click, fill, press, select, scroll, wait_for, text, html, query_all, url>"


# The global the run's namespace binds the clients dict to, besides handing
# it to generate_df as its first argument. app/services/client_keys.py tracks
# this name: any other global holding the dict would let code reach a client
# that rule does not see (test_client_keys checks the live namespace).
CLIENTS_GLOBAL = "db_clients"


class ClientsDict(dict):
    """The clients dict handed to generated code.

    A plain dict of the current client keys, plus the keys the clients had
    before their agent or connection was renamed (`former`: old key ->
    current key). A former key answers a direct lookup -- `ds_clients[old]`,
    `.get(old)`, `old in ds_clients` -- and nothing else: iteration, `len`,
    `.keys()`, `.values()` and `.items()` see the current keys only, so code
    that walks every client reaches each one once, as before the rename.
    """

    def __init__(self, clients: Optional[Dict] = None, former: Optional[Dict[str, str]] = None):
        super().__init__(clients or {})
        self.former = {old: current for old, current in (former or {}).items()
                       if not dict.__contains__(self, old) and dict.__contains__(self, current)}

    def __missing__(self, key):
        current = self.former.get(key) if isinstance(key, str) else None
        if current is None:
            raise KeyError(key)
        return dict.__getitem__(self, current)

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        current = self.former.get(key) if isinstance(key, str) else None
        return dict.__getitem__(self, current) if current is not None else default

    def __contains__(self, key):
        return dict.__contains__(self, key) or (isinstance(key, str) and key in self.former)


# Largest text one llm call sends (the RPC header carries it as JSON).
LLM_MAX_CALL_CHARS = 24_000_000


def _llm_cell(v) -> Optional[str]:
    if v is None:
        return None
    try:
        if v != v:  # NaN / NaT
            return None
    except Exception:
        pass
    return v if isinstance(v, str) else str(v)


class LLMHelper:
    """`llm` in generated code: one LLM question over a whole column.

    Every call de-duplicates its values here and sends the distinct ones in a
    single call to the trusted side (app/ai/code_execution/sandbox_llm.py),
    which picks the model, answers from its cache, batches the rest and
    validates every answer. Results come back one per input row, aligned to
    the input's index; None (NaN for scores) where no valid answer came back.
    """

    __slots__ = ("_call",)

    def __init__(self, call: Callable):
        self._call = call

    # ── plumbing ──────────────────────────────────────────────────────────
    @staticmethod
    def _inputs(values):
        import pandas as pd

        if isinstance(values, str) or values is None or not hasattr(values, "__iter__"):
            return [_llm_cell(values)], None, True
        if isinstance(values, pd.DataFrame):
            cols = [str(c) for c in values.columns]
            texts = []
            for row in values.itertuples(index=False, name=None):
                parts = [f"{c}: {_llm_cell(x)}" for c, x in zip(cols, row) if _llm_cell(x) is not None]
                texts.append("\n".join(parts) if parts else None)
            return texts, values.index, False
        if isinstance(values, pd.Series):
            return [_llm_cell(v) for v in values.tolist()], values.index, False
        if isinstance(values, dict):
            raise TypeError("llm: pass a Series, list or DataFrame of values, not a dict")
        return [_llm_cell(v) for v in list(values)], None, False

    def _ask(self, op: str, values, spec: Dict[str, Any], model):
        rows, index, scalar = self._inputs(values)
        slot: Dict[str, int] = {}
        distinct: List[str] = []
        keys: List[Optional[int]] = []
        chars = 0
        for v in rows:
            if v is None:
                keys.append(None)
                continue
            k = slot.get(v)
            if k is None:
                k = slot[v] = len(distinct)
                distinct.append(v)
                chars += len(v)
            keys.append(k)
        if chars > LLM_MAX_CALL_CHARS:
            raise ValueError(f"llm.{op}: {chars} characters of distinct values in one call; "
                             f"the limit is {LLM_MAX_CALL_CHARS}. Shorten or split the values.")
        if not distinct:
            return [None] * len(rows), index, scalar
        res = self._call(op, {"values": distinct, "spec": spec, "model": model, "rows": len(rows)})
        answers = res.get("answers") or []
        m = res.get("model") or {}
        print(f"llm.{op}: {len(rows)} rows, {len(distinct)} distinct, {res.get('cached', 0)} from cache, "
              f"{res.get('sent', 0)} sent in {res.get('requests', 0)} requests"
              + (f", {res['failed']} without an answer (not cached; a rerun asks again)" if res.get("failed") else "")
              + (f", {res['declined']} answered with no value" if res.get("declined") else "")
              + f" (model {m.get('model_id')}, record {m.get('record_id')})")
        return [answers[k] if k is not None and k < len(answers) else None for k in keys], index, scalar

    @staticmethod
    def _series(values, index, scalar, dtype=None):
        import pandas as pd

        if scalar:
            return values[0]
        return pd.Series(values, index=index, dtype=dtype)

    # ── operations ────────────────────────────────────────────────────────
    def classify(self, values, labels, instructions=None, model=None, with_proba=False):
        if isinstance(labels, dict):
            labels = {str(k): (None if v is None else str(v)) for k, v in labels.items()}
        else:
            labels = [str(x) for x in list(labels)]
        got, index, scalar = self._ask("classify", values, {"labels": labels, "instructions": instructions}, model)
        out = [a["a"] if a else None for a in got]
        if with_proba:
            import pandas as pd
            probs = [a.get("p") if a else None for a in got]
            if scalar:
                return {"label": out[0], "p": probs[0]}
            return pd.DataFrame({"label": out, "p": probs}, index=index)
        return self._series(out, index, scalar, dtype=object)

    def filter(self, values, condition, model=None, threshold=0.5, with_proba=False):
        got, index, scalar = self._ask("filter", values, {"condition": condition}, model)
        thr = float(threshold)
        out, probs = [], []
        for a in got:
            p = a.get("p") if a else None
            probs.append(p)
            out.append(bool(p >= thr) if p is not None else bool(a and a.get("a")))
        if with_proba:
            import pandas as pd
            if scalar:
                return {"match": out[0], "p": probs[0]}
            return pd.DataFrame({"match": out, "p": probs}, index=index)
        return self._series(out, index, scalar, dtype=bool)

    def score(self, values, criterion, low=1, high=5, model=None):
        got, index, scalar = self._ask("score", values, {"criterion": criterion, "low": low, "high": high}, model)
        out = [float(a["a"]) if a and a.get("a") is not None else float("nan") for a in got]
        return self._series(out, index, scalar, dtype=float)

    def extract(self, values, fields, instructions=None, model=None):
        import pandas as pd

        if not isinstance(fields, dict):
            raise TypeError("llm.extract: fields must be a dict of {name: type}")
        fields = {str(k): ("str" if v is None else (v if isinstance(v, str) else getattr(v, "__name__", str(v))))
                  for k, v in fields.items()}
        got, index, scalar = self._ask("extract", values, {"fields": fields, "instructions": instructions}, model)
        rows = [(a["a"] if a and isinstance(a.get("a"), dict) else {}) for a in got]
        if scalar:
            return {f: rows[0].get(f) for f in fields}
        return pd.DataFrame([{f: r.get(f) for f in fields} for r in rows], index=index, columns=list(fields))

    def generate(self, values, prompt, model=None):
        got, index, scalar = self._ask("generate", values, {"prompt": prompt}, model)
        return self._series([a["a"] if a else None for a in got], index, scalar, dtype=object)

    def models(self):
        return self._call("models", {})


# What generated code can ask for by parameter name (invoke_generate_df).
# Every run -- the agent's and each re-run of saved code -- must be able to
# hand each of these over when the org allows it (test_sandbox_helpers).
INJECTABLE_NAMES = ("calendar_date_bounds", "http", "load_step", "load_entity", "params", "lists", "browser", "llm")


def invoke_generate_df(
    fn: Callable, wrapped_clients: Dict, excel_files: List,
    http_client: Any,
    load_step: Optional[Callable] = None, load_entity: Optional[Callable] = None,
    params: Optional[Dict] = None,
    lists: Any = None, browser: Any = None, llm: Any = None,
):
    """Call generate_df, binding injectables by parameter name.

    `ds_clients` and `excel_files` are always passed positionally. Any of
    `calendar_date_bounds`, `http`, `load_step`, `load_entity`, `params`, `lists`, `browser`, `llm` are passed by keyword only
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
        "llm": llm,
    }
    try:
        names = set(inspect.signature(fn).parameters.keys())
    except (TypeError, ValueError):
        names = set()
    assert set(injectables) == set(INJECTABLE_NAMES)
    kwargs = {k: v for k, v in injectables.items() if k in names}
    return fn(wrapped_clients, excel_files, **kwargs)
