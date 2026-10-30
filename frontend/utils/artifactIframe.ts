/**
 * Shared builder for the artifact iframe HTML.
 *
 * Used by:
 *  - pages/r/[id]/index.vue          (public report view, read-only)
 *  - components/dashboard/ArtifactFrame.vue  (in-app editor; passes polishMode: true)
 *
 * Sister runtime: frontend/public/mcp-artifact-app.html (loaded by Claude / Cursor
 * via MCP Apps spec, gets data via postMessage instead of inlining). That file
 * keeps its own DOCTYPE shell because MCP hosts receive it as a whole document
 * (ui:// resource text from routes/mcp.py, never by URL — the app-origin copy is
 * unframeable and inert), but it
 * loads /libs/artifact-globals.js so the runtime surface (useMemo, useState,
 * KPICard, useFilters, …) matches what this template provides.
 */

/**
 * The sandbox runtime version. It must equal the backend's
 * ARTIFACT_RUNTIME_VERSION and it decides the legacy vs themed look, so bumping
 * it re-skins artifacts. When artifact-globals.js gains or changes a global, do
 * not bump it: change the revision= token on the globals URL in
 * artifactPageLibs below and in public/mcp-artifact-app.html (e.g. chat-1 ->
 * chat-2), so a browser never pairs a cached OLD runtime with NEW artifact code
 * ("DataTable is not defined"). Production builds also add a content hash h=.
 */
export const ARTIFACT_GLOBALS_VERSION = '11'; // v11: themed design system (setTheme/useTheme, token utilities, Icon/Sparkline/Delta/PageHeader…, className merge); v10: FilterSelect single-select mode; // v10: FilterSelect single-select mode (scalar params); v9: vizById() id-keyed data access + dark-mode variants + forced-dark wrapper

export interface ArtifactIframeFile {
  id: string;
  content_type: string;
  filename?: string;
  /** data: URI carrying the file bytes (host resolves + injects so <img>/<iframe> need no auth). */
  dataUri?: string;
}

/**
 * Viewer identity injected per render (never stored on the artifact).
 * Shape mirrors GET /users/me/viewer_context. null/undefined = anonymous
 * viewer (public token pages, headless validation/thumbnail renders).
 */
export interface ArtifactViewerContext {
  id: string;
  name?: string | null;
  email?: string | null;
  image_url?: string | null;
  role?: string | null;
  profile_attributes?: Record<string, unknown> | null;
  /** Org group names, alphabetical, server-capped. Empty/missing when none. */
  groups?: string[] | null;
}

/** One declared query parameter, aggregated across the artifact's queries. */
export interface ArtifactParamDeclaration {
  name: string;
  type?: string | null;
  label?: string | null;
  source?: string | null; // 'input' | 'identity' | 'input_identity_default'
  default?: unknown;
  required?: boolean;
  options?: unknown[] | null;
  /** Choices fed by another query in the report (the filter-space pattern). */
  options_source?: { query_id: string; value_column: string; label_column?: string | null } | null;
  /** Queries (by id) that declare this param — a control drives all of them. */
  query_ids?: string[];
}

export interface ArtifactParamsPayload {
  declarations: ArtifactParamDeclaration[];
  /** Current applied values by param name (identity params carry no value —
   *  the server binds them per viewer). */
  values: Record<string, unknown>;
  /** Host-resolved stable choices per param name (static options and
   *  options-source query rows, normalized) — useParamOptions() reads this. */
  options?: Record<string, Array<{ value: unknown; label: string }>>;
  /** Highest ARTIFACT_SET_PARAMS seq the host has processed. The runtime
   *  store keeps a user's committed-but-unacked values authoritative until a
   *  push carries ack >= their commit seq, so typing never resets mid
   *  round-trip and a stale echo can't clobber newer input. */
  ack?: number;
}

export interface ArtifactIframeData {
  report: unknown;
  visualizations: unknown[];
  /** Embedded images/PDFs, rendered by the <BowFile> sandbox global. */
  files?: ArtifactIframeFile[];
  /** The viewing user, or null when anonymous. Every field is nullable —
   *  generated code must guard (current_user?.name). Display-only. */
  current_user?: ArtifactViewerContext | null;
  /** Declared query parameters + current values (useParams() runtime). */
  params?: ArtifactParamsPayload | null;
  /**
   * Sandbox runtime generation the artifact was authored for (from
   * content.runtime_version). >= 11 enables the themed design system and
   * merge-semantics className; absent/0 keeps the legacy look so stored
   * artifacts never change appearance.
   */
  runtime?: { version: number } | null;
}

export interface ArtifactIframeOptions {
  data: ArtifactIframeData;
  code: string;
  mode?: 'page' | 'slides';
  /** Inject polish element-picker. Only meaningful in the editor. */
  polishMode?: boolean;
  fixtureMode?: boolean;
  /** Resource apps prohibit third-party image requests; legacy pages retain HTTPS images. */
  resourceApp?: boolean;
  /** Text shown inside #root before Babel transforms the artifact code. */
  loadingLabel?: string;
  /** Default 'production'. 'development' gives clearer React error messages. */
  reactBuild?: 'production' | 'development';
  /**
   * Host app color mode. 'dark' sets class="dark" on the iframe <html> (and
   * Tailwind runs with darkMode:'class'), so artifact code and the sandbox
   * globals can use dark: variants. Toggle live without a reload by posting
   * { type: 'ARTIFACT_SET_COLOR_MODE', mode: 'dark' | 'light' } to the iframe.
   */
  colorMode?: 'light' | 'dark';
  /**
   * The artifact code precompiled by the backend exactly as babel-standalone
   * would compile it here (ArtifactSchema.compiled_js / compiled_js_key).
   * Used only when `key` matches precompiledKey(code) for the code being
   * rendered; otherwise the iframe loads Babel and compiles in the browser.
   */
  precompiled?: { js?: string | null; key?: string | null } | null;
  /**
   * Source text of vendored libs keyed by URL (from artifactLibs.ts — fetched
   * by the host page, where the HTTP cache works). A lib present here runs as
   * an inline script; one missing falls back to <script src>.
   */
  inlineLibs?: Record<string, string> | null;
}

const SC = '</' + 'script>';

/**
 * Content hashes of /libs files, injected at build time (nuxt.config.ts
 * vite.define). A hashed URL is served with a one-year immutable
 * Cache-Control (backend/app/core/spa.py), so repeat views skip the network
 * entirely; a new build that changes a file changes its URL.
 */
declare const __BOW_LIB_HASHES__: Record<string, string> | undefined;
const LIB_HASHES: Record<string, string> =
  typeof __BOW_LIB_HASHES__ !== 'undefined' && __BOW_LIB_HASHES__ ? __BOW_LIB_HASHES__ : {};

/** URL for a vendored lib, carrying its content hash when known. */
export function libUrl(file: string, query = ''): string {
  const hash = LIB_HASHES[file];
  const parts = [query, hash ? `h=${hash}` : ''].filter(Boolean);
  return `/libs/${file}${parts.length ? '?' + parts.join('&') : ''}`;
}

/** The scripts the page iframe loads, in execution order. */
export function artifactPageLibs(opts: { reactBuild?: 'production' | 'development'; babel?: boolean; pdf?: boolean } = {}): string[] {
  const dev = opts.reactBuild === 'development';
  return [
    libUrl('tailwindcss-3.4.16.js'),
    libUrl('artifact-tailwind.js', `v=${ARTIFACT_GLOBALS_VERSION}`),
    libUrl(dev ? 'react-18.development.js' : 'react-18.production.min.js'),
    libUrl(dev ? 'react-dom-18.development.js' : 'react-dom-18.production.min.js'),
    ...(opts.babel === false ? [] : [libUrl('babel-standalone.min.js')]),
    libUrl('echarts-5.min.js'),
    libUrl('lucide.min.js'),
    ...(opts.pdf === false ? [] : [libUrl('pdf.min.js')]),
    libUrl('artifact-globals.js', `v=${ARTIFACT_GLOBALS_VERSION}&revision=chat-1`),
    libUrl('artifact-sdk.js', 'v=3'),
  ];
}

/**
 * Must match app.services.artifact_precompile.source_key: FNV-1a 32-bit over
 * UTF-16 code units, prefixed with the length.
 */
export function precompiledKey(code: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < code.length; i++) {
    h ^= code.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `${code.length}:${h.toString(16).padStart(8, '0')}`;
}

// Same shape as artifact_precompile._SINGLE_BABEL_SCRIPT_RE: the code must be
// exactly one classic text/babel inline script for the swap to be exact.
const SINGLE_BABEL_SCRIPT_RE = /^\s*<script\s+type=["']text\/babel["']\s*>([\s\S]*?)<\/script\s*>\s*$/i;

function usablePrecompiled(opts: ArtifactIframeOptions): string | null {
  const pc = opts.precompiled;
  if (!pc || typeof pc.js !== 'string' || !pc.js || typeof pc.key !== 'string') return null;
  const code = opts.code || '';
  const m = SINGLE_BABEL_SCRIPT_RE.exec(code);
  if (!m || /<\/script|<!--|\u0000/i.test(m[1]) || !m[1].trim()) return null;
  if (precompiledKey(code) !== pc.key) return null;
  return pc.js;
}

/**
 * pdf.js (320KB) only backs BowPdfViewer/BowFile for PDFs. Load it when an
 * embedded file is a PDF or the code mentions pdf at all (BowPdfViewer src=,
 * a .pdf URL, …) — without it BowPdfViewer still renders its fallback card.
 */
function needsPdf(opts: ArtifactIframeOptions): boolean {
  if (/pdf/i.test(opts.code || '')) return true;
  return (opts.data?.files || []).some((f) => /pdf/i.test(`${f?.content_type || ''} ${f?.filename || ''}`));
}

/**
 * Executes precompiled artifact code the way babel-standalone's
 * transformScriptTags executes its output: on DOMContentLoaded, from a
 * listener registered where the Babel <script> tag used to be (so listener
 * order is unchanged), as a fresh <script> appended to <head> (so scope,
 * timing and artifact-globals' appendChild import-stripping are unchanged).
 */
export interface ArtifactLibPlan {
  /** Inline-able scripts that run in <head>, in order (Babel excluded). */
  head: string[];
  /** Inline-able scripts that run in <body> after ARTIFACT_DATA, in order. */
  body: string[];
  /** babel-standalone, only when there is no usable precompiled code. Always a
   *  <script src>: 2.3MB the host would otherwise download up front, and the
   *  browser compiles a src script off the main thread — inlined, it cost the
   *  fallback path more than it saved. */
  babelSrc: string | null;
  /** pdf.js stays a <script src> (it derives a fallback worker URL from
   *  document.currentScript.src); null when the artifact needs no PDF viewer. */
  pdfSrc: string | null;
  /** The usable precompiled artifact JS, or null (Babel compiles in-iframe). */
  precompiledJs: string | null;
}

/** Which libs the page iframe will load for these options. */
export function artifactLibPlan(opts: ArtifactIframeOptions): ArtifactLibPlan {
  const reactBuild = opts.reactBuild ?? 'production';
  const precompiledJs = usablePrecompiled(opts);
  const all = artifactPageLibs({ reactBuild, babel: false, pdf: false });
  return {
    head: all.slice(0, all.length - 2),
    body: all.slice(all.length - 2),
    babelSrc: precompiledJs ? null : libUrl('babel-standalone.min.js'),
    pdfSrc: needsPdf(opts) ? libUrl('pdf.min.js') : null,
    precompiledJs,
  };
}

/** URLs the host should fetch (artifactLibs.ts) before building the srcdoc. */
export function inlineableArtifactLibs(opts: ArtifactIframeOptions): string[] {
  if ((opts.mode ?? 'page') !== 'page') return [];
  const plan = artifactLibPlan(opts);
  return [...plan.head, ...plan.body];
}

// Runs one lib's source as a classic script element inserted right after the
// calling <script> — same global scope and execution order as the
// <script src> it replaces. The source travels as a JSON string and is set via
// .text, so no lib content is ever parsed as HTML. insertBefore (not
// appendChild) because artifact-globals later hooks appendChild.
const INLINE_RUNNER = `window.__bowRunLib=function(t){var c=document.currentScript,s=document.createElement('script');s.text=t;c.parentNode.insertBefore(s,c.nextSibling);};`;

function inlineLibScript(source: string): string {
  const payload = JSON.stringify(source).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
  return `<script>window.__bowRunLib(${payload})${SC}`;
}

function precompiledLoaderScript(js: string): string {
  const payload = JSON.stringify(js).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
  return `<script>(function(){var c=${payload};window.addEventListener('DOMContentLoaded',function(){var s=document.createElement('script');s.text=c;document.getElementsByTagName('head')[0].appendChild(s);},false);})();${SC}`;
}

/**
 * Legacy slides artifacts stored browser-renderable HTML in content.code;
 * the current pipeline stores python-pptx source that only the backend can
 * execute. Only markup may be injected into the slides iframe — Python
 * dumped into a <body> renders as a wall of source text.
 */
export function isHtmlSlidesCode(code: string): boolean {
  return /<\s*(!doctype|html|head|body|script|style|div|section)\b/i.test(code || '');
}

function buildSlidesHtml(data: ArtifactIframeData, code: string): string {
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="form-action 'none'">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="${libUrl('tailwindcss-3.4.16.js')}">${SC}
  <style>
    html, body { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
    .slide { transition: opacity 0.3s ease-in-out; }
  </style>
</head>
<body class="bg-slate-900">
  <script>window.ARTIFACT_DATA = ${JSON.stringify(data).replace(/</g, '\\u003c')};${SC}

  ${code}
</body>
</html>`;
}

function polishScript(): string {
  return `
    // Polish mode: element pick, highlight & custom cursor
    (function() {
      var polishActive = false;
      var currentHighlight = null;

      var polishStyle = document.createElement('style');
      polishStyle.textContent = [
        '.__polish-highlight { outline: 2px solid #6366f1 !important; outline-offset: 2px; }',
        '.__polish-active { cursor: crosshair !important; }',
        '.__polish-active * { cursor: crosshair !important; }',
        '.__polish-cursor { position: fixed; pointer-events: none; z-index: 99999; display: none; }',
        '.__polish-cursor-inner { display: flex; align-items: center; gap: 6px; background: #4f46e5; color: white; font-size: 12px; font-weight: 500; font-family: system-ui, sans-serif; padding: 5px 10px 5px 8px; border-radius: 20px; box-shadow: 0 4px 12px rgba(79,70,229,0.35); white-space: nowrap; }'
      ].join('\\n');
      document.head.appendChild(polishStyle);

      var cursorEl = document.createElement('div');
      cursorEl.className = '__polish-cursor';
      cursorEl.innerHTML = '<div class="__polish-cursor-inner"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.37 2.63 14 7l-1.59-1.59a2 2 0 0 0-2.82 0L8 7l9 9 1.59-1.59a2 2 0 0 0 0-2.82L17 10l4.37-4.37a2.12 2.12 0 1 0-3-3Z"/><path d="M9 8c-2 3-4 3.5-7 4l8 10c2-1 6-5 6-7"/><path d="M14.5 17.5 4.5 15"/></svg>Click to select</div>';
      document.body.appendChild(cursorEl);

      function onMouseMove(e) {
        cursorEl.style.left = (e.clientX + 12) + 'px';
        cursorEl.style.top = (e.clientY + 12) + 'px';
      }

      function snapToMeaningful(el) {
        var selfTag = (el.tagName || '').toLowerCase();
        if (/^(h[1-6]|table|ul|ol|img|svg|canvas|section|article|header|footer|nav|main)$/.test(selfTag)) {
          return el;
        }
        var node = el;
        var maxDepth = 6;
        while (node && node !== document.body && node.id !== 'root' && maxDepth-- > 0) {
          var cls = node.className || '';
          if (typeof cls === 'string' && (
            /rounded-(lg|xl|2xl)/.test(cls) ||
            /shadow/.test(cls) ||
            /\\bp-[4-9]\\b/.test(cls) ||
            /\\bp-1[0-9]/.test(cls) ||
            node.getAttribute('role') ||
            node.hasAttribute('data-section') ||
            node.hasAttribute('data-card')
          )) {
            return node;
          }
          if (node.parentElement && node.parentElement !== document.body && node.parentElement.id !== 'root') {
            node = node.parentElement;
          } else {
            break;
          }
        }
        return el;
      }

      // The ⓘ provenance button is runtime chrome, not artifact code, so a
      // pick on it resolves to the element it describes.
      function infoOwner(el) {
        if (!el.closest) return null;
        var marker = el.closest('[data-bow-ibtn]');
        if (marker) {
          // The data-bow-viz overlay draws markers outside their owners.
          var owners = document.querySelectorAll('[data-bow-viz]');
          return owners[parseInt(marker.getAttribute('data-bow-info-for'), 10)] || null;
        }
        var inline = el.closest('[data-bow-info]');
        return inline && inline.parentElement ? inline.parentElement.parentElement : null;
      }

      function pickTarget(el) {
        return infoOwner(el) || snapToMeaningful(el);
      }

      // Without the ⓘ markup, which would otherwise fill the snippet budget
      // before the card's own content.
      function snippetOf(target) {
        var clone = target.cloneNode(true);
        clone.querySelectorAll('[data-bow-info]').forEach(function(n) {
          var wrap = n.parentElement;
          (wrap && wrap !== clone && wrap.children.length === 1 ? wrap : n).remove();
        });
        return clone.outerHTML.replace(/ class="[^"]*__polish[^"]*"/g, function(m) { return m.replace(/__polish-highlight/g, '').replace(/\\s+/g, ' '); }).slice(0, 500);
      }

      function onHover(e) {
        if (!polishActive) return;
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        var target = pickTarget(e.target);
        target.classList.add('__polish-highlight');
        currentHighlight = target;
      }
      function onOut(e) {
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        currentHighlight = null;
      }
      function onClick(e) {
        if (!polishActive) return;
        e.preventDefault();
        e.stopPropagation();
        // A throw here would reach window.onerror and read as a dashboard
        // render failure; a pick that cannot be described stays in pick mode.
        var element;
        try {
          var target = pickTarget(e.target);
          var rect = target.getBoundingClientRect();
          element = {
            tag: target.tagName,
            // getAttribute, not className: on SVG elements that is an SVGAnimatedString.
            classes: (target.getAttribute('class') || '').replace(/__polish-highlight/g, '').trim(),
            text: (target.textContent || '').slice(0, 100).trim(),
            htmlSnippet: snippetOf(target),
            rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
          };
        } catch (err) {
          console.warn('Polish pick failed:', err);
          return;
        }
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        polishActive = false;
        document.body.classList.remove('__polish-active');
        cursorEl.style.display = 'none';
        document.removeEventListener('mousemove', onMouseMove, true);
        window.parent.postMessage({ type: 'POLISH_ELEMENT_SELECTED', element: element }, '*');
      }

      window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'POLISH_ENTER') {
          polishActive = true;
          document.body.classList.add('__polish-active');
          cursorEl.style.display = 'block';
          document.addEventListener('mousemove', onMouseMove, true);
          document.body.addEventListener('mouseover', onHover, true);
          document.body.addEventListener('mouseout', onOut, true);
          document.body.addEventListener('click', onClick, true);
        } else if (e.data && e.data.type === 'POLISH_EXIT') {
          polishActive = false;
          document.body.classList.remove('__polish-active');
          cursorEl.style.display = 'none';
          document.removeEventListener('mousemove', onMouseMove, true);
          if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
          currentHighlight = null;
          document.body.removeEventListener('mouseover', onHover, true);
          document.body.removeEventListener('mouseout', onOut, true);
          document.body.removeEventListener('click', onClick, true);
        }
      });
    })();
`;
}

function errorBoundaryScript(): string {
  return `
    // Error reporting: forward compile/runtime errors to the parent.
    window.__artifactErrorSent = false;
    function reportArtifactError(msg) {
      if (window.__artifactErrorSent) return;
      window.__artifactErrorSent = true;
      window.parent.postMessage({ type: 'ARTIFACT_ERROR', payload: { message: msg } }, '*');
    }

    window.onerror = function(msg, source, line, col, err) {
      var message = (err && err.message) || String(msg);
      if (message.indexOf('ResizeObserver') !== -1) return false;
      reportArtifactError(message);
    };
    window.addEventListener('unhandledrejection', function(e) {
      reportArtifactError(e.reason && e.reason.message ? e.reason.message : String(e.reason));
    });

    // Wrap ReactDOM.render with an error boundary so a thrown component
    // surfaces via reportArtifactError instead of producing a blank iframe.
    (function() {
      class ArtifactErrorBoundary extends React.Component {
        constructor(props) { super(props); this.state = { hasError: false }; }
        static getDerivedStateFromError() { return { hasError: true }; }
        componentDidCatch(error) { reportArtifactError(error.message || String(error)); }
        render() { return this.state.hasError ? null : this.props.children; }
      }
      var origRender = ReactDOM.render;
      ReactDOM.render = function(element, container) {
        var wrapped = React.createElement(ArtifactErrorBoundary, null, element);
        return origRender.call(ReactDOM, wrapped, container);
      };
    })();
`;
}

function readySignalScript(): string {
  return `
    // After Babel processes text/babel scripts, signal readiness to the parent.
    window.addEventListener('DOMContentLoaded', function() {
      setTimeout(function() {
        if (window.__artifactErrorSent) return;
        var root = document.getElementById('root');
        if (root && root.children.length > 0) {
          window.parent.postMessage({ type: 'ARTIFACT_READY' }, '*');
        } else {
          reportArtifactError('Dashboard code did not render any content');
        }
      }, 500);
    });
`;
}

export function buildArtifactIframeHtml(opts: ArtifactIframeOptions): string {
  const mode = opts.mode ?? 'page';
  if (mode === 'slides') return buildSlidesHtml(opts.data, opts.code);

  const loadingLabel = opts.loadingLabel ?? 'Loading...';
  const plan = artifactLibPlan(opts);
  const inline = opts.inlineLibs || {};
  const anyInline = [...plan.head, ...plan.body].some((u) => inline[u] !== undefined);
  const tag = (url: string) => (inline[url] !== undefined ? inlineLibScript(inline[url]) : `<script src="${url}">${SC}`);
  // Preload hints let the iframe fetch every src-loaded script in parallel;
  // the tags below still execute strictly in order.
  const preloads = [...plan.head, ...(plan.babelSrc ? [plan.babelSrc] : []), ...plan.body, ...(plan.pdfSrc ? [plan.pdfSrc] : [])]
    .filter((u) => inline[u] === undefined)
    .map((href) => `<link rel="preload" as="script" href="${href}">`).join('\n  ');
  const [tailwindSrc, tailwindCfgSrc, reactSrc, reactDomSrc, echartsSrc, lucideSrc] = plan.head;
  const [globalsSrc, sdkSrc] = plan.body;

  const embeddedData = JSON.stringify(opts.data).replace(/</g, '\\u003c');
  // getRandomValues is available on plain-HTTP self-hosted installations too.
  const runtimeNonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  const assetOrigin = window.location.origin;
  const isolationPolicy = `default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' ${assetOrigin}/libs/; style-src 'unsafe-inline' ${assetOrigin}/libs/; img-src data: blob: ${assetOrigin}${opts.resourceApp ? '' : ' https:'}; font-src data: ${assetOrigin}; connect-src 'none'; worker-src blob:; frame-src blob: data:; form-action 'none'; base-uri 'none';`;
  const polish = opts.polishMode ? polishScript() : '';
  const isDark = opts.colorMode === 'dark';

  return `<!DOCTYPE html>
<html${isDark ? ' class="dark"' : ''}>
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${isolationPolicy}">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  ${preloads}
  ${anyInline ? `<script>${INLINE_RUNNER}${SC}` : ''}
  ${tag(tailwindSrc)}
  ${tag(tailwindCfgSrc)}
  ${tag(reactSrc)}
  ${tag(reactDomSrc)}
  ${plan.precompiledJs ? precompiledLoaderScript(plan.precompiledJs) : `<script src="${plan.babelSrc}">${SC}`}
  ${tag(echartsSrc)}
  ${tag(lucideSrc)}
  ${plan.pdfSrc ? `<script src="${plan.pdfSrc}">${SC}` : ''}
  <link rel="stylesheet" href="${libUrl('artifact-fonts.css', `v=${ARTIFACT_GLOBALS_VERSION}`)}">
  <style>
    html, body, #root { height: 100%; margin: 0; padding: 0; }
    body { font-family: var(--bow-font-body, system-ui, -apple-system, sans-serif); background-color: var(--bow-bg, #ffffff); color: var(--bow-ink, #0f172a); }
    html.dark body { background-color: var(--bow-bg, #111827); color: var(--bow-ink, #e5e7eb); }
  </style>
</head>
<body>
  <div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;">${loadingLabel}</div></div>

  <script>
    // Live color-mode toggle from the host (no iframe reload).
    window.addEventListener('message', function (e) {
      if (e.source !== window.parent) return;
      var d = e && e.data;
      if (d && d.type === 'ARTIFACT_SET_COLOR_MODE') {
        // A forced-dark artifact (root <div className="dark">) stays dark
        // whatever the host theme — see watchForcedDark in artifact-globals.
        document.documentElement.classList.toggle('dark', d.mode === 'dark' || !!window.__bowForcedDark);
        // Charts (and anything else theme-bound at init time) listen for this.
        try { window.dispatchEvent(new CustomEvent('bow-colormode', { detail: { mode: d.mode } })); } catch (err) {}
      }
    });
  ${SC}
  <script>window.ARTIFACT_DATA = ${embeddedData};window.__BOW_RUNTIME_NONCE__="${runtimeNonce}";window.__BOW_FIXTURE_MODE__=${!!opts.fixtureMode};${SC}
  ${tag(globalsSrc)}
  ${tag(sdkSrc)}
  ${anyInline ? `<script>try { delete window.__bowRunLib; } catch (e) {}${SC}` : ''}

  <script>${polish}${errorBoundaryScript()}${SC}

  ${plan.precompiledJs ? '' : opts.code}

  <script>${readySignalScript()}${SC}
</body>
</html>`;
}
