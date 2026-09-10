/**
 * artifact-globals.js — Single source of truth for sandbox runtime globals.
 *
 * Loaded by: ArtifactFrame.vue, r/[id]/index.vue, artifact_libs.py (headless
 * validation, thumbnails, PDF) and the standalone HTML export.
 * Requires: React 18, ReactDOM 18, echarts 5, Tailwind CSS (+ artifact-tailwind.js)
 * loaded beforehand; lucide (icons) is optional.
 * Expects: window.ARTIFACT_DATA set before this script runs.
 *
 * Runtime v11 — the themed design system:
 *   • setTheme(name | spec, overrides) selects one of the built-in themes (or a
 *     custom one) and writes its tokens as CSS variables; Tailwind utilities
 *     (bg-surface, text-ink, font-display, rounded-card, …) and the ECharts
 *     'bow' theme derive from them, in both light and dark mode.
 *   • Components are token-driven and expose `variant` props; `className`
 *     REPLACES a component's visual classes (its layout classes stay).
 *   • Legacy artifacts (ARTIFACT_DATA.runtime.version < 11, or absent) keep the
 *     pre-v11 look and the additive className semantics they were written for.
 */
(function() {
  'use strict';

  var h = React.createElement;

  // ── React hooks as globals ──────────────────────────────────────────────────
  window.useState = React.useState;
  window.useEffect = React.useEffect;
  window.useRef = React.useRef;
  window.useMemo = React.useMemo;
  window.useCallback = React.useCallback;

  // ── Runtime generation ──────────────────────────────────────────────────────
  // Hosts stamp ARTIFACT_DATA.runtime = { version } from content.runtime_version.
  // Artifacts authored before the themed runtime carry no stamp and get the
  // legacy look + semantics — stored rows are never rewritten.
  var RUNTIME_VERSION = 11;
  function _runtimeVersion() {
    var d = window.ARTIFACT_DATA || {};
    var r = d.runtime || {};
    var v = parseInt(r.version, 10);
    return isNaN(v) ? 0 : v;
  }
  var LEGACY = _runtimeVersion() < RUNTIME_VERSION;
  window.__bowLegacyRuntime = LEGACY;
  window.BOW_RUNTIME_VERSION = RUNTIME_VERSION;

  // ── Live data store ─────────────────────────────────────────────────────────
  window.__artifactDataListeners = [];
  function notifyArtifactData() {
    for (var i = 0; i < window.__artifactDataListeners.length; i++) {
      try { window.__artifactDataListeners[i](); } catch (e) {}
    }
  }
  window.__setArtifactData = function(data) {
    window.ARTIFACT_DATA = data;
    if (window.__paramStore) window.__paramStore._ingest(data);
    notifyArtifactData();
  };

  window.useArtifactData = function() {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      var fn = function() { forceUpdate(function(c) { return c + 1; }); };
      window.__artifactDataListeners.push(fn);
      return function() {
        var idx = window.__artifactDataListeners.indexOf(fn);
        if (idx >= 0) window.__artifactDataListeners.splice(idx, 1);
      };
    }, []);
    return window.ARTIFACT_DATA;
  };

  window.vizById = function(id) {
    var data = window.ARTIFACT_DATA || {};
    var list = data.visualizations || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].id) === String(id)) return list[i];
    }
    return null;
  };

  window.useCurrentUser = function() {
    var data = window.ARTIFACT_DATA || {};
    return data.current_user || null;
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // THEMES — design tokens. Each theme pairs a display, body and mono face
  // (all vendored locally, see artifact-fonts.css) with a light and a dark
  // palette, a radius scale and a shadow treatment.
  // ═══════════════════════════════════════════════════════════════════════════

  var FONT_STACKS = {
    fraunces: "'Fraunces', 'Iowan Old Style', Georgia, serif",
    instrument: "'Instrument Serif', 'Iowan Old Style', Georgia, serif",
    playfair: "'Playfair Display', Georgia, serif",
    dmserif: "'DM Serif Display', Georgia, serif",
    bricolage: "'Bricolage Grotesque', 'Segoe UI', system-ui, sans-serif",
    sora: "'Sora', 'Segoe UI', system-ui, sans-serif",
    manrope: "'Manrope', 'Segoe UI', system-ui, sans-serif",
    jakarta: "'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif",
    geist: "'Geist', 'Segoe UI', system-ui, sans-serif",
    inter: "'Inter', 'Segoe UI', system-ui, sans-serif",
    outfit: "'Outfit', 'Segoe UI', system-ui, sans-serif",
    dmsans: "'DM Sans', 'Segoe UI', system-ui, sans-serif",
    jetbrains: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
    plexmono: "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
    system: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
  };
  window.BOW_FONTS = FONT_STACKS;

  var RADIUS = {
    sharp: { sm: '3px', md: '5px', lg: '8px' },
    crisp: { sm: '4px', md: '8px', lg: '12px' },
    soft: { sm: '6px', md: '10px', lg: '16px' },
    round: { sm: '10px', md: '16px', lg: '24px' }
  };
  var SHADOWS = {
    none: { card: '0 0 0 0 transparent', lift: '0 1px 2px rgba(0,0,0,0.06)' },
    soft: { card: '0 1px 2px rgba(16,24,40,0.05)', lift: '0 12px 32px -12px rgba(16,24,40,0.22)' },
    deep: { card: '0 1px 3px rgba(0,0,0,0.25)', lift: '0 20px 40px -16px rgba(0,0,0,0.6)' }
  };

  var THEMES = {
    ledger: {
      label: 'Ledger', mood: 'finance, board reporting, anything that should feel audited and permanent',
      fonts: { display: FONT_STACKS.fraunces, body: FONT_STACKS.inter, mono: FONT_STACKS.jetbrains, numeric: FONT_STACKS.fraunces },
      radius: 'crisp', shadow: 'none',
      light: { bg: '#f6f4ee', surface: '#fffdf8', surface2: '#efeadf', line: '#e3dccb', line2: '#cdc3ab', ink: '#1c1a15', ink2: '#55503f', ink3: '#8a836f', accent: '#8f2d1c', accent2: '#1f4e79', accentInk: '#ffffff', positive: '#2e6b46', warning: '#a26a12', negative: '#a12e2e', chart: ['#8f2d1c', '#1f4e79', '#c39a2b', '#3e6b4a', '#6b4a7a', '#c8703e', '#2f7c86', '#8b6f43'] },
      dark: { bg: '#15140f', surface: '#1e1c16', surface2: '#27241c', line: '#35311f', line2: '#4a4531', ink: '#f1ede3', ink2: '#bdb6a3', ink3: '#877f6a', accent: '#e07a5f', accent2: '#6fa3d6', accentInk: '#15140f', positive: '#7cc094', warning: '#e0b45c', negative: '#e0716a', chart: ['#e07a5f', '#6fa3d6', '#e3c463', '#88b58f', '#b394c7', '#f0a06d', '#63b8c1', '#c9a679'] }
    },
    nocturne: {
      label: 'Nocturne', mood: 'dark-native analytics, live operations, anything that should feel like a control room', forcedDark: true,
      fonts: { display: FONT_STACKS.sora, body: FONT_STACKS.geist, mono: FONT_STACKS.jetbrains, numeric: FONT_STACKS.sora },
      radius: 'soft', shadow: 'deep',
      light: { bg: '#f2f5fa', surface: '#ffffff', surface2: '#e9eef6', line: '#d5dce8', line2: '#b7c2d6', ink: '#0f172a', ink2: '#475569', ink3: '#7c8aa3', accent: '#0e7ba8', accent2: '#b8860b', accentInk: '#ffffff', positive: '#15803d', warning: '#b45309', negative: '#b91c1c', chart: ['#0e7ba8', '#b8860b', '#6d4bd1', '#0f9d6b', '#d1345b', '#2563eb', '#e07b28', '#0d9488'] },
      dark: { bg: '#0b0f17', surface: '#121826', surface2: '#1a2234', line: '#22304a', line2: '#31446a', ink: '#e8edf7', ink2: '#a7b3cc', ink3: '#6b7896', accent: '#4cc9f0', accent2: '#f2c94c', accentInk: '#0b0f17', positive: '#4ade80', warning: '#fbbf24', negative: '#f87171', chart: ['#4cc9f0', '#f2c94c', '#a78bfa', '#34d399', '#fb7185', '#60a5fa', '#fdba74', '#2dd4bf'] }
    },
    atelier: {
      label: 'Atelier', mood: 'music, media, culture, creative products — expressive, editorial, a little theatrical',
      fonts: { display: FONT_STACKS.instrument, body: FONT_STACKS.jakarta, mono: FONT_STACKS.plexmono, numeric: FONT_STACKS.instrument },
      radius: 'round', shadow: 'soft',
      light: { bg: '#f4f1f7', surface: '#fbfaff', surface2: '#ece7f3', line: '#dcd4e8', line2: '#c1b5d6', ink: '#1d1730', ink2: '#574c70', ink3: '#8b809f', accent: '#5b2fd6', accent2: '#ff6b5e', accentInk: '#ffffff', positive: '#1f9d6a', warning: '#c07f13', negative: '#d63c5e', chart: ['#5b2fd6', '#ff6b5e', '#22b1a0', '#f2b134', '#3f7cf5', '#e35fa6', '#7dd25c', '#9d7bff'] },
      dark: { bg: '#140f1f', surface: '#1d162c', surface2: '#271e3a', line: '#382d52', line2: '#4e4170', ink: '#f3eefc', ink2: '#bfb3d8', ink3: '#857a9e', accent: '#a98cff', accent2: '#ff8a7a', accentInk: '#140f1f', positive: '#5fd3a3', warning: '#f0c060', negative: '#ff7d97', chart: ['#a98cff', '#ff8a7a', '#5fd8c8', '#f8cc63', '#7aa8ff', '#f78ccb', '#a5e37f', '#c9b6ff'] }
    },
    signal: {
      label: 'Signal', mood: 'operations, monitoring, logistics, incident and SLA tracking — dense, alert, utilitarian',
      fonts: { display: FONT_STACKS.bricolage, body: FONT_STACKS.manrope, mono: FONT_STACKS.plexmono, numeric: FONT_STACKS.plexmono },
      radius: 'sharp', shadow: 'none',
      light: { bg: '#f3f4f6', surface: '#ffffff', surface2: '#eaecef', line: '#d9dde3', line2: '#b9c0ca', ink: '#111418', ink2: '#4b5563', ink3: '#7d8590', accent: '#ff5a1f', accent2: '#0f4c81', accentInk: '#ffffff', positive: '#12855c', warning: '#cf8a04', negative: '#d1242f', chart: ['#0f4c81', '#ff5a1f', '#12855c', '#cf8a04', '#7048e8', '#d1242f', '#0aa6a6', '#6b7280'] },
      dark: { bg: '#0f1114', surface: '#171a1f', surface2: '#1f242b', line: '#2b323b', line2: '#3d4652', ink: '#eef1f4', ink2: '#aeb7c2', ink3: '#717b88', accent: '#ff7a45', accent2: '#66a6e0', accentInk: '#0f1114', positive: '#3ec98a', warning: '#f5b63c', negative: '#f36b6b', chart: ['#66a6e0', '#ff7a45', '#3ec98a', '#f5b63c', '#a58cff', '#f36b6b', '#3fd0d0', '#9aa3ad'] }
    },
    meadow: {
      label: 'Meadow', mood: 'health, people, sustainability, education, community — calm, organic, generous spacing',
      fonts: { display: FONT_STACKS.dmserif, body: FONT_STACKS.dmsans, mono: FONT_STACKS.jetbrains, numeric: FONT_STACKS.dmsans },
      radius: 'round', shadow: 'soft',
      light: { bg: '#f3f6ef', surface: '#fbfcf8', surface2: '#e9eee1', line: '#d6ddca', line2: '#b8c4a6', ink: '#1b2418', ink2: '#4f5d47', ink3: '#83917a', accent: '#2f6f3e', accent2: '#d98c2b', accentInk: '#ffffff', positive: '#2f6f3e', warning: '#b7791f', negative: '#b3402e', chart: ['#2f6f3e', '#d98c2b', '#5b8fb9', '#8a5a9e', '#c95d4f', '#7fa650', '#3a9a97', '#a67c52'] },
      dark: { bg: '#111710', surface: '#182018', surface2: '#202a1f', line: '#2e3a2c', line2: '#43523f', ink: '#eef3ea', ink2: '#b6c3af', ink3: '#7f8c78', accent: '#7cc48b', accent2: '#f0b35a', accentInk: '#111710', positive: '#7cc48b', warning: '#f0b35a', negative: '#ee8a7a', chart: ['#7cc48b', '#f0b35a', '#8ab8e0', '#b78fcc', '#ee8a7a', '#a9cf6e', '#6fc4c1', '#c9a57e'] }
    },
    slate: {
      label: 'Slate', mood: 'corporate, internal tooling, general business reporting — quiet, neutral, precise (the safe default)',
      fonts: { display: FONT_STACKS.outfit, body: FONT_STACKS.inter, mono: FONT_STACKS.jetbrains, numeric: FONT_STACKS.outfit },
      radius: 'soft', shadow: 'soft',
      light: { bg: '#f5f6f8', surface: '#ffffff', surface2: '#eceef2', line: '#dcdfe5', line2: '#c0c5ce', ink: '#171a21', ink2: '#4a5160', ink3: '#7d8593', accent: '#2f4f9e', accent2: '#0f8b8d', accentInk: '#ffffff', positive: '#1e7f4f', warning: '#b7791f', negative: '#b42323', chart: ['#2f4f9e', '#0f8b8d', '#e08a1e', '#7d4fb5', '#c23b5e', '#5f8f2e', '#3f8fd0', '#8a7a5f'] },
      dark: { bg: '#0f1218', surface: '#171b23', surface2: '#1f2430', line: '#2c3342', line2: '#3e475a', ink: '#edf0f5', ink2: '#aeb6c5', ink3: '#73809a', accent: '#7c9bea', accent2: '#4fc2c4', accentInk: '#0f1218', positive: '#5cc48e', warning: '#e5b656', negative: '#ef7676', chart: ['#7c9bea', '#4fc2c4', '#f0a94e', '#b191e6', '#f07f9b', '#a2cd6a', '#77b9f0', '#c4b191'] }
    },
    sunset: {
      label: 'Sunset', mood: 'retail, consumer, marketing, hospitality, growth — warm, friendly, energetic',
      fonts: { display: FONT_STACKS.sora, body: FONT_STACKS.jakarta, mono: FONT_STACKS.plexmono, numeric: FONT_STACKS.sora },
      radius: 'round', shadow: 'soft',
      light: { bg: '#fff7f2', surface: '#fffdfb', surface2: '#ffece2', line: '#f5d9ca', line2: '#e8b9a0', ink: '#2a1a14', ink2: '#6b4d40', ink3: '#9c7f70', accent: '#e8503a', accent2: '#6b2d86', accentInk: '#ffffff', positive: '#2a8a5c', warning: '#d78a1a', negative: '#c93a3a', chart: ['#e8503a', '#6b2d86', '#f5a623', '#2f9e8f', '#e77fb3', '#4e6fd8', '#8fb339', '#d96b3f'] },
      dark: { bg: '#1a1210', surface: '#241a17', surface2: '#2f221e', line: '#43312b', line2: '#5d453c', ink: '#fbf1ec', ink2: '#d1b8ad', ink3: '#97817a', accent: '#ff7a63', accent2: '#c48ce0', accentInk: '#1a1210', positive: '#66c496', warning: '#f2b054', negative: '#ff7373', chart: ['#ff7a63', '#c48ce0', '#ffc35c', '#5fd0c0', '#ff9ccd', '#8aa2ff', '#b8d96a', '#ff9d73'] }
    },
    graphite: {
      label: 'Graphite', mood: 'engineering, infrastructure, data platforms, developer-facing metrics — monospace numerals, dark-native', forcedDark: true,
      fonts: { display: FONT_STACKS.geist, body: FONT_STACKS.geist, mono: FONT_STACKS.plexmono, numeric: FONT_STACKS.plexmono },
      radius: 'sharp', shadow: 'deep',
      light: { bg: '#f4f4f5', surface: '#ffffff', surface2: '#ebebed', line: '#dadadd', line2: '#bdbec3', ink: '#17181a', ink2: '#4c4f55', ink3: '#7e828a', accent: '#b8860b', accent2: '#0f8b8d', accentInk: '#ffffff', positive: '#1e7f4f', warning: '#b8860b', negative: '#b42323', chart: ['#b8860b', '#0f8b8d', '#2f6fd0', '#7d4fb5', '#c23b5e', '#5f8f2e', '#d9701f', '#3f9aa0'] },
      dark: { bg: '#131416', surface: '#1a1c1f', surface2: '#222528', line: '#2c3034', line2: '#3f444a', ink: '#ececec', ink2: '#b3b7bd', ink3: '#767c85', accent: '#f5b942', accent2: '#4ec5b3', accentInk: '#131416', positive: '#62c98a', warning: '#f5b942', negative: '#ef6f6f', chart: ['#f5b942', '#4ec5b3', '#8ab4f8', '#c792ea', '#f28b82', '#9ccc65', '#ffab70', '#80cbc4'] }
    },
    // The pre-v11 look, applied to legacy artifacts so stored rows render as
    // they always did. Not offered to new artifacts.
    legacy: {
      label: 'Legacy', mood: 'pre-v11 artifacts', hidden: true,
      fonts: { display: FONT_STACKS.system, body: FONT_STACKS.system, mono: FONT_STACKS.jetbrains, numeric: FONT_STACKS.system },
      radius: 'soft', shadow: 'soft',
      light: { bg: '#ffffff', surface: '#ffffff', surface2: '#f8fafc', line: '#e2e8f0', line2: '#cbd5e1', ink: '#0f172a', ink2: '#475569', ink3: '#94a3b8', accent: '#3b82f6', accent2: '#8b5cf6', accentInk: '#ffffff', positive: '#10b981', warning: '#f59e0b', negative: '#ef4444', chart: ['#3B82F6', '#10B981', '#8B5CF6', '#F59E0B', '#EF4444', '#06B6D4', '#EC4899', '#14B8A6', '#60A5FA', '#34D399'] },
      dark: { bg: '#111827', surface: '#0f172a', surface2: '#1e293b', line: '#334155', line2: '#475569', ink: '#f1f5f9', ink2: '#cbd5e1', ink3: '#94a3b8', accent: '#60a5fa', accent2: '#a78bfa', accentInk: '#0f172a', positive: '#34d399', warning: '#fbbf24', negative: '#f87171', chart: ['#60A5FA', '#34D399', '#A78BFA', '#FBBF24', '#F87171', '#22D3EE', '#F472B6', '#2DD4BF', '#93C5FD', '#6EE7B7'] }
    }
  };
  window.BOW_THEMES = THEMES;

  function _hexToRgb(hex) {
    var s = String(hex || '').trim();
    var m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(s);
    if (!m) return null;
    var x = m[1];
    if (x.length === 3) x = x[0] + x[0] + x[1] + x[1] + x[2] + x[2];
    var n = parseInt(x, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function _rgba(hex, a) {
    var rgb = _hexToRgb(hex);
    if (!rgb) return hex;
    return 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',' + a + ')';
  }
  function _clone(o) { return JSON.parse(JSON.stringify(o)); }

  var _themeListeners = [];
  var _theme = null;        // resolved theme spec (fonts, light, dark, radius, shadow)
  var _themeName = LEGACY ? 'legacy' : 'slate';
  var _tokens = null;       // the palette in effect (light or dark)
  var _forcedDark = false;

  function _bowIsDark() {
    return document.documentElement.classList.contains('dark');
  }

  // Resolve (name | spec) + overrides into a full theme spec.
  function _resolveTheme(spec, overrides) {
    var base;
    if (typeof spec === 'string') {
      base = THEMES[spec] ? _clone(THEMES[spec]) : _clone(THEMES.slate);
      if (!THEMES[spec]) console.warn('[setTheme] unknown theme "' + spec + '" — using slate. Available: ' + Object.keys(THEMES).filter(function(k) { return !THEMES[k].hidden; }).join(', '));
      base.name = THEMES[spec] ? spec : 'slate';
    } else if (spec && typeof spec === 'object') {
      var parent = THEMES[spec.extends] || THEMES.slate;
      base = _clone(parent);
      base.name = spec.name || 'custom';
      if (spec.fonts) for (var fk in spec.fonts) base.fonts[fk] = spec.fonts[fk];
      if (spec.light) for (var lk in spec.light) base.light[lk] = spec.light[lk];
      if (spec.dark) for (var dk in spec.dark) base.dark[dk] = spec.dark[dk];
      if (spec.radius) base.radius = spec.radius;
      if (spec.shadow) base.shadow = spec.shadow;
      if (spec.forcedDark != null) base.forcedDark = !!spec.forcedDark;
    } else {
      base = _clone(THEMES.slate); base.name = 'slate';
    }
    var o = overrides || {};
    // Overrides apply to both palettes: colors that read the same on either
    // ground (accent, chart) go straight through; grounds (bg/surface) only
    // to the light palette unless a dark: block is supplied.
    var both = ['accent', 'accent2', 'accentInk', 'positive', 'warning', 'negative', 'chart'];
    for (var i = 0; i < both.length; i++) {
      if (o[both[i]] != null) { base.light[both[i]] = o[both[i]]; base.dark[both[i]] = o[both[i]]; }
    }
    var lightOnly = ['bg', 'surface', 'surface2', 'line', 'line2', 'ink', 'ink2', 'ink3'];
    for (var j = 0; j < lightOnly.length; j++) if (o[lightOnly[j]] != null) base.light[lightOnly[j]] = o[lightOnly[j]];
    if (o.light) for (var a in o.light) base.light[a] = o.light[a];
    if (o.dark) for (var b in o.dark) base.dark[b] = o.dark[b];
    if (o.fontDisplay) base.fonts.display = o.fontDisplay;
    if (o.fontBody) base.fonts.body = o.fontBody;
    if (o.fontMono) base.fonts.mono = o.fontMono;
    if (o.fontNumeric) base.fonts.numeric = o.fontNumeric;
    if (o.radius) base.radius = o.radius;
    if (o.shadow) base.shadow = o.shadow;
    if (o.forcedDark != null) base.forcedDark = !!o.forcedDark;
    if (o.mode === 'dark') base.forcedDark = true;
    if (!RADIUS[base.radius]) base.radius = 'soft';
    if (!SHADOWS[base.shadow]) base.shadow = 'soft';
    return base;
  }

  var _TOKEN_KEYS = { bg: 'bg', surface: 'surface', surface2: 'surface-2', line: 'line', line2: 'line-2', ink: 'ink', ink2: 'ink-2', ink3: 'ink-3', accent: 'accent', accent2: 'accent-2', accentInk: 'accent-ink', positive: 'positive', warning: 'warning', negative: 'negative' };

  function _writeCssVars(theme, pal) {
    var root = document.documentElement.style;
    for (var k in _TOKEN_KEYS) {
      var css = _TOKEN_KEYS[k];
      var hex = pal[k];
      root.setProperty('--bow-' + css, hex);
      var rgb = _hexToRgb(hex);
      root.setProperty('--bow-' + css + '-rgb', rgb ? rgb.join(' ') : '0 0 0');
    }
    for (var i = 0; i < 8; i++) {
      var c = (pal.chart && pal.chart[i % pal.chart.length]) || pal.accent;
      root.setProperty('--bow-chart-' + (i + 1), c);
      var crgb = _hexToRgb(c);
      root.setProperty('--bow-chart-' + (i + 1) + '-rgb', crgb ? crgb.join(' ') : '0 0 0');
    }
    root.setProperty('--bow-font-display', theme.fonts.display);
    root.setProperty('--bow-font-body', theme.fonts.body);
    root.setProperty('--bow-font-mono', theme.fonts.mono);
    root.setProperty('--bow-font-numeric', theme.fonts.numeric || theme.fonts.display);
    var r = RADIUS[theme.radius] || RADIUS.soft;
    root.setProperty('--bow-radius-sm', r.sm);
    root.setProperty('--bow-radius-md', r.md);
    root.setProperty('--bow-radius-lg', r.lg);
    var s = SHADOWS[theme.shadow] || SHADOWS.soft;
    root.setProperty('--bow-shadow-card', s.card);
    root.setProperty('--bow-shadow-lift', s.lift);
    document.documentElement.setAttribute('data-bow-theme', theme.name);
  }

  // Base stylesheet: body ground + type, display faces on page headings,
  // tabular numerals where digits line up, focus rings from the accent.
  (function injectBaseStyles() {
    var st = document.createElement('style');
    st.id = 'bow-theme-base';
    st.textContent = [
      'html { color-scheme: light; }',
      'html.dark { color-scheme: dark; }',
      'body { background: var(--bow-bg, #fff); color: var(--bow-ink, #111); font-family: var(--bow-font-body, system-ui, sans-serif); -webkit-font-smoothing: antialiased; }',
      '#root h1, #root h2 { font-family: var(--bow-font-display, inherit); letter-spacing: -0.015em; text-wrap: balance; }',
      '#root h1 { font-weight: 600; }',
      '#root table, #root .tabular { font-variant-numeric: tabular-nums; }',
      '#root :focus-visible { outline: 2px solid var(--bow-accent); outline-offset: 2px; }',
      '#root ::selection { background: color-mix(in srgb, var(--bow-accent) 22%, transparent); }',
      '@media (prefers-reduced-motion: reduce) { #root * { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; } }',
      '@media print { .bow-dt-offpage { display: table-row !important; } .bow-dt-chrome { display: none !important; } .bow-dt-scroll { max-height: none !important; overflow: visible !important; } }'
    ].join('\n');
    (document.head || document.documentElement).appendChild(st);
  })();

  function _applyTheme() {
    if (!_theme) _theme = _resolveTheme(_themeName);
    _forcedDark = !!_theme.forcedDark;
    if (_forcedDark && !_bowIsDark()) {
      window.__bowForcedDark = true;
      document.documentElement.classList.add('dark');
    }
    var dark = _forcedDark || _bowIsDark();
    _tokens = dark ? _theme.dark : _theme.light;
    _writeCssVars(_theme, _tokens);
    _registerChartTheme(_theme, _tokens, dark);
    window.bowTheme = { name: _theme.name, dark: dark, fonts: _theme.fonts, colors: _tokens, chart: _tokens.chart, radius: RADIUS[_theme.radius], forcedDark: _forcedDark };
    for (var i = 0; i < _themeListeners.length; i++) { try { _themeListeners[i](); } catch (e) {} }
    try { window.dispatchEvent(new CustomEvent('bow-theme', { detail: { name: _theme.name, dark: dark } })); } catch (e) {}
  }

  // setTheme('ledger') | setTheme('atelier', { accent: '#d63c5e', fontDisplay: BOW_FONTS.playfair })
  // | setTheme({ extends: 'slate', name: 'acme', light: {...}, dark: {...} })
  window.setTheme = function(spec, overrides) {
    _theme = _resolveTheme(spec, overrides);
    _themeName = _theme.name;
    _applyTheme();
    return window.bowTheme;
  };

  // useTheme() — current tokens (re-renders on theme/color-mode changes).
  // Returns { name, dark, fonts, colors: {bg, surface, ..., chart:[...]}, chart, radius }.
  window.useTheme = function() {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      var fn = function() { forceUpdate(function(c) { return c + 1; }); };
      _themeListeners.push(fn);
      return function() { var i = _themeListeners.indexOf(fn); if (i >= 0) _themeListeners.splice(i, 1); };
    }, []);
    return window.bowTheme;
  };

  // ── ECharts theme from tokens ───────────────────────────────────────────────
  function _registerChartTheme(theme, pal, dark) {
    if (typeof echarts === 'undefined') return;
    var text = { color: pal.ink2, fontFamily: theme.fonts.body, fontSize: 12 };
    var spec = {
      color: pal.chart,
      backgroundColor: 'transparent',
      textStyle: { fontFamily: theme.fonts.body, color: pal.ink2 },
      title: { textStyle: { color: pal.ink, fontFamily: theme.fonts.display, fontWeight: 600, fontSize: 15 }, subtextStyle: { color: pal.ink3 } },
      legend: { textStyle: { color: pal.ink2, fontFamily: theme.fonts.body }, itemWidth: 12, itemHeight: 8, icon: 'roundRect' },
      categoryAxis: {
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: pal.ink3, fontSize: 11, fontFamily: theme.fonts.body },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: pal.ink3, fontSize: 11, fontFamily: theme.fonts.body },
        splitLine: { lineStyle: { color: _rgba(pal.line2, dark ? 0.35 : 0.55), type: [4, 4] } }
      },
      timeAxis: { axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: pal.ink3, fontSize: 11 }, splitLine: { show: false } },
      logAxis: { axisLine: { show: false }, axisLabel: { color: pal.ink3, fontSize: 11 }, splitLine: { lineStyle: { color: _rgba(pal.line2, 0.4) } } },
      line: { smooth: false, symbol: 'circle', symbolSize: 6, showSymbol: false, lineStyle: { width: 2.25 }, emphasis: { focus: 'series' } },
      bar: { itemStyle: { borderRadius: [3, 3, 0, 0] }, barMaxWidth: 42 },
      pie: { itemStyle: { borderRadius: 4, borderColor: pal.surface, borderWidth: 2 }, label: { color: pal.ink2 } },
      radar: { axisName: { color: pal.ink2 }, splitLine: { lineStyle: { color: _rgba(pal.line2, 0.5) } }, splitArea: { show: false } },
      gauge: { axisLine: { lineStyle: { color: [[1, _rgba(pal.line2, 0.4)]] } }, detail: { color: pal.ink, fontFamily: theme.fonts.numeric } },
      grid: { left: 8, right: 12, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: dark ? pal.surface2 : pal.surface,
        borderColor: pal.line2, borderWidth: 1, borderRadius: 8, padding: [10, 12],
        textStyle: { color: pal.ink, fontSize: 12, fontFamily: theme.fonts.body },
        extraCssText: 'box-shadow: 0 12px 32px -12px rgba(0,0,0,0.35);',
        axisPointer: { lineStyle: { color: pal.ink3 }, crossStyle: { color: pal.ink3 }, shadowStyle: { color: _rgba(pal.ink, 0.06) } }
      },
      dataZoom: { textStyle: text, borderColor: pal.line, fillerColor: _rgba(pal.accent, 0.12), handleStyle: { color: pal.surface, borderColor: pal.accent } },
      visualMap: { textStyle: text }
    };
    echarts.registerTheme('bow', spec);
    echarts.registerTheme('bow-dark', spec);
  }

  // ── Icons (lucide) ──────────────────────────────────────────────────────────
  // <Icon name="trending-up" size={16} strokeWidth={2} className="text-accent" />
  var _iconNodeCache = {};
  function _pascal(name) {
    return String(name || '').split(/[-_\s]+/).map(function(p) { return p ? p[0].toUpperCase() + p.slice(1) : ''; }).join('');
  }
  function _camelAttr(k) { return k.replace(/-([a-z])/g, function(_, c) { return c.toUpperCase(); }); }
  function _iconNode(name) {
    if (_iconNodeCache[name] !== undefined) return _iconNodeCache[name];
    var L = window.lucide;
    var node = null;
    if (L && L.icons) {
      node = L.icons[name] || L.icons[_pascal(name)] || null;
    }
    _iconNodeCache[name] = node;
    return node;
  }
  window.Icon = function(props) {
    props = props || {};
    var node = _iconNode(props.name);
    var size = props.size || 16;
    if (!node) return null;
    var attrs = node[1] || {};
    var children = node[2] || [];
    var svgProps = {
      xmlns: 'http://www.w3.org/2000/svg', width: size, height: size, viewBox: attrs.viewBox || '0 0 24 24',
      fill: 'none', stroke: 'currentColor', strokeWidth: props.strokeWidth || attrs['stroke-width'] || 2,
      strokeLinecap: 'round', strokeLinejoin: 'round', className: props.className || '', style: props.style,
      'aria-hidden': props.title ? undefined : 'true', role: props.title ? 'img' : undefined
    };
    var kids = children.map(function(c, i) {
      var tag = c[0], a = c[1] || {}, p = { key: i };
      for (var k in a) p[_camelAttr(k)] = a[k];
      return h(tag, p);
    });
    if (props.title) kids.unshift(h('title', { key: 't' }, props.title));
    return h('svg', svgProps, kids);
  };
  window.hasIcon = function(name) { return !!_iconNode(name); };

  // ── LoadingSpinner ──────────────────────────────────────────────────────────
  window.LoadingSpinner = function(props) {
    var size = props && props.size ? props.size : 24;
    return h('svg', {
      xmlns: 'http://www.w3.org/2000/svg', width: size, height: size,
      viewBox: '0 0 24 24', className: props && props.className ? props.className : ''
    },
      h('path', { fill: 'currentColor', d: 'M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8A8 8 0 0 1 12 20Z', opacity: '0.35' }),
      h('path', { fill: 'currentColor', d: 'M20 12h2A10 10 0 0 0 12 2V4A8 8 0 0 1 20 12Z' },
        h('animateTransform', { attributeName: 'transform', dur: '1s', from: '0 12 12', repeatCount: 'indefinite', to: '360 12 12', type: 'rotate' }))
    );
  };

  // ── fmt() number formatter ──────────────────────────────────────────────────
  window.fmt = function(n, opts) {
    if (n == null) return '—';
    if (typeof n !== 'number') return String(n);
    opts = opts || {};
    if (opts.currency && LEGACY) {
      // Pre-v11 behaviour, kept byte-for-byte so legacy artifacts render the same numbers.
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: opts.currency === true ? 'USD' : opts.currency, maximumFractionDigits: opts.decimals != null ? opts.decimals : 0 }).format(n);
    }
    if (opts.pct && LEGACY) return n.toFixed(1) + '%';
    if (opts.currency) {
      var abs = Math.abs(n);
      var cur = opts.currency === true ? 'USD' : opts.currency;
      if (opts.compact !== false && abs >= 1e6) {
        var unit = abs >= 1e9 ? 'B' : 'M';
        var div = abs >= 1e9 ? 1e9 : 1e6;
        var sym = new Intl.NumberFormat('en-US', { style: 'currency', currency: cur, maximumFractionDigits: 0 }).format(0).replace(/[\d.,\s]/g, '');
        return (n < 0 ? '-' : '') + sym + (abs / div).toFixed(1) + unit;
      }
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: cur, maximumFractionDigits: opts.decimals != null ? opts.decimals : (abs < 100 ? 2 : 0) }).format(n);
    }
    if (opts.ratio) n = n * 100;  // a share (0.358) → 35.8%
    if (opts.pct || opts.ratio) return (opts.sign && n > 0 ? '+' : '') + n.toFixed(opts.decimals != null ? opts.decimals : 1) + '%';
    if (opts.compact === false) return n.toLocaleString(undefined, { maximumFractionDigits: opts.decimals != null ? opts.decimals : 2 });
    if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: opts.decimals != null ? opts.decimals : 2 });
  };

  // ── exportCSV() ─────────────────────────────────────────────────────────────
  window.exportCSV = function(rows, opts) {
    opts = opts || {};
    if (!Array.isArray(rows) || rows.length === 0) { console.warn('[exportCSV] no rows to export'); return; }
    var fields;
    if (Array.isArray(opts.columns) && opts.columns.length > 0) {
      fields = opts.columns.map(function(c) { return typeof c === 'string' ? c : (c && c.field); }).filter(Boolean);
    } else {
      fields = Object.keys(rows[0] || {});
    }
    if (fields.length === 0) { console.warn('[exportCSV] no columns to export'); return; }
    var escape = function(v) {
      if (v == null) return '';
      if (typeof v === 'object') { try { v = JSON.stringify(v); } catch (e) { v = String(v); } }
      else v = String(v);
      if (/[",\r\n]/.test(v)) return '"' + v.replace(/"/g, '""') + '"';
      return v;
    };
    var lines = [fields.map(escape).join(',')];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i] || {};
      var cells = [];
      for (var j = 0; j < fields.length; j++) cells.push(escape(row[fields[j]]));
      lines.push(cells.join(','));
    }
    var filename = opts.filename || 'export.csv';
    if (!/\.csv$/i.test(filename)) filename += '.csv';
    var blob = new Blob(['\uFEFF' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 0);
  };

  // ── CustomTooltip ───────────────────────────────────────────────────────────
  window.CustomTooltip = function(props) {
    if (!props.active || !props.payload || !props.payload.length) return null;
    return h('div', { className: 'bg-surface text-ink border border-line-2 px-3 py-2 rounded-control shadow-lift text-sm' }, [
      h('p', { key: 'l', className: 'font-medium text-ink-2 mb-1' }, props.label),
    ].concat(props.payload.map(function(p, i) {
      return h('p', { key: i, className: 'flex items-center gap-2' }, [
        h('span', { key: 'd', className: 'w-2 h-2 rounded-full inline-block', style: { backgroundColor: p.color } }),
        h('span', { key: 'n', className: 'text-ink-3' }, p.name + ': '),
        h('span', { key: 'v', className: 'font-semibold tabular-nums' }, typeof p.value === 'number' ? p.value.toLocaleString() : p.value),
      ]);
    })));
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Filter store + useFilters — client-side row filtering over loaded data
  // ═══════════════════════════════════════════════════════════════════════════

  window.__filterStore = (function() {
    var filters = {};
    var listeners = [];
    return {
      get: function() { return filters; },
      set: function(field, value) {
        var next = {};
        for (var k in filters) next[k] = filters[k];
        if (value == null || value === '') delete next[field];
        else next[field] = value;
        filters = next;
        for (var i = 0; i < listeners.length; i++) listeners[i]();
      },
      reset: function() {
        filters = {};
        for (var i = 0; i < listeners.length; i++) listeners[i]();
      },
      sub: function(fn) {
        listeners.push(fn);
        return function() { var idx = listeners.indexOf(fn); if (idx >= 0) listeners.splice(idx, 1); };
      }
    };
  })();

  window.useFilters = function() {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      return window.__filterStore.sub(function() { forceUpdate(function(c) { return c + 1; }); });
    }, []);
    var filters = window.__filterStore.get();
    var filterRows = React.useCallback(function(rows, fieldMap) {
      var currentFilters = window.__filterStore.get();
      var entries = Object.entries(currentFilters);
      if (!entries.length) return rows;
      return rows.filter(function(row) {
        for (var i = 0; i < entries.length; i++) {
          var key = entries[i][0], val = entries[i][1];
          var col = (fieldMap && fieldMap[key]) ? fieldMap[key] : key;
          if (!Object.prototype.hasOwnProperty.call(row, col)) continue;
          var rv = row[col];
          if (val && typeof val === 'object' && !Array.isArray(val) && (val.from || val.to)) {
            var s = String(rv);
            if (val.from && s < val.from) return false;
            if (val.to && s > val.to) return false;
          } else if (Array.isArray(val)) {
            if (val.length > 0 && val.indexOf(String(rv)) === -1) return false;
          } else {
            if (val && String(rv).toLowerCase().indexOf(String(val).toLowerCase()) === -1) return false;
          }
        }
        return true;
      });
    }, [filters]);
    return { filters: filters, setFilter: window.__filterStore.set, resetFilters: window.__filterStore.reset, filterRows: filterRows };
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Params store + useParams — server-side query parameters (unchanged contract)
  // ═══════════════════════════════════════════════════════════════════════════

  window.__paramStore = (function() {
    var declarations = [];
    var values = {};
    var options = {};
    var pending = {};
    var inflight = {};
    var inflightSeq = {};
    var commitSeq = 0;
    var loading = false;
    var error = null;
    var listeners = [];
    var timer = null;

    function notify() { for (var i = 0; i < listeners.length; i++) { try { listeners[i](); } catch (e) {} } }
    function post(msg) { try { window.parent.postMessage(msg, '*'); } catch (e) {} }

    function commit(targets) {
      var changes = {};
      var any = false;
      commitSeq += 1;
      for (var k in pending) { changes[k] = pending[k]; inflight[k] = pending[k]; inflightSeq[k] = commitSeq; any = true; }
      if (!any) return;
      pending = {};
      loading = true;
      error = null;
      notify();
      post({ type: 'ARTIFACT_SET_PARAMS', changes: changes, targets: targets || null, seq: commitSeq });
    }

    return {
      _ingest: function(data) {
        var p = (data && data.params) || {};
        declarations = p.declarations || [];
        values = p.values || {};
        options = p.options || {};
        var ack = (typeof p.ack === 'number') ? p.ack : null;
        for (var k in inflight) {
          var confirmed = (ack !== null) ? (inflightSeq[k] <= ack) : (JSON.stringify(values[k]) === JSON.stringify(inflight[k]));
          if (confirmed) { delete inflight[k]; delete inflightSeq[k]; }
        }
        loading = false;
        error = null;
        notify();
      },
      _status: function(payload) {
        loading = !!(payload && payload.loading);
        error = (payload && payload.error) || null;
        notify();
      },
      getDeclarations: function() { return declarations; },
      getValues: function() {
        var out = {};
        var k;
        for (k in values) out[k] = values[k];
        for (k in inflight) out[k] = inflight[k];
        for (k in pending) out[k] = pending[k];
        return out;
      },
      getOptions: function(name) {
        if (options && options[name] && options[name].length) return options[name];
        for (var i = 0; i < declarations.length; i++) {
          var d = declarations[i];
          if (d && d.name === name && d.options && d.options.length) {
            return d.options.map(function(v) { return (v && typeof v === 'object' && 'value' in v) ? v : { value: v, label: String(v) }; });
          }
        }
        return null;
      },
      getPending: function() { return pending; },
      isLoading: function() { return loading; },
      getError: function() { return error; },
      setParam: function(name, value, opts) {
        pending[name] = value;
        notify();
        if (opts && opts.apply === false) return;
        if (timer) clearTimeout(timer);
        timer = setTimeout(function() { timer = null; commit(null); }, 250);
      },
      setParams: function(changes, opts) {
        for (var k in (changes || {})) pending[k] = changes[k];
        notify();
        if (opts && opts.apply === false) return;
        if (timer) clearTimeout(timer);
        timer = setTimeout(function() { timer = null; commit(null); }, 250);
      },
      apply: function(targets) { if (timer) { clearTimeout(timer); timer = null; } commit(targets || null); },
      refresh: function(targets) { loading = true; notify(); post({ type: 'ARTIFACT_REFRESH_PARAMS', targets: targets || null }); },
      sub: function(fn) {
        listeners.push(fn);
        return function() { var idx = listeners.indexOf(fn); if (idx >= 0) listeners.splice(idx, 1); };
      }
    };
  })();

  window.useParams = function() {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      return window.__paramStore.sub(function() { forceUpdate(function(c) { return c + 1; }); });
    }, []);
    var store = window.__paramStore;
    return {
      declarations: store.getDeclarations(), values: store.getValues(), pending: store.getPending(),
      loading: store.isLoading(), error: store.getError(),
      setParam: store.setParam, setParams: store.setParams, apply: store.apply, refresh: store.refresh, getOptions: store.getOptions
    };
  };

  window.useParamOptions = function(name) {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      return window.__paramStore.sub(function() { forceUpdate(function(c) { return c + 1; }); });
    }, []);
    return window.__paramStore.getOptions(name);
  };

  try { window.__paramStore._ingest(window.ARTIFACT_DATA || {}); } catch (e) {}

  window.addEventListener('message', function(e) {
    if (e.source !== window.parent || !e.data) return;
    if (e.data.type === 'ARTIFACT_DATA' && e.data.payload) {
      window.__setArtifactData(e.data.payload);
    } else if (e.data.type === 'ARTIFACT_PARAMS_STATUS') {
      window.__paramStore._status(e.data.payload || {});
    }
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // InfoPopover — provenance popup (Data / Code / Calculation) for components
  // ═══════════════════════════════════════════════════════════════════════════

  function _infoFilterVal(v) {
    if (v == null) return '';
    if (Array.isArray(v)) return v.join(', ');
    if (typeof v === 'object') {
      if (v.from != null || v.to != null) return (v.from || '…') + ' → ' + (v.to || '…');
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return String(v);
  }
  function _infoCell(v) {
    if (v == null) return '—';
    if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
    if (typeof v === 'object') { try { return JSON.stringify(v); } catch (e) { return String(v); } }
    return String(v);
  }
  function _infoCols(viz, rows) {
    var cols = viz.columns || [];
    if (cols.length) {
      return cols.map(function(c) {
        if (typeof c === 'string') return { field: c, header: c };
        return { field: c.field || c.headerName || c.name, header: c.headerName || c.field || c.name, dtype: c.dtype };
      }).filter(function(c) { return c.field; });
    }
    var src = (rows && rows.length) ? rows : (viz.rows || []);
    var r = src[0];
    if (r && typeof r === 'object') return Object.keys(r).map(function(k) { return { field: k, header: k }; });
    return [];
  }
  function _infoMeta(viz) {
    var dm = viz.dataModel || {};
    var view = viz.view || {};
    var innerView = view.view || view;
    var type = dm.type || innerView.type;
    var rowCount = Array.isArray(viz.rows) ? viz.rows.length : (viz.row_count != null ? viz.row_count : null);
    return { source: viz.dataSource || null, type: type ? String(type).replace(/_/g, ' ') : null, rowCount: rowCount, aggregation: innerView.aggregation || null };
  }
  function _infoCalc(calc) {
    if (!calc) return null;
    if (typeof calc === 'string') return calc.trim() || null;
    if (typeof calc === 'object') {
      var agg = calc.agg || calc.fn || calc.aggregation;
      var expr = calc.expr || calc.expression || calc.field || calc.value;
      var s = '';
      if (agg && expr) s = String(agg).toUpperCase() + '(' + expr + ')';
      else if (expr) s = String(expr);
      else if (agg) s = String(agg).toUpperCase();
      var gb = calc.groupBy || calc.group_by;
      if (gb) s += ', grouped by ' + gb;
      if (calc.filter) s += ', where ' + calc.filter;
      return s || null;
    }
    return null;
  }
  function buildInfoRows(viz) {
    if (!viz || typeof viz !== 'object') return [];
    var rows = [];
    var dm = viz.dataModel || {};
    var view = viz.view || {};
    var innerView = view.view || view;
    var type = dm.type || innerView.type;
    if (viz.dataSource) rows.push({ label: 'Source', value: String(viz.dataSource) });
    if (type) rows.push({ label: 'Type', value: String(type).replace(/_/g, ' ') });
    var rowCount = Array.isArray(viz.rows) ? viz.rows.length : (viz.row_count != null ? viz.row_count : null);
    if (rowCount != null) rows.push({ label: 'Rows', value: String(rowCount) });
    var cols = viz.columns || [];
    if (cols.length) {
      rows.push({ label: 'Columns (' + cols.length + ')', value: cols.map(function(c) {
        if (typeof c === 'string') return c;
        return (c.headerName || c.field || c.name || '') + (c.dtype ? '  · ' + c.dtype : '');
      }).join('\n'), pre: true });
    }
    if (innerView.aggregation) rows.push({ label: 'Aggregation', value: String(innerView.aggregation) });
    if (viz.description) rows.push({ label: 'Description', value: String(viz.description) });
    if (viz.code) rows.push({ label: 'Query', value: String(viz.code), code: true });
    if (viz.id) rows.push({ label: 'ID', value: String(viz.id), mono: true });
    return rows;
  }
  window.buildInfoRows = buildInfoRows;

  var POP = {
    panel: 'bg-surface border border-line-2 text-ink rounded-control shadow-lift',
    label: 'text-[10px] font-medium uppercase tracking-eyebrow text-ink-3 mb-1',
    code: 'text-[11px] font-mono text-ink-2 bg-surface-2 border border-line rounded-chip',
    muted: 'text-[11px] text-ink-3',
    th: 'text-start font-medium text-ink-3 bg-surface-2 border-b border-line px-2 py-1.5 whitespace-nowrap sticky top-0',
    td: 'px-2 py-1 text-ink-2 border-b border-line/60 whitespace-nowrap',
    tabOn: 'border-ink text-ink', tabOff: 'border-transparent text-ink-3 hover:text-ink-2',
    btnOn: 'text-ink-2 bg-surface-2', btnOff: 'text-ink-3/70 hover:text-ink-2 hover:bg-surface-2'
  };

  function _dataTabBody(viz, opts) {
    opts = opts || {};
    var meta = _infoMeta(viz);
    var rawRows = Array.isArray(viz.rows) ? viz.rows : [];
    var overrideRows = Array.isArray(opts.rows) ? opts.rows : null;
    var dataRows = overrideRows != null ? overrideRows : rawRows;
    var cols = _infoCols(viz, dataRows);
    var rawCount = rawRows.length || (viz.row_count != null ? viz.row_count : 0);
    var isFiltered = overrideRows != null && rawCount > 0 && overrideRows.length !== rawCount;
    var MAXR = 100;
    var activeFilters = {};
    try { activeFilters = (window.__filterStore ? window.__filterStore.get() : {}) || {}; } catch (e) {}
    var colFields = cols.map(function(c) { return c.field; });
    var shownFilterKeys = Object.keys(activeFilters).filter(function(k) { return colFields.indexOf(k) !== -1; });
    var metaBits = [];
    if (meta.source) metaBits.push(meta.source);
    if (meta.type) metaBits.push(meta.type);
    if (isFiltered) metaBits.push(dataRows.length + ' of ' + rawCount + ' rows (filtered)');
    else metaBits.push((overrideRows != null ? dataRows.length : (meta.rowCount != null ? meta.rowCount : dataRows.length)) + ' rows');
    if (cols.length) metaBits.push(cols.length + ' cols');
    if (meta.aggregation) metaBits.push('agg: ' + meta.aggregation);
    var filterNote = shownFilterKeys.length
      ? 'Filters: ' + shownFilterKeys.map(function(k) { return k + '=' + _infoFilterVal(activeFilters[k]); }).join(', ')
      : (isFiltered ? 'Filtered view' : null);
    var calcText = _infoCalc(opts.calc);
    var exportBtn = (dataRows.length && cols.length) ? h('button', {
      key: 'dl', type: 'button', 'data-testid': 'bow-popover-export', title: 'Download CSV' + (isFiltered ? ' (filtered rows)' : ''),
      onClick: function(e) { e.stopPropagation(); window.exportCSV(dataRows, { columns: cols.map(function(c) { return c.field; }), filename: (viz.title || 'export') }); },
      className: 'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-chip text-[11px] font-medium text-ink-3 hover:text-ink hover:bg-surface-2 transition-colors shrink-0'
    }, [h(window.Icon, { key: 'i', name: 'download', size: 11 }), h('span', { key: 't' }, 'CSV')]) : null;

    return h('div', { key: 'data', style: { display: 'flex', flexDirection: 'column', gap: 8 } }, [
      calcText ? h('div', { key: 'calc' }, [
        h('div', { key: 'l', className: POP.label }, 'Calculation'),
        h('div', { key: 'v', className: POP.code + ' px-2 py-1.5' }, calcText)
      ]) : null,
      (metaBits.length || exportBtn) ? h('div', { key: 'm', className: 'flex items-center justify-between gap-2' }, [
        h('span', { key: 'mb', className: POP.muted }, metaBits.join('  ·  ')), exportBtn
      ]) : null,
      filterNote ? h('div', { key: 'af', className: 'text-[11px] text-ink-2' }, filterNote) : null,
      cols.length ? h('div', { key: 'tbl', className: 'border border-line rounded-chip overflow-auto', style: { maxHeight: 300 } },
        h('table', { className: 'border-collapse tabular', style: { minWidth: '100%' } }, [
          h('thead', { key: 'h' }, h('tr', {}, cols.map(function(c, i) { return h('th', { key: i, className: POP.th, style: { fontSize: 11 } }, c.header); }))),
          h('tbody', { key: 'b' }, dataRows.slice(0, MAXR).map(function(row, ri) {
            return h('tr', { key: ri, className: ri % 2 ? 'bg-surface-2/50' : '' }, cols.map(function(c, ci) {
              var cell = _infoCell(row[c.field]);
              return h('td', { key: ci, title: cell, className: POP.td, style: { fontSize: 11, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' } }, cell);
            }));
          }))
        ])) : h('div', { key: 'nd', className: 'text-xs text-ink-3 py-4 text-center' }, 'No data available'),
      (dataRows.length > MAXR) ? h('div', { key: 'more', className: 'text-[10px] text-ink-3' }, 'Showing ' + MAXR + ' of ' + dataRows.length + ' rows') : null
    ]);
  }

  function _codeTabBody(viz) {
    return h('div', { key: 'code' }, viz.code
      ? h('pre', { className: POP.code + ' p-2 overflow-auto', style: { maxHeight: 340, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 } }, viz.code)
      : h('div', { className: 'text-xs text-ink-3 py-4 text-center' }, 'No query available for this visualization.'));
  }

  function _infoGlyph(size) {
    return h('svg', { width: size || 15, height: size || 15, viewBox: '0 0 16 16', fill: 'none' }, [
      h('circle', { key: 'c', cx: 8, cy: 8, r: 6.4, stroke: 'currentColor', strokeWidth: 1.2 }),
      h('circle', { key: 'd', cx: 8, cy: 5.2, r: 0.95, fill: 'currentColor' }),
      h('path', { key: 'b', d: 'M8 7.4v4', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round' })
    ]);
  }

  function _popoverPanel(viz, tab, setTab, onClose, body, style, extraProps) {
    function tabButton(id, label) {
      var active = tab === id;
      return h('button', { key: id, type: 'button', onClick: function() { setTab(id); },
        className: 'px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors ' + (active ? POP.tabOn : POP.tabOff) }, label);
    }
    var props = Object.assign({ className: POP.panel, style: style }, extraProps || {});
    return h('div', props, [
      h('div', { key: 'hd', className: 'flex items-start justify-between gap-2 px-3.5 pt-2.5 pb-1' }, [
        h('div', { key: 't', className: 'text-xs font-semibold text-ink leading-snug' }, viz.title || 'Details'),
        h('button', { key: 'x', type: 'button', 'aria-label': 'Close', onClick: onClose, className: 'shrink-0 -mt-0.5 text-ink-3 hover:text-ink' },
          h('svg', { width: 14, height: 14, viewBox: '0 0 14 14', fill: 'none' }, h('path', { d: 'M3.5 3.5l7 7M10.5 3.5l-7 7', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' })))
      ]),
      h('div', { key: 'tabs', className: 'flex gap-1 px-3 border-b border-line' }, [tabButton('data', 'Data'), tabButton('code', 'Code')]),
      h('div', { key: 'bd', className: 'px-3.5 py-3 overflow-auto' }, body),
      viz.id ? h('div', { key: 'ft', className: 'px-3.5 py-2 border-t border-line text-[10px] font-mono text-ink-3 break-all' }, 'ID  ' + viz.id) : null
    ]);
  }

  window.InfoPopover = function(props) {
    var viz = props.viz;
    var _o = React.useState(false), open = _o[0], setOpen = _o[1];
    var _p = React.useState(null), pos = _p[0], setPos = _p[1];
    var _t = React.useState('data'), tab = _t[0], setTab = _t[1];
    var btnRef = React.useRef(null);
    var panelRef = React.useRef(null);

    React.useEffect(function() {
      if (!open) return;
      function onDown(e) {
        if (btnRef.current && btnRef.current.contains(e.target)) return;
        if (panelRef.current && panelRef.current.contains(e.target)) return;
        setOpen(false);
      }
      function onKey(e) { if (e.key === 'Escape') setOpen(false); }
      document.addEventListener('mousedown', onDown);
      document.addEventListener('keydown', onKey);
      return function() { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
    }, [open]);

    React.useEffect(function() {
      if (!open || !btnRef.current) return;
      function reposition() {
        if (!btnRef.current) return;
        var r = btnRef.current.getBoundingClientRect();
        var W = Math.min(400, window.innerWidth - 16);
        var left = Math.min(r.right - W, window.innerWidth - W - 8);
        if (left < 8) left = 8;
        var spaceBelow = window.innerHeight - r.bottom;
        var below = spaceBelow > 240;
        setPos({ left: left, top: below ? r.bottom + 6 : null, bottom: below ? null : (window.innerHeight - r.top + 6), width: W });
      }
      reposition();
      window.addEventListener('scroll', reposition, true);
      window.addEventListener('resize', reposition);
      return function() { window.removeEventListener('scroll', reposition, true); window.removeEventListener('resize', reposition); };
    }, [open]);

    if (!viz) return null;
    var body = tab === 'code' ? _codeTabBody(viz) : _dataTabBody(viz, { rows: props.rows, calc: props.calc });
    var panel = (open && pos) ? ReactDOM.createPortal(
      _popoverPanel(viz, tab, setTab, function() { setOpen(false); }, body, {
        position: 'fixed', left: pos.left, top: pos.top != null ? pos.top : undefined, bottom: pos.bottom != null ? pos.bottom : undefined,
        width: pos.width, zIndex: 99999, maxHeight: '72vh', display: 'flex', flexDirection: 'column'
      }, { ref: panelRef }),
      document.body) : null;

    return h('span', { className: 'inline-flex align-middle' }, [
      h('button', {
        key: 'btn', ref: btnRef, type: 'button', 'aria-label': 'Details',
        onClick: function(e) { e.stopPropagation(); setOpen(function(o) { return !o; }); },
        className: 'inline-flex items-center justify-center w-5 h-5 rounded-full transition-colors ' + (open ? POP.btnOn : POP.btnOff)
      }, _infoGlyph(15)),
      panel
    ]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Design-system primitives
  // ═══════════════════════════════════════════════════════════════════════════

  function _join() {
    var out = [];
    for (var i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]);
    return out.join(' ');
  }

  // Class merging (v11): `className` is ADDITIVE for layout (col-span, h-full,
  // mt-6, …) but a class of the same *kind* as a default REPLACES that default
  // — bg-*, text color, border color, rounded-*, shadow-*, padding. So
  // className="lg:col-span-2" keeps the card look, className="bg-ink text-bg"
  // restyles it without a bg-surface/bg-ink collision, and className="p-0"
  // removes the padding. Variant-prefixed classes (hover:, md:) never evict a
  // base default.
  var _CLASS_KINDS = [
    { name: 'bg', re: /^bg-/ },
    { name: 'text-color', re: /^text-(?!(?:xs|sm|base|lg|[2-9]?xl|\[\d|start|end|left|right|center|justify|wrap|nowrap|balance|pretty|ellipsis|clip)\b)/ },
    { name: 'border-color', re: /^border-(?!(?:\d|t\b|b\b|l\b|r\b|s\b|e\b|x\b|y\b|t-|b-|l-|r-|s-|e-|x-|y-|solid|dashed|dotted|none|collapse|separate|spacing)).+/ },
    { name: 'rounded', re: /^rounded/ },
    { name: 'shadow', re: /^shadow/ },
    { name: 'padding', re: /^p[xysetblr]?-/ }
  ];
  function _kindOf(cls) {
    if (cls.indexOf(':') !== -1) return null;
    for (var i = 0; i < _CLASS_KINDS.length; i++) if (_CLASS_KINDS[i].re.test(cls)) return _CLASS_KINDS[i].name;
    return null;
  }
  function _mergeClasses(defaults, className) {
    if (className == null || className === '') return defaults;
    var extra = String(className).split(/\s+/).filter(Boolean);
    var evict = {};
    for (var i = 0; i < extra.length; i++) { var k = _kindOf(extra[i]); if (k) evict[k] = true; }
    var kept = String(defaults || '').split(/\s+/).filter(function(c) { return c && !evict[_kindOf(c)]; });
    return kept.concat(extra).join(' ');
  }
  window.bowMergeClasses = _mergeClasses;

  // Visual class sets per variant. `className` REPLACES these (v11); layout
  // classes (position, padding, overflow) are always kept.
  var CARD_VARIANTS = {
    card: 'bg-surface border border-line text-ink rounded-card shadow-card',
    plain: 'bg-transparent text-ink',
    inset: 'bg-surface-2 text-ink rounded-card',
    lift: 'bg-surface border border-line text-ink rounded-card shadow-lift',
    accent: 'bg-accent text-accent-ink rounded-card',
    inverse: 'bg-ink text-bg rounded-card',
    outline: 'bg-transparent border border-line-2 text-ink rounded-card'
  };
  var PAD = { none: '', sm: 'p-4', md: 'p-5', lg: 'p-6 md:p-7' };
  var LEGACY_KPI = 'relative rounded-2xl border p-5 shadow-sm overflow-hidden bg-white border-slate-200 text-slate-900 dark:bg-slate-900 dark:border-slate-700 dark:text-slate-100';
  var LEGACY_SECTION = 'relative rounded-2xl border shadow-sm p-6 bg-white border-slate-200 dark:bg-slate-900 dark:border-slate-700';

  // Eyebrow — small uppercase label above a heading or number.
  window.Eyebrow = function(props) {
    props = props || {};
    return h(props.as || 'div', { className: _mergeClasses('text-[11px] font-medium uppercase tracking-eyebrow text-ink-3', props.className), style: props.style }, props.children);
  };

  // Badge — semantic pill. tone: neutral | accent | positive | warning | negative | inverse
  var BADGE = {
    neutral: 'bg-surface-2 text-ink-2', accent: 'bg-accent/12 text-accent', positive: 'bg-positive/12 text-positive',
    warning: 'bg-warning/15 text-warning', negative: 'bg-negative/12 text-negative', inverse: 'bg-ink text-bg', outline: 'border border-line-2 text-ink-2'
  };
  window.Badge = function(props) {
    props = props || {};
    var cls = _mergeClasses(_join('inline-flex items-center gap-1 rounded-chip px-2 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap', BADGE[props.tone] || BADGE.neutral), props.className);
    return h('span', { className: cls, style: props.style, title: props.title }, [
      props.icon ? h(window.Icon, { key: 'i', name: props.icon, size: 11 }) : null,
      props.dot ? h('span', { key: 'd', className: 'w-1.5 h-1.5 rounded-full bg-current' }) : null,
      h('span', { key: 'c' }, props.children)
    ]);
  };

  // Delta — change indicator. value: number. pct: value is a ratio (0.12 = +12%) or,
  // with unit='pp', percentage points. invert: lower is better.
  window.Delta = function(props) {
    props = props || {};
    var v = typeof props.value === 'number' ? props.value : null;
    if (v == null) return h('span', { className: 'text-xs text-ink-3' }, props.fallback || '—');
    var up = v > 0, flat = Math.abs(v) < 1e-9;
    var good = flat ? null : (props.invert ? !up : up);
    var tone = flat ? 'text-ink-3' : (good ? 'text-positive' : 'text-negative');
    var txt;
    if (props.pct || props.ratio) txt = (up ? '+' : '') + (v * 100).toFixed(props.decimals != null ? props.decimals : 1) + '%';
    else if (props.unit === 'pp') txt = (up ? '+' : '') + v.toFixed(props.decimals != null ? props.decimals : 1) + ' pp';
    else txt = (up ? '+' : '') + window.fmt(v, { currency: props.currency, decimals: props.decimals });
    var icon = flat ? 'minus' : (up ? 'arrow-up-right' : 'arrow-down-right');
    var cls = _mergeClasses(_join('inline-flex items-center gap-0.5 text-xs font-medium tabular-nums', tone, props.chip ? 'rounded-chip px-1.5 py-0.5 ' + (flat ? 'bg-surface-2' : (good ? 'bg-positive/12' : 'bg-negative/12')) : ''), props.className);
    return h('span', { className: cls, style: props.style }, [
      h(window.Icon, { key: 'i', name: icon, size: 12, strokeWidth: 2.5 }),
      h('span', { key: 't' }, txt),
      props.label ? h('span', { key: 'l', className: 'font-normal text-ink-3 ms-1' }, props.label) : null
    ]);
  };

  // Sparkline — compact inline trend. data: number[]; area: fill under the line.
  window.Sparkline = function(props) {
    props = props || {};
    var data = (props.data || []).map(Number).filter(function(x) { return !isNaN(x); });
    var W = props.width || 120, H = props.height || 32, pad = 2;
    if (data.length < 2) return h('svg', { width: props.width ? W : '100%', height: H, viewBox: '0 0 ' + W + ' ' + H, className: props.className });
    var min = Math.min.apply(null, data), max = Math.max.apply(null, data);
    var span = (max - min) || 1;
    var pts = data.map(function(v, i) {
      var x = pad + (i / (data.length - 1)) * (W - pad * 2);
      var y = pad + (1 - (v - min) / span) * (H - pad * 2);
      return [x, y];
    });
    var d = pts.map(function(p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
    var area = d + ' L' + pts[pts.length - 1][0].toFixed(1) + ' ' + (H - pad) + ' L' + pts[0][0].toFixed(1) + ' ' + (H - pad) + ' Z';
    var color = props.color || 'currentColor';
    var last = pts[pts.length - 1];
    var gid = 'bow-sp-' + Math.floor(Math.random() * 1e9);
    return h('svg', { width: props.width ? W : '100%', height: H, viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none', className: props.className, style: Object.assign({ display: 'block', overflow: 'visible' }, props.style || {}) }, [
      props.area !== false ? h('defs', { key: 'g' }, h('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, [
        h('stop', { key: 'a', offset: '0%', stopColor: color, stopOpacity: 0.28 }),
        h('stop', { key: 'b', offset: '100%', stopColor: color, stopOpacity: 0.02 })
      ])) : null,
      props.area !== false ? h('path', { key: 'area', d: area, fill: 'url(#' + gid + ')', stroke: 'none' }) : null,
      h('path', { key: 'line', d: d, fill: 'none', stroke: color, strokeWidth: props.strokeWidth || 1.75, strokeLinejoin: 'round', strokeLinecap: 'round', vectorEffect: 'non-scaling-stroke' }),
      props.endpoint !== false ? h('circle', { key: 'end', cx: last[0], cy: last[1], r: 2.25, fill: color }) : null
    ]);
  };

  // ProgressBar — value 0..1 (or value/max). tone: accent | positive | warning | negative | ink
  window.ProgressBar = function(props) {
    props = props || {};
    var max = props.max || 1;
    var ratio = Math.max(0, Math.min(1, (Number(props.value) || 0) / max));
    var tone = { accent: 'bg-accent', accent2: 'bg-accent-2', positive: 'bg-positive', warning: 'bg-warning', negative: 'bg-negative', ink: 'bg-ink' }[props.tone || 'accent'] || 'bg-accent';
    return h('div', { className: _join('w-full overflow-hidden rounded-full bg-surface-2', props.className), style: Object.assign({ height: props.height || 6 }, props.style || {}), role: 'progressbar', 'aria-valuenow': Math.round(ratio * 100), 'aria-valuemin': 0, 'aria-valuemax': 100 },
      h('div', { className: _join('h-full rounded-full transition-[width] duration-500', tone), style: { width: (ratio * 100).toFixed(1) + '%', backgroundColor: props.color } }));
  };

  // Segmented — a compact single-choice control (view switchers, ranges).
  window.Segmented = function(props) {
    props = props || {};
    var opts = (props.options || []).map(function(o) { return typeof o === 'object' && o !== null ? o : { value: o, label: String(o) }; });
    var value = props.value;
    return h('div', { role: 'radiogroup', className: _join('inline-flex items-center gap-0.5 rounded-control bg-surface-2 p-0.5', props.className), style: props.style }, opts.map(function(o) {
      var on = String(o.value) === String(value);
      return h('button', {
        key: String(o.value), type: 'button', role: 'radio', 'aria-checked': on,
        onClick: function() { if (props.onChange) props.onChange(o.value); },
        className: 'px-2.5 py-1 text-xs font-medium rounded-chip transition-colors ' + (on ? 'bg-surface text-ink shadow-card' : 'text-ink-3 hover:text-ink-2')
      }, [o.icon ? h(window.Icon, { key: 'i', name: o.icon, size: 12, className: 'inline me-1 -mt-px' }) : null, o.label]);
    }));
  };

  // Divider — thin rule with an optional centered label.
  window.Divider = function(props) {
    props = props || {};
    if (!props.children) return h('hr', { className: _join('border-0 border-t border-line', props.className), style: props.style });
    return h('div', { className: _join('flex items-center gap-3 text-[11px] uppercase tracking-eyebrow text-ink-3', props.className), style: props.style }, [
      h('span', { key: 'a', className: 'flex-1 border-t border-line' }), h('span', { key: 'b' }, props.children), h('span', { key: 'c', className: 'flex-1 border-t border-line' })
    ]);
  };

  // PageHeader — the page's opening: eyebrow, display title, subtitle, actions.
  window.PageHeader = function(props) {
    props = props || {};
    var big = props.size === 'lg';
    return h('header', { className: _join('flex flex-wrap items-end justify-between gap-x-6 gap-y-3', props.className), style: props.style }, [
      h('div', { key: 'l', className: 'min-w-0' }, [
        props.eyebrow ? h(window.Eyebrow, { key: 'e', className: 'text-[11px] font-medium uppercase tracking-eyebrow text-accent mb-2' }, props.eyebrow) : null,
        h('h1', { key: 't', className: _mergeClasses(big ? 'text-4xl md:text-5xl leading-[1.05] font-display text-ink' : 'text-2xl md:text-3xl leading-tight font-display text-ink', props.titleClassName) }, props.title),
        props.subtitle ? h('p', { key: 's', className: _mergeClasses('mt-2 text-sm md:text-[15px] text-ink-2 max-w-2xl', props.subtitleClassName) }, props.subtitle) : null
      ]),
      props.actions ? h('div', { key: 'r', className: 'flex flex-wrap items-center gap-2' }, props.actions) : null
    ]);
  };

  // KPICard — the headline number.
  //   title/label, value, subtitle/hint, delta (number), deltaPct, deltaLabel, invertDelta,
  //   spark (number[]), icon, viz/rows/calc, variant, size ('md'|'lg'), align, className/style.
  window.KPICard = function(props) {
    props = props || {};
    if (LEGACY) {
      var color = props.color || '#3B82F6';
      var lcls = LEGACY_KPI + (props.className ? ' ' + props.className : '');
      var ltitle = 'text-xs font-medium uppercase tracking-wider mb-1 text-slate-500 dark:text-slate-400' + (props.titleClassName ? ' ' + props.titleClassName : '');
      var lsub = 'text-sm mt-1 text-slate-500 dark:text-slate-400' + (props.subtitleClassName ? ' ' + props.subtitleClassName : '');
      return h('div', { className: lcls, style: props.style }, [
        h('div', { key: 'bar', className: 'absolute inset-x-0 top-0 h-1', style: { background: 'linear-gradient(90deg, ' + color + ', ' + color + '99)' } }),
        props.viz ? h('div', { key: 'info', className: 'absolute top-2.5 right-2.5 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null,
        h('p', { key: 't', className: ltitle }, props.title),
        h('p', { key: 'v', className: 'text-2xl font-semibold' }, props.value),
        props.subtitle ? h('p', { key: 's', className: lsub }, props.subtitle) : null
      ]);
    }
    var variant = props.variant || 'card';
    var onAccent = variant === 'accent' || variant === 'inverse';
    var pad = PAD[props.padding || 'md'];
    var cls = _mergeClasses(_join('relative overflow-hidden', pad, CARD_VARIANTS[variant] || CARD_VARIANTS.card), props.className);
    var big = props.size === 'lg';
    var muted = onAccent ? 'opacity-75' : 'text-ink-3';
    var titleCls = _mergeClasses(_join('text-[11px] font-medium uppercase tracking-eyebrow', muted), props.titleClassName);
    var valueCls = _mergeClasses(_join('font-numeric font-semibold tracking-tight tabular-nums leading-none', big ? 'text-4xl md:text-5xl mt-3' : 'text-[28px] mt-2'), props.valueClassName);
    var subCls = _mergeClasses(_join('text-xs mt-2', onAccent ? 'opacity-75' : 'text-ink-3'), props.subtitleClassName);
    var hasDelta = typeof props.delta === 'number';
    var sparkColor = onAccent ? 'currentColor' : (props.sparkColor || 'var(--bow-accent)');
    return h('div', { className: cls, style: props.style, 'data-bow-kpi': '1' }, [
      props.viz ? h('div', { key: 'info', className: 'absolute top-2.5 end-2.5 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null,
      h('div', { key: 'head', className: 'flex items-center gap-2 pe-6' }, [
        props.icon ? h('span', { key: 'ic', className: _join('inline-flex items-center justify-center w-6 h-6 rounded-chip', onAccent ? 'bg-white/15' : 'bg-accent/10 text-accent') }, h(window.Icon, { name: props.icon, size: 13 })) : null,
        h('p', { key: 't', className: titleCls }, props.title || props.label)
      ]),
      h('p', { key: 'v', className: valueCls }, props.value),
      (hasDelta || props.subtitle || props.hint) ? h('div', { key: 'foot', className: 'flex items-center gap-2 flex-wrap mt-2' }, [
        hasDelta ? h(window.Delta, { key: 'd', value: props.delta, pct: props.deltaPct !== false && props.deltaPct != null ? props.deltaPct : (Math.abs(props.delta) < 5 && props.deltaPct !== false), invert: props.invertDelta, label: props.deltaLabel, chip: !onAccent, className: onAccent ? 'text-current' : undefined }) : null,
        (props.subtitle || props.hint) ? h('span', { key: 's', className: subCls.replace(' mt-2', '') }, props.subtitle || props.hint) : null
      ]) : null,
      (props.spark && props.spark.length > 1) ? h('div', { key: 'sp', className: 'mt-3 -mb-1', style: { color: sparkColor } }, h(window.Sparkline, { data: props.spark, height: props.sparkHeight || 34, color: sparkColor })) : null
    ]);
  };

  // SectionCard — container for a chart/table/section.
  //   title, subtitle, eyebrow, actions, viz/rows/calc, variant, padding, className/style.
  window.SectionCard = function(props) {
    props = props || {};
    if (LEGACY) {
      var lcls = LEGACY_SECTION + (props.className ? ' ' + props.className : '');
      var lt = 'text-lg font-semibold text-slate-800 dark:text-slate-100' + (props.titleClassName ? ' ' + props.titleClassName : '');
      var ls = 'text-sm mt-1 text-slate-500 dark:text-slate-400' + (props.subtitleClassName ? ' ' + props.subtitleClassName : '');
      return h('div', { className: lcls, style: props.style }, [
        props.viz ? h('div', { key: 'info', className: 'absolute top-3 right-3 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null,
        props.title ? h('div', { key: 'hdr', className: 'mb-4 pr-6' }, [h('h2', { key: 't', className: lt }, props.title), props.subtitle ? h('p', { key: 's', className: ls }, props.subtitle) : null]) : null,
        h('div', { key: 'body' }, props.children)
      ]);
    }
    var variant = props.variant || 'card';
    var onAccent = variant === 'accent' || variant === 'inverse';
    var pad = PAD[props.padding || 'md'];
    var cls = _mergeClasses(_join('relative', pad, CARD_VARIANTS[variant] || CARD_VARIANTS.card), props.className);
    var titleCls = _mergeClasses('text-[15px] font-semibold leading-snug text-ink', props.titleClassName);
    var subCls = _mergeClasses(_join('text-xs mt-0.5', onAccent ? 'opacity-75' : 'text-ink-3'), props.subtitleClassName);
    var hasHeader = props.title || props.eyebrow || props.actions;
    return h('section', { className: cls, style: props.style }, [
      props.viz ? h('div', { key: 'info', className: 'absolute top-3 end-3 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null,
      hasHeader ? h('div', { key: 'hdr', className: _join('flex items-start justify-between gap-3', props.viz ? 'pe-6' : '', props.headerClassName || 'mb-4') }, [
        h('div', { key: 'l', className: 'min-w-0' }, [
          props.eyebrow ? h(window.Eyebrow, { key: 'e', className: 'text-[11px] font-medium uppercase tracking-eyebrow text-accent mb-1' }, props.eyebrow) : null,
          props.title ? h('h3', { key: 't', className: titleCls }, props.title) : null,
          props.subtitle ? h('p', { key: 's', className: subCls }, props.subtitle) : null
        ]),
        props.actions ? h('div', { key: 'a', className: 'flex items-center gap-2 shrink-0' }, props.actions) : null
      ]) : null,
      h('div', { key: 'body', className: props.bodyClassName }, props.children)
    ]);
  };

  // EmptyState — for zero rows after filtering.
  window.EmptyState = function(props) {
    props = props || {};
    return h('div', { className: _join('flex flex-col items-center justify-center text-center gap-2 py-10 text-ink-3', props.className), style: props.style }, [
      h(window.Icon, { key: 'i', name: props.icon || 'inbox', size: 22, className: 'opacity-60' }),
      h('p', { key: 't', className: 'text-sm' }, props.children || props.title || 'No data matches the current filters'),
      props.action ? h('div', { key: 'a', className: 'mt-1' }, props.action) : null
    ]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // DataTable — sortable, paginated, RTL-aware table (token-themed)
  // ═══════════════════════════════════════════════════════════════════════════

  var _RTL_CHARS = /[֐-׿؀-ۿ܀-ݏיִ-ﭏ]/g;
  function _dtDetectDir(cols, rows) {
    var sample = '';
    for (var i = 0; i < cols.length; i++) sample += (cols[i].header || '') + ' ';
    for (var r = 0; r < rows.length && r < 5; r++) {
      var row = rows[r] || {};
      for (var c = 0; c < cols.length; c++) { var v = row[cols[c].field]; if (typeof v === 'string') sample += v + ' '; }
    }
    var rtl = (sample.match(_RTL_CHARS) || []).length;
    var ltr = (sample.match(/[A-Za-z]/g) || []).length;
    return rtl > ltr ? 'rtl' : 'ltr';
  }
  function _dtColIsNumeric(col, rows) {
    if (col.dtype && /int|float|double|decimal|number/i.test(String(col.dtype))) return true;
    var seen = 0;
    for (var i = 0; i < rows.length && seen < 5; i++) {
      var v = rows[i] ? rows[i][col.field] : null;
      if (v == null) continue;
      seen++;
      if (typeof v !== 'number') return false;
    }
    return seen > 0;
  }
  function _dtCompare(a, b) {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    var na = typeof a === 'number' ? a : (a !== '' && !isNaN(Number(a)) ? Number(a) : null);
    var nb = typeof b === 'number' ? b : (b !== '' && !isNaN(Number(b)) ? Number(b) : null);
    if (na != null && nb != null) return na - nb;
    return String(a).localeCompare(String(b));
  }

  var DT = LEGACY ? {
    thead: 'text-xs uppercase bg-slate-50 dark:bg-slate-800 sticky top-0 z-[1]',
    th: 'px-4 py-3 font-medium text-slate-500 dark:text-slate-400 whitespace-nowrap select-none',
    thHover: ' cursor-pointer hover:text-slate-800 dark:hover:text-slate-200',
    sortOn: 'text-slate-700 dark:text-slate-200', sortOff: 'text-slate-300 dark:text-slate-600',
    tr: 'border-b border-slate-100 dark:border-slate-800 transition-colors ',
    trSel: 'bg-blue-50 hover:bg-blue-50 dark:bg-blue-950/40 dark:hover:bg-blue-950/40 ',
    trZebra: 'bg-slate-50/40 dark:bg-slate-800/40 ', trHover: 'hover:bg-slate-100 dark:hover:bg-slate-800 ',
    td: 'px-4 py-2 text-slate-700 dark:text-slate-200 ',
    input: 'rounded-lg border border-slate-200 text-slate-700 bg-white dark:border-slate-700 dark:text-slate-200 dark:bg-slate-900 dark:placeholder-slate-500 px-3 py-1.5 text-sm outline-none focus:border-blue-400 min-w-[160px]',
    btn: 'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-500 border border-slate-200 hover:text-slate-800 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-400 dark:hover:text-slate-200 dark:hover:bg-slate-800 transition-colors shrink-0',
    foot: 'flex items-center justify-between gap-2 pt-2 text-xs text-slate-500 dark:text-slate-400',
    nav: 'px-2 py-1 rounded-md border border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800 disabled:opacity-40 disabled:cursor-default',
    empty: 'text-sm text-slate-400 py-8 text-center', table: 'w-full text-sm'
  } : {
    thead: 'text-[11px] uppercase tracking-eyebrow bg-surface-2/70 sticky top-0 z-[1] backdrop-blur',
    th: 'px-3 py-2.5 font-medium text-ink-3 whitespace-nowrap select-none border-b border-line',
    thHover: ' cursor-pointer hover:text-ink',
    sortOn: 'text-ink', sortOff: 'text-ink-3/50',
    tr: 'border-b border-line/70 transition-colors ',
    trSel: 'bg-accent/8 hover:bg-accent/8 ',
    trZebra: 'bg-surface-2/40 ', trHover: 'hover:bg-surface-2/70 ',
    td: 'px-3 py-2 text-ink-2 ',
    input: 'rounded-control border border-line bg-surface text-ink placeholder:text-ink-3 px-3 py-1.5 text-sm outline-none focus:border-accent min-w-[160px]',
    btn: 'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-control text-xs font-medium text-ink-2 border border-line hover:text-ink hover:bg-surface-2 transition-colors shrink-0',
    foot: 'flex items-center justify-between gap-2 pt-2 text-xs text-ink-3',
    nav: 'px-2 py-1 rounded-chip border border-line hover:bg-surface-2 disabled:opacity-40 disabled:cursor-default',
    empty: 'text-sm text-ink-3 py-8 text-center', table: 'w-full text-sm tabular'
  };

  window.DataTable = function(props) {
    var viz = props.viz || {};
    var baseRows = Array.isArray(props.rows) ? props.rows : (Array.isArray(viz.rows) ? viz.rows : []);
    var colSource = Array.isArray(props.columns) && props.columns.length ? { columns: props.columns } : viz;
    var cols = React.useMemo(function() {
      var c = _infoCols(colSource, baseRows);
      // Rows passed explicitly may be a derived shape (a grouped/ranked array)
      // whose keys differ from viz.columns — fall back to the row keys rather
      // than rendering a grid of dashes.
      if (Array.isArray(props.rows) && !Array.isArray(props.columns) && baseRows.length && baseRows[0] && typeof baseRows[0] === 'object') {
        var keys = Object.keys(baseRows[0]);
        var overlap = c.filter(function(col) { return keys.indexOf(col.field) !== -1; }).length;
        if (!overlap) c = keys.map(function(k) { return { field: k, header: k }; });
      }
      return c;
    }, [props.columns, viz.columns, baseRows]);
    var sortable = props.sortable !== false;
    var selectable = props.selectable !== false;
    var exportable = props.exportable !== false;
    var searchable = !!props.searchable;
    var pageSize = props.pageSize != null ? props.pageSize : 15;
    var printCap = props.printCap != null ? props.printCap : 1000;
    var maxHeight = props.maxHeight != null ? props.maxHeight : (pageSize > 0 ? 'none' : 400);
    var compact = props.density === 'compact';
    var _sort = React.useState(null), sort = _sort[0], setSort = _sort[1];
    var _pg = React.useState(0), page = _pg[0], setPage = _pg[1];
    var _q = React.useState(''), query = _q[0], setQuery = _q[1];
    var _sel = React.useState(null), selRow = _sel[0], setSelRow = _sel[1];
    var dir = props.dir || _dtDetectDir(cols, baseRows);
    var numericByField = React.useMemo(function() {
      var m = {};
      for (var i = 0; i < cols.length; i++) m[cols[i].field] = _dtColIsNumeric(cols[i], baseRows);
      return m;
    }, [cols, baseRows]);
    var filtered = React.useMemo(function() {
      if (!query) return baseRows;
      var q = query.toLowerCase();
      return baseRows.filter(function(row) {
        for (var i = 0; i < cols.length; i++) { var v = row ? row[cols[i].field] : null; if (v != null && String(v).toLowerCase().indexOf(q) !== -1) return true; }
        return false;
      });
    }, [baseRows, cols, query]);
    var sorted = React.useMemo(function() {
      if (!sort) return filtered;
      var out = filtered.slice();
      out.sort(function(ra, rb) {
        var a = ra ? ra[sort.field] : null, b = rb ? rb[sort.field] : null;
        if (a == null && b == null) return 0;
        if (a == null) return 1;
        if (b == null) return -1;
        var c = _dtCompare(a, b);
        return sort.desc ? -c : c;
      });
      return out;
    }, [filtered, sort]);
    var paged = pageSize > 0;
    var pageCount = paged ? Math.max(1, Math.ceil(sorted.length / pageSize)) : 1;
    var curPage = Math.min(page, pageCount - 1);
    var start = paged ? curPage * pageSize : 0;
    var end = paged ? Math.min(start + pageSize, sorted.length) : sorted.length;
    var domRows = sorted.slice(0, Math.max(end, Math.min(sorted.length, printCap)));
    function clickHeader(field) {
      if (!sortable) return;
      setPage(0);
      setSort(function(s) {
        if (!s || s.field !== field) return { field: field, desc: false };
        if (!s.desc) return { field: field, desc: true };
        return null;
      });
    }
    function clickRow(row, absIdx) {
      if (props.onRowClick) props.onRowClick(row, absIdx);
      if (selectable) setSelRow(function(cur) { return cur === row ? null : row; });
    }
    var isRtl = dir === 'rtl';
    var render = props.renderCell || null;
    var toolbar = null;
    if (searchable || exportable) {
      toolbar = h('div', { key: 'tb', className: 'bow-dt-chrome flex items-center justify-between gap-2 mb-2' }, [
        searchable ? h('input', { key: 's', type: 'text', value: query, 'data-testid': 'bow-dt-search', placeholder: props.searchPlaceholder || 'Search...',
          onChange: function(e) { setQuery(e.target.value); setPage(0); }, className: DT.input }) : h('span', { key: 's' }),
        exportable ? h('button', { key: 'x', type: 'button', 'data-testid': 'bow-dt-export', title: 'Download CSV',
          onClick: function() { window.exportCSV(sorted, { columns: cols.map(function(c) { return c.field; }), filename: (viz.title || 'table') }); },
          className: DT.btn }, [h(window.Icon, { key: 'i', name: 'download', size: 12 }), h('span', { key: 't' }, 'CSV')]) : null
      ]);
    }
    var thead = h('thead', { key: 'h', className: DT.thead }, h('tr', {}, cols.map(function(c, i) {
      var numeric = numericByField[c.field];
      var isSorted = sort && sort.field === c.field;
      return h('th', { key: i, onClick: function() { clickHeader(c.field); }, 'aria-sort': isSorted ? (sort.desc ? 'descending' : 'ascending') : undefined,
        className: DT.th + (compact ? ' py-1.5' : '') + ' ' + (numeric ? 'text-end' : 'text-start') + (sortable ? DT.thHover : '') },
        h('span', { className: 'inline-flex items-center gap-1' }, [
          h('span', { key: 't' }, c.header),
          sortable ? h('span', { key: 'a', className: 'text-[9px] leading-none ' + (isSorted ? DT.sortOn : DT.sortOff) }, isSorted ? (sort.desc ? '▼' : '▲') : '⇅') : null
        ]));
    })));
    var tbody = h('tbody', { key: 'b' }, domRows.map(function(row, i) {
      var onPage = i >= start && i < end;
      var isSel = selectable && selRow === row;
      var zebra = props.striped && ((i - start) % 2 === 1);
      return h('tr', { key: i, onClick: function() { clickRow(row, i); },
        className: (onPage ? '' : 'bow-dt-offpage hidden ') + DT.tr + (isSel ? DT.trSel : (zebra ? DT.trZebra : '') + DT.trHover) + ((selectable || props.onRowClick) ? 'cursor-pointer' : '') },
        cols.map(function(c, j) {
          var numeric = numericByField[c.field];
          var raw = row ? row[c.field] : null;
          var content = render ? render(raw, row, c) : null;
          if (content == null) content = (numeric && typeof raw === 'number' && props.format) ? props.format(raw, c) : _infoCell(raw);
          var short = raw == null || typeof raw === 'number' || String(raw).length <= 28;
          return h('td', { key: j, dir: 'auto', className: DT.td + (compact ? 'py-1 ' : '') + (numeric ? 'text-end tabular-nums' : 'text-start') + (short ? ' whitespace-nowrap' : '') + (j === 0 && props.emphasizeFirst !== false && !numeric ? ' font-medium text-ink' : '') }, content);
        }));
    }));
    var footer = null;
    var footBits = [];
    if (query) footBits.push(sorted.length + ' of ' + baseRows.length + ' rows match');
    if (sorted.length > printCap) footBits.push('first ' + printCap + ' rows in print/PDF');
    if (paged && sorted.length > pageSize) {
      footer = h('div', { key: 'f', className: 'bow-dt-chrome ' + DT.foot }, [
        h('span', { key: 'c', 'data-testid': 'bow-dt-range' }, (sorted.length ? (start + 1) : 0) + '–' + end + ' of ' + sorted.length + (footBits.length ? '  ·  ' + footBits.join('  ·  ') : '')),
        h('span', { key: 'nav', className: 'inline-flex items-center gap-1' }, [
          h('button', { key: 'p', type: 'button', 'data-testid': 'bow-dt-prev', disabled: curPage === 0, onClick: function() { setPage(Math.max(0, curPage - 1)); }, className: DT.nav }, isRtl ? '›' : '‹'),
          h('span', { key: 'pg', className: 'px-1 tabular-nums' }, (curPage + 1) + '/' + pageCount),
          h('button', { key: 'n', type: 'button', 'data-testid': 'bow-dt-next', disabled: curPage >= pageCount - 1, onClick: function() { setPage(Math.min(pageCount - 1, curPage + 1)); }, className: DT.nav }, isRtl ? '‹' : '›')
        ])
      ]);
    } else if (footBits.length) {
      footer = h('div', { key: 'f', className: 'bow-dt-chrome pt-2 text-xs text-ink-3', 'data-testid': 'bow-dt-range' }, footBits.join('  ·  '));
    }
    var infoBtn = (props.info && props.viz) ? h('div', { key: 'i', className: 'absolute top-0 z-10 ' + (isRtl ? 'left-0' : 'right-0') }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null;
    var empty = !cols.length || !baseRows.length;
    return h('div', { dir: dir, 'data-testid': 'bow-datatable', className: 'relative' + (props.className ? ' ' + props.className : ''), style: props.style },
      empty ? [infoBtn, h('div', { key: 'e', className: DT.empty }, props.emptyText || 'No data available')]
        : [infoBtn, toolbar, h('div', { key: 'scroll', className: 'bow-dt-scroll overflow-auto', style: { maxHeight: maxHeight } }, h('table', { className: DT.table }, [thead, tbody])), footer]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Filters — FilterSelect (portaled), FilterSearch, FilterDateRange
  // ═══════════════════════════════════════════════════════════════════════════

  var FILTER_THEME = LEGACY ? 'bg-white border-slate-200 text-slate-900 dark:bg-slate-900 dark:border-slate-700 dark:text-slate-100' : 'bg-surface border-line text-ink';
  var FILTER_LABEL = LEGACY ? 'block text-xs font-medium opacity-60 mb-1' : 'block text-[11px] font-medium uppercase tracking-eyebrow text-ink-3 mb-1';
  var CONTROL_RADIUS = LEGACY ? 'rounded-lg' : 'rounded-control';
  var FOCUS = LEGACY ? 'focus:border-blue-400' : 'focus:border-accent';

  window.FilterSelect = function(props) {
    var label = props.label || '';
    var rawOpts = props.options || [];
    var opts = rawOpts.map(function(o) { return typeof o === 'object' && o !== null ? { val: String(o.value), lbl: o.label || String(o.value) } : { val: String(o), lbl: String(o) }; });
    var selected = (props.selected || []).map(String);
    var onChange = props.onChange || function() {};
    var single = props.single === true || props.multiple === false;
    var theme = props.className || FILTER_THEME;
    var searchable = props.searchable !== undefined ? props.searchable : opts.length >= 8;
    var _s = React.useState(false), open = _s[0], setOpen = _s[1];
    var _q = React.useState(''), query = _q[0], setQuery = _q[1];
    var btnRef = React.useRef(null);
    var ddRef = React.useRef(null);
    var searchRef = React.useRef(null);
    var _pos = React.useState(null), pos = _pos[0], setPos = _pos[1];

    React.useEffect(function() {
      if (!open) return;
      function handleClick(e) {
        if (btnRef.current && btnRef.current.contains(e.target)) return;
        if (ddRef.current && ddRef.current.contains(e.target)) return;
        setOpen(false);
      }
      document.addEventListener('mousedown', handleClick);
      return function() { document.removeEventListener('mousedown', handleClick); };
    }, [open]);
    React.useEffect(function() {
      if (open && searchable && searchRef.current) searchRef.current.focus();
      if (!open) setQuery('');
    }, [open]);
    React.useEffect(function() {
      if (!open || !btnRef.current) return;
      function reposition() {
        if (!btnRef.current) return;
        var rect = btnRef.current.getBoundingClientRect();
        var spaceBelow = window.innerHeight - rect.bottom;
        var top = spaceBelow > 200 ? rect.bottom + 4 : rect.top - 4;
        setPos({ top: top, left: rect.left, width: Math.max(rect.width, 200), anchor: spaceBelow > 200 ? 'below' : 'above' });
      }
      reposition();
      window.addEventListener('scroll', reposition, true);
      window.addEventListener('resize', reposition);
      return function() { window.removeEventListener('scroll', reposition, true); window.removeEventListener('resize', reposition); };
    }, [open]);

    function toggle(val) {
      if (single) { onChange([val]); setOpen(false); return; }
      var idx = selected.indexOf(val);
      onChange(idx >= 0 ? selected.filter(function(v) { return v !== val; }) : selected.concat([val]));
    }
    var filtered = searchable && query ? opts.filter(function(o) { return o.lbl.toLowerCase().indexOf(query.toLowerCase()) !== -1; }) : opts;
    var selLabels = opts.filter(function(o) { return selected.indexOf(o.val) >= 0; }).map(function(o) { return o.lbl; });
    var display = selected.length === 0 ? (props.placeholder || 'All') : selLabels.length <= 2 ? selLabels.join(', ') : selected.length + ' selected';

    var ddChildren = [];
    if (searchable) {
      ddChildren.push(h('div', { key: 'search', className: 'px-2 pt-1 pb-1 sticky top-0 ' + theme }, [
        h('input', { key: 'q', ref: searchRef, type: 'text', value: query, placeholder: 'Search...', onChange: function(e) { setQuery(e.target.value); },
          className: 'w-full ' + CONTROL_RADIUS + ' border px-2 py-1 text-sm outline-none ' + FOCUS + ' ' + theme, style: props.style, onClick: function(e) { e.stopPropagation(); } })
      ]));
    }
    if (selected.length > 0 && !single) {
      ddChildren.push(h('button', { key: 'clr', type: 'button', className: 'w-full text-start px-3 py-1.5 text-xs font-medium opacity-60 hover:opacity-100', onClick: function() { onChange([]); } }, 'Clear all'));
    }
    filtered.forEach(function(o) {
      var isSelected = selected.indexOf(o.val) >= 0;
      ddChildren.push(h('label', { key: 'opt-' + o.val, className: 'flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-black/5 dark:hover:bg-white/10' + (isSelected ? ' font-medium' : '') }, [
        h('input', { key: 'cb', type: single ? 'radio' : 'checkbox', checked: isSelected, onChange: function() { toggle(o.val); }, className: (single ? '' : 'rounded ') + 'accent-[var(--bow-accent)]' }),
        h('span', { key: 'v', className: 'truncate' }, o.lbl)
      ]));
    });
    if (!filtered.length) ddChildren.push(h('div', { key: 'none', className: 'px-3 py-2 text-xs opacity-60' }, 'No matches'));

    var ddStyle = { position: 'fixed', zIndex: 99999, top: pos && pos.anchor === 'below' ? pos.top : undefined, bottom: pos && pos.anchor === 'above' ? (window.innerHeight - pos.top) : undefined, left: pos ? pos.left : undefined, width: pos ? pos.width : undefined, maxHeight: 288 };
    if (props.style) { for (var sk in props.style) ddStyle[sk] = props.style[sk]; }
    var dropdown = (open && pos) ? ReactDOM.createPortal(h('div', { ref: ddRef, className: CONTROL_RADIUS + ' border shadow-lift overflow-auto py-1 ' + theme, style: ddStyle }, ddChildren), document.body) : null;

    return h('div', { className: 'relative inline-block min-w-[140px]' + (props.wrapperClassName ? ' ' + props.wrapperClassName : '') }, [
      label ? h('label', { key: 'l', className: FILTER_LABEL }, label) : null,
      h('button', { ref: btnRef, key: 'btn', type: 'button', 'aria-haspopup': 'listbox', 'aria-expanded': open,
        className: 'w-full flex items-center justify-between gap-2 ' + CONTROL_RADIUS + ' border px-3 py-1.5 text-sm cursor-pointer ' + FOCUS + ' ' + theme, style: props.style, onClick: function() { setOpen(!open); } }, [
        h('span', { key: 't', className: 'truncate' + (selected.length ? ' font-medium' : '') }, display),
        h('svg', { key: 'i', width: 12, height: 12, viewBox: '0 0 12 12', className: 'opacity-50 shrink-0' }, h('path', { d: 'M3 5l3 3 3-3', stroke: 'currentColor', strokeWidth: 1.5, fill: 'none' }))
      ]),
      dropdown
    ]);
  };

  window.FilterSearch = function(props) {
    var label = props.label || '';
    var theme = props.className || FILTER_THEME;
    return h('div', { className: 'inline-block min-w-[140px]' + (props.wrapperClassName ? ' ' + props.wrapperClassName : '') }, [
      label ? h('label', { key: 'l', className: FILTER_LABEL }, label) : null,
      h('div', { key: 'w', className: 'relative' }, [
        LEGACY ? null : h('span', { key: 'i', className: 'absolute inset-y-0 start-2.5 flex items-center text-ink-3 pointer-events-none' }, h(window.Icon, { name: 'search', size: 13 })),
        h('input', { key: 'inp', type: 'text', value: props.value || '', placeholder: props.placeholder || 'Search...', onChange: props.onChange || function() {},
          className: 'w-full ' + CONTROL_RADIUS + ' border py-1.5 text-sm outline-none ' + FOCUS + ' ' + (LEGACY ? 'px-3 ' : 'ps-8 pe-3 ') + theme, style: props.style })
      ])
    ]);
  };

  window.FilterDateRange = function(props) {
    var label = props.label || '';
    var value = props.value || {};
    var onChange = props.onChange || function() {};
    var theme = props.className || FILTER_THEME;
    var inputType = props.type || 'date';
    var inputCls = 'w-full ' + CONTROL_RADIUS + ' border px-2 py-1.5 text-sm outline-none ' + FOCUS + ' ' + theme;
    return h('div', { className: 'inline-block min-w-[200px]' + (props.wrapperClassName ? ' ' + props.wrapperClassName : '') }, [
      label ? h('label', { key: 'l', className: FILTER_LABEL }, label) : null,
      h('div', { key: 'row', className: 'flex items-center gap-2' }, [
        h('input', { key: 'from', type: inputType, value: value.from || '', onChange: function(e) { onChange({ from: e.target.value || null, to: value.to || null }); }, className: inputCls, style: props.style }),
        h('span', { key: 'sep', className: 'text-xs opacity-50' }, '–'),
        h('input', { key: 'to', type: inputType, value: value.to || '', onChange: function(e) { onChange({ from: value.from || null, to: e.target.value || null }); }, className: inputCls, style: props.style })
      ])
    ]);
  };

  // FilterBar — a wrapping row for controls with an optional reset.
  window.FilterBar = function(props) {
    props = props || {};
    return h('div', { className: _join('flex flex-wrap items-end gap-3', props.className), style: props.style }, [
      props.children,
      props.onReset ? h('button', { key: 'reset', type: 'button', onClick: props.onReset, className: 'inline-flex items-center gap-1 self-end text-xs font-medium text-ink-3 hover:text-ink px-2 py-1.5' }, [h(window.Icon, { key: 'i', name: 'rotate-ccw', size: 12 }), props.resetLabel || 'Reset']) : null
    ]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // BowFile / BowPdfViewer — embedded files
  // ═══════════════════════════════════════════════════════════════════════════

  function _bowFindFile(id) {
    var data = window.ARTIFACT_DATA || {};
    var files = Array.isArray(data.files) ? data.files : [];
    for (var i = 0; i < files.length; i++) { if (files[i] && String(files[i].id) === String(id)) return files[i]; }
    return null;
  }
  function _bowPdfCard(src, filename) {
    return h('div', { className: 'flex flex-col items-center justify-center gap-3 h-full w-full bg-surface-2 rounded-control border border-line text-center p-6' }, [
      h(window.Icon, { key: 'ic', name: 'file-text', size: 40, className: 'text-negative', strokeWidth: 1.5 }),
      h('div', { key: 'nm', className: 'text-sm font-medium text-ink' }, filename || 'Document.pdf'),
      h('a', { key: 'op', href: src, target: '_blank', rel: 'noopener', className: 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-control bg-ink text-bg text-xs font-medium' }, 'Open PDF')
    ]);
  }
  window.BowPdfViewer = function(props) {
    var containerRef = React.useRef(null);
    var _s = React.useState('loading'); var status = _s[0], setStatus = _s[1];
    var height = props.height || 520;
    React.useEffect(function() {
      if (typeof pdfjsLib === 'undefined') { setStatus('nolib'); return; }
      var cancelled = false;
      try { pdfjsLib.GlobalWorkerOptions.workerSrc = '/libs/pdf.worker.min.js'; } catch (e) {}
      function toBytes(dataUri) {
        var b64 = dataUri.indexOf(',') >= 0 ? dataUri.slice(dataUri.indexOf(',') + 1) : dataUri;
        var bin = atob(b64), len = bin.length, bytes = new Uint8Array(len);
        for (var i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
        return bytes;
      }
      var src = props.src || '';
      if (!src) { setStatus('error'); return; }
      function loadBytes() {
        if (/^data:/i.test(src)) return Promise.resolve(toBytes(src));
        return fetch(src).then(function(r) { if (!r.ok) throw new Error('fetch ' + r.status); return r.arrayBuffer(); }).then(function(buf) { return new Uint8Array(buf); });
      }
      loadBytes().then(function(bytes) { return pdfjsLib.getDocument({ data: bytes }).promise; }).then(function(pdf) {
        if (cancelled) return;
        var container = containerRef.current;
        if (!container) return;
        container.innerHTML = '';
        var maxPages = Math.min(pdf.numPages, props.maxPages || 25);
        var seq = Promise.resolve();
        for (var p = 1; p <= maxPages; p++) {
          (function(pageNum) {
            seq = seq.then(function() {
              return pdf.getPage(pageNum).then(function(page) {
                if (cancelled || !containerRef.current) return;
                var cw = containerRef.current.clientWidth || 800;
                var base = page.getViewport({ scale: 1 });
                var scale = Math.min(cw / base.width, 2);
                var viewport = page.getViewport({ scale: scale });
                var canvas = document.createElement('canvas');
                canvas.width = viewport.width; canvas.height = viewport.height;
                canvas.style.width = '100%'; canvas.style.display = 'block'; canvas.style.marginBottom = '8px'; canvas.style.borderRadius = '6px'; canvas.style.boxShadow = '0 1px 3px rgba(0,0,0,0.12)';
                containerRef.current.appendChild(canvas);
                return page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise;
              });
            });
          })(p);
        }
        seq.then(function() { if (!cancelled) setStatus('ready'); }).catch(function() { if (!cancelled) setStatus('error'); });
      }).catch(function() { if (!cancelled) setStatus('error'); });
      return function() { cancelled = true; };
    }, [props.src]);
    if (status === 'nolib' || status === 'error') return h('div', { style: { height: height } }, _bowPdfCard(props.src, props.filename));
    return h('div', { className: 'relative w-full rounded-control border border-line bg-surface-2 overflow-y-auto', style: { height: height, padding: 8 } }, [
      status === 'loading' ? h('div', { key: 'ld', className: 'absolute inset-0 flex items-center justify-center text-ink-3 text-sm' }, h(window.LoadingSpinner, { size: 28 })) : null,
      h('div', { key: 'pages', ref: containerRef, className: 'w-full' })
    ]);
  };
  window.BowFile = function(props) {
    props = props || {};
    var file = _bowFindFile(props.id);
    var wrapCls = 'relative overflow-hidden ' + (props.className || '');
    var wrapStyle = Object.assign({ width: '100%' }, props.style || {});
    if (!file) {
      return h('div', { className: wrapCls + ' flex items-center justify-center bg-surface-2 border border-dashed border-line-2 rounded-control text-ink-3 text-sm', style: Object.assign({ minHeight: 160 }, wrapStyle) }, 'File not found: ' + (props.id || ''));
    }
    var ct = String(file.content_type || '').toLowerCase();
    var src = file.url || file.dataUri || '';
    var overlay = props.children != null ? h('div', { key: 'ov', className: 'absolute inset-0 pointer-events-none' }, props.children) : null;
    var media;
    if (ct.indexOf('pdf') !== -1) {
      media = h(window.BowPdfViewer, { key: 'pdf', src: src, filename: file.filename, height: props.height || 520 });
    } else if (ct.indexOf('image') !== -1 || !ct) {
      media = h('img', { key: 'img', src: src, alt: props.alt || file.filename || '', className: 'w-full h-full rounded-control', style: { objectFit: props.fit || 'contain', display: 'block', maxWidth: '100%' } });
    } else {
      media = h('a', { key: 'dl', href: src, download: file.filename || 'file', className: 'inline-flex items-center gap-2 px-3 py-2 rounded-control border border-line text-ink-2 hover:bg-surface-2 text-sm' }, 'Download ' + (file.filename || 'file'));
    }
    return h('div', { className: wrapCls, style: wrapStyle }, overlay ? [media, overlay] : media);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Theme boot + forced dark + EChart
  // ═══════════════════════════════════════════════════════════════════════════

  // Forced-dark artifacts (pre-v11 pattern: <div className="dark"> at the root)
  // mirror the class onto <html> so portaled UI and charts follow.
  (function watchForcedDark() {
    function sync() {
      var root = document.getElementById('root');
      var forced = !!(root && (root.classList.contains('dark') || root.querySelector(':scope .dark'))) || _forcedDark;
      window.__bowForcedDark = forced;
      if (forced && !_bowIsDark()) {
        document.documentElement.classList.add('dark');
        try { window.dispatchEvent(new CustomEvent('bow-colormode', { detail: { mode: 'dark' } })); } catch (e) {}
      }
    }
    try {
      var mo = new MutationObserver(sync);
      mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
      sync();
    } catch (e) { /* non-fatal */ }
  })();

  // Apply the default theme now (legacy look for old artifacts, slate for new)
  // and re-derive tokens whenever the host flips color mode.
  _applyTheme();
  window.addEventListener('bow-colormode', function() { _applyTheme(); });

  function safeOption(opt) {
    if (opt && opt.tooltip && typeof opt.tooltip.formatter === 'function') {
      var orig = opt.tooltip.formatter;
      opt.tooltip.formatter = function() { try { return orig.apply(this, arguments); } catch (e) { return ''; } };
    }
    return opt;
  }

  // <EChart option height className viz rows calc palette /> — palette: array of
  // colors overriding the theme's chart palette for this chart only.
  window.EChart = function(props) {
    var ref = React.useRef(null);
    var chartRef = React.useRef(null);
    var ht = props.height || 320;
    var _gen = React.useState(0), gen = _gen[0], setGen = _gen[1];
    React.useEffect(function() {
      function bump() { setGen(function(c) { return c + 1; }); }
      window.addEventListener('bow-colormode', bump);
      window.addEventListener('bow-theme', bump);
      return function() { window.removeEventListener('bow-colormode', bump); window.removeEventListener('bow-theme', bump); };
    }, []);
    function withPalette(opt) {
      if (!opt) return opt;
      if (props.palette && props.palette.length && !opt.color) { var o = {}; for (var k in opt) o[k] = opt[k]; o.color = props.palette; return o; }
      return opt;
    }
    React.useEffect(function() {
      if (!ref.current) return;
      var chart = echarts.init(ref.current, 'bow', { renderer: props.renderer || 'canvas' });
      chartRef.current = chart;
      if (props.option) chart.setOption(safeOption(withPalette(props.option)));
      if (props.onClick) chart.on('click', props.onClick);
      var ro = new ResizeObserver(function() { chart.resize(); });
      ro.observe(ref.current);
      return function() { ro.disconnect(); chart.dispose(); };
    }, [gen]);
    React.useEffect(function() {
      if (chartRef.current && props.option) chartRef.current.setOption(safeOption(withPalette(props.option)), true);
    }, [props.option]);
    var chart = h('div', { ref: ref, style: { width: '100%', height: ht }, className: props.className || '' });
    if (!props.viz) return chart;
    return h('div', { className: 'relative', style: { width: '100%' } }, [
      h('div', { key: 'info', className: 'absolute top-2 end-2 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })),
      chart
    ]);
  };

  window.resizeAllCharts = function() {
    if (typeof echarts !== 'undefined') {
      var charts = document.querySelectorAll('[_echarts_instance_]');
      charts.forEach(function(el) { var chart = echarts.getInstanceByDom(el); if (chart) chart.resize(); });
    }
  };
  setTimeout(window.resizeAllCharts, 100);
  setTimeout(window.resizeAllCharts, 500);
  window.addEventListener('resize', window.resizeAllCharts);
  // Fonts load asynchronously; charts measured with fallback metrics re-layout
  // once the real faces are in.
  try { if (document.fonts && document.fonts.ready) document.fonts.ready.then(function() { setTimeout(window.resizeAllCharts, 50); }); } catch (e) {}

  // ═══════════════════════════════════════════════════════════════════════════
  // InfoOverlay — ⓘ popovers for custom markup via data-bow-viz / data-bow-calc
  // ═══════════════════════════════════════════════════════════════════════════

  window.InfoOverlay = function() {
    var _tick = React.useState(0), setTick = _tick[1];
    var _open = React.useState(null), openT = _open[0], setOpenT = _open[1];
    var _tab = React.useState('data'), tab = _tab[0], setTab = _tab[1];
    var panelRef = React.useRef(null);
    React.useEffect(function() {
      var raf = null;
      function ping() { if (raf) return; raf = requestAnimationFrame(function() { raf = null; setTick(function(c) { return c + 1; }); }); }
      var root = document.getElementById('root') || document.body;
      var mo = new MutationObserver(ping);
      mo.observe(root, { childList: true, subtree: true, attributes: true });
      window.addEventListener('scroll', ping, true);
      window.addEventListener('resize', ping);
      var t1 = setTimeout(ping, 150), t2 = setTimeout(ping, 600), t3 = setTimeout(ping, 1500);
      return function() { mo.disconnect(); window.removeEventListener('scroll', ping, true); window.removeEventListener('resize', ping); clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); if (raf) cancelAnimationFrame(raf); };
    }, []);
    React.useEffect(function() {
      if (!openT) return;
      function onDown(e) { if (e.target && e.target.closest && e.target.closest('[data-bow-ibtn], [data-bow-panel]')) return; setOpenT(null); }
      function onKey(e) { if (e.key === 'Escape') setOpenT(null); }
      document.addEventListener('mousedown', onDown);
      document.addEventListener('keydown', onKey);
      return function() { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
    }, [openT]);

    var data = window.ARTIFACT_DATA || {};
    var vizs = Array.isArray(data.visualizations) ? data.visualizations : [];
    var rtl = (document.documentElement.getAttribute('dir') || '').toLowerCase() === 'rtl';
    var targets = [];
    var els = document.querySelectorAll('[data-bow-viz]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var r = el.getBoundingClientRect();
      if ((r.width === 0 && r.height === 0) || r.bottom < 0 || r.top > window.innerHeight) continue;
      var raw = el.getAttribute('data-bow-viz');
      var byId = null;
      if (raw && !/^\d+$/.test(raw)) byId = window.vizById(raw);
      targets.push({ rect: r, vizIndex: parseInt(raw, 10) || 0, viz: byId, calc: el.getAttribute('data-bow-calc') || null, title: el.getAttribute('data-bow-title') || null });
    }
    var markers = targets.map(function(t, i) {
      return h('button', { key: 'm' + i, type: 'button', 'data-bow-ibtn': '1', 'aria-label': 'Details',
        onClick: function(e) { e.stopPropagation(); setTab('data'); setOpenT(t); },
        style: { position: 'fixed', top: Math.max(2, t.rect.top + 6), left: rtl ? t.rect.left + 4 : t.rect.right - 24, zIndex: 99998 },
        className: 'inline-flex items-center justify-center w-5 h-5 rounded-full bg-surface/80 backdrop-blur text-ink-3 hover:text-ink border border-line shadow-card transition-colors' }, _infoGlyph(14));
    });
    var panel = null;
    if (openT) {
      var viz = openT.viz || vizs[openT.vizIndex] || {};
      var W = Math.min(400, window.innerWidth - 16);
      var left = Math.min(openT.rect.right - W, window.innerWidth - W - 8); if (left < 8) left = 8;
      var spaceBelow = window.innerHeight - openT.rect.top;
      var below = spaceBelow > 260;
      var vizForPanel = Object.assign({}, viz, { title: openT.title || viz.title });
      panel = _popoverPanel(vizForPanel, tab, setTab, function() { setOpenT(null); },
        tab === 'code' ? _codeTabBody(viz) : _dataTabBody(viz, { calc: openT.calc }),
        { position: 'fixed', left: left, width: W, zIndex: 99999, maxHeight: '72vh', top: below ? (openT.rect.top + 28) : undefined, bottom: below ? undefined : (window.innerHeight - openT.rect.top + 6), display: 'flex', flexDirection: 'column' },
        { ref: panelRef, 'data-bow-panel': '1' });
    }
    if (!markers.length && !panel) return null;
    return h('div', { style: { position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 99998 } },
      markers.concat(panel ? [panel] : []).map(function(node, i) { return h('div', { key: i, style: { pointerEvents: 'auto' } }, node); }));
  };

  (function mountInfoOverlay() {
    if (window.__BOW_INFO === false) return;
    if (window.__bowInfoMounted) return;
    window.__bowInfoMounted = true;
    try {
      var host = document.createElement('div');
      host.id = '__bow_info_overlay';
      document.body.appendChild(host);
      if (ReactDOM.createRoot) ReactDOM.createRoot(host).render(h(window.InfoOverlay));
      else ReactDOM.render(h(window.InfoOverlay), host);
    } catch (e) { /* non-fatal */ }
  })();

  // ─── Babel sandbox patches ──────────────────────────────────────────────────
  (function patchBabel() {
    if (window.Babel && window.Babel.availablePresets && window.Babel.availablePresets.react) {
      var _origReact = window.Babel.availablePresets.react;
      window.Babel.availablePresets.react = function(api, opts, dir) {
        return _origReact(api, Object.assign({}, opts, { runtime: 'classic' }), dir);
      };
    }
    function stripImports(code) {
      var s = code.replace(/import\s*\{[^}]*\}\s*from\s*['"][^'"]+['"]\s*;?/g, '');
      return s.replace(/^[ \t]*import\b(?!\s*\().*$/gm, '');
    }
    var _origAppendChild = Node.prototype.appendChild;
    Node.prototype.appendChild = function(node) {
      if (node && node.nodeType === 1 && node.tagName === 'SCRIPT' && !node.getAttribute('src') && !node.getAttribute('type') && node.textContent && /\bimport\b/.test(node.textContent)) {
        node.textContent = stripImports(node.textContent);
      }
      return _origAppendChild.call(this, node);
    };
  })();

})();
