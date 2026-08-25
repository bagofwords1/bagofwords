/**
 * Offline params host for standalone HTML exports.
 *
 * In the app an artifact runs inside an iframe whose parent (ArtifactFrame.vue)
 * is authenticated: `useParams()` commits a change, the runtime posts
 * ARTIFACT_SET_PARAMS to that parent, the parent re-runs the underlying queries
 * server-side and pushes fresh rows back down as ARTIFACT_DATA.
 *
 * A standalone export has no such parent. Two things follow, and this file
 * exists because of the first:
 *
 *  1. `post()` in artifact-globals.js does `window.parent.postMessage(...)`.
 *     In a top-level document `window.parent === window`, so the call does NOT
 *     throw — the message is delivered to the page itself, nobody answers it,
 *     and the store's `loading` flag stays true forever. Every param control
 *     would spin indefinitely. Doing nothing is not an option.
 *
 *  2. The queries genuinely cannot re-run: there is no backend. Where it is
 *     sound to do so (see "What may be emulated"), this host answers the intent
 *     by filtering the rows already embedded in the file.
 *
 * `useFilters` needs nothing from this file — it already filters in-browser,
 * over rows that are present, and keeps working in an export untouched.
 *
 * ── What may be emulated ────────────────────────────────────────────────────
 * The embedded rows are a query's stored result, i.e. the rows the SERVER
 * returned for the param values bound when that query last ran. That makes
 * local re-filtering sound in exactly one case: the param had no value applied
 * at export time, so the snapshot is unfiltered along that column and every
 * value the user can pick is still present in the data.
 *
 * If a param DID have a value applied, the snapshot only ever contained the
 * matching slice. Filtering it to a different value would silently return an
 * empty or partial result and present it as fact. Those params are therefore
 * left inert and named in the badge — a control that visibly does nothing is
 * far better than one that quietly lies.
 *
 * Loaded AFTER artifact-globals.js (it drives window.__setArtifactData) and
 * after window.ARTIFACT_DATA is set.
 */
(function () {
  'use strict';

  if (!window.ARTIFACT_DATA || typeof window.__setArtifactData !== 'function') return;

  var config = window.__BOW_OFFLINE_EXPORT_CONFIG || {};

  // The snapshot every recompute starts from. Filtering always derives from
  // THIS, never from the live data — otherwise widening a filter could never
  // recover rows a narrower one removed.
  var baseline = JSON.parse(JSON.stringify(window.ARTIFACT_DATA));
  var baseParams = baseline.params || {};
  var declarations = baseParams.declarations || [];

  var appliedValues = {};
  for (var k in (baseParams.values || {})) appliedValues[k] = baseParams.values[k];

  // ── helpers ────────────────────────────────────────────────────────────────

  function isEmpty(value) {
    return value == null ||
      value === '' ||
      (Array.isArray(value) && value.length === 0);
  }

  /** 'Order Date' / order_date / orderDate all collapse to 'orderdate'. */
  function normalize(name) {
    return String(name == null ? '' : name).toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  // ── what this file can and cannot honour ───────────────────────────────────

  var emulatable = {};   // param name -> true
  var unemulated = {};   // param name -> reason

  (function classify() {
    for (var i = 0; i < declarations.length; i++) {
      var decl = declarations[i];
      if (!decl || !decl.name) continue;

      if (decl.source === 'identity') {
        // Bound server-side per viewer; offline there is no identity to bind.
        unemulated[decl.name] = 'identity';
      } else if (!isEmpty((baseParams.values || {})[decl.name])) {
        // Snapshot is already narrowed along this param — see the header.
        unemulated[decl.name] = 'baked-in';
      } else {
        emulatable[decl.name] = true;
      }
    }
  })();

  window.__BOW_OFFLINE_EXPORT = {
    exportedAt: config.exportedAt || null,
    emulated: Object.keys(emulatable),
    unemulated: Object.keys(unemulated),
    reasons: unemulated,
    values: appliedValues
  };

  /**
   * The row key a param filters on, or null when the param does not name a
   * column present in this visualization (so it changed the SQL, not just a
   * WHERE clause, and cannot be emulated here).
   * viz.columns entries are {field, headerName} dicts, not bare strings.
   */
  function resolveColumn(viz, paramName) {
    var target = normalize(paramName);
    var columns = viz.columns || [];

    for (var i = 0; i < columns.length; i++) {
      var col = columns[i];
      var field = (col && typeof col === 'object') ? col.field : col;
      if (field != null && normalize(field) === target) return field;
    }
    // Fall back to the rows themselves: some steps carry rows whose keys are
    // not mirrored in a columns array.
    var sample = (viz.rows || [])[0];
    if (sample && typeof sample === 'object') {
      for (var key in sample) {
        if (normalize(key) === target) return key;
      }
    }
    return null;
  }

  function compare(a, b) {
    var na = Number(a), nb = Number(b);
    if (!isNaN(na) && !isNaN(nb)) return na < nb ? -1 : (na > nb ? 1 : 0);
    var sa = String(a), sb = String(b);
    return sa < sb ? -1 : (sa > sb ? 1 : 0);
  }

  /**
   * Does one row value satisfy one param value?
   *
   * Params map to SQL predicates, so scalars match by case-insensitive
   * EQUALITY — deliberately unlike useFilters' substring match, which would
   * let a param of "US" select "AUSTRIA".
   */
  function matches(rowValue, paramValue) {
    if (Array.isArray(paramValue)) {
      for (var i = 0; i < paramValue.length; i++) {
        if (String(rowValue).toLowerCase() === String(paramValue[i]).toLowerCase()) return true;
      }
      return false;
    }
    if (paramValue && typeof paramValue === 'object' && ('from' in paramValue || 'to' in paramValue)) {
      if (!isEmpty(paramValue.from) && compare(rowValue, paramValue.from) < 0) return false;
      if (!isEmpty(paramValue.to) && compare(rowValue, paramValue.to) > 0) return false;
      return true;
    }
    return String(rowValue).toLowerCase() === String(paramValue).toLowerCase();
  }

  // ── recompute ──────────────────────────────────────────────────────────────

  function predicatesFor(viz) {
    var out = [];
    for (var i = 0; i < declarations.length; i++) {
      var decl = declarations[i];
      if (!decl || !decl.name || !emulatable[decl.name]) continue;
      if (isEmpty(appliedValues[decl.name])) continue;

      // query_ids scopes a param to the queries that declare it. A declaration
      // with no scope, or a viz with no query id, falls through to applying it:
      // an unscoped param is meant to drive the whole dashboard.
      // The export payload spells it query_id; the in-app host spells it
      // queryId. Accept either so this file works against both.
      var vizQueryId = viz.query_id || viz.queryId;
      var scope = decl.query_ids || [];
      if (scope.length && vizQueryId && scope.indexOf(String(vizQueryId)) === -1) continue;

      var column = resolveColumn(viz, decl.name);
      if (column === null) continue;

      out.push({ column: column, value: appliedValues[decl.name] });
    }
    return out;
  }

  function filterVisualization(viz) {
    var predicates = predicatesFor(viz);
    if (!predicates.length) return viz;

    var next = {};
    for (var key in viz) next[key] = viz[key];
    next.rows = (viz.rows || []).filter(function (row) {
      for (var j = 0; j < predicates.length; j++) {
        var p = predicates[j];
        if (!Object.prototype.hasOwnProperty.call(row, p.column)) continue;
        if (!matches(row[p.column], p.value)) return false;
      }
      return true;
    });
    return next;
  }

  function recompute(ack) {
    var next = {};
    for (var key in baseline) next[key] = baseline[key];

    next.visualizations = (baseline.visualizations || []).map(filterVisualization);
    next.params = {
      declarations: declarations,
      values: appliedValues,
      options: baseParams.options || {},
      // Echoing the commit seq is what releases the store's in-flight values
      // and clears `loading` — see __paramStore._ingest.
      ack: ack
    };

    window.__BOW_OFFLINE_EXPORT.values = appliedValues;

    // Applied directly rather than posted back. postMessage would work in a
    // top-level document (window.parent === window, so the runtime's
    // `e.source !== window.parent` guard passes), but breaks the moment the
    // exported file is itself embedded in an iframe. A direct call behaves
    // identically to a host push in both cases.
    window.__setArtifactData(next);
  }

  // ── intent bridge ──────────────────────────────────────────────────────────

  var lastSeq = 0;

  window.addEventListener('message', function (event) {
    var data = event && event.data;
    if (!data || !data.type) return;

    if (data.type === 'ARTIFACT_SET_PARAMS') {
      var changes = data.changes || {};
      for (var name in changes) appliedValues[name] = changes[name];
      lastSeq = Math.max(lastSeq, Number(data.seq) || 0);
      // `targets` is ignored on purpose: it exists so the host can re-run only
      // the affected queries, but recomputing every visualization from the
      // baseline with the current values is equivalent and cannot drift.
      recompute(lastSeq);
    } else if (data.type === 'ARTIFACT_REFRESH_PARAMS') {
      // Nothing to re-fetch offline; answering still clears the spinner
      // refresh() turned on.
      recompute(lastSeq);
    }
  });

  if (window.top !== window.self) {
    // Embedded: artifact-globals posts its intent to the real outer frame, so
    // the listener above never sees it and controls will not respond.
    console.warn(
      '[bow-export] This dashboard was exported as a standalone file. ' +
      'Open it directly rather than in an iframe for its filters to work.'
    );
  }

  // ── snapshot badge ─────────────────────────────────────────────────────────

  function renderBadge() {
    if (!config.showBadge) return;

    var names = Object.keys(unemulated);
    // Nothing worth saying: no params at all, or every one of them works.
    if (!declarations.length && !config.exportedAt) return;

    var badge = document.createElement('div');
    badge.setAttribute('data-bow-export-badge', '');
    badge.style.cssText = [
      'position:fixed', 'bottom:12px', 'right:12px', 'z-index:2147483647',
      'max-width:320px', 'padding:8px 12px', 'border-radius:8px',
      'background:rgba(17,24,39,0.92)', 'color:#f9fafb',
      'font:12px/1.45 system-ui,-apple-system,sans-serif',
      'box-shadow:0 4px 14px rgba(0,0,0,0.22)', 'cursor:pointer'
    ].join(';');
    badge.title = 'Dismiss';
    badge.addEventListener('click', function () {
      if (badge.parentNode) badge.parentNode.removeChild(badge);
    });

    var text = 'Offline snapshot';
    if (config.exportedAt) text += ' · data as of ' + config.exportedAt;
    if (names.length) {
      text += '. These filters need a live connection and are not applied: ' +
        names.join(', ') + '.';
    }
    badge.textContent = text;
    document.body.appendChild(badge);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderBadge);
  } else {
    renderBadge();
  }
})();
