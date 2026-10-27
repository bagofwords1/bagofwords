import type { Config } from 'tailwindcss'

/*
 * The app shell's design system.
 *
 * Until this file existed there was no Tailwind theme config at all, and
 * `app.config.ts` set exactly one value (`ui.primary`). Every one of the
 * app's 401 components therefore re-derived its own type sizes, radii,
 * shadows and motion from raw utilities. The measured result was 21 distinct
 * font sizes (14 of them arbitrary `text-[11px]`-style values), three corner
 * radii used at near-equal frequency, and 601 transitions sharing 43 timing
 * declarations.
 *
 * Two rules for anything added here:
 *
 *   1. Be additive or value-only. This config does not rename or remove a
 *      Tailwind utility the app already uses — 17,245 `gray-*` references
 *      across 359 files mean a palette rename is its own project, not a
 *      side effect of this one. `gray` is deliberately left untouched so
 *      Nuxt UI v2's own `ui.gray` handling keeps working.
 *   2. Name intent, not appearance. `text-muted` survives a palette change;
 *      `text-gray-400` is how we got here.
 *
 * Token values live in `assets/css/tokens.css` as CSS variables so the
 * contrast-bearing ones can flip between light and dark. This file only
 * maps them onto utilities.
 */
export default <Partial<Config>>{
  theme: {
    extend: {
      /*
       * Semantic colors, backed by the variables in tokens.css.
       *
       * These exist because no single Tailwind shade is correct in both
       * themes: `gray-400` as text is 2.6:1 on white and 6.8:1 on gray-900.
       * `text-muted` resolves to the right one in each.
       *
       * `<alpha-value>` keeps opacity modifiers working, so `text-muted/70`
       * still does what it looks like.
       */
      colors: {
        ink: {
          DEFAULT: 'rgb(var(--bow-ink-rgb) / <alpha-value>)',
          2: 'rgb(var(--bow-ink-2-rgb) / <alpha-value>)',
          muted: 'rgb(var(--bow-ink-muted-rgb) / <alpha-value>)',
          subtle: 'rgb(var(--bow-ink-subtle-rgb) / <alpha-value>)',
          inverted: 'rgb(var(--bow-ink-inverted-rgb) / <alpha-value>)',
        },
        surface: {
          DEFAULT: 'rgb(var(--bow-bg-rgb) / <alpha-value>)',
          subtle: 'rgb(var(--bow-bg-subtle-rgb) / <alpha-value>)',
          muted: 'rgb(var(--bow-bg-muted-rgb) / <alpha-value>)',
          elevated: 'rgb(var(--bow-bg-elevated-rgb) / <alpha-value>)',
          inverted: 'rgb(var(--bow-bg-inverted-rgb) / <alpha-value>)',
        },
        line: {
          DEFAULT: 'rgb(var(--bow-line-rgb) / <alpha-value>)',
          strong: 'rgb(var(--bow-line-strong-rgb) / <alpha-value>)',
          subtle: 'rgb(var(--bow-line-subtle-rgb) / <alpha-value>)',
        },
      },

      /*
       * Type scale.
       *
       * The floor is 11px, not the 13px first proposed: a hard 13px floor
       * would have grown 1,714 call sites by 2-3px, and the dense surfaces
       * that use them (table chrome, badges, the sidebar ticker, console
       * traces) are width-constrained — it would have forced reflow and
       * truncation across the app. The defect was never "11px exists", it
       * was 14 *arbitrary* sizes including 8px, 9px, 10.5px, 11.5px and
       * 12.5px, which is why nothing lined up.
       *
       * So: one legitimate dense step (`2xs`), and Tailwind's existing
       * steps left at their stock values — `text-xs`/`text-sm` carry 3,361
       * call sites between them and shifting either would be a visual
       * regression, not a fix. Line heights are tightened on the small end,
       * where Tailwind's stock 1.5 ratio is loose for UI text.
       *
       * 21 sizes -> 9. Floor 8px -> 11px.
       */
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }], // 11px/16 — dense metadata, badges, table chrome
        xs: ['0.75rem', { lineHeight: '1.125rem' }], // 12px/18 — stock size, tighter leading
        sm: ['0.875rem', { lineHeight: '1.25rem' }], // 14px/20 — default UI text
        base: ['1rem', { lineHeight: '1.5rem' }], // 16px/24
        lg: ['1.125rem', { lineHeight: '1.75rem' }], // 18px/28 — section headings
        xl: ['1.25rem', { lineHeight: '1.75rem' }], // 20px/28
        '2xl': ['1.5rem', { lineHeight: '2rem' }], // 24px/32
        '3xl': ['1.875rem', { lineHeight: '2.25rem' }], // 30px/36
        '4xl': ['2.25rem', { lineHeight: '2.5rem' }], // 36px/40

        /*
         * The conversation ramp: three steps that all derive from one
         * variable, so a single message stops mixing unrelated systems.
         * At the default scale they are 14 / 13 / 11px — the `sm` step and
         * two derived tiers — and the reader's stored preference moves all
         * three together (14 -> 16 -> 18 -> 20px body).
         *
         *   text-chat    body copy, the composer field
         *   text-chat-2  code, collapsed reasoning, tool-card labels
         *   text-chat-3  badges, ids, durations — true metadata only
         *
         * Use them inside a conversation only. Everywhere else a fixed step
         * from the scale above is still the right answer.
         */
        chat: ['var(--bow-chat-font-size)', { lineHeight: 'var(--bow-chat-line-height)' }],
        'chat-2': ['var(--bow-chat-font-size-2)', { lineHeight: '1.45' }],
        'chat-3': ['var(--bow-chat-font-size-3)', { lineHeight: '1.45' }],
      },

      /*
       * The conversation measure. One token drives the transcript column,
       * the composer and the landing-page composer, which is what keeps the
       * box you type into the same width as the text you read back — they
       * had drifted to 672px and ~53vw respectively, so the composer
       * visibly resized the moment a report was created. It is a multiple
       * of the chat font (see tokens.css), so line length stays ~96
       * characters at every reader step instead of only at the default.
       */
      maxWidth: {
        chat: 'var(--bow-measure-chat)',
      },

      /*
       * Two radii, achieved without touching a single call site.
       *
       * `rounded` (1,014 uses) and `rounded-md` (579) both resolve to 6px,
       * which collapses the two into one visual radius; `rounded-lg` (519)
       * stays 8px for cards and panels. That turns the three-radius muddle
       * — 4px chips inside 8px cards next to 6px buttons — into a
       * deliberate two-step scale with zero file edits.
       */
      borderRadius: {
        sm: 'var(--bow-radius-sm)', // 4px — only for <=16px square affordances
        DEFAULT: 'var(--bow-radius-md)', // 6px (was 4px)
        md: 'var(--bow-radius-md)', // 6px — controls: buttons, inputs, chips
        lg: 'var(--bow-radius-lg)', // 8px — cards, panels, popovers
        xl: 'var(--bow-radius-xl)', // 12px — modals, large surfaces
        /*
         * Named, not a `2xl` step: Tailwind's stock `rounded-2xl` (16px) is
         * already on 21 call sites, and redefining it would move all of them
         * as a side effect of a composer change. This is the one surface
         * that wants it, so it says so.
         */
        composer: 'var(--bow-radius-composer)', // 20px — the composer surface
      },

      boxShadow: {
        sm: 'var(--bow-shadow-sm)',
        DEFAULT: 'var(--bow-shadow-sm)',
        md: 'var(--bow-shadow-md)',
        lg: 'var(--bow-shadow-lg)',
        xl: 'var(--bow-shadow-xl)',
      },

      /*
       * Motion.
       *
       * The `DEFAULT` entries are the point: 601 of the app's transitions
       * carry no `duration-*` or `ease-*` class, so a bare `transition` or
       * `transition-colors` inherits these. One config change gives the
       * untimed 93% a consistent curve and speed.
       */
      transitionDuration: {
        DEFAULT: 'var(--bow-duration-base)', // 180ms (was 150ms, uncurved)
        fast: 'var(--bow-duration-fast)', // 120ms
        base: 'var(--bow-duration-base)', // 180ms
        slow: 'var(--bow-duration-slow)', // 280ms
      },
      transitionTimingFunction: {
        DEFAULT: 'var(--bow-ease)',
        standard: 'var(--bow-ease)',
        out: 'var(--bow-ease-out)',
      },

      /*
       * A layer scale, replacing the ad-hoc ladder the app had grown:
       * z-10/20/30/40/50 plus z-[60], z-[70], z-[90], z-[1000], z-[1001],
       * z-[1050], z-[1100] and z-[2000]. That 1000 -> 1001 -> 1050 -> 1100
       * -> 2000 sequence is the fingerprint of stacking bugs fixed by
       * bumping the number, which is a bug class, not a style preference.
       *
       * Tailwind's numeric z-0..z-50 are untouched; these are named, so
       * `z-modal` cannot drift against `z-dropdown`.
       */
      zIndex: {
        base: '0',
        raised: '10',
        dropdown: '1000',
        sticky: '1100',
        overlay: '1200',
        modal: '1300',
        popover: '1400',
        tooltip: '1500',
        toast: '1600',
      },
    },
  },
}
