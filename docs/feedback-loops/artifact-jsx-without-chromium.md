# Dashboard JSX validation without Chromium

The syntax gate should reject malformed JSX even when the server cannot launch a browser.

## Reproduction

Previously,
`backend/app/ai/tools/implementations/_artifact_parse.py:116` launched Chromium
to check syntax. With `BOW_CHROMIUM_EXECUTABLE=/missing/chromium`,
`await parse_check_page_code('const x = <a></b>;')` logged a launch failure and
returned `None`, the same result as a successful parse.

## Regression loop

Use the normal backend development environment. In a fresh checkout, install
the vendored libraries with `bash scripts/download-vendor-libs.sh frontend/public/libs`
from the repository root. A DevBox already has these assets.

```sh
cd backend
TESTING=true uv run pytest tests/unit/test_artifact_parse.py tests/e2e/test_artifact_render_unchecked.py --db=sqlite
```

The tests exercise malformed attribute expressions, mismatched tags, an incomplete
expression, valid wrapped source, and a missing compiler. The create and edit
regressions point Chromium at a nonexistent executable. Creation returns a
parse error and a rejected edit leaves the previous version intact.

## Why the check works offline

The syntax check runs the vendored Babel compiler with the Node executable
packaged with Python Playwright. Only trusted Babel code runs. Generated JSX
is compiler input. Agent and MCP page creation and editing check syntax before
reporting success. A missing compiler produces a validation failure. Runtime
exceptions and layout defects still require a browser render.
