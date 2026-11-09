# Feedback Loop — shell tool text is larger than CreateData / InspectData

The collapsed shell command should share the data tools' typography and row
spacing. Only its icon, label and expanded details differ.

## Root cause (validated)

`frontend/components/tools/RemoteShellTool.vue` put `text-chat-2` on a `UButton`
without replacing the button's `text-sm` size configuration. Nuxt UI's class
merger treats the custom chat font-size token as a text color and drops it when
a text-color class is also present, leaving the fixed `text-sm` size. The command also used `font-mono`, a 14px terminal
icon, and an inline-flex button that introduced an extra line-box gap.

At the default reading setting the reference rows rendered at **13.0004px**
with **18.8506px** line height and **12px** icons. The shell row instead used
**14px**, **20px**, and a **14px** icon; its command used a different font family.

## Reproduce and verify

Build/run the frontend, boot a throwaway backend, and seed a fixture user using
`tools/agent/boot_stack.sh` and `tools/agent/seed_org.py`. Then:

```bash
cd frontend
PLAYWRIGHT_BASE_URL=http://localhost:3000 BOW_BASE_URL=http://localhost:8000 \
BOW_TEST_EMAIL="$FIXTURE_EMAIL" BOW_TEST_PASSWORD="$FIXTURE_PASSWORD" \
  node tests/reports/shell-tool-row-style.mjs after
```

Use `before` instead of `after` on the base revision for before screenshots and
the failing measurements. The script creates a real report through the API,
then provides synthetic CreateData, InspectData and SSH completion records at
the HTTP response boundary. It uses public browser APIs; it does not depend on
Vue development internals, a real SSH host or an LLM.

It compares font size/family/weight, line height, letter spacing, icon size and
row height in **English and Hebrew at all five reading scales**. It also checks
keyboard expansion/collapse, visible keyboard focus, monospace command details,
dark mode, and mobile overflow. Screenshots and measurements land under
`media/pr/shell-tool-row-style/`.

Observed on the production bundle: base measurements had **50 mismatches**;
the fixed bundle passed **10 combinations with zero mismatches**. All rows now
render at 13.0004px / 18.8506px line height / 18.84375px row height at the default
reading step, with 12px icons. Keyboard, focus, monospace details and mobile
checks passed in both locales. The production build passed. The
baseline build used for the local captures has byte-identical versions of all
three tool components to base commit `440b74ace` (verified with git diff).

## Fix

Put the same `text-chat-2` ramp as the reference headers on the shell wrapper,
outside the button's class merger. Use Nuxt UI's local `override` strategy to
clear its fixed size, so the button inherits the wrapper's size and line height,
with normal font weight. Use a block flex row, the InspectData icon/text/duration/
chevron spacing, and its 12px icon. The collapsed command inherits the regular
UI font; the expanded command retains monospace. Keep the semantic button and
its keyboard focus ring.

Only the shell component changes. Approval/execution behavior, the reference
data tools, shared typography tokens, translations, and expanded content are
unchanged. This is a static style correction; expansion behavior is regression
checked but no flow or animation is changed.
