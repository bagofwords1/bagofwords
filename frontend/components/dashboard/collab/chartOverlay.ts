// Per-chart marks on the rendered dashboard (collab01): "Changed by Avi" on
// hover, a violet "Avi's agent: editing" outline, "Running as you…" over a
// chart still running with the reader's credentials.
//
// The dashboard runs in a sandboxed iframe without allow-same-origin, so the
// host cannot reach into its DOM. Instead a small listener script is appended
// to the page code: the host posts the marks (BOW_COLLAB_MARKS) and the script
// finds each chart and draws an overlay on a layer of its own. It answers with
// the keys it could place (BOW_COLLAB_LOCATED).
//
// Finding a chart: the runtime's chart components take the chart as a `viz`
// prop (vizById(id) → {id, ...}), so a DOM node is "chart X" when it is the
// outermost element a component with props.viz.id === X renders (read off
// React's fiber on the node). Hand-written markup marked data-bow-viz="<id>"
// counts too. A chart the script cannot find keeps its entry in the Changes
// panel; only the outline is missing.

export type ChartMarkTone = 'changed' | 'agent' | 'pending'

export interface ChartMark {
    /** The chart's element key (or a synthetic key for pending charts). */
    key: string
    /** Visualization ids rendering this chart in the page on screen. */
    ids: string[]
    tone: ChartMarkTone
    label: string
    /** Highlighted from the Changes panel: outline + label shown. */
    active?: boolean
}

export const COLLAB_MARKS_MESSAGE = 'BOW_COLLAB_MARKS'
export const COLLAB_LOCATED_MESSAGE = 'BOW_COLLAB_LOCATED'

const SC = '</' + 'script>'

// Plain ES5 so it runs before (and independently of) the page's Babel pass.
const SCRIPT = `
(function () {
  var marks = [], rtl = false, layer = null, boxes = {}, pointer = null, scanTimer = null, lastLocated = '';
  var COLORS = { changed: '#f59e0b', agent: '#8b5cf6', pending: '#0ea5e9' };

  function ensureLayer() {
    if (layer && layer.isConnected) return layer;
    var style = document.createElement('style');
    style.textContent = '@keyframes __bowCollabSpin{to{transform:rotate(360deg)}}'
      + '.__bow-collab-spin{display:inline-block;width:10px;height:10px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:__bowCollabSpin .8s linear infinite;vertical-align:-1px}';
    document.head.appendChild(style);
    layer = document.createElement('div');
    layer.setAttribute('data-bow-collab-layer', '1');
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483000;';
    document.body.appendChild(layer);
    return layer;
  }

  function fiberOf(el) {
    // Own keys only: a for-in over a DOM node walks hundreds of inherited ones.
    var keys = Object.keys(el);
    for (var i = 0; i < keys.length; i++) { if (keys[i].indexOf('__reactFiber$') === 0) return el[keys[i]]; }
    return null;
  }

  // Ids of charts whose outermost rendered element is el.
  function ownedIds(el) {
    var ids = [];
    var attr = el.getAttribute && el.getAttribute('data-bow-viz');
    if (attr && !/^\\d+$/.test(attr)) ids.push(String(attr));
    var f = fiberOf(el);
    // Walk up through the components until the next DOM element (tag 5) or
    // the root (tag 3): each of them renders el as its outermost node.
    for (var p = f ? f.return : null; p && p.tag !== 5 && p.tag !== 3; p = p.return) {
      var props = p.memoizedProps;
      if (!props || typeof props !== 'object') continue;
      var v = props.viz;
      if (v && typeof v === 'object' && v.id != null) ids.push(String(v.id));
      if (typeof props.vizId === 'string') ids.push(props.vizId);
    }
    return ids;
  }

  function locate() {
    var wanted = {};
    marks.forEach(function (m) { m.ids.forEach(function (id) { wanted[id] = true; }); });
    var found = {};
    var root = document.getElementById('root') || document.body;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.closest && el.closest('[data-bow-collab-layer]')) continue;
      var ids = ownedIds(el);
      for (var j = 0; j < ids.length; j++) {
        // Document order: the first (outermost) element wins.
        if (wanted[ids[j]] && !found[ids[j]]) found[ids[j]] = el;
      }
    }
    return found;
  }

  function render() {
    scanTimer = null;
    ensureLayer();
    var found = locate();
    var seen = {}, located = [];
    marks.forEach(function (m) {
      var el = null;
      for (var i = 0; i < m.ids.length && !el; i++) el = found[m.ids[i]] || null;
      if (!el) return;
      located.push(m.key);
      seen[m.key] = true;
      var box = boxes[m.key];
      if (!box) {
        box = { frame: document.createElement('div'), label: document.createElement('div') };
        box.frame.appendChild(box.label);
        layer.appendChild(box.frame);
        boxes[m.key] = box;
      }
      box.el = el;
      box.mark = m;
      var color = COLORS[m.tone] || COLORS.changed;
      box.frame.setAttribute('data-bow-collab-mark', m.key);
      box.frame.setAttribute('data-bow-collab-tone', m.tone);
      box.frame.style.cssText = 'position:fixed;box-sizing:border-box;border:2px solid ' + color + ';border-radius:10px;'
        + (m.tone === 'pending' ? 'background:rgba(255,255,255,.72);display:flex;align-items:center;justify-content:center;' : '');
      box.label.innerHTML = '';
      if (m.tone === 'pending') {
        var spin = document.createElement('span');
        spin.className = '__bow-collab-spin';
        box.label.appendChild(spin);
        box.label.appendChild(document.createTextNode(' '));
      }
      box.label.appendChild(document.createTextNode(String(m.label || '')));
      box.label.style.cssText = 'font:500 11px/1.4 system-ui,-apple-system,sans-serif;color:#fff;background:' + color + ';'
        + 'padding:2px 8px;border-radius:6px;white-space:nowrap;max-width:90%;overflow:hidden;text-overflow:ellipsis;'
        + (m.tone === 'pending'
          ? 'position:static;color:#0369a1;background:#e0f2fe;'
          : 'position:absolute;top:-11px;' + (rtl ? 'right' : 'left') + ':10px;');
    });
    Object.keys(boxes).forEach(function (key) {
      if (!seen[key]) { boxes[key].frame.remove(); delete boxes[key]; }
    });
    place();
    var sig = located.sort().join('|');
    if (sig !== lastLocated) {
      lastLocated = sig;
      try { window.parent.postMessage({ type: '${COLLAB_LOCATED_MESSAGE}', keys: located }, '*'); } catch (e) {}
    }
  }

  // Changed-by outlines show on hover or when the panel points at them;
  // agent and pending marks always show.
  function place() {
    Object.keys(boxes).forEach(function (key) {
      var b = boxes[key];
      if (!b.el || !b.el.isConnected) { b.frame.style.display = 'none'; return; }
      var r = b.el.getBoundingClientRect();
      var hovered = pointer && pointer.x >= r.left && pointer.x <= r.right && pointer.y >= r.top && pointer.y <= r.bottom;
      var visible = b.mark.tone !== 'changed' || b.mark.active || hovered;
      b.frame.style.display = visible && r.width > 0 && r.height > 0 ? (b.mark.tone === 'pending' ? 'flex' : 'block') : 'none';
      b.frame.style.left = (r.left - 3) + 'px';
      b.frame.style.top = (r.top - 3) + 'px';
      b.frame.style.width = (r.width + 6) + 'px';
      b.frame.style.height = (r.height + 6) + 'px';
    });
  }

  function schedule(delay) {
    if (scanTimer) return;
    scanTimer = setTimeout(render, delay || 0);
  }

  var placing = false;
  function placeSoon() {
    if (placing) return;
    placing = true;
    requestAnimationFrame(function () { placing = false; place(); });
  }

  window.addEventListener('message', function (e) {
    if (e.source !== window.parent || !e.data || e.data.type !== '${COLLAB_MARKS_MESSAGE}') return;
    var list = Array.isArray(e.data.marks) ? e.data.marks : [];
    marks = list.filter(function (m) { return m && typeof m.key === 'string' && Array.isArray(m.ids); }).map(function (m) {
      return { key: m.key, ids: m.ids.map(String), tone: String(m.tone || 'changed'), label: String(m.label || ''), active: !!m.active };
    });
    rtl = !!e.data.rtl;
    if (scanTimer) { clearTimeout(scanTimer); scanTimer = null; }
    render();
  });
  document.addEventListener('mousemove', function (e) { pointer = { x: e.clientX, y: e.clientY }; placeSoon(); }, true);
  document.addEventListener('mouseleave', function () { pointer = null; placeSoon(); });
  window.addEventListener('scroll', placeSoon, true);
  window.addEventListener('resize', placeSoon);
  // The page renders (and re-renders on new data) after this script runs.
  new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var t = records[i].target;
      if (!(t.closest && t.closest('[data-bow-collab-layer]'))) { if (marks.length) schedule(300); return; }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
`

/** The listener script, ready to append to a dashboard's page code. */
export function chartOverlayScriptTag(): string {
    return `<script>${SCRIPT}${SC}`
}

/**
 * The visualization ids that render a chart key on the page on screen: the
 * visualization whose id is the key, else (a copy keeps its source's key)
 * the one with the same title.
 */
export function vizIdsForKey(key: string, title: string | null | undefined, visualizations: Array<{ id: string; title?: string }>): string[] {
    const byId = visualizations.filter((v) => String(v.id) === String(key)).map((v) => String(v.id))
    if (byId.length) return byId
    const want = (title || '').trim().toLowerCase()
    if (!want) return []
    return visualizations.filter((v) => (v.title || '').trim().toLowerCase() === want).map((v) => String(v.id))
}
