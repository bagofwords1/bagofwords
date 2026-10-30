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
    if (data && typeof data.verification_revision === 'number') {
      window.requestAnimationFrame(function() {
        window.parent.postMessage({ type: 'ARTIFACT_DATA_RECEIVED', revision: data.verification_revision }, '*');
      });
    }
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
    if (LEGACY) {
      echarts.registerTheme('bow', {
        color: ['#3B82F6', '#10B981', '#8B5CF6', '#F59E0B', '#EF4444', '#06B6D4', '#EC4899', '#14B8A6', '#60A5FA', '#34D399'],
        backgroundColor: 'transparent',
        categoryAxis: {
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#64748b', fontSize: 12 }, splitLine: { show: false }
        },
        valueAxis: {
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#64748b', fontSize: 12 }, splitLine: { lineStyle: { color: '#f1f5f9' } }
        },
        line: { smooth: true, symbol: 'none', lineStyle: { width: 2 } },
        bar: { itemStyle: { borderRadius: [6, 6, 0, 0] } },
        pie: { itemStyle: { borderRadius: 6 } },
        grid: { left: 40, right: 20, top: 20, bottom: 40, containLabel: true },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          borderColor: 'rgba(51, 65, 85, 0.5)',
          borderWidth: 1, borderRadius: 12, padding: [12, 16],
          textStyle: { color: '#fff', fontSize: 13 }, trigger: 'axis'
        }
      });

      // Dark-mode counterpart, picked automatically when the iframe <html> carries
      // the `dark` class (host app in dark mode).
      echarts.registerTheme('bow-dark', {
        color: ['#60A5FA', '#34D399', '#A78BFA', '#FBBF24', '#F87171', '#22D3EE', '#F472B6', '#2DD4BF', '#93C5FD', '#6EE7B7'],
        backgroundColor: 'transparent',
        categoryAxis: {
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#94a3b8', fontSize: 12 }, splitLine: { show: false }
        },
        valueAxis: {
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#94a3b8', fontSize: 12 }, splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.15)' } }
        },
        legend: { textStyle: { color: '#cbd5e1' } },
        line: { smooth: true, symbol: 'none', lineStyle: { width: 2 } },
        bar: { itemStyle: { borderRadius: [6, 6, 0, 0] } },
        pie: { itemStyle: { borderRadius: 6 } },
        grid: { left: 40, right: 20, top: 20, bottom: 40, containLabel: true },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.98)',
          borderColor: 'rgba(71, 85, 105, 0.6)',
          borderWidth: 1, borderRadius: 12, padding: [12, 16],
          textStyle: { color: '#fff', fontSize: 13 }, trigger: 'axis'
        }
      });

      return;
    }
    var text = { color: pal.ink2, fontFamily: theme.fonts.body, fontSize: 12 };
    var spec = {
      color: pal.chart,
      backgroundColor: 'transparent',
      textStyle: { fontFamily: theme.fonts.body, color: pal.ink2 },
      title: { textStyle: { color: pal.ink, fontFamily: theme.fonts.display, fontWeight: 600, fontSize: 15 }, subtextStyle: { color: pal.ink3 } },
      legend: { textStyle: { color: pal.ink2, fontFamily: theme.fonts.body }, itemWidth: 12, itemHeight: 8, icon: 'roundRect' },
      categoryAxis: {
        axisLine: { show: false }, axisTick: { show: false },
        // hideOverlap: a ranked chart with many categories drops the labels
        // that would collide instead of printing them on top of each other.
        axisLabel: { color: pal.ink3, fontSize: 11, fontFamily: theme.fonts.body, hideOverlap: true },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: pal.ink3, fontSize: 11, fontFamily: theme.fonts.body, hideOverlap: true },
        splitLine: { lineStyle: { color: _rgba(pal.line2, dark ? 0.35 : 0.55), type: [4, 4] } }
      },
      timeAxis: { axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: pal.ink3, fontSize: 11, hideOverlap: true }, splitLine: { show: false } },
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
    if (opts.pct || opts.ratio) {
      // `ratio` is explicit (0.358 → 35.8%). `pct` accepts either form: a
      // magnitude at or below 1 is read as a share, anything larger as an
      // already-computed percentage. Passing a share to pct and printing
      // "0.4%" instead of "35.8%" was the most common wrong number on a
      // generated dashboard; `exact: true` opts out for a genuine sub-1%
      // percentage. share(part, whole) remains the clearest spelling.
      if (opts.ratio || (!opts.exact && n !== 0 && Math.abs(n) <= 1)) n = n * 100;
      return (opts.sign && n > 0 ? '+' : '') + n.toFixed(opts.decimals != null ? opts.decimals : 1) + '%';
    }
    if (opts.compact === false) return n.toLocaleString(undefined, { maximumFractionDigits: opts.decimals != null ? opts.decimals : 2 });
    if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: opts.decimals != null ? opts.decimals : 2 });
  };

  // ── share() ─────────────────────────────────────────────────────────────────
  // share(part, whole) → "37.0%". The one right way to print a proportion:
  // fmt(part / whole, {pct:true}) prints "0.4%" because pct expects a number
  // that is already a percentage. Returns "—" when whole is 0/null.
  window.share = function(part, whole, opts) {
    opts = opts || {};
    var p = Number(part), w = Number(whole);
    if (!isFinite(p) || !isFinite(w) || w === 0) return '—';
    var pct = (p / w) * 100;
    return (opts.sign && pct > 0 ? '+' : '') + pct.toFixed(opts.decimals != null ? opts.decimals : 1) + '%';
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
    var hostLoading = false;
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
        loading = hostLoading || Object.keys(inflight).length > 0;
        // Data delivery may follow a failure status; it must not erase it.
        notify();
      },
      _status: function(payload) {
        hostLoading = !!(payload && payload.loading);
        loading = hostLoading;
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
  // A SQL timestamp lands in a cell as "2021-02-06T00:00:00.000"; printing it
  // raw is a database dump, so midnight renders as the date alone and any
  // other time keeps HH:MM. Themed runtime only — legacy tables are untouched.
  var _ISO_DT = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/;

  function _infoCell(v) {
    if (v == null) return '—';
    if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
    if (typeof v === 'object') { try { return JSON.stringify(v); } catch (e) { return String(v); } }
    var str = String(v);
    if (!LEGACY) {
      var m = _ISO_DT.exec(str);
      if (m) return (m[2] === '00' && m[3] === '00') ? m[1] : (m[1] + ' ' + m[2] + ':' + m[3]);
    }
    return str;
  }
  // A header that is just the raw field name (TOTALDURATIONMILLISECONDS,
  // unit_price, avgTrackLength) reads as a database dump; humanize it. An
  // explicit headerName is always respected.
  function _humanHeader(field) {
    var f = String(field || '');
    if (!f) return f;
    if (/[ ]/.test(f)) return f;                       // already a phrase
    var words;
    if (/[_-]/.test(f)) words = f.split(/[_-]+/);
    else if (/^[A-Z0-9]+$/.test(f)) words = [f];       // SHOUTCASE: single token
    else words = f.replace(/([a-z0-9])([A-Z])/g, '$1 $2').split(' ');
    return words.filter(Boolean).map(function(w) {
      if (/^[A-Z]{2,}$/.test(w) && w.length <= 4) return w;   // ID, URL, SKU
      return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
    }).join(' ');
  }

  function _infoCols(viz, rows) {
    var cols = viz.columns || [];
    if (cols.length) {
      return cols.map(function(c) {
        if (typeof c === 'string') return { field: c, header: _humanHeader(c) };
        var field = c.field || c.headerName || c.name;
        var header = c.headerName || c.name;
        return { field: field, header: header && header !== field ? header : _humanHeader(field), dtype: c.dtype };
      }).filter(function(c) { return c.field; });
    }
    var src = (rows && rows.length) ? rows : (viz.rows || []);
    var r = src[0];
    if (r && typeof r === 'object') return Object.keys(r).map(function(k) { return { field: k, header: _humanHeader(k) }; });
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

  // The ⓘ's geometry is pinned inline: dashboards often style `button`/`svg`
  // generically (e.g. `#root button { padding; border }`), which would squeeze
  // the glyph to nothing inside a padded box. Inline styles outrank those rules.
  var INFO_BTN_BOX = { display: 'inline-flex', alignItems: 'center', justifyContent: 'center', padding: 0, margin: 0, minWidth: 0, minHeight: 0, width: 20, height: 20, borderRadius: 9999, boxSizing: 'border-box', lineHeight: 0 };
  function _infoGlyph(size) {
    var px = size || 15;
    return h('svg', { width: px, height: px, viewBox: '0 0 16 16', fill: 'none', style: { width: px, height: px, minWidth: px, flexShrink: 0, display: 'block' } }, [
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

    // data-bow-info: the polish picker resolves a pick on this button to the
    // card that hosts it (card > positioning div > this span).
    return h('span', { className: 'inline-flex align-middle', 'data-bow-info': '1' }, [
      h('button', {
        key: 'btn', ref: btnRef, type: 'button', 'aria-label': 'Details',
        style: Object.assign({ border: 0 }, INFO_BTN_BOX),
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
    // onChange receives the typed text, like FilterSelect/FilterDateRange pass
    // their values — generated code passes a bare setter (onChange={setQuery})
    // and then calls query.toLowerCase(); handing it the raw event crashed the
    // dashboard. The text is boxed with a `target.value` shim so handlers
    // written for the event (e => setQuery(e.target.value)) keep working.
    var onChange = props.onChange ? function(e) {
      var v = e.target.value;
      var payload = Object(v);
      payload.target = { value: v };
      payload.currentTarget = payload.target;
      props.onChange(payload);
    } : function() {};
    return h('div', { className: 'inline-block min-w-[140px]' + (props.wrapperClassName ? ' ' + props.wrapperClassName : '') }, [
      label ? h('label', { key: 'l', className: FILTER_LABEL }, label) : null,
      h('div', { key: 'w', className: 'relative' }, [
        LEGACY ? null : h('span', { key: 'i', className: 'absolute inset-y-0 start-2.5 flex items-center text-ink-3 pointer-events-none' }, h(window.Icon, { name: 'search', size: 13 })),
        h('input', { key: 'inp', type: 'text', value: props.value == null ? '' : String(props.value), placeholder: props.placeholder || 'Search...', onChange: onChange,
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

  // Grid margins may be a number, "8", "8%" or "8px"; only a plain pixel
  // value can be compared to the space a legend needs.
  function _px(v) {
    if (typeof v === 'number') return v;
    var m = /^(\d+(?:\.\d+)?)(px)?$/.exec(String(v || '').trim());
    return m ? parseFloat(m[1]) : Infinity;   // '%' or 'auto' → leave it alone
  }

  function safeOption(opt) {
    if (opt && opt.tooltip && typeof opt.tooltip.formatter === 'function') {
      var orig = opt.tooltip.formatter;
      opt.tooltip.formatter = function() { try { return orig.apply(this, arguments); } catch (e) { return ''; } };
    }
    return opt;
  }

  // Content signature of an ECharts option: a stable string that changes when
  // anything the chart draws changes. Functions count by source. With
  // `withoutData`, data arrays (series/axis `data`, dataset `source`) are left
  // out, so two options with the same signature differ only in values — safe
  // to merge, since merge replaces data arrays wholesale. null = not
  // serialisable (cycles, DOM): the caller always rebuilds.
  function _optionSig(opt, withoutData) {
    try {
      return JSON.stringify(opt, function(key, v) {
        if (typeof v === 'function') return 'fn:' + String(v);
        if (withoutData && (key === 'data' || key === 'source') && Array.isArray(v)) return '[data]';
        if (typeof v === 'number' && !isFinite(v)) return String(v);
        return v;
      });
    } catch (e) { return null; }
  }
  function _hasFunction(v, depth) {
    if (typeof v === 'function') return true;
    if (!v || typeof v !== 'object' || (depth || 0) > 6) return false;
    if (Array.isArray(v)) {
      // Data arrays are long and hold values, not callbacks.
      for (var i = 0; i < v.length && i < 50; i++) if (_hasFunction(v[i], (depth || 0) + 1)) return true;
      return false;
    }
    for (var k in v) if (_hasFunction(v[k], (depth || 0) + 1)) return true;
    return false;
  }

  // <EChart option height className viz rows calc palette /> — palette: array of
  // colors overriding the theme's chart palette for this chart only.
  window.EChart = function(props) {
    var ref = React.useRef(null);
    var chartRef = React.useRef(null);
    var ht = props.height || (LEGACY ? 400 : 320);
    var _gen = React.useState(0), gen = _gen[0], setGen = _gen[1];
    React.useEffect(function() {
      function bump() { setGen(function(c) { return c + 1; }); }
      window.addEventListener('bow-colormode', bump);
      window.addEventListener('bow-theme', bump);
      return function() { window.removeEventListener('bow-colormode', bump); window.removeEventListener('bow-theme', bump); };
    }, []);
    function withPalette(opt) {
      if (!opt) return opt;
      var o = opt;
      if (props.palette && props.palette.length && !opt.color) { o = {}; for (var k in opt) o[k] = opt[k]; o.color = props.palette; }
      // A legend needs room the theme's tight grid does not reserve: give a
      // bottom legend a bottom margin and a top legend a top margin unless the
      // author positioned the grid explicitly (the classic overlap bug).
      var lg = o.legend;
      if (lg && !Array.isArray(lg) && lg.show !== false && !Array.isArray(o.grid)) {
        var g = {}; for (var gk in (o.grid || {})) g[gk] = o.grid[gk];
        var atBottom = lg.bottom != null || (lg.top == null && lg.y === 'bottom');
        // A legend needs a band of its own; a grid margin smaller than that
        // puts the legend on top of the axis labels, so raise it rather than
        // only filling in a missing value.
        if (atBottom && (g.bottom == null || _px(g.bottom) < 36)) g.bottom = 36;
        if (!atBottom && (g.top == null || _px(g.top) < 44)) g.top = 44;
        if (o === opt) { o = {}; for (var k2 in opt) o[k2] = opt[k2]; }
        o.grid = g;
      }
      return o;
    }
    // Signatures of the option last applied: `full` (everything) and `shape`
    // (everything but the data arrays). See applyOption.
    var appliedRef = React.useRef({ full: null, shape: null });
    React.useEffect(function() {
      if (!ref.current) return;
      var el = ref.current;
      var chart = echarts.init(el, 'bow', { renderer: props.renderer || 'canvas' });
      chartRef.current = chart;
      appliedRef.current = { full: null, shape: null };
      if (props.option) {
        var opt = withPalette(props.option);
        appliedRef.current = { full: _optionSig(opt, false), shape: _optionSig(opt, true) };
        chart.setOption(safeOption(opt));
      }
      if (props.onClick) chart.on('click', props.onClick);
      // A chart in a hidden tab or collapsed panel measures 0×0; resizing it
      // then would only blank it. It re-lays out when it is shown again.
      var ro = new ResizeObserver(function() { if (el.clientWidth && el.clientHeight) chart.resize(); });
      ro.observe(el);
      return function() { ro.disconnect(); chart.dispose(); };
    }, [gen]);
    // Artifact code builds `option` inline, so it is a new object on EVERY
    // render of the parent — opening a modal, switching a tab, typing in a
    // search box. Rebuilding the chart for each (setOption notMerge) replayed
    // its intro animation every time. Instead:
    //   same content            → nothing to draw (formatters refreshed only)
    //   same shape, new data    → merge, so marks animate to their new values
    //   different shape         → full rebuild, so nothing stale survives
    React.useEffect(function() {
      var chart = chartRef.current;
      if (!chart || !props.option) return;
      var opt = withPalette(props.option);
      var full = _optionSig(opt, false);
      var last = appliedRef.current;
      if (full !== null && full === last.full) {
        // Same source, but a formatter may close over newer state.
        if (_hasFunction(opt)) chart.setOption(safeOption(opt), { notMerge: false, silent: true });
        return;
      }
      var shape = _optionSig(opt, true);
      appliedRef.current = { full: full, shape: shape };
      if (shape !== null && shape === last.shape) chart.setOption(safeOption(opt), { notMerge: false });
      else chart.setOption(safeOption(opt), true);
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
  // Chat building blocks — ChatResult, ChatMessage, useConversation
  // ═══════════════════════════════════════════════════════════════════════════
  // Generated chat apps compose these instead of re-implementing the stream
  // handling and the result rendering. They reference each other (and EChart /
  // DataTable) through this closure, so an app that defines its own ChatResult
  // only changes its own usage.

  var _EChart = window.EChart;
  var _DataTable = window.DataTable;
  var _Icon = window.Icon;
  var _LoadingSpinner = window.LoadingSpinner;
  function _chatCls(base, extra) {
    if (LEGACY) return extra ? base + ' ' + extra : base;
    return _mergeClasses(base, extra);
  }
  var CHAT = LEGACY ? {
    result: 'space-y-2',
    title: 'text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400',
    note: 'text-xs text-slate-500 dark:text-slate-400'
  } : {
    result: 'space-y-2',
    title: 'text-[11px] font-medium uppercase tracking-eyebrow text-ink-3',
    note: 'text-xs text-ink-3'
  };

  // Note texts; apps pass `labels` in their own language. {shown}/{total} are
  // filled in.
  var _CHAT_LABELS = {
    rowsShown: 'Showing {shown} of {total} rows',
    columnsShown: '{shown} of {total} columns',
    partial: 'Partial table',
    incomplete: 'Some results of this answer could not be shown',
    inProgress: 'Answer in progress...',
    steps: 'Steps',
    failed: 'The answer failed',
    stopped: 'The answer was stopped',
    olderMessages: 'Load earlier messages',
    jumpToLatest: 'Jump to latest',
    send: 'Send',
    stop: 'Stop',
    unavailable: 'Chat is not available here',
    authRequired: 'Sign in to chat',
    conversations: 'Conversations',
    newConversation: 'New chat',
    rename: 'Rename',
    archive: 'Archive',
    archiveConfirm: 'Archive this conversation?',
    cancel: 'Cancel'
  };
  function _chatFill(template, vars) {
    return String(template).replace(/\{(shown|total)\}/g, function(_, k) { return vars && vars[k] != null ? String(vars[k]) : ''; });
  }
  function _chatLabels(labels) {
    var src = {};
    for (var k in _CHAT_LABELS) src[k] = labels && typeof labels[k] === 'string' ? labels[k] : _CHAT_LABELS[k];
    return {
      rowsShown: function(vars) { return _chatFill(src.rowsShown, vars); },
      columnsShown: function(vars) { return _chatFill(src.columnsShown, vars); },
      partial: _chatFill(src.partial), incomplete: _chatFill(src.incomplete), inProgress: _chatFill(src.inProgress),
      steps: _chatFill(src.steps), failed: _chatFill(src.failed), stopped: _chatFill(src.stopped),
      olderMessages: _chatFill(src.olderMessages), jumpToLatest: _chatFill(src.jumpToLatest),
      send: _chatFill(src.send), stop: _chatFill(src.stop), unavailable: _chatFill(src.unavailable), authRequired: _chatFill(src.authRequired),
      conversations: _chatFill(src.conversations), newConversation: _chatFill(src.newConversation), rename: _chatFill(src.rename),
      archive: _chatFill(src.archive), archiveConfirm: _chatFill(src.archiveConfirm), cancel: _chatFill(src.cancel)
    };
  }
  function _chatUnique(values) { return Array.from(new Set(values)); }

  // The chart option for a result's hint, or null when there is no hint or the
  // hint does not fit the result: the type must be known, x / every y / series
  // must name a column, y must be non-empty and there must be rows. A hint with
  // aggregate 'count' draws the number of rows per x (and series; y is
  // ignored). Nulls as in EChartsVisual: x is bucketed as String(v ?? ''), a
  // null or '' series is dropped, a pie skips a null category. It counts rows,
  // not numeric values as EChartsVisual does. Any other aggregate, or count on
  // a scatter, draws nothing.
  var _CHAT_CHART_TYPES = ['bar', 'line', 'area', 'pie', 'scatter'];
  function _chatCountOption(c, rows, columns) {
    var key = function(v) { return String(v == null ? '' : v); };
    var xs = _chatUnique(rows.map(function(x) { return key(x[c.x]); }));
    var count = function(list, v) { var n = 0; list.forEach(function(x) { if (key(x[c.x]) === v) n++; }); return n; };
    if (c.type === 'pie') {
      var named = rows.filter(function(x) { return x[c.x] != null; });
      return { tooltip: {}, series: [{ type: 'pie', data: _chatUnique(named.map(function(x) { return key(x[c.x]); })).map(function(v) { return { name: v, value: count(named, v) }; }) }] };
    }
    var t = c.type === 'area' ? 'line' : c.type;
    var lines = c.series
      ? _chatUnique(rows.map(function(x) { return key(x[c.series]); })).filter(Boolean).map(function(g) { return { name: g, rows: rows.filter(function(x) { return key(x[c.series]) === g; }) }; })
      : [{ name: (columns.find(function(k) { return k.field === c.y[0]; }) || {}).headerName || c.y[0], rows: rows }];
    return { tooltip: { trigger: 'axis' }, legend: {}, xAxis: { type: 'category', data: xs }, yAxis: { type: 'value', minInterval: 1 },
      series: lines.map(function(l) {
        return { name: l.name, type: t, areaStyle: c.type === 'area' ? {} : undefined,
          data: xs.map(function(v) { var n = count(l.rows, v); return n ? n : null; }) };
      }) };
  }
  function _chatResultOption(r) {
    if (!r || typeof r !== 'object') return null;
    var c = r.chart;
    if (!c || typeof c !== 'object' || _CHAT_CHART_TYPES.indexOf(c.type) === -1) return null;
    var columns = Array.isArray(r.columns) ? r.columns.filter(function(k) { return k && typeof k === 'object'; }) : [];
    var fields = {};
    columns.forEach(function(k) { if (typeof k.field === 'string') fields[k.field] = true; });
    if (!Array.isArray(c.y) || !c.y.length || !fields[c.x]) return null;
    for (var i = 0; i < c.y.length; i++) if (!fields[c.y[i]]) return null;
    if (c.series && !fields[c.series]) return null;
    var rows = Array.isArray(r.rows) ? r.rows.filter(function(x) { return x && typeof x === 'object'; }) : [];
    if (!rows.length) return null;
    if (c.aggregate != null) return c.aggregate === 'count' && c.type !== 'scatter' ? _chatCountOption(c, rows, columns) : null;
    if (c.type === 'pie') return { tooltip: {}, series: [{ type: 'pie', data: rows.map(function(x) { return { name: String(x[c.x]), value: x[c.y[0]] }; }) }] };
    if (c.type === 'scatter') return { tooltip: {}, xAxis: { type: 'value' }, yAxis: { type: 'value' }, series: [{ type: 'scatter', data: rows.map(function(x) { return [x[c.x], x[c.y[0]]]; }) }] };
    var xs = _chatUnique(rows.map(function(x) { return x[c.x]; }));
    var t = c.type === 'area' ? 'line' : c.type;
    var lines = c.series
      ? _chatUnique(rows.map(function(x) { return x[c.series]; })).map(function(g) { return { name: String(g), col: c.y[0], rows: rows.filter(function(x) { return x[c.series] === g; }) }; })
      : c.y.map(function(col) { return { name: (columns.find(function(k) { return k.field === col; }) || {}).headerName || col, col: col, rows: rows }; });
    return { tooltip: { trigger: 'axis' }, legend: {}, xAxis: { type: 'category', data: xs }, yAxis: { type: 'value' },
      series: lines.map(function(l) {
        return { name: l.name, type: t, areaStyle: c.type === 'area' ? {} : undefined,
          data: xs.map(function(v) { var row = l.rows.find(function(x) { return x[c.x] === v; }); return row ? row[l.col] : null; }) };
      }) };
  }

  // The truncation note: rows (and columns when they differ) shown of the
  // total, or "partial table" when only long cells were cut.
  function _chatResultNote(r, L) {
    if (!r || typeof r !== 'object' || !r.truncated) return null;
    L = L || _chatLabels();
    var rows = Array.isArray(r.rows) ? r.rows.length : 0;
    var columns = Array.isArray(r.columns) ? r.columns.length : 0;
    var rowCount = typeof r.row_count === 'number' ? r.row_count : rows;
    var columnCount = typeof r.column_count === 'number' ? r.column_count : columns;
    if (rows < rowCount || columns < columnCount) {
      var note = L.rowsShown({ shown: rows, total: rowCount });
      if (columns !== columnCount) note += ', ' + L.columnsShown({ shown: columns, total: columnCount });
      return note;
    }
    return L.partial;
  }

  // <ChatResult result={r} height={260} pageSize={10} labels={...} className />
  var _CHAT_NO_ROWS = [];
  var ChatResult = React.memo(function ChatResult(props) {
    props = props || {};
    var r = props.result;
    var ok = !!r && typeof r === 'object';
    var option = React.useMemo(function() { return ok ? _chatResultOption(r) : null; }, [r]);
    var columns = React.useMemo(function() {
      if (!ok || !Array.isArray(r.columns)) return [];
      return r.columns.filter(function(k) { return k && typeof k === 'object'; }).map(function(k) { return { field: k.field, headerName: k.headerName, dtype: k.type }; });
    }, [r]);
    var rows = ok && Array.isArray(r.rows) ? r.rows : _CHAT_NO_ROWS;
    if (!ok) return null;
    var note = _chatResultNote(r, _chatLabels(props.labels));
    var title = typeof r.title === 'string' || typeof r.title === 'number' ? String(r.title) : '';
    return h('div', { 'data-testid': 'bow-chat-result', className: _chatCls(CHAT.result, props.className) }, [
      title ? h('div', { key: 't', dir: 'auto', 'data-testid': 'bow-chat-result-title', className: CHAT.title }, title) : null,
      option ? h(_EChart, { key: 'c', option: option, height: props.height || 260 }) : null,
      h(_DataTable, { key: 'd', rows: rows, columns: columns, pageSize: props.pageSize != null ? props.pageSize : 10, density: 'compact' }),
      note ? h('p', { key: 'n', dir: 'auto', 'data-testid': 'bow-chat-result-note', className: CHAT.note }, note) : null
    ]);
  });
  window.ChatResult = ChatResult;

  // Every text block gets its own direction (a Hebrew paragraph next to an
  // English one); code stays left-to-right.
  var _CHAT_DIR_AUTO = { p: 1, h1: 1, h2: 1, h3: 1, h4: 1, h5: 1, h6: 1, ul: 1, ol: 1, li: 1, table: 1, th: 1, td: 1, blockquote: 1 };
  function _chatWithDir(node) {
    if (Array.isArray(node)) return node.map(_chatWithDir);
    if (!React.isValidElement(node)) return node;
    var extra = null;
    if (typeof node.type === 'string') {
      if (_CHAT_DIR_AUTO[node.type]) extra = { dir: 'auto' };
      else if (node.type === 'pre') extra = { dir: 'ltr' };
    }
    var kids = node.props ? node.props.children : undefined;
    if (kids === undefined) return extra ? React.cloneElement(node, extra) : node;
    // Children are passed back the way they came (separate arguments), so the
    // walk adds no key requirements of its own.
    var args = [node, extra].concat(Array.isArray(kids) ? kids.map(_chatWithDir) : [_chatWithDir(kids)]);
    return React.cloneElement.apply(React, args);
  }
  // Markdown through bow.markdown, with a direction per block; null without it.
  function _chatMarkdown(text) {
    var bw = window.bow;
    if (!text || !bw || typeof bw.markdown !== 'function') return null;
    try { return _chatWithDir(bw.markdown(text)); } catch (e) { return null; }
  }
  // The direction of a text: its first strong character (Hebrew, Arabic, Syriac
  // as in _RTL_CHARS -> 'rtl'; Latin, Greek, Cyrillic -> 'ltr'), or null when it
  // has none (digits, punctuation, Markdown marks).
  var _CHAT_STRONG = new RegExp(_RTL_CHARS.source + '|[A-Za-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u024F\\u1E00-\\u1EFF\\u0370-\\u03FF\\u0400-\\u04FF]');
  var _CHAT_RTL_ONE = new RegExp(_RTL_CHARS.source);
  function _chatDirOf(text) {
    var m = _CHAT_STRONG.exec(text == null ? '' : String(text));
    return m ? (_CHAT_RTL_ONE.test(m[0]) ? 'rtl' : 'ltr') : null;
  }
  function _chatLabelOf(v) { return typeof v === 'string' || typeof v === 'number' ? String(v) : ''; }
  var _CHAT_RUNNING = { running: 1, in_progress: 1, started: 1 };
  var _CHAT_SUCCESS = { success: 1, completed: 1, done: 1 };
  var _CHAT_FAILED = { error: 1, stopped: 1, cancelled: 1, canceled: 1, failed: 1 };
  var CHAT_MSG = LEGACY ? {
    root: 'space-y-2',
    user: 'ms-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100',
    text: 'text-slate-800 dark:text-slate-100',
    tools: 'space-y-1 text-xs text-slate-500 dark:text-slate-400',
    steps: 'text-xs text-slate-500 dark:text-slate-400',
    muted: 'flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400'
  } : {
    root: 'space-y-2',
    user: 'ms-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-card px-3 py-2 bg-surface-2 text-ink',
    text: 'text-ink',
    tools: 'space-y-1 text-xs text-ink-3',
    steps: 'text-xs text-ink-3',
    muted: 'flex items-center gap-2 text-xs text-ink-3'
  };

  // <ChatMessage message={m} showTools labels={...} className />
  var ChatMessage = React.memo(function ChatMessage(props) {
    props = props || {};
    var m = props.message;
    var ok = !!m && typeof m === 'object';
    var role = ok && m.role === 'user' ? 'user' : 'assistant';
    var text = ok && m.text != null ? String(m.text) : '';
    var body = React.useMemo(function() {
      return !ok || role === 'user' ? null : _chatMarkdown(text);
    }, [ok, role, text]);
    // The agent's narration before the answer (messages() `steps`).
    var stepsIn = ok && role !== 'user' && Array.isArray(m.steps) ? m.steps : null;
    var steps = React.useMemo(function() {
      return (stepsIn || []).filter(function(x) { return typeof x === 'string' && x.trim(); });
    }, [stepsIn]);
    if (!ok) return null;
    var L = _chatLabels(props.labels);
    var status = m.status == null ? '' : String(m.status);
    var running = status === 'in_progress';
    var rootProps = { 'data-testid': 'bow-chat-message', 'data-role': role, 'data-status': status, 'aria-busy': running, className: _chatCls(CHAT_MSG.root, props.className) };
    if (role === 'user') {
      return h('div', rootProps, h('p', { dir: 'auto', 'data-testid': 'bow-chat-text', className: CHAT_MSG.user }, text));
    }
    // The reply's own direction places the steps toggle, the tools, the markers
    // and the indicator on its start side; each text block keeps dir="auto".
    var dir = _chatDirOf(text) || _chatDirOf(steps.join('\n'));
    if (dir) rootProps.dir = dir;
    var kids = [];
    if (steps.length) {
      // Collapsed: the answer stays the message; the steps are there on request.
      kids.push(h('details', { key: 'steps', 'data-testid': 'bow-chat-steps', className: CHAT_MSG.steps }, [
        h('summary', { key: 's', className: 'cursor-pointer select-none' }, h('span', { dir: 'auto', 'data-testid': 'bow-chat-steps-label' }, L.steps)),
        h('div', { key: 'l', className: 'mt-1 space-y-1 border-s ps-2' }, steps.map(function(x, i) {
          return h('div', { key: i, dir: 'auto', 'data-testid': 'bow-chat-step' },
            _chatMarkdown(x) || h('p', { className: 'whitespace-pre-wrap' }, x));
        }))
      ]));
    }
    if (text) {
      kids.push(h('div', { key: 'text', 'data-testid': 'bow-chat-text', className: CHAT_MSG.text },
        body || h('p', { dir: 'auto', className: 'whitespace-pre-wrap' }, text)));
    }
    var tools = props.showTools === false || !Array.isArray(m.tools) ? [] : m.tools.filter(function(t) {
      return t && typeof t === 'object' && (_chatLabelOf(t.title) || _chatLabelOf(t.name));
    });
    if (tools.length) {
      kids.push(h('ul', { key: 'tools', 'data-testid': 'bow-chat-tools', className: CHAT_MSG.tools }, tools.map(function(t, i) {
        var s = String(t.status || '');
        var mark = _CHAT_RUNNING[s] ? h(_LoadingSpinner, { size: 12 })
          : _CHAT_SUCCESS[s] ? h(_Icon, { name: 'check', size: 12 })
          : _CHAT_FAILED[s] ? h(_Icon, { name: 'x', size: 12 }) : null;
        return h('li', { key: t.id != null ? 'id-' + t.id : 'i-' + i, dir: 'auto', 'data-testid': 'bow-chat-tool', 'data-status': s, className: 'flex items-center gap-1.5' },
          [mark ? h('span', { key: 'm', className: 'inline-flex shrink-0' }, mark) : null, h('span', { key: 'l' }, _chatLabelOf(t.title) || _chatLabelOf(t.name))]);
      })));
    }
    var results = Array.isArray(m.results) ? m.results : [];
    results.forEach(function(r, i) {
      kids.push(h(ChatResult, { key: 'r-' + (r && r.id != null ? r.id : i), result: r, labels: props.labels }));
    });
    if (m.results_incomplete) {
      kids.push(h('p', { key: 'inc', dir: 'auto', 'data-testid': 'bow-chat-incomplete', className: CHAT.note }, L.incomplete));
    }
    // A failed or stopped answer says so (also with no text, as history loads it).
    var ended = status === 'error' ? L.failed : status === 'stopped' ? L.stopped : '';
    if (ended) {
      kids.push(h('p', { key: 'ended', dir: 'auto', 'data-testid': 'bow-chat-ended', 'data-status': status, className: CHAT.note }, ended));
    }
    if (running) {
      kids.push(h('div', { key: 'busy', 'data-testid': 'bow-chat-typing', role: 'status', className: CHAT_MSG.muted },
        [h(_LoadingSpinner, { key: 's', size: 16 }), h('span', { key: 't', dir: 'auto' }, L.inProgress)]));
    }
    return h('div', rootProps, kids);
  });
  window.ChatMessage = ChatMessage;

  // ── useConversation ─────────────────────────────────────────────────────────
  // const chat = useConversation({conversationId, title, agents, historyLimit})
  // -> {messages, conversations, conversationId, policy, loading, sending,
  //     answering, error, hasOlder, loadingOlder, send, stop, select,
  //     newConversation, reload, loadOlder, rename, archive}
  // conversations: [{id, title, created_at, updated_at, in_progress}]; rename
  // and archive are optimistic and revert on failure. Opened without a
  // conversationId, it reopens the newest conversation still answering.
  // messages have the bow.conversations.messages() item shape. One answer runs
  // at a time; history is re-read after each answer that reached the server and
  // merged with the server page as the truth; leaving a running answer keeps it
  // running on the server (only stop() stops it). `answering` is `sending` or
  // an answer of the open conversation still in progress (also one left
  // running and come back to); stop() stops either. loadOlder() puts the page
  // before the oldest loaded one first; only conversation loads and loadOlder()
  // move its cursor.
  var _CHAT_REFRESH_MS = 5000;
  var _CHAT_REFRESH_MAX = 24;
  var _CHAT_INITIAL = { messages: [], conversations: [], conversationId: null, policy: null, loading: false, sending: false, error: null, hasOlder: false, loadingOlder: false };

  function _chatConv() { var b = window.bow; return b && b.conversations ? b.conversations : null; }
  function _chatError(e, fallback) {
    return { code: (e && e.code) || fallback || 'UNAVAILABLE', message: (e && e.message) || String(e || 'Request failed') };
  }
  function _chatItems(res) { return res && Array.isArray(res.items) ? res.items : []; }
  function _chatCursor(res) { return res && res.next_before != null ? res.next_before : null; }
  // Sending, or an answer of the open conversation is still in progress.
  function _chatAnswering(state) {
    return !!state.sending || state.messages.some(function(m) { return m && m.role === 'assistant' && m.status === 'in_progress'; });
  }
  function _chatIsLocal(m) { return !!m && typeof m.id === 'string' && m.id.indexOf('local-') === 0; }
  function _chatNormalize(m) {
    if (!m || typeof m !== 'object') return null;
    return { id: m.id, role: m.role, text: m.text == null ? '' : String(m.text), status: m.status,
      created_at: m.created_at != null ? m.created_at : null,
      steps: Array.isArray(m.steps) ? m.steps : [],
      tools: Array.isArray(m.tools) ? m.tools : [], results: Array.isArray(m.results) ? m.results : [],
      results_incomplete: !!m.results_incomplete };
  }
  function _chatLen(v) { return Array.isArray(v) ? v.length : -1; }
  function _chatSameResult(a, b) {
    return !!a && !!b && a.id === b.id && _chatLen(a.rows) === _chatLen(b.rows) && _chatLen(a.columns) === _chatLen(b.columns) &&
      !!a.truncated === !!b.truncated && !!a.chart === !!b.chart;
  }
  function _chatHas(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  // The fetched page is the truth (D-3). Optimistic local-* items it covers are
  // dropped: (a) a local user item with the same trimmed text as an unclaimed
  // fetched user item (one-to-one, newest first), (b) any local item followed by
  // a local item the page holds, (c) a local assistant right after a user item
  // dropped by (a). Kept server items before the first covered item go before
  // the page; every other kept item after it. A fetched in_progress never
  // replaces a status in `held` (the run's own completed or stop). Result
  // objects of the same id and shape are kept, so charts do not reset.
  function _chatMerge(local, fetched, held) {
    local = Array.isArray(local) ? local : [];
    held = held || {};
    var page = (Array.isArray(fetched) ? fetched : []).map(_chatNormalize).filter(Boolean);
    var inPage = {}, localById = {};
    page.forEach(function(f) { inPage[f.id] = true; });
    local.forEach(function(l) { if (l && !_chatIsLocal(l)) localById[l.id] = l; });
    var covered = local.map(function(l) { return !!l && inPage[l.id] === true; });
    // (a)
    var claimed = {}, dropA = {};
    page.forEach(function(f, j) { if (f.role === 'user' && localById[f.id]) claimed[j] = true; });
    for (var i = local.length - 1; i >= 0; i--) {
      var l = local[i];
      if (!_chatIsLocal(l) || l.role !== 'user') continue;
      var t = String(l.text == null ? '' : l.text).trim();
      for (var j = page.length - 1; j >= 0; j--) {
        if (claimed[j] || page[j].role !== 'user' || page[j].text.trim() !== t) continue;
        claimed[j] = true; dropA[i] = true; covered[i] = true;
        break;
      }
    }
    // (b) and (c)
    var laterInPage = false;
    for (var k = local.length - 1; k >= 0; k--) {
      if (_chatIsLocal(local[k]) && laterInPage) covered[k] = true;
      if (local[k] && inPage[local[k].id] === true) laterInPage = true;
    }
    for (var n = 1; n < local.length; n++) {
      if (dropA[n - 1] && _chatIsLocal(local[n]) && local[n].role === 'assistant') covered[n] = true;
    }
    var firstCovered = covered.indexOf(true);
    if (firstCovered === -1) firstCovered = local.length;
    var before = [], after = [];
    local.forEach(function(l, idx) {
      if (!l || covered[idx]) return;
      if (idx < firstCovered && !_chatIsLocal(l)) before.push(l); else after.push(l);
    });
    var merged = page.map(function(f) {
      var l = localById[f.id];
      if (f.status === 'in_progress' && _chatHas(held, f.id)) {
        if (l) return l;
        var kept = {}; for (var key in f) kept[key] = f[key];
        kept.status = held[f.id];
        return kept;
      }
      if (l && l.results.length && f.results.length) {
        var reused = f.results.map(function(fr) {
          var lr = l.results.find(function(x) { return x && fr && x.id === fr.id; });
          return lr && _chatSameResult(lr, fr) ? lr : fr;
        });
        var out = {}; for (var k2 in f) out[k2] = f[k2];
        out.results = reused;
        return out;
      }
      return f;
    });
    return before.concat(merged, after);
  }

  // An older page goes before every current message; ids already present are
  // dropped. The same array when nothing is added.
  function _chatPrepend(messages, fetched) {
    var have = {};
    messages.forEach(function(m) { if (m) have[m.id] = true; });
    var add = (Array.isArray(fetched) ? fetched : []).map(_chatNormalize).filter(function(m) {
      if (!m || have[m.id]) return false;
      have[m.id] = true;
      return true;
    });
    return add.length ? add.concat(messages) : messages;
  }

  function _chatNormalizeConv(x) {
    return { id: x.id, title: x.title, created_at: x.created_at != null ? x.created_at : null,
      updated_at: x.updated_at != null ? x.updated_at : null, in_progress: !!x.in_progress };
  }
  // The same array when the item already has that flag (or is not listed).
  function _chatWithFlag(list, id, flag) {
    var at = list.findIndex(function(x) { return x && x.id === id; });
    if (at === -1 || list[at].in_progress === flag) return list;
    var out = list.slice(), item = {};
    for (var k in list[at]) item[k] = list[at][k];
    item.in_progress = flag;
    out[at] = item;
    return out;
  }
  // Post-step of every action (rule 21): the open conversation's flag is
  // derived from its own state; the same state when nothing changes.
  function _chatSyncCurrent(state) {
    if (state.conversationId == null) return state;
    var list = _chatWithFlag(state.conversations, state.conversationId, _chatAnswering(state));
    if (list === state.conversations) return state;
    var s = {}; for (var k in state) s[k] = state[k];
    s.conversations = list;
    return s;
  }
  function _chatReduce(state, a) { return _chatSyncCurrent(_chatStep(state, a)); }

  function _chatStep(state, a) {
    switch (a.type) {
      case 'apply': {
        var next = {}; for (var k in state) next[k] = state[k];
        if (a.patch) for (var p in a.patch) next[p] = a.patch[p];
        if (a.items) {
          next.messages = next.messages.map(function(m) {
            if (!m || !_chatHas(a.items, m.id)) return m;
            var u = {}; for (var mk in m) u[mk] = m[mk];
            var patch = a.items[m.id];
            for (var pk in patch) u[pk] = patch[pk];
            return u;
          });
        }
        if (a.append) next.messages = next.messages.concat(a.append);
        return next;
      }
      case 'created': {
        var c = _chatNormalizeConv(a.conversation);
        var listed = state.conversations.some(function(x) { return x && x.id === c.id; });
        var s1 = {}; for (var k1 in state) s1[k1] = state[k1];
        if (!listed) s1.conversations = [c].concat(state.conversations);
        s1.conversationId = c.id;
        return s1;
      }
      case 'listed': {
        // Rule 25: keep created ids the page does not hold yet; never an
        // archived id; a pending rename's title wins.
        var archived = a.archived || {}, renames = a.renames || {}, fetchedIds = {};
        var page = a.conversations.filter(function(x) { return x && typeof x === 'object' && !_chatHas(archived, x.id); }).map(function(x) {
          var n = _chatNormalizeConv(x);
          if (_chatHas(renames, n.id)) n.title = renames[n.id];
          fetchedIds[n.id] = true;
          return n;
        });
        var kept = state.conversations.filter(function(x) { return x && _chatHas(a.keep || {}, x.id) && !fetchedIds[x.id] && !_chatHas(archived, x.id); });
        var s4 = {}; for (var k5 in state) s4[k5] = state[k5];
        s4.conversations = kept.concat(page);
        return s4;
      }
      case 'retitle': {
        var changed = false;
        var list = state.conversations.map(function(x) {
          if (!x || x.id !== a.id) return x;
          if (x.title === a.title && (a.updated_at == null || x.updated_at === a.updated_at)) return x;
          changed = true;
          var y = {}; for (var ky in x) y[ky] = x[ky];
          y.title = a.title;
          if (a.updated_at != null) y.updated_at = a.updated_at;
          return y;
        });
        if (!changed) return state;
        var s6 = {}; for (var k7 in state) s6[k7] = state[k7];
        s6.conversations = list;
        return s6;
      }
      case 'unlist': {
        var rest = state.conversations.filter(function(x) { return !x || x.id !== a.id; });
        if (rest.length === state.conversations.length) return state;
        var s7 = {}; for (var k8 in state) s7[k8] = state[k8];
        s7.conversations = rest;
        return s7;
      }
      case 'relist': {
        if (state.conversations.some(function(x) { return x && x.id === a.item.id; })) return state;
        var back = state.conversations.slice();
        back.splice(Math.min(a.index, back.length), 0, a.item);
        var s8 = {}; for (var k9 in state) s8[k9] = state[k9];
        s8.conversations = back;
        return s8;
      }
      case 'switch': {
        // Rule 22 (D-30): the left conversation keeps its flag when its answer
        // still runs (streaming when left, or a server answer in progress),
        // then the patch opens the next one; the post-step syncs only that one.
        var s9 = {}; for (var k10 in state) s9[k10] = state[k10];
        if (state.conversationId != null) {
          var still = !!a.wasStreaming || state.messages.some(function(m) {
            return m && m.role === 'assistant' && m.status === 'in_progress' && !_chatIsLocal(m);
          });
          s9.conversations = _chatWithFlag(state.conversations, state.conversationId, still);
        }
        for (var p9 in a.patch) s9[p9] = a.patch[p9];
        return s9;
      }
      case 'load': {
        var s2 = {}; for (var k3 in state) s2[k3] = state[k3];
        s2.messages = a.messages.map(_chatNormalize).filter(Boolean);
        if (a.patch) for (var p2 in a.patch) s2[p2] = a.patch[p2];
        return s2;
      }
      case 'prepend': {
        var s5 = {}; for (var k6 in state) s5[k6] = state[k6];
        s5.messages = _chatPrepend(state.messages, a.fetched);
        if (a.patch) for (var p5 in a.patch) s5[p5] = a.patch[p5];
        return s5;
      }
      case 'merge': {
        var s3 = {}; for (var k4 in state) s3[k4] = state[k4];
        s3.messages = _chatMerge(state.messages, a.fetched, a.held);
        return s3;
      }
      default: return state;
    }
  }

  function _chatInitialState(opts) {
    var s = {}; for (var k in _CHAT_INITIAL) s[k] = _CHAT_INITIAL[k];
    if (!_chatConv()) { s.error = { code: 'UNAVAILABLE', message: 'Conversations are unavailable here' }; return s; }
    s.loading = true;
    s.conversationId = opts && opts.conversationId ? opts.conversationId : null;
    return s;
  }

  // The per-hook controller: refs and callbacks, created once per mount.
  function _chatController(dispatch, opts) {
    var hl = opts && typeof opts.historyLimit === 'number' && opts.historyLimit > 0 ? Math.floor(opts.historyLimit) : 30;
    var c = {
      opts: opts || {}, initialId: opts && opts.conversationId ? opts.conversationId : null, limit: hl,
      run: null, gen: 0, mounted: false, load: null, policyWait: null, convId: null, policy: null, create: null,
      held: {}, readSeq: 0, readFloor: 0, timer: null, count: 0, seq: 0, createdIds: {},
      errSeq: 0, errShown: false, before: null, olderLoad: null, messages: [], conversations: [],
      renames: {}, renameSeq: 0, confirmed: {}, confirmedSeq: {}, archived: {}, listTimer: null, listCount: 0, olderErr: null, touched: false
    };
    // `errSeq` counts every applied change of `error` (set or clear);
    // `errShown` mirrors whether one is shown now.
    function put(action, gen) {
      if (!c.mounted || (gen !== undefined && gen !== c.gen)) return false;
      dispatch(action);
      if (action.patch && _chatHas(action.patch, 'error')) { c.errSeq++; c.errShown = !!action.patch.error; }
      return true;
    }
    function noop() {}
    function cancelTimer() { if (c.timer !== null) { clearTimeout(c.timer); c.timer = null; } }
    function raiseFloor() { c.readFloor = ++c.readSeq; }
    function endRun(run, patch) {
      if (c.run !== run) return;
      c.run = null;
      var p = { sending: false }; if (patch) for (var k in patch) p[k] = patch[k];
      put({ type: 'apply', patch: p }, run.gen);
    }

    // A tagged re-read (rule 12): applied only in its generation, when nothing
    // newer was applied or sent since it was issued, and while no other run is
    // active.
    // `onError` (reload only) receives a rejection that the same checks did not drop.
    function reread(convId, run, onError) {
      var conv = _chatConv();
      if (!conv || !convId) return Promise.resolve(false);
      var tag = ++c.readSeq, gen = c.gen;
      function current() { return gen === c.gen && tag > c.readFloor && (c.run === null || c.run === run) && c.mounted; }
      var skipped = {};
      return Promise.resolve().then(function() {
        // Left (or unmounted) before the request went out: send nothing.
        if (gen !== c.gen || !c.mounted) return skipped;
        return conv.messages(convId, { limit: c.limit });
      }).then(function(res) {
        if (res === skipped || !current()) return false;
        c.readFloor = tag;
        var held = {}; for (var k in c.held) held[k] = c.held[k];
        put({ type: 'merge', fetched: _chatItems(res), held: held }, gen);
        return true;
      }, function(e) {
        if (onError && current()) onError(e);
        return false;
      });
    }

    // A list page through the 'listed' rules (rule 25).
    // `issued` is c.renameSeq when the list request went out. A pending rename
    // always wins; a confirmed one wins over lists issued before it and is
    // dropped once a list issued after it applies. The list's titles are the
    // last saved ones, unless a rename confirmed after the list was issued.
    function listed(res, issued) {
      if (issued === undefined) issued = c.renameSeq;
      for (var r in c.renames) if (c.renames[r].confirmed && c.renames[r].seq <= issued) delete c.renames[r];
      _chatItems(res).forEach(function(x) {
        if (!x || x.id == null || (c.confirmedSeq[x.id] || 0) > issued) return;
        c.confirmed[x.id] = x.title;
        c.confirmedSeq[x.id] = 0;
      });
      var renames = {};
      for (var id in c.renames) renames[id] = c.renames[id].title;
      var archived = {}; for (var x in c.archived) archived[x] = true;
      put({ type: 'listed', conversations: _chatItems(res), keep: c.createdIds, archived: archived, renames: renames });
    }
    function cancelList() { if (c.listTimer !== null) { clearTimeout(c.listTimer); c.listTimer = null; } }

    function startLoad() {
      var conv = _chatConv();
      if (!conv) return;
      var gen = c.gen, id = c.initialId;
      c.count = 0;
      c.listCount = 0;
      if (id) c.convId = id;
      // The first applied error wins while it (or a later error) is still shown;
      // once select, newConversation or a send cleared it, a later one applies.
      // The policy and the list do not belong to one conversation, so their
      // errors apply in any generation while mounted; the initial history's
      // error only in its own generation.
      var reported = false;
      function report(e, g) {
        if (reported && c.errShown) return;
        if (put({ type: 'apply', patch: { error: _chatError(e) } }, g)) reported = true;
      }
      var policyWait = Promise.resolve().then(function() { return conv.policy(); }).then(function(p) {
        c.policy = p || null;
        put({ type: 'apply', patch: { policy: c.policy } });
        return c.policy;
      }, function(e) { report(e); throw e; });
      c.policyWait = policyWait;
      var load = policyWait.then(function(p) {
        if (p && p.available === false) return null;
        var issued;
        var tasks = [Promise.resolve().then(function() { issued = c.renameSeq; return conv.list({ limit: 20 }); }).then(function(res) {
          // Keep conversations created meanwhile that this page does not hold yet.
          listed(res, issued);
          // Rule 27 (OD-10): opened without a conversation and nothing done yet:
          // reopen the newest conversation whose answer still runs. select()
          // takes over `loading` (this load's own loading:false is dropped).
          if (id || gen !== c.gen || c.convId !== null || c.run !== null || c.touched) return;
          var running = _chatItems(res).find(function(x) { return x && x.in_progress === true && x.id != null; });
          if (running) c.select(running.id);
        }, function(e) { report(e); })];
        if (id) {
          tasks.push(Promise.resolve().then(function() { return conv.messages(id, { limit: c.limit }); }).then(function(res) {
            if (put({ type: 'load', messages: _chatItems(res), patch: { hasOlder: _chatCursor(res) !== null } }, gen)) c.before = _chatCursor(res);
          }, function(e) {
            if (gen === c.gen && e && e.code === 'NOT_FOUND' && c.convId === id) {
              c.convId = null;
              put({ type: 'apply', patch: { conversationId: null } }, gen);
            }
            report(e, gen);
          }));
        }
        return Promise.all(tasks);
      }).then(null, noop).then(function() {
        if (c.policyWait === policyWait) c.policyWait = null;
        if (c.load === load) c.load = null;
        put({ type: 'apply', patch: { loading: false } }, gen);
      });
      c.load = load;
    }

    // Leave the active run (rule 8, MR-11): it never dispatches again and only
    // its local stream is aborted. The answer keeps running on the server
    // (only stop() stops it); coming back loads it in progress (rule 13).
    // Returns whether the run was streaming (its answer keeps running).
    function leave() {
      var run = c.run;
      var wasStreaming = !!run && run.phase === 'streaming' && !run.terminal;
      if (run) {
        if (!run.terminal) run.terminal = 'left';
        c.run = null;
        run.controller.abort();
      }
      c.gen++;
      return wasStreaming;
    }

    function serverStop(run) {
      if (run.stopSent || !run.messageId || !run.convId) return;
      run.stopSent = true;
      var conv = _chatConv();
      try { if (conv) conv.stop(run.convId, run.messageId).then(noop, noop); } catch (e) { /* the abort below still ends the stream */ }
      // After the stop request is posted, so the host sees the stop first.
      Promise.resolve().then(noop).then(function() { run.controller.abort(); });
    }

    function applyEvent(run, ev) {
      if (!ev || typeof ev !== 'object') return;
      if (run.terminal) {
        if (ev.type === 'started' && run.stopWanted && !run.messageId && ev.message_id != null) {
          run.messageId = String(ev.message_id);
          if (run.terminal === 'stopped' && run.gen === c.gen) c.held[run.messageId] = 'stopped';
          serverStop(run);
        }
        return;
      }
      var items = {};
      switch (ev.type) {
        case 'started':
          if (run.messageId || ev.message_id == null) return;
          run.messageId = String(ev.message_id);
          items[run.userLocalId] = { status: 'success' };
          if (run.assistantId === run.assistantLocalId) {
            items[run.assistantLocalId] = { id: run.messageId };
            run.assistantId = run.messageId;
          }
          run.started = true;
          break;
        case 'text_delta':
        case 'text': {
          var block = ev.block == null ? '_' : String(ev.block);
          if (!_chatHas(run.blocks, block)) { run.order.push(block); run.blocks[block] = ''; }
          var piece = ev.text == null ? '' : String(ev.text);
          run.blocks[block] = ev.type === 'text' ? piece : run.blocks[block] + piece;
          // As messages() reads it back: the last block with text is the
          // answer, the earlier ones its steps (so nothing moves on the re-read).
          var said = run.order.map(function(b) { return run.blocks[b]; }).filter(Boolean);
          items[run.assistantId] = { text: said.length ? said[said.length - 1] : '', steps: said.slice(0, -1) };
          break;
        }
        case 'tool': {
          var tool = { name: ev.name, title: ev.title, status: ev.status };
          if (ev.id != null) tool = { id: ev.id, name: ev.name, title: ev.title, status: ev.status };
          var at = ev.id != null ? run.tools.findIndex(function(t) { return t.id === ev.id; }) : -1;
          run.tools = run.tools.slice();
          if (at >= 0) run.tools[at] = tool; else run.tools.push(tool);
          items[run.assistantId] = { tools: run.tools };
          break;
        }
        case 'result': {
          var r = {}; for (var k in ev) if (k !== 'type') r[k] = ev[k];
          if (r.id != null && run.results.some(function(x) { return x.id === r.id; })) return;
          run.results = run.results.concat([r]);
          items[run.assistantId] = { results: run.results };
          break;
        }
        case 'completed': {
          var status = ev.status || 'success';
          run.terminal = 'completed';
          c.held[run.assistantId] = status;
          items[run.assistantId] = { status: status, results_incomplete: !!ev.results_incomplete };
          break;
        }
        default: return;
      }
      put({ type: 'apply', items: items }, run.gen);
    }

    function failRun(run, e, code) {
      run.terminal = 'error';
      var items = {};
      items[run.assistantId] = { status: 'error' };
      items[run.userLocalId] = { status: run.started ? 'success' : 'error' };
      put({ type: 'apply', items: items, patch: { error: code ? { code: code, message: 'The answer ended early' } : _chatError(e) } }, run.gen);
    }

    function startCreate(run, text) {
      var conv = _chatConv(), o = c.opts || {};
      var rec = { gen: run.gen, promise: null };
      rec.promise = Promise.resolve().then(function() {
        return conv.create({ title: o.title || text.slice(0, 80), agents: o.agents });
      }).then(function(created) {
        if (rec.gen === c.gen && c.mounted && created && created.id != null) {
          c.convId = created.id;
          c.createdIds[created.id] = true;
          c.confirmed[created.id] = created.title;
          c.confirmedSeq[created.id] = 0;
          dispatch({ type: 'created', conversation: created });
        }
        return created;
      });
      c.create = rec;
      function clear() { if (c.create === rec) c.create = null; }
      rec.promise.then(clear, clear);
      return rec;
    }

    async function runSend(run, text) {
      var waits = [c.load, c.policyWait].filter(Boolean);
      if (waits.length) await Promise.all(waits).then(noop, noop);
      if (run.gen !== c.gen || run.terminal) return;
      if (c.policy && c.policy.available === false) {
        endRun(run, { error: { code: 'FORBIDDEN', message: c.policy.reason || 'not available' } });
        return;
      }
      var conv = _chatConv();
      var user = { id: run.userLocalId, role: 'user', text: text, status: 'pending', created_at: null, steps: [], tools: [], results: [], results_incomplete: false };
      var assistant = { id: run.assistantLocalId, role: 'assistant', text: '', status: 'in_progress', created_at: null, steps: [], tools: [], results: [], results_incomplete: false };
      put({ type: 'apply', append: [user, assistant] }, run.gen);
      var convId = c.convId;
      if (!convId) {
        run.phase = 'creating';
        var created = null;
        var pending = c.create && c.create.gen === c.gen ? c.create : null;
        if (pending) created = await pending.promise.then(function(v) { return v; }, function() { return null; });
        if (!created && !run.terminal) {
          var rec = startCreate(run, text);
          try { created = await rec.promise; } catch (e) {
            if (!run.terminal && c.run === run) {
              run.terminal = 'error';
              var failed = {};
              failed[run.userLocalId] = { status: 'error' };
              failed[run.assistantLocalId] = { status: 'error' };
              put({ type: 'apply', items: failed }, run.gen);
              endRun(run, { error: _chatError(e) });
            }
            return;
          }
        }
        if (!created || created.id == null) {
          // An adopted create failed after this run was stopped or left, or a
          // create resolved without an id.
          if (!run.terminal && c.run === run) { failRun(run, { code: 'UNAVAILABLE', message: 'The conversation could not be created' }); endRun(run); }
          return;
        }
        convId = created.id;
      }
      run.convId = convId;
      if (run.terminal) return;
      run.phase = 'streaming';
      var error = null;
      try {
        var stream = conv.send(convId, text, { signal: run.controller.signal });
        for await (var ev of stream) applyEvent(run, ev);
      } catch (e) { error = e; }
      var ours = run.controller.signal.aborted;
      // Left or unmounted (also after `completed`): no re-read, no dispatch.
      if (run.terminal === 'left' || run.gen !== c.gen || !c.mounted) return;
      if (run.terminal === 'stopped') {
        // The stopped run's own re-read, when nothing replaced it meanwhile.
        if (run.gen === c.gen && c.run === null) await reread(convId, run);
        return;
      }
      if (error && !ours && !run.terminal) {
        failRun(run, error);
        if (run.messageId) await reread(convId, run);
        endRun(run);
        return;
      }
      if (!run.terminal) failRun(run, null, 'INTERRUPTED');
      run.phase = 'rereading';
      await reread(convId, run);
      run.phase = 'done';
      endRun(run);
    }

    c.send = function(text) {
      c.touched = true;
      var t = text == null ? '' : String(text).trim();
      if (!t || c.run || !c.mounted) return Promise.resolve();
      if (!_chatConv()) {
        put({ type: 'apply', patch: { error: { code: 'UNAVAILABLE', message: 'Conversations are unavailable here' } } });
        return Promise.resolve();
      }
      if (c.policy && c.policy.available === false) {
        put({ type: 'apply', patch: { error: { code: 'FORBIDDEN', message: c.policy.reason || 'not available' } } });
        return Promise.resolve();
      }
      var run = { gen: c.gen, convId: null, messageId: null, controller: new AbortController(), phase: 'queued',
        terminal: null, stopWanted: false, stopSent: false, started: false,
        userLocalId: 'local-' + (++c.seq), assistantLocalId: 'local-' + (++c.seq), assistantId: null,
        blocks: {}, order: [], tools: [], results: [] };
      run.assistantId = run.assistantLocalId;
      c.run = run;
      cancelTimer();
      c.count = 0;
      raiseFloor();
      put({ type: 'apply', patch: { sending: true, error: null } });
      // Whatever happens inside, the run ends (never leaves `sending` stuck).
      return runSend(run, t).then(noop, noop).then(function() { endRun(run); });
    };

    // Rule 26: no run of its own, but the open conversation's newest server
    // answer is still in progress (left running and come back to): stop that
    // one by its id. It shows 'stopped' at once (held against a lagging
    // re-read); a tagged re-read after the stop brings the server's status.
    function stopReturned() {
      var conv = _chatConv(), id = c.convId, target = null;
      for (var i = c.messages.length - 1; i >= 0; i--) {
        var m = c.messages[i];
        if (m && m.role === 'assistant' && m.status === 'in_progress' && m.id != null && !_chatIsLocal(m) && c.held[m.id] !== 'stopped') { target = m; break; }
      }
      if (!conv || !id || !target || !c.mounted) return Promise.resolve();
      var gen = c.gen, items = {};
      c.held[target.id] = 'stopped';
      cancelTimer();
      items[target.id] = { status: 'stopped' };
      put({ type: 'apply', items: items });
      // A stop that did not reach the server shows: the answer is no longer
      // held stopped (the re-read brings it back in progress and the refresh
      // resumes) and `error` says why.
      return Promise.resolve().then(function() { return conv.stop(id, target.id); }).then(noop, function(e) {
        if (gen !== c.gen) return;
        if (c.held[target.id] === 'stopped') delete c.held[target.id];
        put({ type: 'apply', patch: { error: _chatError(e) } }, gen);
      }).then(function() {
        if (gen === c.gen && c.run === null) return reread(id, null);
      }).then(noop, noop);
    }

    c.stop = function() {
      var run = c.run;
      if (!run) return stopReturned();
      if (run.terminal || (run.phase !== 'queued' && run.phase !== 'creating' && run.phase !== 'streaming')) return Promise.resolve();
      run.terminal = 'stopped';
      run.stopWanted = true;
      c.run = null;
      var items = {};
      if (run.phase === 'creating') {
        items[run.userLocalId] = { status: 'stopped' };
        items[run.assistantId] = { status: 'stopped' };
      } else if (run.phase === 'streaming') {
        items[run.assistantId] = { status: 'stopped' };
        if (!run.started) items[run.userLocalId] = { status: 'success' };
      }
      put({ type: 'apply', items: items, patch: { sending: false } }, run.gen);
      if (run.phase === 'streaming' && run.messageId) {
        c.held[run.messageId] = 'stopped';
        serverStop(run);
      }
      return Promise.resolve();
    };

    function resetFor(convId) {
      var wasStreaming = leave();
      cancelTimer();
      c.count = 0;
      c.listCount = 0;
      c.held = {};
      raiseFloor();
      c.convId = convId;
      c.before = null;
      c.olderLoad = null;
      c.olderErr = null;
      return wasStreaming;
    }

    c.select = function(id) {
      if (!c.mounted) return Promise.resolve();
      c.touched = true;
      var conv = _chatConv();
      if (!conv) { put({ type: 'apply', patch: { error: { code: 'UNAVAILABLE', message: 'Conversations are unavailable here' } } }); return Promise.resolve(); }
      var wasStreaming = resetFor(id);
      var gen = c.gen;
      put({ type: 'switch', wasStreaming: wasStreaming, patch: { sending: false, conversationId: id, messages: [], error: null, loading: true, hasOlder: false, loadingOlder: false } });
      var load = Promise.resolve().then(function() { return conv.messages(id, { limit: c.limit }); }).then(function(res) {
        if (put({ type: 'load', messages: _chatItems(res), patch: { hasOlder: _chatCursor(res) !== null } }, gen)) c.before = _chatCursor(res);
      }, function(e) {
        put({ type: 'apply', patch: { error: _chatError(e) } }, gen);
      }).then(function() {
        if (c.load === load) c.load = null;
        put({ type: 'apply', patch: { loading: false } }, gen);
      });
      c.load = load;
      return load;
    };

    c.newConversation = function() {
      if (!c.mounted) return;
      c.touched = true;
      var wasStreaming = resetFor(null);
      c.load = null;
      put({ type: 'switch', wasStreaming: wasStreaming, patch: { sending: false, conversationId: null, messages: [], error: null, loading: false, hasOlder: false, loadingOlder: false } });
    };

    // Rule 19: optimistic; the latest rename of an id wins; a failure reverts.
    c.rename = function(id, title) {
      var conv = _chatConv();
      var t = String(title == null ? '' : title).trim().slice(0, 200);
      if (!t || !id || !c.mounted || !conv) return Promise.resolve();
      var seq = ++c.renameSeq;
      c.renames[id] = { seq: seq, title: t };
      put({ type: 'retitle', id: id, title: t });
      return Promise.resolve().then(function() { return conv.rename(id, t); }).then(function(res) {
        var saved = res && res.title != null ? res.title : t;
        if (seq > (c.confirmedSeq[id] || 0)) { c.confirmed[id] = saved; c.confirmedSeq[id] = seq; }
        var entry = c.renames[id];
        // A later rename is still pending or confirmed: it decides the title.
        if (entry && entry.seq > seq) return;
        // This one, or a later one that failed and reverted: the saved title shows.
        if (!entry && c.confirmedSeq[id] !== seq) return;
        c.renames[id] = { seq: seq, title: saved, confirmed: true };
        put({ type: 'retitle', id: id, title: saved, updated_at: res && res.updated_at });
      }, function(e) {
        var entry = c.renames[id];
        if (!entry || entry.seq !== seq) return;
        // Back to the last saved title (a list's or a rename's), never an
        // earlier unsaved one; a saved rename keeps guarding against older lists.
        delete c.renames[id];
        if (_chatHas(c.confirmed, id)) {
          if (c.confirmedSeq[id] > 0) c.renames[id] = { seq: c.confirmedSeq[id], title: c.confirmed[id], confirmed: true };
          put({ type: 'retitle', id: id, title: c.confirmed[id] });
        }
        put({ type: 'apply', patch: { error: _chatError(e) } });
      }).then(noop, noop);
    };

    // Rule 20: optimistic removal (the open one becomes a new conversation
    // first); an archived id never comes back from a list; a failure restores.
    c.archive = function(id) {
      var conv = _chatConv();
      if (!id || !c.mounted || !conv || c.archived[id]) return Promise.resolve();
      c.touched = true;
      if (id === c.convId) c.newConversation();
      c.archived[id] = true;
      var index = c.conversations.findIndex(function(x) { return x && x.id === id; });
      var item = index === -1 ? null : c.conversations[index];
      put({ type: 'unlist', id: id });
      return Promise.resolve().then(function() { return conv.archive(id); }).then(noop, function(e) {
        delete c.archived[id];
        if (item) put({ type: 'relist', item: item, index: index });
        put({ type: 'apply', patch: { error: _chatError(e) } });
      }).then(noop, noop);
    };

    // Rule 23: while another conversation is in progress, re-read the list
    // every 5 s, at most _CHAT_REFRESH_MAX times per reset.
    c.checkList = function(conversations, conversationId) {
      if (!c.mounted || c.listTimer !== null || c.listCount >= _CHAT_REFRESH_MAX) return;
      if (!conversations.some(function(x) { return x && x.id !== conversationId && x.in_progress; })) return;
      c.listTimer = setTimeout(function() {
        c.listTimer = null;
        c.listCount += 1;
        var conv = _chatConv();
        if (!conv || !c.mounted) return;
        var issued = c.renameSeq;
        Promise.resolve().then(function() { return conv.list({ limit: 20 }); }).then(function(res) { listed(res, issued); }, noop);
      }, _CHAT_REFRESH_MS);
    };

    // Rule 17: the page before the oldest loaded one, put first. Generation
    // only (not a tagged re-read: it never replaces anything); allowed while
    // an answer runs.
    c.loadOlder = function() {
      var conv = _chatConv();
      if (!conv || !c.mounted || !c.convId || c.before === null || c.olderLoad !== null) return Promise.resolve();
      var gen = c.gen, id = c.convId, before = c.before;
      put({ type: 'apply', patch: { loadingOlder: true } });
      var load = Promise.resolve().then(function() { return conv.messages(id, { limit: c.limit, before: before }); }).then(function(res) {
        if (gen !== c.gen || !c.mounted) return;
        c.before = _chatCursor(res);
        var patch = { hasOlder: c.before !== null, loadingOlder: false };
        // Clears the error of a failed earlier attempt, when nothing changed
        // `error` since (errSeq, as reload does).
        if (c.olderErr !== null && c.olderErr === c.errSeq) patch.error = null;
        c.olderErr = null;
        put({ type: 'prepend', fetched: _chatItems(res), patch: patch }, gen);
      }, function(e) {
        if (put({ type: 'apply', patch: { error: _chatError(e), loadingOlder: false } }, gen)) c.olderErr = c.errSeq;
      }).then(function() {
        if (c.olderLoad === load) c.olderLoad = null;
      });
      c.olderLoad = load;
      return load;
    };

    c.reload = function() {
      var conv = _chatConv();
      if (!conv || !c.mounted) return Promise.resolve();
      // A failure sets `error` in this generation; a reload without one clears
      // it, unless anything else changed `error` while the reload was in flight.
      var gen = c.gen, failed = false, errSeq = c.errSeq;
      function fail(e) { failed = true; put({ type: 'apply', patch: { error: _chatError(e) } }, gen); }
      var issued = c.renameSeq;
      var tasks = [Promise.resolve().then(function() { return conv.list({ limit: 20 }); }).then(function(res) { listed(res, issued); }, fail)];
      if (c.convId && !c.run) tasks.push(reread(c.convId, null, fail));
      return Promise.all(tasks).then(function() {
        if (!failed && c.errSeq === errSeq) put({ type: 'apply', patch: { error: null } }, gen);
      }).then(noop, noop);
    };

    // Rule 13: re-read a conversation whose answer still runs on the server.
    c.check = function(messages) {
      if (!c.mounted || c.run || c.timer !== null || !c.convId || c.count >= _CHAT_REFRESH_MAX) return;
      if (!messages.some(function(m) { return m && m.role === 'assistant' && m.status === 'in_progress'; })) return;
      c.timer = setTimeout(function() {
        c.timer = null;
        c.count += 1;
        if (!c.run && c.convId) reread(c.convId, null);
      }, _CHAT_REFRESH_MS);
    };

    c.mount = function() {
      c.mounted = true;
      startLoad();
    };
    c.unmount = function() {
      leave();
      cancelTimer();
      cancelList();
      c.held = {};
      raiseFloor();
      c.mounted = false;
    };
    return c;
  }

  function useConversation(opts) {
    // React is read here, at render time (not captured when this file loads).
    var st = React.useReducer(_chatReduce, opts, _chatInitialState);
    var state = st[0], dispatch = st[1];
    var ref = React.useRef(null);
    if (!ref.current) ref.current = _chatController(dispatch, opts);
    var c = ref.current;
    c.opts = opts || {};
    c.messages = state.messages;
    c.conversations = state.conversations;
    React.useEffect(function() {
      c.mount();
      return c.unmount;
    }, []);
    React.useEffect(function() { c.check(state.messages); }, [state.messages, state.sending]);
    React.useEffect(function() { c.checkList(state.conversations, state.conversationId); }, [state.conversations, state.conversationId]);
    return {
      messages: state.messages, conversations: state.conversations, conversationId: state.conversationId,
      policy: state.policy, loading: state.loading, sending: state.sending, answering: _chatAnswering(state), error: state.error,
      hasOlder: state.hasOlder, loadingOlder: state.loadingOlder,
      send: c.send, stop: c.stop, select: c.select, newConversation: c.newConversation, reload: c.reload,
      loadOlder: c.loadOlder, rename: c.rename, archive: c.archive
    };
  }
  window.useConversation = useConversation;

  // ═══════════════════════════════════════════════════════════════════════════
  // Motion: usePresence, Modal, Drawer, Tabs
  // ═══════════════════════════════════════════════════════════════════════════
  // Hand-written `{open && <div className="fixed inset-0 …">}` cannot animate
  // out: the element is gone the moment `open` flips. These keep an overlay
  // mounted while it animates out, and keep visited tab panels mounted so the
  // charts inside are not rebuilt (and their intro replayed) on every switch.
  // Keyframes live in a stylesheet of their own, outside Tailwind, and the
  // `bow-motion` class honours prefers-reduced-motion (portals render outside
  // #root, so the base stylesheet's #root rule does not reach them).
  var MOTION_IN_MS = 220, MOTION_OUT_MS = 160;
  var EASE_OUT = 'cubic-bezier(0.16, 1, 0.3, 1)', EASE_IN = 'cubic-bezier(0.4, 0, 1, 1)';
  (function injectMotionStyles() {
    var st = document.createElement('style');
    st.id = 'bow-motion';
    st.textContent = [
      '@keyframes bow-fade-in { from { opacity: 0 } to { opacity: 1 } }',
      '@keyframes bow-fade-out { from { opacity: 1 } to { opacity: 0 } }',
      '@keyframes bow-pop-in { from { opacity: 0; transform: translateY(8px) scale(0.97) } to { opacity: 1; transform: none } }',
      '@keyframes bow-pop-out { from { opacity: 1; transform: none } to { opacity: 0; transform: translateY(4px) scale(0.98) } }',
      '@keyframes bow-slide-in-right { from { transform: translateX(100%) } to { transform: none } }',
      '@keyframes bow-slide-out-right { from { transform: none } to { transform: translateX(100%) } }',
      '@keyframes bow-slide-in-left { from { transform: translateX(-100%) } to { transform: none } }',
      '@keyframes bow-slide-out-left { from { transform: none } to { transform: translateX(-100%) } }',
      '@keyframes bow-slide-in-bottom { from { transform: translateY(100%) } to { transform: none } }',
      '@keyframes bow-slide-out-bottom { from { transform: none } to { transform: translateY(100%) } }',
      '@keyframes bow-panel-in { from { opacity: 0; transform: translateY(4px) } to { opacity: 1; transform: none } }',
      '.bow-tablist { scrollbar-width: none; }',
      '.bow-tablist::-webkit-scrollbar { display: none; }',
      '@media (prefers-reduced-motion: reduce) { .bow-motion, .bow-motion * { animation-duration: 1ms !important; transition-duration: 1ms !important; } }'
    ].join('\n');
    (document.head || document.documentElement).appendChild(st);
  })();

  function _reducedMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
  }
  function _anim(name, entering) {
    return name + ' ' + (entering ? MOTION_IN_MS : MOTION_OUT_MS) + 'ms ' + (entering ? EASE_OUT : EASE_IN) + ' both';
  }
  function _isRtl() {
    try { return getComputedStyle(document.documentElement).direction === 'rtl' || getComputedStyle(document.body).direction === 'rtl'; } catch (e) { return false; }
  }
  var _motionUid = 0;
  function _useUid(prefix) {
    var r = React.useRef(null);
    if (r.current === null) r.current = prefix + (++_motionUid);
    return r.current;
  }

  // usePresence(open, exitMs?) → { mounted, closing }. Render while `mounted`;
  // apply the exit style while `closing`. The element unmounts exitMs after
  // `open` turns false; reopening mid-exit cancels it.
  window.usePresence = function(open, exitMs) {
    var _s = React.useState(!!open), present = _s[0], setPresent = _s[1];
    var ms = exitMs == null ? MOTION_OUT_MS : exitMs;
    React.useEffect(function() {
      if (open) { setPresent(true); return; }
      var t = setTimeout(function() { setPresent(false); }, _reducedMotion() ? 0 : ms);
      return function() { clearTimeout(t); };
    }, [!!open, ms]);
    return { mounted: !!open || present, closing: !open && present };
  };

  // Open overlays, bottom → top. Escape and the focus trap act on the top one;
  // the page scroll lock lasts while any is open.
  var _overlayStack = [];
  var _savedOverflow = null;
  function _lockScroll() {
    if (_overlayStack.length !== 1) return;
    var de = document.documentElement;
    _savedOverflow = { html: de.style.overflow, body: document.body.style.overflow, gutter: de.style.scrollbarGutter };
    // Keep a classic scrollbar's space reserved, or the page shifts sideways
    // as the dialog opens.
    if (window.innerWidth > de.clientWidth) de.style.scrollbarGutter = 'stable';
    de.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
  }
  function _unlockScroll() {
    if (_overlayStack.length || !_savedOverflow) return;
    document.documentElement.style.overflow = _savedOverflow.html;
    document.body.style.overflow = _savedOverflow.body;
    document.documentElement.style.scrollbarGutter = _savedOverflow.gutter;
    _savedOverflow = null;
  }
  var _FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"]),[contenteditable="true"]';
  function _focusables(root) {
    return Array.prototype.filter.call(root.querySelectorAll(_FOCUSABLE), function(el) {
      return el.offsetParent !== null || el === document.activeElement;
    });
  }

  // Shared behaviour of Modal and Drawer while open: stack entry, scroll lock,
  // initial focus, Escape (top overlay only), Tab trap, focus restore.
  function _useOverlay(open, id, panelRef, props) {
    var onCloseRef = React.useRef(props.onClose);
    onCloseRef.current = props.onClose;
    var dismissable = props.closeOnEscape !== false;
    React.useEffect(function() {
      if (!open) return;
      var opener = document.activeElement;
      _overlayStack.push(id);
      _lockScroll();
      var raf = requestAnimationFrame(function() {
        var panel = panelRef.current;
        if (!panel || panel.contains(document.activeElement)) return;
        var target = panel.querySelector('[autofocus]') || panel.querySelector('[data-bow-autofocus]') || panel;
        try { target.focus({ preventScroll: true }); } catch (e) {}
      });
      function onKey(e) {
        if (_overlayStack[_overlayStack.length - 1] !== id) return;
        if (e.key === 'Escape' && dismissable) {
          e.stopPropagation();
          if (onCloseRef.current) onCloseRef.current();
        } else if (e.key === 'Tab') {
          var panel = panelRef.current;
          if (!panel) return;
          var items = _focusables(panel);
          if (!items.length) { e.preventDefault(); panel.focus(); return; }
          var first = items[0], last = items[items.length - 1], active = document.activeElement;
          if (e.shiftKey && (active === first || active === panel || !panel.contains(active))) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && (active === last || !panel.contains(active))) { e.preventDefault(); first.focus(); }
        }
      }
      document.addEventListener('keydown', onKey);
      return function() {
        cancelAnimationFrame(raf);
        document.removeEventListener('keydown', onKey);
        var i = _overlayStack.indexOf(id);
        if (i !== -1) _overlayStack.splice(i, 1);
        _unlockScroll();
        if (opener && opener.focus && document.contains(opener)) { try { opener.focus({ preventScroll: true }); } catch (e) {} }
      };
    }, [open, dismissable]);
  }

  function _overlayHeader(props, titleId, onClose) {
    var close = props.hideClose ? null : h('button', {
      key: 'x', type: 'button', 'aria-label': props.closeLabel || 'Close', onClick: onClose,
      className: 'shrink-0 -m-1 p-1 rounded-control text-ink-3 hover:text-ink hover:bg-surface-2 transition-colors'
    }, h(window.Icon, { name: 'x', size: 18 }));
    if (!props.title && !props.subtitle) {
      return close ? h('div', { key: 'hx', className: 'absolute top-3 end-3 z-10' }, close) : null;
    }
    return h('div', { key: 'hd', className: 'flex items-start gap-3 px-5 pt-4 pb-3 border-b border-line' }, [
      h('div', { key: 't', className: 'min-w-0 flex-1' }, [
        props.title ? h('h2', { key: 'h', id: titleId, className: 'text-base font-semibold text-ink leading-6' }, props.title) : null,
        props.subtitle ? h('p', { key: 's', className: 'mt-0.5 text-sm text-ink-3' }, props.subtitle) : null
      ]),
      close
    ]);
  }
  function _overlayBody(props) {
    var pad = { none: '', sm: 'px-4 py-3', md: 'px-5 py-4', lg: 'px-6 py-5' }[props.padding || 'md'];
    return h('div', { key: 'bd', className: _join('flex-1 min-h-0 overflow-auto', pad, props.bodyClassName) }, props.children);
  }
  function _overlayFooter(props) {
    if (!props.footer) return null;
    return h('div', { key: 'ft', className: 'flex flex-wrap items-center justify-end gap-2 px-5 py-3 border-t border-line' }, props.footer);
  }
  function _backdrop(closing, onClick) {
    return h('div', {
      key: 'bg', 'aria-hidden': 'true', onMouseDown: onClick,
      className: 'absolute inset-0 bg-black/40 dark:bg-black/60',
      style: { animation: _anim(closing ? 'bow-fade-out' : 'bow-fade-in', !closing) }
    });
  }

  // <Modal open onClose title subtitle footer size padding closeOnBackdrop closeOnEscape hideClose className>
  window.Modal = function(props) {
    props = props || {};
    var open = !!props.open;
    var p = window.usePresence(open);
    var panelRef = React.useRef(null);
    var id = _useUid('bow-modal-');
    _useOverlay(open, id, panelRef, props);
    if (!p.mounted) return null;
    var close = function() { if (props.onClose) props.onClose(); };
    var width = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-2xl', xl: 'max-w-4xl', full: 'max-w-none h-full' }[props.size || 'md'] || 'max-w-lg';
    // An explicit max-w-* in className wins over the size preset.
    if (/(^|\s)max-w-/.test(props.className || '')) width = '';
    var titleId = props.title ? id + '-title' : undefined;
    return ReactDOM.createPortal(
      h('div', {
        className: 'bow-motion fixed inset-0 flex items-center justify-center p-4',
        style: { zIndex: props.zIndex || 1000, pointerEvents: p.closing ? 'none' : 'auto' }
      }, [
        _backdrop(p.closing, function(e) { if (e.target === e.currentTarget && props.closeOnBackdrop !== false) close(); }),
        h('div', {
          key: 'panel', ref: panelRef, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
          'aria-label': titleId ? undefined : props.ariaLabel, tabIndex: -1,
          className: _mergeClasses('relative flex flex-col w-full max-h-full overflow-hidden bg-surface text-ink border border-line rounded-card shadow-lift outline-none ' + width, props.className),
          style: Object.assign({ animation: _anim(p.closing ? 'bow-pop-out' : 'bow-pop-in', !p.closing) }, props.style || {})
        }, [_overlayHeader(props, titleId, close), _overlayBody(props), _overlayFooter(props)])
      ]),
      document.body);
  };

  // <Drawer open onClose side="end|start|bottom" width title subtitle footer …>
  // A side panel for detail views and filters. `end` is the right edge in LTR
  // and the left edge in RTL.
  window.Drawer = function(props) {
    props = props || {};
    var open = !!props.open;
    var p = window.usePresence(open);
    var panelRef = React.useRef(null);
    var id = _useUid('bow-drawer-');
    _useOverlay(open, id, panelRef, props);
    if (!p.mounted) return null;
    var close = function() { if (props.onClose) props.onClose(); };
    var side = props.side || 'end';
    var rtl = _isRtl();
    var physical = side === 'bottom' ? 'bottom'
      : side === 'left' || side === 'right' ? side
      : ((side === 'end') !== rtl ? 'right' : 'left');
    var w = props.width || 420;
    var place = physical === 'bottom'
      ? { left: 0, right: 0, bottom: 0, maxHeight: props.height || '85vh' }
      : { top: 0, bottom: 0, width: 'min(' + (typeof w === 'number' ? w + 'px' : w) + ', 100vw - 2rem)' };
    if (physical === 'right') place.right = 0;
    if (physical === 'left') place.left = 0;
    var edge = physical === 'bottom' ? 'rounded-t-card border-t' : physical === 'right' ? 'border-s' : 'border-e';
    var titleId = props.title ? id + '-title' : undefined;
    return ReactDOM.createPortal(
      h('div', {
        className: 'bow-motion fixed inset-0',
        style: { zIndex: props.zIndex || 1000, pointerEvents: p.closing ? 'none' : 'auto' }
      }, [
        _backdrop(p.closing, function(e) { if (e.target === e.currentTarget && props.closeOnBackdrop !== false) close(); }),
        h('div', {
          key: 'panel', ref: panelRef, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId,
          'aria-label': titleId ? undefined : props.ariaLabel, tabIndex: -1,
          className: _mergeClasses('absolute flex flex-col overflow-hidden bg-surface text-ink border-line shadow-lift outline-none ' + edge, props.className),
          style: Object.assign(place, { animation: _anim('bow-slide-' + (p.closing ? 'out' : 'in') + '-' + physical, !p.closing) }, props.style || {})
        }, [_overlayHeader(props, titleId, close), _overlayBody(props), _overlayFooter(props)])
      ]),
      document.body);
  };

  // <Tabs items={[{ value, label, icon, badge, disabled, content }]} value onChange
  //       defaultValue variant="underline|pills" className panelClassName />
  // `content` may be a node or a function returning one. A panel mounts the
  // first time its tab is opened and then STAYS mounted (hidden), so charts
  // and scroll positions survive switching back. Omit `content` everywhere to
  // use Tabs as a bar that only reports `onChange`.
  window.Tabs = function(props) {
    props = props || {};
    var items = (props.items || []).filter(Boolean).map(function(it) {
      return typeof it === 'object' ? it : { value: it, label: String(it) };
    });
    var controlled = props.value !== undefined;
    var _s = React.useState(props.defaultValue !== undefined ? props.defaultValue : (items[0] && items[0].value));
    var current = controlled ? props.value : _s[0];
    var active = items.some(function(it) { return String(it.value) === String(current); }) ? current : (items[0] && items[0].value);
    var key = String(active);
    var visitedRef = React.useRef({});
    visitedRef.current[key] = true;
    var switchedRef = React.useRef(false);
    var prevRef = React.useRef(key);
    if (prevRef.current !== key) { switchedRef.current = true; prevRef.current = key; }
    var listRef = React.useRef(null);
    var tabRefs = React.useRef({});
    var _ind = React.useState(null), ind = _ind[0], setInd = _ind[1];
    var id = _useUid('bow-tabs-');
    var pills = props.variant === 'pills';

    function select(v) {
      if (!controlled) _s[1](v);
      if (props.onChange && String(v) !== key) props.onChange(v);
    }
    var keyRef = React.useRef(key);
    keyRef.current = key;
    // Reads the active key from a ref: the observer below is created once and
    // calls this long after the render that defined it.
    function measure() {
      var el = tabRefs.current[keyRef.current];
      if (!el || !el.offsetWidth) return;
      setInd(function(prev) {
        var next = { left: el.offsetLeft, width: el.offsetWidth, top: el.offsetTop, height: el.offsetHeight, animate: !!prev };
        return prev && prev.left === next.left && prev.width === next.width && prev.top === next.top && prev.height === next.height ? prev : next;
      });
    }
    React.useLayoutEffect(function() {
      measure();
      var el = tabRefs.current[key];
      if (el && el.scrollIntoView && switchedRef.current) {
        try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {}
      }
    }, [key, items.length]);
    // Created once: a fresh observer per render fires its initial callback on
    // every render, and the re-render that triggers loops at frame rate.
    React.useEffect(function() {
      var alive = true;
      var remeasure = function() { if (alive) measure(); };
      var ro = typeof ResizeObserver !== 'undefined' && listRef.current ? new ResizeObserver(remeasure) : null;
      if (ro) ro.observe(listRef.current);
      try { if (document.fonts && document.fonts.ready) document.fonts.ready.then(remeasure); } catch (e) {}
      return function() { alive = false; if (ro) ro.disconnect(); };
    }, []);

    function onKeyDown(e) {
      var enabled = items.filter(function(it) { return !it.disabled; });
      var idx = enabled.findIndex(function(it) { return String(it.value) === key; });
      var fwd = _isRtl() ? 'ArrowLeft' : 'ArrowRight', back = _isRtl() ? 'ArrowRight' : 'ArrowLeft';
      var next = null;
      if (e.key === fwd) next = enabled[(idx + 1) % enabled.length];
      else if (e.key === back) next = enabled[(idx - 1 + enabled.length) % enabled.length];
      else if (e.key === 'Home') next = enabled[0];
      else if (e.key === 'End') next = enabled[enabled.length - 1];
      if (!next) return;
      e.preventDefault();
      select(next.value);
      var el = tabRefs.current[String(next.value)];
      if (el) el.focus();
    }

    var indicator = ind ? h('span', {
      key: 'ind', 'data-bow-tab-indicator': '1', 'aria-hidden': 'true',
      className: pills ? 'absolute rounded-chip bg-surface shadow-card' : 'absolute bottom-0 h-0.5 rounded-full bg-accent',
      style: {
        left: 0, width: ind.width,
        top: pills ? ind.top : undefined, height: pills ? ind.height : undefined,
        transform: 'translateX(' + ind.left + 'px)',
        transition: ind.animate ? 'transform 260ms ' + EASE_OUT + ', width 260ms ' + EASE_OUT : 'none'
      }
    }) : null;

    var tabs = items.map(function(it) {
      var v = String(it.value), on = v === key;
      return h('button', {
        key: v, ref: function(el) { tabRefs.current[v] = el; },
        type: 'button', role: 'tab', id: id + '-tab-' + v, 'aria-selected': on ? 'true' : 'false',
        'aria-controls': it.content !== undefined ? id + '-panel-' + v : undefined,
        tabIndex: on ? 0 : -1, disabled: it.disabled || undefined,
        onClick: function() { select(it.value); },
        className: _join(
          'relative z-[1] inline-flex items-center gap-1.5 whitespace-nowrap text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed outline-none focus-visible:ring-2 focus-visible:ring-accent/50',
          pills ? 'px-3 py-1.5 rounded-chip' : 'px-3 py-2.5 rounded-t-control',
          on ? 'text-ink' : 'text-ink-3 hover:text-ink-2')
      }, [
        it.icon ? h(window.Icon, { key: 'i', name: it.icon, size: 14 }) : null,
        h('span', { key: 'l' }, it.label),
        it.badge != null ? h('span', { key: 'b', className: 'ms-0.5 rounded-full bg-surface-2 px-1.5 text-[11px] leading-4 text-ink-3 tabular-nums' }, it.badge) : null
      ]);
    });

    var bar = h('div', {
      key: 'bar', ref: listRef, role: 'tablist', 'aria-orientation': 'horizontal', 'aria-label': props.ariaLabel, onKeyDown: onKeyDown,
      className: _mergeClasses(pills
        ? 'bow-tablist relative inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-control bg-surface-2 p-0.5'
        : 'bow-tablist relative flex items-end gap-1 overflow-x-auto border-b border-line', props.className)
    }, [indicator].concat(tabs));

    var hasPanels = items.some(function(it) { return it.content !== undefined; });
    if (!hasPanels) return bar;
    var panels = items.map(function(it) {
      var v = String(it.value), on = v === key;
      if (!visitedRef.current[v] || it.content === undefined) return null;
      var content = typeof it.content === 'function' ? it.content() : it.content;
      return h('div', {
        key: v, role: 'tabpanel', id: id + '-panel-' + v, 'aria-labelledby': id + '-tab-' + v,
        className: _join('bow-motion', props.panelClassName),
        // display:none restarts the CSS animation each time the panel is shown;
        // the very first panel appears without one.
        style: on ? (switchedRef.current ? { animation: 'bow-panel-in 200ms ' + EASE_OUT + ' both' } : undefined) : { display: 'none' }
      }, content);
    });
    return h('div', { className: _join('min-w-0', props.containerClassName) }, [bar, h('div', { key: 'panels', className: 'pt-4' }, panels)]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Chat components — ChatMessages, ChatComposer, ChatThreads, ChatSuggestions (compose with useConversation)
  // ═══════════════════════════════════════════════════════════════════════════
  // Each takes the hook's object as `chat`. They reach ChatMessage, Icon,
  // LoadingSpinner, Modal and Drawer through this closure (captured here, after
  // the owner's definitions), so an app's own function of one of those names
  // does not change them.
  var _Modal = window.Modal, _Drawer = window.Drawer;
  var _CHAT_BOTTOM_PX = 40;
  var CHAT_LIST = LEGACY ? {
    root: 'min-h-0 flex-1 overflow-y-auto',
    inner: 'space-y-4 p-4',
    older: 'mx-auto flex items-center gap-2 rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-60 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800',
    jump: 'pointer-events-auto mb-2 inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-700 shadow dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200',
    error: 'text-sm text-red-600 dark:text-red-400'
  } : {
    root: 'min-h-0 flex-1 overflow-y-auto',
    inner: 'space-y-4 p-4',
    older: 'mx-auto flex items-center gap-2 rounded-chip border border-line px-3 py-1 text-xs text-ink-2 hover:bg-surface-2 disabled:opacity-60',
    jump: 'pointer-events-auto mb-2 inline-flex items-center gap-1.5 rounded-chip border border-line bg-surface px-3 py-1 text-xs text-ink-2 shadow-lift',
    error: 'text-sm text-red-600 dark:text-red-400'
  };

  // The element that scrolls the list (D-18): the list itself when it
  // overflows; the document only when the list has no height of its own (its
  // box is as tall as its content: an app that gave it none), its own styles
  // are in place (overflow-y auto: before the Tailwind classes apply its box
  // always follows its content) and the document overflows; else the list.
  // A short list with a fixed height never moves the page around it.
  // Re-resolved at every check.
  function _chatScroller(el, inner) {
    if (!el) return null;
    if (el.scrollHeight > el.clientHeight + 1 || !el.getClientRects().length) return el;
    if (!inner || el.clientHeight > inner.offsetHeight + 1) return el;
    var oy = getComputedStyle(el).overflowY;
    if (oy !== 'auto' && oy !== 'scroll') return el;
    var d = document.scrollingElement;
    return d && d.scrollHeight > d.clientHeight + 1 ? d : el;
  }
  // How far the transcript's end is below the view's end. With the document as
  // the scroller that is the list's own bottom, not the page's: content after
  // the list never counts, so following an answer never scrolls past it.
  function _chatGap(sc, inner) {
    if (sc === document.scrollingElement) return inner ? inner.getBoundingClientRect().bottom - sc.clientHeight : 0;
    return sc.scrollHeight - sc.scrollTop - sc.clientHeight;
  }
  function _chatEdge(sc) { return sc === document.scrollingElement ? 0 : sc.getBoundingClientRect().top; }
  function _chatRows(inner) { return inner ? inner.querySelectorAll(':scope > [data-testid="bow-chat-row"]') : []; }

  // <ChatMessages chat={chat} empty={node} labels={...} showTools className />
  // Opens at the newest message, follows the answer only while the user is at
  // the bottom (within 40 px), otherwise keeps the row at the top of the view
  // where it is (growth above or below it, older pages). It records that row
  // on every scroll and restores it after every content change (D-17), with
  // the browser's own scroll anchoring off.
  function ChatMessages(props) {
    props = props || {};
    var chat = props.chat && typeof props.chat === 'object' ? props.chat : null;
    var labelsKey = JSON.stringify(props.labels || null);
    var labels = React.useMemo(function() { return props.labels; }, [labelsKey]);
    var rootRef = React.useRef(null), innerRef = React.useRef(null);
    var atBottom = React.useRef(true), anchor = React.useRef(null), jumping = React.useRef(false);
    // The position the component itself last scrolled to: the scroll event that
    // follows is its own, not the user's (content that grew meanwhile, e.g. late
    // styles, must not read as the user leaving the bottom).
    var own = React.useRef(null);
    // The last position seen on a scroll event, and the jump's fallback timer.
    var lastTop = React.useRef(null), jumpTimer = React.useRef(null);
    var _oe = React.useState(null), olderError = _oe[0], setOlderError = _oe[1];
    var prev = React.useRef(null);
    var _jump = React.useState(false), showJump = _jump[0], setJump = _jump[1];
    var _unseen = React.useState(false), unseen = _unseen[0], setUnseen = _unseen[1];

    function record(sc) {
      var edge = _chatEdge(sc), rows = _chatRows(innerRef.current);
      anchor.current = null;
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i].getBoundingClientRect();
        if (r.bottom > edge + 1) { anchor.current = { id: rows[i].getAttribute('data-message-id'), offset: r.top - edge }; return; }
      }
    }
    function measure(sc) {
      var seen = lastTop.current;
      lastTop.current = sc.scrollTop;
      if (own.current !== null && Math.abs(sc.scrollTop - own.current) < 1) return;
      own.current = null;
      var at = _chatGap(sc, innerRef.current) <= _CHAT_BOTTOM_PX;
      // During the smooth scroll of Jump to latest the view moves down; a
      // scroll up meanwhile is the user's and ends the jump.
      if (jumping.current) {
        if (!at && (seen === null || sc.scrollTop >= seen)) return;
        jumping.current = false;
      }
      atBottom.current = at;
      record(sc);
      setJump(!at);
      if (at) setUnseen(false);
    }
    // At the bottom: stay there. Otherwise: put the anchored row back where it was.
    function stick() {
      var sc = _chatScroller(rootRef.current, innerRef.current);
      if (!sc) return;
      if (atBottom.current) {
        if (sc !== document.scrollingElement) sc.scrollTop = sc.scrollHeight;
        else { var gap = _chatGap(sc, innerRef.current); if (gap > 0.5) sc.scrollTop += gap; }
        own.current = sc.scrollTop;
        return;
      }
      var a = anchor.current;
      if (!a) return;
      var rows = _chatRows(innerRef.current);
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute('data-message-id') !== a.id) continue;
        var delta = rows[i].getBoundingClientRect().top - _chatEdge(sc) - a.offset;
        if (Math.abs(delta) >= 0.5) { sc.scrollTop += delta; own.current = sc.scrollTop; }
        return;
      }
    }

    React.useEffect(function() {
      var el = rootRef.current;
      if (!el) return;
      function onScroll() { var sc = _chatScroller(el, innerRef.current); if (sc) measure(sc); }
      el.addEventListener('scroll', onScroll, { passive: true });
      window.addEventListener('scroll', onScroll, { passive: true });
      var ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(function() { stick(); }) : null;
      if (ro) { ro.observe(el); if (innerRef.current) ro.observe(innerRef.current); }
      return function() {
        el.removeEventListener('scroll', onScroll);
        window.removeEventListener('scroll', onScroll);
        if (ro) ro.disconnect();
        if (jumpTimer.current !== null) { clearTimeout(jumpTimer.current); jumpTimer.current = null; }
      };
    }, [!!chat]);

    var messages = chat && Array.isArray(chat.messages) ? chat.messages : [];
    var cur = chat ? { id: chat.conversationId, loading: !!chat.loading, sending: !!chat.sending, messages: messages,
      loadingOlder: !!chat.loadingOlder, error: chat.error || null } : null;
    React.useLayoutEffect(function() {
      if (!cur) return;
      var p = prev.current;
      prev.current = cur;
      // Opening a conversation, a finished load and the user's own send go to the bottom.
      if (!p || p.id !== cur.id || (p.loading && !cur.loading) || (!p.sending && cur.sending)) {
        atBottom.current = true;
        jumping.current = false;
        setJump(false);
        setUnseen(false);
      } else if (!atBottom.current && (p.messages !== cur.messages || cur.sending)) {
        setUnseen(true);
      }
      // An error that ended an older load shows by its button (the user is at
      // the top), until `error` changes.
      if (p && p.loadingOlder && !cur.loadingOlder && cur.error && cur.error !== p.error) setOlderError(cur.error);
      else if (olderError && cur.error !== olderError) setOlderError(null);
      stick();
    });

    if (!chat) return null;
    var L = _chatLabels(labels);
    var kids = [];
    if (chat.hasOlder) {
      kids.push(h('button', { key: 'older', type: 'button', dir: 'auto', 'data-testid': 'bow-chat-older', className: CHAT_LIST.older,
        disabled: !!chat.loadingOlder, onClick: function() { if (typeof chat.loadOlder === 'function') chat.loadOlder(); } },
        [chat.loadingOlder ? h(_LoadingSpinner, { key: 's', size: 12 }) : null, h('span', { key: 'l' }, L.olderMessages)]));
    }
    var olderShown = !!olderError && chat.error === olderError;
    if (olderShown) {
      kids.push(h('p', { key: 'older-error', role: 'alert', dir: 'auto', 'data-testid': 'bow-chat-older-error', className: _join(CHAT_LIST.error, 'text-center') },
        _chatLabelOf(olderError.message) || _chatLabelOf(olderError.code)));
    }
    messages.forEach(function(m, i) {
      if (!m || typeof m !== 'object') return;
      kids.push(h('div', { key: m.id != null ? 'm-' + m.id : 'i-' + i, 'data-testid': 'bow-chat-row', 'data-message-id': m.id },
        h(ChatMessage, { message: m, labels: labels, showTools: props.showTools })));
    });
    if (chat.loading && !messages.length) {
      kids.push(h('div', { key: 'loading', 'data-testid': 'bow-chat-loading', className: 'flex justify-center py-6' }, h(_LoadingSpinner, { size: 20 })));
    }
    if (!chat.loading && !messages.length) {
      kids.push(h('div', { key: 'empty', 'data-testid': 'bow-chat-empty' }, props.empty == null ? null : props.empty));
    }
    if (chat.error && !olderShown) {
      kids.push(h('p', { key: 'error', role: 'alert', dir: 'auto', 'data-testid': 'bow-chat-error', className: CHAT_LIST.error },
        _chatLabelOf(chat.error.message) || _chatLabelOf(chat.error.code)));
    }
    var jump = showJump ? h('div', { key: 'jump', className: 'pointer-events-none sticky bottom-0 flex h-0 items-end justify-center' },
      h('button', { type: 'button', dir: 'auto', 'data-testid': 'bow-chat-jump', 'data-unseen': unseen ? 'true' : undefined, className: CHAT_LIST.jump,
        onClick: function() {
          var sc = _chatScroller(rootRef.current, innerRef.current);
          atBottom.current = true;
          jumping.current = true;
          setJump(false);
          setUnseen(false);
          if (sc) {
            var top = sc === document.scrollingElement ? sc.scrollTop + Math.max(0, _chatGap(sc, innerRef.current)) : sc.scrollHeight;
            try { sc.scrollTo({ top: top, behavior: 'smooth' }); } catch (e) { sc.scrollTop = top; }
          }
          if (jumpTimer.current !== null) clearTimeout(jumpTimer.current);
          jumpTimer.current = setTimeout(function() { jumpTimer.current = null; jumping.current = false; }, 1000);
        } }, [h(_Icon, { key: 'i', name: 'arrow-down', size: 14 }), h('span', { key: 'l' }, L.jumpToLatest)])) : null;
    return h('div', { ref: rootRef, role: 'log', 'aria-busy': !!chat.sending, 'data-testid': 'bow-chat-messages',
      className: _chatCls(CHAT_LIST.root, props.className), style: { overflowAnchor: 'none' } },
      [h('div', { key: 'inner', ref: innerRef, className: CHAT_LIST.inner }, kids), jump]);
  }
  window.ChatMessages = ChatMessages;

  var CHAT_INPUT = LEGACY ? {
    wrap: 'relative rounded-xl border border-slate-200 bg-white focus-within:ring-2 focus-within:ring-blue-500/40 dark:border-slate-700 dark:bg-slate-900',
    input: 'block w-full resize-none bg-transparent py-2.5 ps-3 pe-11 text-sm text-slate-900 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed disabled:opacity-60 dark:text-slate-100',
    button: 'absolute bottom-1.5 end-1.5 inline-flex h-8 w-8 items-center justify-center rounded-lg bg-blue-600 text-white disabled:opacity-40',
    note: 'mt-1 text-xs text-slate-500 dark:text-slate-400'
  } : {
    wrap: 'relative rounded-card border border-line bg-surface focus-within:ring-2 focus-within:ring-accent/40',
    input: 'block w-full resize-none bg-transparent py-2.5 ps-3 pe-11 text-sm text-ink outline-none placeholder:text-ink-3 disabled:cursor-not-allowed disabled:opacity-60',
    button: 'absolute bottom-1.5 end-1.5 inline-flex h-8 w-8 items-center justify-center rounded-control bg-accent text-accent-ink disabled:opacity-40',
    note: 'mt-1 text-xs text-ink-3'
  };

  // <ChatComposer chat={chat} placeholder dir labels autoFocus maxRows className />
  // Enter sends (not while an IME composition is open, not while answering),
  // Shift+Enter adds a line; Stop replaces Send while chat.answering. Its
  // direction: `dir` as given, else dir="auto" with text and the
  // placeholder's direction while empty (dir="auto" on an empty field is ltr).
  function ChatComposer(props) {
    props = props || {};
    var chat = props.chat && typeof props.chat === 'object' ? props.chat : null;
    var _d = React.useState(''), draft = _d[0], setDraft = _d[1];
    // The draft as of the last change, cleared synchronously on submit: two
    // Enter keydowns in one task would both read the same render's `draft`.
    var draftRef = React.useRef('');
    var composing = React.useRef(false);
    var inputRef = React.useRef(null);
    // When this composer last sent: Stop takes Send's place at once, so the
    // second click of a double click (or a double tap) would stop the question.
    var sentAt = React.useRef(-Infinity);
    var maxRows = typeof props.maxRows === 'number' && props.maxRows > 0 ? Math.floor(props.maxRows) : 6;
    React.useLayoutEffect(function() {
      var el = inputRef.current;
      if (!el) return;
      el.style.height = 'auto';
      var cs = getComputedStyle(el);
      var lh = parseFloat(cs.lineHeight) || 20;
      var edges = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0) + (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
      // One row: no inline height, so the row follows its styles (they may
      // arrive after this runs).
      if (el.scrollHeight <= el.clientHeight + 1) { el.style.height = ''; el.style.overflowY = 'hidden'; return; }
      var max = maxRows * lh + edges, full = el.scrollHeight + (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
      el.style.height = Math.min(full, max) + 'px';
      el.style.overflowY = full > max + 1 ? 'auto' : 'hidden';
    });
    React.useEffect(function() { if (props.autoFocus && inputRef.current) inputRef.current.focus(); }, []);
    if (!chat) return null;
    var L = _chatLabels(props.labels);
    var unavailable = !!((chat.policy && chat.policy.available === false) || (chat.error && chat.error.code === 'UNAVAILABLE' && !window.bow));
    var answering = !!chat.answering;
    function submit() {
      var text = draftRef.current.trim();
      if (!text || answering || unavailable || typeof chat.send !== 'function') return;
      draftRef.current = '';
      sentAt.current = performance.now();
      chat.send(text);
      setDraft('');
      if (inputRef.current) inputRef.current.focus();
    }
    function onKeyDown(e) {
      if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229 || composing.current) return;
      e.preventDefault();
      submit();
    }
    var dir = props.dir ? props.dir : draft ? 'auto' : (_chatDirOf(props.placeholder) || undefined);
    var button = answering
      ? h('button', { key: 'stop', type: 'button', 'data-testid': 'bow-chat-stop', 'aria-label': L.stop, title: L.stop, className: CHAT_INPUT.button,
          onClick: function(e) {
            // A pointer click (detail >= 1) right after this composer's send, or
            // any multi-click, is the rest of the Send click. Keyboard activation
            // (detail 0) always stops.
            if (e.detail > 1 || (e.detail >= 1 && performance.now() - sentAt.current < 500)) return;
            if (typeof chat.stop === 'function') chat.stop();
          } }, h(_Icon, { name: 'square', size: 14 }))
      : h('button', { key: 'send', type: 'submit', 'data-testid': 'bow-chat-send', 'aria-label': L.send, title: L.send, className: CHAT_INPUT.button,
          disabled: !draft.trim() || unavailable,
          // Also on click: a sandboxed frame without allow-forms never submits the form.
          onClick: function(e) { e.preventDefault(); submit(); } }, h(_Icon, { name: 'arrow-up', size: 16 }));
    var reason = unavailable ? ((chat.policy && chat.policy.reason) === 'auth_required' ? L.authRequired : L.unavailable) : null;
    return h('form', { 'data-testid': 'bow-chat-composer', className: _chatCls('', props.className),
      onSubmit: function(e) { e.preventDefault(); submit(); } }, [
      h('div', { key: 'w', className: CHAT_INPUT.wrap }, [
        h('textarea', { key: 't', ref: inputRef, rows: 1, 'data-testid': 'bow-chat-input', dir: dir, value: draft,
          placeholder: props.placeholder, 'aria-label': props.placeholder || L.send, disabled: unavailable, className: CHAT_INPUT.input,
          onChange: function(e) { draftRef.current = e.target.value; setDraft(e.target.value); },
          onKeyDown: onKeyDown,
          onCompositionStart: function() { composing.current = true; },
          onCompositionEnd: function() { composing.current = false; } }),
        button
      ]),
      reason ? h('p', { key: 'r', dir: 'auto', 'data-testid': 'bow-chat-unavailable', className: CHAT_INPUT.note }, reason) : null
    ]);
  }
  window.ChatComposer = ChatComposer;

  var _CHAT_NARROW_PX = 640;
  var CHAT_THREADS = LEGACY ? {
    root: 'flex w-64 shrink-0 flex-col overflow-y-auto border-e border-slate-200 dark:border-slate-700',
    head: 'flex items-center justify-between gap-2 px-3 py-2',
    title: 'text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400',
    button: 'inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800',
    list: 'space-y-0.5 px-2 pb-2',
    item: 'group flex items-center gap-1 rounded-lg',
    on: 'bg-slate-100 dark:bg-slate-800',
    select: 'flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-start text-sm text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-800',
    action: 'inline-flex h-7 w-7 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-700',
    input: 'min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm text-slate-900 outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100',
    confirm: 'rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white',
    cancel: 'rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-700 dark:border-slate-700 dark:text-slate-200'
  } : {
    root: 'flex w-64 shrink-0 flex-col overflow-y-auto border-e border-line',
    head: 'flex items-center justify-between gap-2 px-3 py-2',
    title: 'text-xs font-medium uppercase tracking-eyebrow text-ink-3',
    button: 'inline-flex items-center gap-1.5 rounded-control px-2 py-1 text-xs text-ink-2 hover:bg-surface-2',
    list: 'space-y-0.5 px-2 pb-2',
    item: 'group flex items-center gap-1 rounded-control',
    on: 'bg-surface-2',
    select: 'flex min-w-0 flex-1 items-center gap-2 rounded-control px-2 py-1.5 text-start text-sm text-ink-2 hover:bg-surface-2',
    action: 'inline-flex h-7 w-7 items-center justify-center rounded-control text-ink-3 hover:bg-surface-2 hover:text-ink',
    input: 'min-w-0 flex-1 rounded-control border border-line bg-surface px-2 py-1 text-sm text-ink outline-none',
    confirm: 'rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink',
    cancel: 'rounded-control border border-line px-3 py-1.5 text-sm text-ink-2'
  };
  // Hidden until hover or focus on other rows; always shown on the open one
  // and on screens without hover. Focusable either way.
  var _CHAT_ACTIONS_HIDDEN = 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100';

  function _useNarrow(watch) {
    var q = '(max-width: ' + (_CHAT_NARROW_PX - 1) + 'px)';
    var mq = typeof window.matchMedia === 'function' ? window.matchMedia(q) : null;
    var _n = React.useState(!!(mq && mq.matches)), narrow = _n[0], setNarrow = _n[1];
    React.useEffect(function() {
      if (!watch || !mq) return;
      var on = function() { setNarrow(mq.matches); };
      on();
      if (mq.addEventListener) mq.addEventListener('change', on); else mq.addListener(on);
      return function() { if (mq.removeEventListener) mq.removeEventListener('change', on); else mq.removeListener(on); };
    }, [watch]);
    return watch && narrow;
  }

  // <ChatThreads chat={chat} labels mode="auto|list|drawer" className />
  // The viewer's conversations: select, new, rename inline, archive (confirmed
  // in a Modal; the focus then moves to the next thread), a spinner while an
  // answer runs. mode="auto" shows a toggle that opens a Drawer below 640 px.
  function ChatThreads(props) {
    props = props || {};
    var chat = props.chat && typeof props.chat === 'object' ? props.chat : null;
    var mode = props.mode === 'list' || props.mode === 'drawer' ? props.mode : 'auto';
    var narrow = _useNarrow(mode === 'auto');
    var asDrawer = mode === 'drawer' || narrow;
    var _o = React.useState(false), open = _o[0], setOpen = _o[1];
    var _e = React.useState(null), editing = _e[0], setEditing = _e[1];
    var _v = React.useState(''), value = _v[0], setValue = _v[1];
    var _c = React.useState(null), confirmId = _c[0], setConfirmId = _c[1];
    var listRef = React.useRef(null), newRef = React.useRef(null), inputRef = React.useRef(null);
    var committed = React.useRef(false);
    // After an archive: {id, index}; the focus goes to the row now at that index,
    // else the one before, else New (the archive button left with its row).
    var focusNext = React.useRef(null);
    var conversations = chat && Array.isArray(chat.conversations) ? chat.conversations.filter(function(x) { return x && typeof x === 'object'; }) : [];
    React.useEffect(function() {
      var f = focusNext.current;
      if (!f || conversations.some(function(x) { return x.id === f.id; })) return;
      focusNext.current = null;
      var root = listRef.current;
      var buttons = root ? root.querySelectorAll('[data-testid="bow-chat-thread-select"]') : [];
      var target = buttons[f.index] || buttons[f.index - 1] || newRef.current;
      if (target) try { target.focus(); } catch (e) {}
    });
    React.useEffect(function() { if (editing && inputRef.current) { inputRef.current.focus(); inputRef.current.select(); } }, [editing]);
    if (!chat) return null;
    var L = _chatLabels(props.labels);

    function startEdit(x) { committed.current = false; setValue(x.title == null ? '' : String(x.title)); setEditing(x.id); }
    function commit(x) {
      if (committed.current) return;
      committed.current = true;
      var t = value.trim();
      setEditing(null);
      if (t && t !== String(x.title == null ? '' : x.title) && typeof chat.rename === 'function') chat.rename(x.id, t);
    }
    function pick(id) {
      if (id !== chat.conversationId || chat.loading) { if (typeof chat.select === 'function') chat.select(id); }
      setOpen(false);
    }
    function confirmArchive() {
      var id = confirmId;
      setConfirmId(null);
      if (!id) return;
      focusNext.current = { id: id, index: conversations.findIndex(function(x) { return x.id === id; }) };
      if (typeof chat.archive === 'function') chat.archive(id);
    }

    var rows = conversations.map(function(x) {
      var on = x.id === chat.conversationId;
      var title = _chatLabelOf(x.title);
      var kids;
      if (editing === x.id) {
        kids = [h('input', { key: 'in', ref: inputRef, 'data-testid': 'bow-chat-thread-input', dir: 'auto', maxLength: 120, value: value,
          'aria-label': L.rename, className: CHAT_THREADS.input,
          onChange: function(e) { setValue(e.target.value); },
          onBlur: function() { commit(x); },
          onKeyDown: function(e) {
            if (e.key === 'Enter') { e.preventDefault(); commit(x); }
            else if (e.key === 'Escape') {
              // Not the Drawer's Escape: only the edit ends.
              e.preventDefault();
              e.stopPropagation();
              committed.current = true;
              setEditing(null);
            }
          } })];
      } else {
        kids = [
          h('button', { key: 's', type: 'button', 'data-testid': 'bow-chat-thread-select', 'aria-current': on ? 'true' : undefined,
            className: CHAT_THREADS.select, onClick: function() { pick(x.id); } },
            h('span', { dir: 'auto', className: 'min-w-0 flex-1 truncate' }, title)),
          x.in_progress ? h('span', { key: 'b', 'data-testid': 'bow-chat-thread-busy', role: 'status', 'aria-label': L.inProgress, className: 'inline-flex shrink-0' },
            h(_LoadingSpinner, { size: 12 })) : null,
          h('span', { key: 'a', className: _join('flex shrink-0 items-center gap-0.5 transition-opacity', on ? '' : _CHAT_ACTIONS_HIDDEN) }, [
            h('button', { key: 'r', type: 'button', 'data-testid': 'bow-chat-thread-rename', 'aria-label': L.rename, title: L.rename,
              className: CHAT_THREADS.action, onClick: function() { startEdit(x); } }, h(_Icon, { name: 'pencil', size: 14 })),
            h('button', { key: 'x', type: 'button', 'data-testid': 'bow-chat-thread-archive', 'aria-label': L.archive, title: L.archive,
              className: CHAT_THREADS.action, onClick: function() { setConfirmId(x.id); } }, h(_Icon, { name: 'archive', size: 14 }))
          ])
        ];
      }
      return h('li', { key: x.id, 'data-testid': 'bow-chat-thread', 'data-id': x.id, 'data-active': on ? 'true' : 'false',
        'data-in-progress': x.in_progress ? 'true' : 'false', className: _join(CHAT_THREADS.item, on ? CHAT_THREADS.on : '') }, kids);
    });
    var body = h('div', { key: 'body', ref: listRef }, [
      h('div', { key: 'h', className: CHAT_THREADS.head }, [
        h('span', { key: 't', dir: 'auto', className: CHAT_THREADS.title }, L.conversations),
        h('button', { key: 'n', ref: newRef, type: 'button', 'data-testid': 'bow-chat-new', className: CHAT_THREADS.button,
          onClick: function() { if (typeof chat.newConversation === 'function') chat.newConversation(); setOpen(false); } },
          [h(_Icon, { key: 'i', name: 'plus', size: 14 }), h('span', { key: 'l', dir: 'auto' }, L.newConversation)])
      ]),
      h('ul', { key: 'u', 'data-testid': 'bow-chat-threads', className: CHAT_THREADS.list }, rows)
    ]);
    var modal = h(_Modal, { key: 'confirm', open: !!confirmId, size: 'sm', title: L.archiveConfirm, onClose: function() { setConfirmId(null); },
      footer: [
        h('button', { key: 'c', type: 'button', 'data-testid': 'bow-chat-archive-cancel', className: CHAT_THREADS.cancel, onClick: function() { setConfirmId(null); } }, L.cancel),
        h('button', { key: 'a', type: 'button', 'data-testid': 'bow-chat-archive-confirm', className: CHAT_THREADS.confirm, onClick: confirmArchive }, L.archive)
      ] });
    if (asDrawer) {
      return h('div', { 'data-testid': 'bow-chat-threads-root', className: _chatCls('shrink-0', props.className) }, [
        h('button', { key: 'toggle', type: 'button', 'data-testid': 'bow-chat-threads-toggle', 'aria-expanded': open ? 'true' : 'false',
          className: CHAT_THREADS.button, onClick: function() { setOpen(true); } },
          [h(_Icon, { key: 'i', name: 'messages-square', size: 16 }), h('span', { key: 'l', dir: 'auto' }, L.conversations)]),
        h(_Drawer, { key: 'drawer', open: open, side: 'start', width: 320, title: L.conversations, padding: 'none', onClose: function() { setOpen(false); } }, body),
        modal
      ]);
    }
    return h('div', { 'data-testid': 'bow-chat-threads-root', className: _chatCls(CHAT_THREADS.root, props.className) }, [body, modal]);
  }
  window.ChatThreads = ChatThreads;

  var CHAT_SUGGEST = LEGACY ? {
    root: 'flex flex-wrap gap-2',
    item: 'rounded-full border border-slate-200 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800'
  } : {
    root: 'flex flex-wrap gap-2',
    item: 'rounded-chip border border-line bg-surface px-3 py-1.5 text-sm text-ink-2 hover:bg-surface-2 disabled:opacity-50'
  };

  // <ChatSuggestions chat={chat} items={['…', {label, text}]} className />
  // Starter questions: a click sends one (never on mount); shown only while
  // the conversation is loaded, empty and available.
  function ChatSuggestions(props) {
    props = props || {};
    var chat = props.chat && typeof props.chat === 'object' ? props.chat : null;
    if (!chat || chat.loading || (Array.isArray(chat.messages) && chat.messages.length) || (chat.policy && chat.policy.available === false)) return null;
    var items = (Array.isArray(props.items) ? props.items : []).map(function(x) {
      if (typeof x === 'string') return { label: x, text: x };
      if (!x || typeof x !== 'object') return null;
      var label = _chatLabelOf(x.label) || _chatLabelOf(x.text), text = _chatLabelOf(x.text) || _chatLabelOf(x.label);
      return { label: label, text: text };
    }).filter(function(x) { return x && x.text.trim() && x.label.trim(); });
    if (!items.length) return null;
    return h('div', { 'data-testid': 'bow-chat-suggestions', className: _chatCls(CHAT_SUGGEST.root, props.className) }, items.map(function(it, i) {
      return h('button', { key: i, type: 'button', dir: 'auto', 'data-testid': 'bow-chat-suggestion', disabled: !!chat.sending, className: CHAT_SUGGEST.item,
        onClick: function() { if (typeof chat.send === 'function') chat.send(it.text); } }, it.label);
    }));
  }
  window.ChatSuggestions = ChatSuggestions;

  // ═══════════════════════════════════════════════════════════════════════════
  // InfoOverlay — ⓘ popovers for custom markup via data-bow-viz / data-bow-calc
  // ═══════════════════════════════════════════════════════════════════════════

  // Whether laid-out content in `node` overlaps `box`: when `deep`, every text
  // node inside it plus text-less visuals (images, icons, controls); else only
  // its own direct text (an ancestor's words around the owner, which
  // elementsFromPoint cannot see). Text nodes, not the node's contents as one
  // range: a range over a wrapper also returns the wrapper's whole box.
  var _INFO_VISUALS = 'img,svg,canvas,video,input,select,textarea,button';
  function _overlapsText(node, box, deep) {
    var hit = function(r) { return r.width && r.height && r.left < box.right && box.left < r.right && r.top < box.bottom && box.top < r.bottom; };
    var texts = [];
    if (deep) {
      var walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      for (var n = walker.nextNode(); n; n = walker.nextNode()) if (/\S/.test(n.nodeValue)) texts.push(n);
      var visuals = node.querySelectorAll(_INFO_VISUALS);
      for (var v = 0; v < visuals.length; v++) if (hit(visuals[v].getBoundingClientRect())) return true;
    } else {
      texts = Array.prototype.filter.call(node.childNodes, function(c) { return c.nodeType === 3 && /\S/.test(c.nodeValue); });
    }
    var range = document.createRange();
    for (var i = 0; i < texts.length; i++) {
      range.selectNodeContents(texts[i]);
      var rects = range.getClientRects();
      for (var j = 0; j < rects.length; j++) if (hit(rects[j])) return true;
    }
    return false;
  }

  // True when a 20px marker at (x, y) would cover only background: at each probe
  // point the topmost element (ignoring the overlay itself) must be an ancestor
  // of the owner, never the owner's own content or an unrelated element, and
  // that ancestor's own text must not reach into the spot.
  function _markerSpotFree(x, y, owner) {
    if (x < 4 || y < 2 || x + 24 > window.innerWidth || y + 22 > window.innerHeight) return false;
    var box = { left: x, top: y, right: x + 20, bottom: y + 20 };
    var checked = [];
    var pts = [[x + 1, y + 1], [x + 19, y + 1], [x + 1, y + 19], [x + 19, y + 19], [x + 10, y + 10]];
    for (var k = 0; k < pts.length; k++) {
      var hits = document.elementsFromPoint(pts[k][0], pts[k][1]);
      for (var j = 0; j < hits.length; j++) {
        var e = hits[j];
        if (e.closest && e.closest('[data-bow-ibtn], [data-bow-panel]')) continue;
        if (e === owner || !e.contains(owner)) return false;
        if (checked.indexOf(e) === -1) {
          checked.push(e);
          if (_overlapsText(e, box, false)) return false;
        }
        break;
      }
    }
    return true;
  }

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
      targets.push({ ownerIndex: i, el: el, rect: r, vizIndex: parseInt(raw, 10) || 0, viz: byId, calc: el.getAttribute('data-bow-calc') || null, title: el.getAttribute('data-bow-title') || null });
    }
    var markerPositions = [];
    var markers = targets.map(function(t, i) {
      var top = Math.max(2, t.rect.top + 6);
      var left = rtl ? t.rect.left + 4 : t.rect.right - 24;
      // An owner too short to host the marker (a label, a chip) can have it
      // sitting on its text. Only then: use free space just beside its end
      // edge, else above or below its end corner; keep the inner corner when
      // none is free. A short owner whose corner is empty keeps it.
      if (t.rect.height < 32 && _overlapsText(t.el, { left: left, top: top, right: left + 20, bottom: top + 20 }, true)) {
        var endX = rtl ? t.rect.left : t.rect.right - 20;
        var spots = [
          { left: rtl ? t.rect.left - 24 : t.rect.right + 4, top: t.rect.top + (t.rect.height - 20) / 2 },
          { left: endX, top: t.rect.top - 22 },
          { left: endX, top: t.rect.bottom + 2 }
        ];
        for (var s = 0; s < spots.length; s++) {
          if (_markerSpotFree(spots[s].left, spots[s].top, t.el)) { left = spots[s].left; top = spots[s].top; break; }
        }
      }
      if (!LEGACY) {
        // Nested source-backed elements can share a corner. Keep each detail
        // control reachable instead of placing one button over another.
        left = Math.max(4, Math.min(left, window.innerWidth - 24));
        while (markerPositions.some(function(p) { return Math.abs(p.left - left) < 24 && Math.abs(p.top - top) < 24; })) {
          var nextLeft = left + (rtl ? 24 : -24);
          if (nextLeft < 4 || nextLeft > window.innerWidth - 24) top += 24;
          else left = nextLeft;
        }
        markerPositions.push({ left: left, top: top });
      }
      // Where the marker really sits (it may have moved off the owner's
      // corner), so the open panel never covers it.
      t.markerTop = top;
      // data-bow-info-for: index of the marked element in document order, so
      // the polish picker can resolve this detached marker to its owner.
      return h('button', { key: 'm' + i, type: 'button', 'data-bow-ibtn': '1', 'data-bow-info-for': String(t.ownerIndex), 'aria-label': 'Details',
        onClick: function(e) { e.stopPropagation(); setTab('data'); setOpenT(t); },
        style: Object.assign({ position: 'fixed', top: top, left: left, zIndex: 99998 }, INFO_BTN_BOX),
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
        { position: 'fixed', left: left, width: W, zIndex: 99999, maxHeight: '72vh', top: below ? Math.max(openT.rect.top + 28, openT.markerTop + 22) : undefined, bottom: below ? undefined : (window.innerHeight - Math.min(openT.rect.top, openT.markerTop) + 6), display: 'flex', flexDirection: 'column' },
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
    // Artifact code runs as a classic script, where `import` is a syntax
    // error, so import statements are removed before it runs. Most imports
    // only name things that already exist as sandbox globals (React,
    // useState, echarts, …) and removing them is enough. An import that
    // introduces a NEW local name — `{ useState as useLocal }`,
    // `import R from 'react'`, `import * as E from 'echarts'` — is replaced
    // with a declaration bound to the matching global instead; removing it
    // left that name undefined. Plain imports keep being removed (declaring
    // them could clash with code that defines the same name itself).
    var IMPORT_GLOBALS = { 'react': 'React', 'react-dom': 'ReactDOM', 'react-dom/client': 'ReactDOM', 'echarts': 'echarts', 'lucide': 'lucide' };
    window.__bowResolveImport = function(mod, name) {
      var g = IMPORT_GLOBALS[mod];
      var ns = g ? window[g] : undefined;
      if (name === 'default' || name === '*') return ns;
      if (ns && ns[name] !== undefined) return ns[name];
      return window[name];
    };
    function importBindings(clause, mod) {
      var canonical = IMPORT_GLOBALS[mod];
      var decls = [];
      var c = clause;
      function bind(local, name) {
        decls.push(local + ' = window.__bowResolveImport(' + JSON.stringify(mod) + ', ' + JSON.stringify(name) + ')');
      }
      var braces = c.match(/\{([\s\S]*)\}/);
      if (braces) {
        braces[1].split(',').forEach(function(part) {
          var m = part.trim().match(/^([\w$]+)(?:\s+as\s+([\w$]+))?$/);
          if (!m) return;
          var imported = m[1], local = m[2] || m[1];
          if (imported === 'default') { if (canonical && local !== canonical) bind(local, 'default'); }
          else if (local !== imported) bind(local, imported);
        });
        c = c.replace(braces[0], '');
      }
      var ns = c.match(/\*\s*as\s+([\w$]+)/);
      if (ns) {
        if (canonical && ns[1] !== canonical) bind(ns[1], '*');
        c = c.replace(ns[0], '');
      }
      var def = c.replace(/,/g, ' ').trim();
      if (/^[\w$]+$/.test(def) && canonical && def !== canonical) bind(def, 'default');
      return decls.length ? 'var ' + decls.join(', ') + ';' : '';
    }
    function stripImports(code) {
      var s = code.replace(/^[ \t]*import\s+([^'";]+?)\s+from\s*(['"])([^'"]+)\2[ \t]*;?/gm, function(_, clause, _q, mod) {
        return importBindings(clause, mod);
      });
      s = s.replace(/import\s*\{[^}]*\}\s*from\s*['"][^'"]+['"]\s*;?/g, '');
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
