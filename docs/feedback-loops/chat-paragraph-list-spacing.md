# Feedback loop — chat is not comfortably readable

The reported text felt too large or incorrectly spaced. Live Chrome measurements
showed 15px text / 24px line height, but adjacent paragraphs had no gap while
short list rows were 32px apart. The fix preserves the font and line height.

## Validated cause and fix

`frontend/pages/reports/[id]/index.vue:6404`: Markstream wraps each paragraph in
`.node-slot > .node-content`. Every paragraph therefore matches `p:last-child`,
which removes the intended paragraph margin. Give non-final paragraph slots a
0.6667em bottom margin (10px at the default size).

The renderer supplies `my-2` on list items. The report only replaced the bottom
margin, leaving an 8px top margin. Reset both block margins and give adjacent
items 0.2667em (4px), yielding a 28px row pitch. Nested lists use the same rule.
List-container, heading, font, width, color and direction rules are unchanged.

## Loop A — deterministic, no backend or credentials

With frontend dependencies installed and the normal frontend dev server running:

```sh
cd frontend
node tests/reports/chat-spacing-fixture.mjs 2a3d7a739
# Open http://localhost:3000/chat-spacing-fixture.html
# In browser DevTools: console.table(checkChatSpacing())
node tests/reports/chat-spacing-fixture.mjs
# Reload the same page and repeat the check.
```

The fixture renders the installed Markstream component through Vue SSR and
compiles the actual report stylesheet with the Vue compiler and Tailwind.
It includes the production tokens, renderer CSS and RTL CSS. English and Hebrew
samples cover paragraphs, ordered/unordered lists and nested lists. Direction
attributes are stamped in the fixture; direction detection itself is not tested
here. The renderer emits Vue resolveComponent warnings during SSR; rendering
and browser geometry were inspected independently.

| Measurement, both languages | Before | After |
|---|---:|---:|
| Paragraph gap | 0px | 10px |
| Short list row pitch | 32px | 28px |
| Font / line height | 15px / 24px | 15px / 24px |
| `checkChatSpacing().every(result => result.pass)` | false | true |

Screenshots at the same viewport:
`media/pr/ai-lucid-goldberg-3ssn9m/chat-spacing/{before,after}-en-he.jpg`.
The generated public HTML is disposable; remove it after testing.

## Loop B — live report

Confirmed the same 0 → 10px paragraph gap and 32 → 28px short-list pitch in the
existing local Hebrew report, using DOM geometry before and after HMR. Body
font and line height stayed 15px / 24px. The fixture uses synthetic data only.

`node tests/unit/textDirection.mjs` passed; `git diff --check` passed.
No backend changes or backend test runs. Safari/Firefox and streaming animation
were not exercised; this is a static spacing change. This verification does
not claim the rest of PR #1557 is fully verified.

## Marker contrast follow-up

Live bullets were `rgb(203, 213, 225)` against `rgb(17, 24, 39)` text.
Markstream's scoped marker rules default to `#cbd5e1` for bullets and `#64748b`
for counters. The report now sets both renderer marker variables to
`currentColor`, so ordered/unordered markers follow the list text's theme color.

The fixture now restores the list component's scope attribute omitted by SSR;
without it, the earlier spacing screenshots did not reproduce marker color.
This correction does not affect the recorded spacing measurements. The check
also asserts marker/text color equality. Before: false; after: all 14 fixture
items (7 per language) and all 21 live-report items matched `rgb(17, 24, 39)`.
Use baseline `cb3007fe1` to reproduce the marker failure. Evidence:
`chat-spacing/markers-before-en-he.jpg` and `markers-after-en-he.jpg` in the same
PR media directory. Dark mode follows currentColor by construction but was not
separately exercised in the browser.

Open WebUI's current app.css uses prose typography, and its Tailwind config
retains the typography plugin's muted markers; black bullets here are an
intentional readability choice, not a claim of exact Open WebUI parity.
