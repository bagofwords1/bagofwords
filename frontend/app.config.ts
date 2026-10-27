/*
 * Nuxt UI v2 theme defaults.
 *
 * This file used to set exactly one value (`primary`), so all 363 `UButton`s
 * and 48 `UInput`s in the app inherited Nuxt UI's stock presets — including
 * two contrast defects. Everything configured here fixes a whole component
 * class at once, which is the cheapest leverage in the frontend: no call
 * site changes, no migration.
 *
 * Contrast figures below are verified by `scripts/check-contrast.mjs` and
 * the one-off audit in that script's header. They are the reason for each
 * override, so keep them accurate if you change a shade.
 *
 * Stock presets live in
 * `node_modules/@nuxt/ui/dist/runtime/ui.config/**` — read the preset before
 * overriding a key, because these objects are merged shallowly per key and a
 * partial `variant` or `size` map silently drops the entries you omit.
 */
export default defineAppConfig({
  ui: {
    // The app uses blue as its accent throughout (buttons, links, active nav).
    // Nuxt UI defaults `primary` to green, which leaked into focus rings on
    // inputs/textareas that don't set an explicit color. Pin it to blue so all
    // form controls match the rest of the app.
    primary: 'blue',

    button: {
      /*
       * Nuxt UI v2's stock `solid` variant is `bg-{color}-500` with
       * `text-white`. Measured, that is:
       *
       *   white on blue-500   3.68:1   <- fails WCAG AA (needs 4.5:1)
       *   white on red-500    3.76:1   <- fails
       *   white on blue-600   5.17:1   <- passes
       *   white on red-600    4.83:1   <- passes
       *
       * Button labels are small text (`size: 'sm'` -> `text-sm`), so 4.5:1
       * applies, not the 3:1 large-text allowance. Shifting the light-mode
       * fill one step to -600 fixes every solid button in the app.
       *
       * Dark mode is deliberately left alone: there the preset flips to
       * `dark:bg-{color}-400` with `dark:text-gray-900`, which measures
       * 6.98:1 on blue-400 and is already correct.
       *
       * NOTE: `green` cannot pass with white text at any sensible step
       * (green-600 is still 3.30:1). Use `color="black"` or a soft variant
       * for a green affirmative action rather than reaching for solid.
       */
      variant: {
        solid:
          'shadow-sm text-white dark:text-gray-900 ' +
          'bg-{color}-600 hover:bg-{color}-700 ' +
          'disabled:bg-{color}-600 aria-disabled:bg-{color}-600 ' +
          'dark:bg-{color}-400 dark:hover:bg-{color}-500 ' +
          'dark:disabled:bg-{color}-400 dark:aria-disabled:bg-{color}-400 ' +
          'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ' +
          'focus-visible:outline-{color}-600 dark:focus-visible:outline-{color}-400',
      },

      /*
       * The stock default is already `size: 'sm'`, which resolves to
       * `text-sm` + `px-2.5 py-1.5` — a 32px control, matching UInput's `sm`.
       * It is restated here so that the app's standard control height is
       * written down somewhere rather than inherited by accident, and so a
       * future preset change can't move it silently.
       */
      default: {
        size: 'sm',
        variant: 'solid',
        color: 'primary',
        loadingIcon: 'i-heroicons-arrow-path-20-solid',
      },
    },

    input: {
      /*
       * Stock is `placeholder-gray-400 dark:placeholder-gray-500`, i.e.
       * 2.54:1 in light mode — the same defect as the app's 1,938 migrated
       * `text-gray-400` call sites, and from the same cause. `ink-subtle`
       * resolves to 3.10:1 light / 3.67:1 dark via the tokens in
       * assets/css/tokens.css.
       *
       * Placeholders stay in the decorative tier on purpose (below 4.5:1):
       * a placeholder must not be the only label on a field, so it carries
       * no information of its own. Where a field has no visible label, fix
       * the label — not this token.
       */
      placeholder: 'placeholder-ink-subtle',
      default: {
        size: 'sm',
      },
    },

    textarea: {
      placeholder: 'placeholder-ink-subtle',
      default: {
        size: 'sm',
      },
    },

    select: {
      placeholder: 'placeholder-ink-subtle',
      default: {
        size: 'sm',
      },
    },

    selectMenu: {
      // 90 call sites, most of them in toolbars next to a UInput or UButton.
      default: {
        size: 'sm',
      },
    },
  },
})
