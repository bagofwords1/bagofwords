/**
 * Shared builder for the artifact iframe HTML.
 *
 * Used by:
 *  - pages/r/[id]/index.vue          (public report view, read-only)
 *  - components/dashboard/ArtifactFrame.vue  (in-app editor; passes polishMode: true)
 *
 * Sister runtime: frontend/public/mcp-artifact-app.html (loaded by Claude / Cursor
 * via MCP Apps spec, gets data via postMessage instead of inlining). That file
 * keeps its own DOCTYPE shell because external hosts load it by URL, but it
 * loads /libs/artifact-globals.js so the runtime surface (useMemo, useState,
 * KPICard, useFilters, …) matches what this template provides.
 */

/**
 * Cache-buster for /libs/artifact-globals.js. The file is served without a
 * content hash, so a browser can pair a cached OLD runtime with NEW artifact
 * code ("DataTable is not defined"). Bump this whenever artifact-globals.js
 * gains or changes a global.
 */
export const ARTIFACT_GLOBALS_VERSION = '5';

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
}

export interface ArtifactIframeOptions {
  data: ArtifactIframeData;
  code: string;
  mode?: 'page' | 'slides';
  /** Inject polish element-picker. Only meaningful in the editor. */
  polishMode?: boolean;
  /** Text shown inside #root before Babel transforms the artifact code. */
  loadingLabel?: string;
  /** Default 'production'. 'development' gives clearer React error messages. */
  reactBuild?: 'production' | 'development';
}

const SC = '</' + 'script>';

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
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="/libs/tailwindcss-3.4.16.js">${SC}
  <style>
    html, body { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
    .slide { transition: opacity 0.3s ease-in-out; }
  </style>
</head>
<body class="bg-slate-900">
  <script>window.ARTIFACT_DATA = ${JSON.stringify(data)};${SC}

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

      function onHover(e) {
        if (!polishActive) return;
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        var target = snapToMeaningful(e.target);
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
        var target = snapToMeaningful(e.target);
        var rect = target.getBoundingClientRect();
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        polishActive = false;
        document.body.classList.remove('__polish-active');
        cursorEl.style.display = 'none';
        document.removeEventListener('mousemove', onMouseMove, true);
        window.parent.postMessage({
          type: 'POLISH_ELEMENT_SELECTED',
          element: {
            tag: target.tagName,
            classes: target.className.replace(/__polish-highlight/g, '').trim(),
            text: (target.textContent || '').slice(0, 100).trim(),
            htmlSnippet: target.outerHTML.replace(/ class="[^"]*__polish[^"]*"/g, function(m) { return m.replace(/__polish-highlight/g, '').replace(/\\s+/g, ' '); }).slice(0, 500),
            rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
          }
        }, '*');
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
  const reactBuild = opts.reactBuild ?? 'production';
  const reactSrc =
    reactBuild === 'development'
      ? '/libs/react-18.development.js'
      : '/libs/react-18.production.min.js';
  const reactDomSrc =
    reactBuild === 'development'
      ? '/libs/react-dom-18.development.js'
      : '/libs/react-dom-18.production.min.js';

  const embeddedData = JSON.stringify(opts.data);
  const polish = opts.polishMode ? polishScript() : '';

  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="/libs/tailwindcss-3.4.16.js">${SC}
  <script crossorigin src="${reactSrc}">${SC}
  <script crossorigin src="${reactDomSrc}">${SC}
  <script src="/libs/babel-standalone.min.js">${SC}
  <script src="/libs/echarts-5.min.js">${SC}
  <script src="/libs/pdf.min.js">${SC}
  <style>
    html, body, #root { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
  </style>
</head>
<body>
  <div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;">${loadingLabel}</div></div>

  <script>window.ARTIFACT_DATA = ${embeddedData};${SC}
  <script src="/libs/artifact-globals.js?v=${ARTIFACT_GLOBALS_VERSION}">${SC}

  <script>${polish}${errorBoundaryScript()}${SC}

  ${opts.code}

  <script>${readySignalScript()}${SC}
</body>
</html>`;
}
