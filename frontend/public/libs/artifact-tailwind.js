/**
 * artifact-tailwind.js — Tailwind Play-CDN configuration for artifact sandboxes.
 *
 * Loaded right after tailwindcss-3.4.16.js in every shell that renders an
 * artifact (the in-app iframe, the public viewer, the headless validation and
 * thumbnail render, the standalone HTML export). It maps the design tokens the
 * runtime writes as CSS variables (see setTheme() in artifact-globals.js) onto
 * Tailwind utilities, so generated code can say `bg-surface text-ink
 * font-display rounded-card` and get the active theme in both color modes.
 *
 * Every color reads `rgb(var(--bow-<token>-rgb) / <alpha-value>)` so opacity
 * modifiers keep working (`bg-accent/10`). Committed to the repo (not
 * downloaded) — see frontend/public/libs/.gitignore.
 */
(function () {
  function token(name) {
    return 'rgb(var(--bow-' + name + '-rgb) / <alpha-value>)';
  }
  var colors = {
    bg: token('bg'),
    surface: token('surface'),
    'surface-2': token('surface-2'),
    line: token('line'),
    'line-2': token('line-2'),
    ink: token('ink'),
    'ink-2': token('ink-2'),
    'ink-3': token('ink-3'),
    accent: token('accent'),
    'accent-2': token('accent-2'),
    'accent-ink': token('accent-ink'),
    positive: token('positive'),
    warning: token('warning'),
    negative: token('negative')
  };
  for (var i = 1; i <= 8; i++) colors['chart-' + i] = token('chart-' + i);

  var existing = (typeof window !== 'undefined' && window.tailwind && window.tailwind.config) || {};
  var config = {
    darkMode: 'class',
    theme: {
      extend: {
        colors: colors,
        fontFamily: {
          display: 'var(--bow-font-display)',
          body: 'var(--bow-font-body)',
          mono: 'var(--bow-font-mono)',
          numeric: 'var(--bow-font-numeric)'
        },
        borderRadius: {
          card: 'var(--bow-radius-lg)',
          control: 'var(--bow-radius-md)',
          chip: 'var(--bow-radius-sm)'
        },
        boxShadow: {
          card: 'var(--bow-shadow-card)',
          lift: 'var(--bow-shadow-lift)'
        },
        letterSpacing: {
          eyebrow: '0.12em'
        }
      }
    }
  };
  // Preserve anything a shell already configured (e.g. an explicit darkMode).
  for (var k in existing) if (k !== 'theme') config[k] = existing[k];
  if (typeof window !== 'undefined') {
    window.tailwind = window.tailwind || {};
    window.tailwind.config = config;
  }
})();
