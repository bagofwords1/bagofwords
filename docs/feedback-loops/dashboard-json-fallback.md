# Dashboard JSON fallback skips discarded fields before writing UTF-8

A SQLite step can hold an escaped lone surrogate in a field such as `_sales`.
FastAPI discards keys beginning with `_sa`, so a reader should still receive
the rest of the row.

## Reproduce and verify

From a checkout containing the regression test, run the following with the
backend development environment installed. To see the original failure in
a disposable checkout of the fix, first restore the old helper with
`git restore --source=f603d3481 -- backend/app/core/json_response.py`.
Restore the fixed helper with `git restore -- backend/app/core/json_response.py`
and run the same test again.

```sh
(cd backend && TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest -q tests/unit/test_json_response.py -k discarded_fields)
```

At `f603d3481`, `backend/app/core/json_response.py:51` passes the unfiltered
data to the stdlib writer after orjson rejects the surrogate. The test fails
with `UnicodeEncodeError` when the writer encodes the JSON as UTF-8.

The fixed fallback applies FastAPI's encoder first. The test checks that the
response matches FastAPI's original output, including the retained fields in
the same nested object.
