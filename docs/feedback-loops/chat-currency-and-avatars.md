# Chat currency rendering and avatars

The reported Hebrew answer contained italic names, lost spaces, and overlapping
text around dollar amounts. The installed stream-markdown-parser reproduced the
cause: `49.62$, ואחריו Richard Cunningham (47.62$)` became a `math_inline` node.

The existing guard in `frontend/utils/markdownMathDollars.ts` permitted some
single-dollar pairs. It now escapes every standalone dollar outside protected
code/HTML. The shared plugin disables heuristic math detection and retains
explicit TeX delimiters and double-dollar display math. Conversational prompts
request explicit TeX notation for equations.

The report transcript also removes the user and assistant avatars and their
reserved gutters. User bubbles and assistant/tool styling distinguish speakers;
existing activity indicators remain.

## Manual verification

Render repeated prefix and suffix amounts in Hebrew, Arabic and English; names,
spaces and literal dollar signs should remain intact. Render an explicit equation
using `\(x^2\)` and display math using `\[x^2\]`. Check code and tables. Confirm
that user bubbles, replies, tool cards and copy controls align without avatars.

The regression script is `cd frontend && node tests/unit/markdownMathDollars.mjs`.
Updated expectations and the RTL currency case have not been run. The user
explicitly requested no further verification and will check locally; no after
screenshots or claims of verified rendering are included.
