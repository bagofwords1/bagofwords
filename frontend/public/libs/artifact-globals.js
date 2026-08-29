/**
 * artifact-globals.js — Single source of truth for sandbox runtime globals.
 *
 * Loaded by: ArtifactFrame.vue, r/[id]/index.vue, artifact_libs.py (headless).
 * Requires: React 18, ReactDOM 18, echarts 5, Tailwind CSS loaded beforehand.
 * Expects: window.ARTIFACT_DATA set before this script runs.
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

  // ── Live data store ─────────────────────────────────────────────────────────
  // The host can push fresh ARTIFACT_DATA at any time (param changes, view-as
  // swaps) via postMessage without reloading the iframe. Components subscribe
  // through useArtifactData()/useParams() and re-render on push.
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

  // ── useArtifactData() ───────────────────────────────────────────────────────
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

  // ── vizById() ───────────────────────────────────────────────────────────────
  // Stable, id-keyed access to a visualization's data. Preferred over
  // positional `viz[N]` (which silently repoints at the wrong dataset when
  // the viz set changes). Positional access remains supported for legacy
  // artifacts — this runtime only ever grows.
  window.vizById = function(id) {
    var data = window.ARTIFACT_DATA || {};
    var list = data.visualizations || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].id) === String(id)) return list[i];
    }
    return null;
  };

  // ── useCurrentUser() ────────────────────────────────────────────────────────
  // The viewing user, injected by the host as ARTIFACT_DATA.current_user:
  // { id, name, email, image_url, role, profile_attributes } — every field
  // nullable. Returns null for anonymous viewers and headless renders, so
  // callers must guard: useCurrentUser()?.name.
  window.useCurrentUser = function() {
    var data = window.ARTIFACT_DATA || {};
    return data.current_user || null;
  };

  // ── LoadingSpinner ──────────────────────────────────────────────────────────
  window.LoadingSpinner = function(props) {
    var size = props && props.size ? props.size : 24;
    return h('svg', {
      xmlns: 'http://www.w3.org/2000/svg', width: size, height: size,
      viewBox: '0 0 24 24', className: props && props.className ? props.className : ''
    },
      h('path', { fill: 'currentColor', d: 'M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8A8 8 0 0 1 12 20Z', opacity: '0.5' }),
      h('path', { fill: 'currentColor', d: 'M20 12h2A10 10 0 0 0 12 2V4A8 8 0 0 1 20 12Z' },
        h('animateTransform', { attributeName: 'transform', dur: '1s', from: '0 12 12', repeatCount: 'indefinite', to: '360 12 12', type: 'rotate' }))
    );
  };

  // ── fmt() number formatter ──────────────────────────────────────────────────
  window.fmt = function(n, opts) {
    if (n == null) return '\u2014';
    if (typeof n !== 'number') return String(n);
    opts = opts || {};
    if (opts.currency) return new Intl.NumberFormat('en-US', { style: 'currency', currency: opts.currency === true ? 'USD' : opts.currency, maximumFractionDigits: opts.decimals != null ? opts.decimals : 0 }).format(n);
    if (opts.pct) return n.toFixed(1) + '%';
    if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  };

  // ── exportCSV() — trigger client-side CSV download ──────────────────────────
  // Signature: exportCSV(rows, { columns, filename } = {})
  //   rows      — array of objects (required)
  //   columns   — optional. Either viz.columns ([{field,...}]) or string[] of keys.
  //               Defaults to Object.keys(rows[0]).
  //   filename  — optional, defaults to 'export.csv'. '.csv' appended if missing.
  // Serializes RFC 4180 CSV with UTF-8 BOM (so Excel opens correctly).
  window.exportCSV = function(rows, opts) {
    opts = opts || {};
    if (!Array.isArray(rows) || rows.length === 0) {
      console.warn('[exportCSV] no rows to export');
      return;
    }
    var fields;
    if (Array.isArray(opts.columns) && opts.columns.length > 0) {
      fields = opts.columns.map(function(c) {
        return typeof c === 'string' ? c : (c && c.field);
      }).filter(Boolean);
    } else {
      fields = Object.keys(rows[0] || {});
    }
    if (fields.length === 0) {
      console.warn('[exportCSV] no columns to export');
      return;
    }

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
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 0);
  };

  // ── CustomTooltip ───────────────────────────────────────────────────────────
  window.CustomTooltip = function(props) {
    if (!props.active || !props.payload || !props.payload.length) return null;
    return h('div', { className: 'bg-slate-900 text-white px-4 py-3 rounded-xl shadow-xl border border-slate-700/50 text-sm' }, [
      h('p', { key: 'l', className: 'font-medium text-slate-300 mb-1' }, props.label),
    ].concat(props.payload.map(function(p, i) {
      return h('p', { key: i, className: 'flex items-center gap-2' }, [
        h('span', { key: 'd', className: 'w-2 h-2 rounded-full inline-block', style: { backgroundColor: p.color } }),
        h('span', { key: 'n', className: 'text-slate-400' }, p.name + ': '),
        h('span', { key: 'v', className: 'font-semibold' }, typeof p.value === 'number' ? p.value.toLocaleString() : p.value),
      ]);
    })));
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // FIX 1: Filter store + useFilters — filterRows reads FRESH from store
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
        return function() {
          var idx = listeners.indexOf(fn);
          if (idx >= 0) listeners.splice(idx, 1);
        };
      }
    };
  })();

  window.useFilters = function() {
    var _s = React.useState(0);
    var forceUpdate = _s[1];

    React.useEffect(function() {
      return window.__filterStore.sub(function() {
        forceUpdate(function(c) { return c + 1; });
      });
    }, []);

    // Snapshot for identity-based deps (useMemo, useCallback downstream)
    var filters = window.__filterStore.get();

    // FIX: filterRows always reads LIVE from the store, never a stale closure.
    // useCallback dep on `filters` ensures identity changes so downstream
    // useMemo([filterRows]) re-runs correctly.
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

    return {
      filters: filters,
      setFilter: window.__filterStore.set,
      resetFilters: window.__filterStore.reset,
      filterRows: filterRows
    };
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // Params store + useParams — server-side query parameters.
  //
  // Unlike useFilters (client-side, filters rows already in the browser),
  // params RE-RUN the underlying queries at the source: the iframe posts the
  // intent to the authenticated host page, the host calls the backend, and
  // fresh rows arrive via a new ARTIFACT_DATA push. Declarations come from
  // ARTIFACT_DATA.params: { declarations: [{name, type, label, source,
  // default, required, options, query_ids}], values: {name: value} }.
  // Identity-source params are locked server-side — they carry no client
  // value and render as "scoped to you", never as an input.
  // ═══════════════════════════════════════════════════════════════════════════

  window.__paramStore = (function() {
    var declarations = [];
    var values = {};
    var options = {};   // name -> [{value, label}] — host-resolved stable choices
    var pending = {};
    // Committed-but-unconfirmed changes. The user's latest input stays
    // authoritative through the host round trip: getValues() overlays
    // values with inflight and pending, so a controlled input can never
    // snap back to a stale applied value while a run is in the air.
    var inflight = {};
    var inflightSeq = {};   // name -> commit seq that last wrote it
    var commitSeq = 0;
    var loading = false;
    var error = null;
    var listeners = [];
    var timer = null;

    function notify() {
      for (var i = 0; i < listeners.length; i++) {
        try { listeners[i](); } catch (e) {}
      }
    }

    function post(msg) {
      try { window.parent.postMessage(msg, '*'); } catch (e) {}
    }

    function commit(targets) {
      var changes = {};
      var any = false;
      commitSeq += 1;
      for (var k in pending) {
        changes[k] = pending[k];
        inflight[k] = pending[k];
        inflightSeq[k] = commitSeq;
        any = true;
      }
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
        // Confirm inflight keys the host has caught up on. ack is the highest
        // ARTIFACT_SET_PARAMS seq the host has seen; a key committed at or
        // below it is reflected in this echo (possibly server-normalized), so
        // the echoed value becomes truth. A key committed later keeps its
        // local value — this push predates it. Without ack (older host),
        // fall back to value equality.
        var ack = (typeof p.ack === 'number') ? p.ack : null;
        for (var k in inflight) {
          var confirmed = (ack !== null)
            ? (inflightSeq[k] <= ack)
            : (JSON.stringify(values[k]) === JSON.stringify(inflight[k]));
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
      // Applied values overlaid with everything newer the user did locally
      // (committed-in-flight, then still-debouncing pending) — what a
      // controlled input should bind so typing never resets mid round-trip.
      getValues: function() {
        var out = {};
        var k;
        for (k in values) out[k] = values[k];
        for (k in inflight) out[k] = inflight[k];
        for (k in pending) out[k] = pending[k];
        return out;
      },
      // Stable [{value, label}] choices for one param: host-resolved rows
      // (static options or an options-source query) — never the currently
      // filtered data, so selecting a value can't collapse the list.
      getOptions: function(name) {
        if (options && options[name] && options[name].length) return options[name];
        for (var i = 0; i < declarations.length; i++) {
          var d = declarations[i];
          if (d && d.name === name && d.options && d.options.length) {
            return d.options.map(function(v) {
              return (v && typeof v === 'object' && 'value' in v)
                ? v : { value: v, label: String(v) };
            });
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
      apply: function(targets) {
        if (timer) { clearTimeout(timer); timer = null; }
        commit(targets || null);
      },
      refresh: function(targets) {
        loading = true;
        notify();
        post({ type: 'ARTIFACT_REFRESH_PARAMS', targets: targets || null });
      },
      sub: function(fn) {
        listeners.push(fn);
        return function() {
          var idx = listeners.indexOf(fn);
          if (idx >= 0) listeners.splice(idx, 1);
        };
      }
    };
  })();

  window.useParams = function() {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      return window.__paramStore.sub(function() {
        forceUpdate(function(c) { return c + 1; });
      });
    }, []);
    var store = window.__paramStore;
    return {
      declarations: store.getDeclarations(),
      values: store.getValues(),
      pending: store.getPending(),
      loading: store.isLoading(),
      error: store.getError(),
      setParam: store.setParam,
      setParams: store.setParams,
      apply: store.apply,
      refresh: store.refresh,
      getOptions: store.getOptions
    };
  };

  // Stable choice list for one declared param — [{value, label}] or null.
  // Re-renders when the host pushes fresh params (e.g. the options query
  // refreshed). See __paramStore.getOptions for resolution order.
  window.useParamOptions = function(name) {
    var _s = React.useState(0);
    var forceUpdate = _s[1];
    React.useEffect(function() {
      return window.__paramStore.sub(function() {
        forceUpdate(function(c) { return c + 1; });
      });
    }, []);
    return window.__paramStore.getOptions(name);
  };

  // Seed the store from the data embedded in the initial srcdoc.
  try { window.__paramStore._ingest(window.ARTIFACT_DATA || {}); } catch (e) {}

  // Host → iframe bridge: fresh data pushes and param-run status. Only the
  // parent frame is trusted.
  window.addEventListener('message', function(e) {
    if (e.source !== window.parent || !e.data) return;
    if (e.data.type === 'ARTIFACT_DATA' && e.data.payload) {
      window.__setArtifactData(e.data.payload);
    } else if (e.data.type === 'ARTIFACT_PARAMS_STATUS') {
      window.__paramStore._status(e.data.payload || {});
    }
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // InfoPopover — built-in provenance popup for prebuilt components.
  // Pass a `viz` object (from useArtifactData().visualizations) to KPICard /
  // SectionCard and a small "i" button appears that opens a clean panel showing
  // the visualization's backing data: source, query, columns, filters, etc.
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

  // Format a single cell value for the data table.
  function _infoCell(v) {
    if (v == null) return '—';
    if (typeof v === 'number') return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
    if (typeof v === 'object') { try { return JSON.stringify(v); } catch (e) { return String(v); } }
    return String(v);
  }

  // Normalize a viz's columns to { field, header, dtype }, falling back to row keys.
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

  // Compact metadata summary for the Data tab header.
  function _infoMeta(viz) {
    var dm = viz.dataModel || {};
    var view = viz.view || {};
    var innerView = view.view || view;
    var type = dm.type || innerView.type;
    var rowCount = Array.isArray(viz.rows) ? viz.rows.length : (viz.row_count != null ? viz.row_count : null);
    return {
      source: viz.dataSource || null,
      type: type ? String(type).replace(/_/g, ' ') : null,
      rowCount: rowCount,
      aggregation: innerView.aggregation || null
    };
  }

  // Format the optional `calc` prop (string or structured object) into a
  // human-readable formula, e.g. "SUM(UnitPrice × Quantity), grouped by Genre".
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

  // Derive an ordered list of { label, value, ... } rows from a viz object.
  function buildInfoRows(viz) {
    if (!viz || typeof viz !== 'object') return [];
    var rows = [];
    var dm = viz.dataModel || {};
    var view = viz.view || {};
    var innerView = view.view || view;
    var type = dm.type || innerView.type;

    if (viz.dataSource) rows.push({ label: 'Source', value: String(viz.dataSource) });
    if (type) rows.push({ label: 'Type', value: String(type).replace(/_/g, ' ') });

    var rowCount = Array.isArray(viz.rows) ? viz.rows.length
      : (viz.row_count != null ? viz.row_count : null);
    if (rowCount != null) rows.push({ label: 'Rows', value: String(rowCount) });

    var cols = viz.columns || [];
    if (cols.length) {
      var colText = cols.map(function(c) {
        if (typeof c === 'string') return c;
        var f = c.headerName || c.field || c.name || '';
        var dt = c.dtype ? '  · ' + c.dtype : '';
        return f + dt;
      }).join('\n');
      rows.push({ label: 'Columns (' + cols.length + ')', value: colText, pre: true });
    }

    var agg = innerView.aggregation;
    if (agg) rows.push({ label: 'Aggregation', value: String(agg) });

    var defFilters = innerView.defaultFilters || [];
    if (defFilters.length) {
      rows.push({
        label: 'Default filters',
        value: defFilters.map(function(f) {
          return (f.column || '') + ' ' + (f.operator || '=') + ' ' + _infoFilterVal(f.value);
        }).join('\n'),
        pre: true
      });
    }

    try {
      var active = window.__filterStore ? window.__filterStore.get() : {};
      var akeys = Object.keys(active || {});
      if (akeys.length) {
        rows.push({
          label: 'Active filters',
          value: akeys.map(function(k) { return k + ': ' + _infoFilterVal(active[k]); }).join('\n'),
          pre: true
        });
      }
    } catch (e) {}

    if (viz.description) rows.push({ label: 'Description', value: String(viz.description) });
    if (viz.code) rows.push({ label: 'Query', value: String(viz.code), code: true });
    if (viz.id) rows.push({ label: 'ID', value: String(viz.id), mono: true });
    return rows;
  }
  window.buildInfoRows = buildInfoRows;

  // Shared renderer for the "Data" tab body (calculation + meta + rows table).
  // Used by both the per-component InfoPopover and the global DataInspector.
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

    // Download the rows this tab displays (filtered set when present, full
    // otherwise) — the FULL array, not the 100-row on-screen slice.
    var exportBtn = (dataRows.length && cols.length) ? h('button', {
      key: 'dl', type: 'button', 'data-testid': 'bow-popover-export',
      title: 'Download CSV' + (isFiltered ? ' (filtered rows)' : ''),
      onClick: function(e) {
        e.stopPropagation();
        window.exportCSV(dataRows, {
          columns: cols.map(function(c) { return c.field; }),
          filename: (viz.title || 'export')
        });
      },
      className: 'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium text-slate-400 hover:text-slate-700 hover:bg-slate-100 dark:hover:text-slate-200 dark:hover:bg-slate-700/60 transition-colors shrink-0'
    }, [
      h('svg', { key: 'i', width: 11, height: 11, viewBox: '0 0 16 16', fill: 'none' }, [
        h('path', { key: 'a', d: 'M8 2v8m0 0l-3-3m3 3l3-3', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' }),
        h('path', { key: 'b', d: 'M3 13h10', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' })
      ]),
      h('span', { key: 't' }, 'CSV')
    ]) : null;

    return h('div', { key: 'data', style: { display: 'flex', flexDirection: 'column', gap: 8 } }, [
      calcText ? h('div', { key: 'calc' }, [
        h('div', { key: 'l', className: 'text-[10px] font-medium uppercase tracking-wide text-slate-400 mb-1' }, 'Calculation'),
        h('div', { key: 'v', className: 'text-xs font-mono text-slate-700 bg-slate-50 border border-slate-100 dark:text-slate-200 dark:bg-slate-800 dark:border-slate-700 rounded-md px-2 py-1.5' }, calcText)
      ]) : null,
      (metaBits.length || exportBtn) ? h('div', { key: 'm', className: 'flex items-center justify-between gap-2' }, [
        h('span', { key: 'mb', className: 'text-[11px] text-slate-400' }, metaBits.join('  ·  ')),
        exportBtn
      ]) : null,
      filterNote ? h('div', { key: 'af', className: 'text-[11px] text-slate-500' }, filterNote) : null,
      cols.length ? h('div', {
        key: 'tbl', className: 'border border-slate-100 dark:border-slate-700 rounded-md overflow-auto', style: { maxHeight: 300 }
      }, h('table', { className: 'border-collapse', style: { minWidth: '100%' } }, [
        h('thead', { key: 'h' }, h('tr', {}, cols.map(function(c, i) {
          return h('th', {
            key: i,
            className: 'text-left font-medium text-slate-500 bg-slate-50 border-b border-slate-100 dark:text-slate-400 dark:bg-slate-800 dark:border-slate-700 px-2 py-1.5 whitespace-nowrap sticky top-0',
            style: { fontSize: 11 }
          }, c.header);
        }))),
        h('tbody', { key: 'b' }, dataRows.slice(0, MAXR).map(function(row, ri) {
          return h('tr', { key: ri, className: ri % 2 ? 'bg-slate-50/40 dark:bg-slate-800/40' : '' }, cols.map(function(c, ci) {
            var cell = _infoCell(row[c.field]);
            return h('td', {
              key: ci, title: cell,
              className: 'px-2 py-1 text-slate-700 border-b border-slate-50 dark:text-slate-200 dark:border-slate-800 whitespace-nowrap',
              style: { fontSize: 11, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }
            }, cell);
          }));
        }))
      ])) : h('div', { key: 'nd', className: 'text-xs text-slate-400 py-4 text-center' }, 'No data available'),
      (dataRows.length > MAXR) ? h('div', { key: 'more', className: 'text-[10px] text-slate-400' },
        'Showing ' + MAXR + ' of ' + dataRows.length + ' rows') : null
    ]);
  }

  // Shared renderer for the "Code" tab body (the generating query).
  function _codeTabBody(viz) {
    return h('div', { key: 'code' }, viz.code
      ? h('pre', {
          className: 'text-[11px] font-mono text-slate-700 bg-slate-50 border border-slate-100 dark:text-slate-200 dark:bg-slate-800 dark:border-slate-700 rounded-md p-2 overflow-auto',
          style: { maxHeight: 340, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }
        }, viz.code)
      : h('div', { className: 'text-xs text-slate-400 py-4 text-center' }, 'No query available for this visualization.'));
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
      return function() {
        document.removeEventListener('mousedown', onDown);
        document.removeEventListener('keydown', onKey);
      };
    }, [open]);

    React.useEffect(function() {
      if (!open || !btnRef.current) return;
      function reposition() {
        if (!btnRef.current) return;
        var r = btnRef.current.getBoundingClientRect();
        var W = 400;
        var left = Math.min(r.right - W, window.innerWidth - W - 8);
        if (left < 8) left = 8;
        var spaceBelow = window.innerHeight - r.bottom;
        var below = spaceBelow > 240;
        setPos({
          left: left,
          top: below ? r.bottom + 6 : null,
          bottom: below ? null : (window.innerHeight - r.top + 6),
          width: W
        });
      }
      reposition();
      window.addEventListener('scroll', reposition, true);
      window.addEventListener('resize', reposition);
      return function() {
        window.removeEventListener('scroll', reposition, true);
        window.removeEventListener('resize', reposition);
      };
    }, [open]);

    if (!viz) return null;

    function tabButton(id, label) {
      var active = tab === id;
      return h('button', {
        key: id, type: 'button', onClick: function() { setTab(id); },
        className: 'px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors '
          + (active ? 'border-slate-800 text-slate-800 dark:border-slate-200 dark:text-slate-200' : 'border-transparent text-slate-400 hover:text-slate-600 dark:hover:text-slate-300')
      }, label);
    }

    var dataBody = _dataTabBody(viz, { rows: props.rows, calc: props.calc });
    var codeBody = _codeTabBody(viz);

    var panel = (open && pos) ? ReactDOM.createPortal(
      h('div', {
        ref: panelRef,
        className: 'bg-white border border-slate-200 dark:bg-slate-900 dark:border-slate-700 rounded-lg shadow-xl',
        style: {
          position: 'fixed', left: pos.left,
          top: pos.top != null ? pos.top : undefined,
          bottom: pos.bottom != null ? pos.bottom : undefined,
          width: pos.width, zIndex: 99999, maxHeight: '72vh',
          display: 'flex', flexDirection: 'column'
        }
      }, [
        h('div', { key: 'hd', className: 'flex items-start justify-between gap-2 px-3.5 pt-2.5 pb-1' }, [
          h('div', { key: 't', className: 'text-xs font-semibold text-slate-800 dark:text-slate-200 leading-snug' }, viz.title || 'Details'),
          h('button', {
            key: 'x', type: 'button', 'aria-label': 'Close',
            onClick: function() { setOpen(false); },
            className: 'shrink-0 -mt-0.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300'
          }, h('svg', { width: 14, height: 14, viewBox: '0 0 14 14', fill: 'none' },
            h('path', { d: 'M3.5 3.5l7 7M10.5 3.5l-7 7', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' })))
        ]),
        h('div', { key: 'tabs', className: 'flex gap-1 px-3 border-b border-slate-100 dark:border-slate-700' }, [
          tabButton('data', 'Data'),
          tabButton('code', 'Code')
        ]),
        h('div', { key: 'bd', className: 'px-3.5 py-3 overflow-auto' }, tab === 'code' ? codeBody : dataBody),
        viz.id ? h('div', {
          key: 'ft', className: 'px-3.5 py-2 border-t border-slate-100 dark:border-slate-700 text-[10px] font-mono text-slate-400 break-all'
        }, 'ID  ' + viz.id) : null
      ]),
      document.body
    ) : null;

    return h('span', { className: 'inline-flex align-middle' }, [
      h('button', {
        key: 'btn', ref: btnRef, type: 'button', 'aria-label': 'Details',
        onClick: function(e) { e.stopPropagation(); setOpen(function(o) { return !o; }); },
        className: 'inline-flex items-center justify-center w-5 h-5 rounded-full transition-colors '
          + (open ? 'text-slate-600 bg-slate-100 dark:text-slate-300 dark:bg-slate-700' : 'text-slate-300 hover:text-slate-500 hover:bg-slate-100 dark:text-slate-600 dark:hover:text-slate-400 dark:hover:bg-slate-800')
      }, h('svg', { width: 15, height: 15, viewBox: '0 0 16 16', fill: 'none' }, [
        h('circle', { key: 'c', cx: 8, cy: 8, r: 6.4, stroke: 'currentColor', strokeWidth: 1.2 }),
        h('circle', { key: 'd', cx: 8, cy: 5.2, r: 0.95, fill: 'currentColor' }),
        h('path', { key: 'b', d: 'M8 7.4v4', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round' })
      ])),
      panel
    ]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // FIX 2: KPICard / SectionCard — additive className + style pass-through
  // ═══════════════════════════════════════════════════════════════════════════

  window.KPICard = function(props) {
    var color = props.color || '#3B82F6';
    // Structural classes always applied; className adds to (not replaces) defaults
    var cls = 'relative rounded-2xl border p-5 shadow-sm overflow-hidden bg-white border-slate-200 text-slate-900 dark:bg-slate-900 dark:border-slate-700 dark:text-slate-100'
      + (props.className ? ' ' + props.className : '');
    var titleCls = 'text-xs font-medium uppercase tracking-wider mb-1 text-slate-500 dark:text-slate-400'
      + (props.titleClassName ? ' ' + props.titleClassName : '');
    var subtitleCls = 'text-sm mt-1 text-slate-500 dark:text-slate-400'
      + (props.subtitleClassName ? ' ' + props.subtitleClassName : '');
    return h('div', { className: cls, style: props.style }, [
      h('div', { key: 'bar', className: 'absolute inset-x-0 top-0 h-1', style: { background: 'linear-gradient(90deg, ' + color + ', ' + color + '99)' } }),
      props.viz ? h('div', { key: 'info', className: 'absolute top-2.5 right-2.5 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null,
      h('p', { key: 't', className: titleCls }, props.title),
      h('p', { key: 'v', className: 'text-2xl font-semibold' }, props.value),
      props.subtitle ? h('p', { key: 's', className: subtitleCls }, props.subtitle) : null,
    ]);
  };

  window.SectionCard = function(props) {
    var cls = 'relative rounded-2xl border shadow-sm p-6 bg-white border-slate-200 dark:bg-slate-900 dark:border-slate-700'
      + (props.className ? ' ' + props.className : '');
    var titleCls = 'text-lg font-semibold text-slate-800 dark:text-slate-100'
      + (props.titleClassName ? ' ' + props.titleClassName : '');
    var subtitleCls = 'text-sm mt-1 text-slate-500 dark:text-slate-400'
      + (props.subtitleClassName ? ' ' + props.subtitleClassName : '');
    return h('div', { className: cls, style: props.style }, [
      props.viz ? h('div', { key: 'info', className: 'absolute top-3 right-3 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })) : null,
      props.title ? h('div', { key: 'hdr', className: 'mb-4 pr-6' }, [
        h('h2', { key: 't', className: titleCls }, props.title),
        props.subtitle ? h('p', { key: 's', className: subtitleCls }, props.subtitle) : null,
      ]) : null,
      h('div', { key: 'body' }, props.children),
    ]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // DataTable — sortable, paginated, RTL-aware table with hover/selection
  // highlight, numeric formatting and CSV export. The default renderer for
  // table visualizations; prefer it over hand-rolled <table> markup.
  // ═══════════════════════════════════════════════════════════════════════════

  // Print support: pagination hides rows from the screen but they stay in the
  // DOM (up to printCap), so PDF export shows the whole table.
  (function() {
    var st = document.createElement('style');
    st.textContent =
      '@media print {' +
      '  .bow-dt-offpage { display: table-row !important; }' +
      '  .bow-dt-chrome { display: none !important; }' +
      '  .bow-dt-scroll { max-height: none !important; overflow: visible !important; }' +
      '}';
    (document.head || document.documentElement).appendChild(st);
  })();

  var _RTL_CHARS = /[֐-׿؀-ۿ܀-ݏיִ-ﭏ]/g;

  // Heuristic direction detection: compare strong-RTL vs latin letter counts
  // in the headers and a sample of string cell values.
  function _dtDetectDir(cols, rows) {
    var sample = '';
    for (var i = 0; i < cols.length; i++) sample += (cols[i].header || '') + ' ';
    for (var r = 0; r < rows.length && r < 5; r++) {
      var row = rows[r] || {};
      for (var c = 0; c < cols.length; c++) {
        var v = row[cols[c].field];
        if (typeof v === 'string') sample += v + ' ';
      }
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

  // null/undefined sort last regardless of direction; numbers (and numeric
  // strings) compare numerically, everything else locale-compares.
  function _dtCompare(a, b) {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    var na = typeof a === 'number' ? a : (a !== '' && !isNaN(Number(a)) ? Number(a) : null);
    var nb = typeof b === 'number' ? b : (b !== '' && !isNaN(Number(b)) ? Number(b) : null);
    if (na != null && nb != null) return na - nb;
    return String(a).localeCompare(String(b));
  }

  window.DataTable = function(props) {
    var viz = props.viz || {};
    var baseRows = Array.isArray(props.rows) ? props.rows
      : (Array.isArray(viz.rows) ? viz.rows : []);
    var colSource = Array.isArray(props.columns) && props.columns.length
      ? { columns: props.columns } : viz;
    var cols = React.useMemo(function() {
      return _infoCols(colSource, baseRows);
    }, [props.columns, viz.columns, baseRows]);

    var sortable = props.sortable !== false;
    var selectable = props.selectable !== false;
    var exportable = props.exportable !== false;
    var searchable = !!props.searchable;
    var pageSize = props.pageSize != null ? props.pageSize : 15;
    var printCap = props.printCap != null ? props.printCap : 1000;
    // When paginated, the page size already bounds the height — a scroll clamp
    // on top of pagination would hide part of the current page. The default
    // 400px clamp only applies to non-paginated (pageSize=0) tables.
    var maxHeight = props.maxHeight != null ? props.maxHeight
      : (pageSize > 0 ? 'none' : 400);

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

    // search → sort → paginate
    var filtered = React.useMemo(function() {
      if (!query) return baseRows;
      var q = query.toLowerCase();
      return baseRows.filter(function(row) {
        for (var i = 0; i < cols.length; i++) {
          var v = row ? row[cols[i].field] : null;
          if (v != null && String(v).toLowerCase().indexOf(q) !== -1) return true;
        }
        return false;
      });
    }, [baseRows, cols, query]);

    var sorted = React.useMemo(function() {
      if (!sort) return filtered;
      var out = filtered.slice();
      out.sort(function(ra, rb) {
        var a = ra ? ra[sort.field] : null;
        var b = rb ? rb[sort.field] : null;
        // nulls last in BOTH directions — outside the desc negation
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
    // DOM rows: everything up to printCap so print/PDF sees the full table;
    // rows outside the current page are hidden on screen via .bow-dt-offpage.
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

    // ── toolbar (search / export) ──
    var toolbar = null;
    if (searchable || exportable) {
      toolbar = h('div', {
        key: 'tb',
        className: 'bow-dt-chrome flex items-center justify-between gap-2 mb-2'
      }, [
        searchable ? h('input', {
          key: 's', type: 'text', value: query,
          'data-testid': 'bow-dt-search',
          placeholder: props.searchPlaceholder || 'Search...',
          onChange: function(e) { setQuery(e.target.value); setPage(0); },
          className: 'rounded-lg border border-slate-200 text-slate-700 bg-white dark:border-slate-700 dark:text-slate-200 dark:bg-slate-900 dark:placeholder-slate-500 px-3 py-1.5 text-sm outline-none focus:border-blue-400 min-w-[160px]'
        }) : h('span', { key: 's' }),
        exportable ? h('button', {
          key: 'x', type: 'button', 'data-testid': 'bow-dt-export',
          title: 'Download CSV',
          onClick: function() {
            window.exportCSV(sorted, {
              columns: cols.map(function(c) { return c.field; }),
              filename: (viz.title || 'table')
            });
          },
          className: 'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-500 border border-slate-200 hover:text-slate-800 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-400 dark:hover:text-slate-200 dark:hover:bg-slate-800 transition-colors shrink-0'
        }, [
          h('svg', { key: 'i', width: 12, height: 12, viewBox: '0 0 16 16', fill: 'none' }, [
            h('path', { key: 'a', d: 'M8 2v8m0 0l-3-3m3 3l3-3', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' }),
            h('path', { key: 'b', d: 'M3 13h10', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' })
          ]),
          h('span', { key: 't' }, 'CSV')
        ]) : null
      ]);
    }

    // ── header ──
    var thead = h('thead', { key: 'h', className: 'text-xs uppercase bg-slate-50 dark:bg-slate-800 sticky top-0 z-[1]' },
      h('tr', {}, cols.map(function(c, i) {
        var numeric = numericByField[c.field];
        var isSorted = sort && sort.field === c.field;
        return h('th', {
          key: i,
          onClick: function() { clickHeader(c.field); },
          'aria-sort': isSorted ? (sort.desc ? 'descending' : 'ascending') : undefined,
          className: 'px-4 py-3 font-medium text-slate-500 dark:text-slate-400 whitespace-nowrap select-none '
            + (numeric ? 'text-end' : 'text-start')
            + (sortable ? ' cursor-pointer hover:text-slate-800 dark:hover:text-slate-200' : '')
        }, h('span', { className: 'inline-flex items-center gap-1' }, [
          h('span', { key: 't' }, c.header),
          sortable ? h('span', {
            key: 'a',
            className: 'text-[9px] leading-none ' + (isSorted ? 'text-slate-700 dark:text-slate-200' : 'text-slate-300 dark:text-slate-600')
          }, isSorted ? (sort.desc ? '▼' : '▲') : '⇅') : null
        ]));
      })));

    // ── body ──
    var tbody = h('tbody', { key: 'b' }, domRows.map(function(row, i) {
      var onPage = i >= start && i < end;
      var isSel = selectable && selRow === row;
      var zebra = props.striped && ((i - start) % 2 === 1);
      return h('tr', {
        key: i,
        onClick: function() { clickRow(row, i); },
        className: (onPage ? '' : 'bow-dt-offpage hidden ')
          + 'border-b border-slate-100 dark:border-slate-800 transition-colors '
          + (isSel ? 'bg-blue-50 hover:bg-blue-50 dark:bg-blue-950/40 dark:hover:bg-blue-950/40 '
              : (zebra ? 'bg-slate-50/40 dark:bg-slate-800/40 ' : '') + 'hover:bg-slate-100 dark:hover:bg-slate-800 ')
          + ((selectable || props.onRowClick) ? 'cursor-pointer' : '')
      }, cols.map(function(c, j) {
        var numeric = numericByField[c.field];
        return h('td', {
          key: j,
          dir: 'auto',
          className: 'px-4 py-2 text-slate-700 dark:text-slate-200 ' + (numeric ? 'text-end tabular-nums' : 'text-start')
        }, _infoCell(row ? row[c.field] : null));
      }));
    }));

    // ── footer: pagination + counts ──
    var footer = null;
    var footBits = [];
    if (query) footBits.push(sorted.length + ' of ' + baseRows.length + ' rows match');
    if (sorted.length > printCap) footBits.push('first ' + printCap + ' rows in print/PDF');
    if (paged && sorted.length > pageSize) {
      footer = h('div', {
        key: 'f',
        className: 'bow-dt-chrome flex items-center justify-between gap-2 pt-2 text-xs text-slate-500 dark:text-slate-400'
      }, [
        h('span', { key: 'c', 'data-testid': 'bow-dt-range' },
          (sorted.length ? (start + 1) : 0) + '–' + end + ' of ' + sorted.length
          + (footBits.length ? '  ·  ' + footBits.join('  ·  ') : '')),
        h('span', { key: 'nav', className: 'inline-flex items-center gap-1' }, [
          h('button', {
            key: 'p', type: 'button', 'data-testid': 'bow-dt-prev', disabled: curPage === 0,
            onClick: function() { setPage(Math.max(0, curPage - 1)); },
            className: 'px-2 py-1 rounded-md border border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800 disabled:opacity-40 disabled:cursor-default'
          }, isRtl ? '›' : '‹'),
          h('span', { key: 'pg', className: 'px-1 tabular-nums' }, (curPage + 1) + '/' + pageCount),
          h('button', {
            key: 'n', type: 'button', 'data-testid': 'bow-dt-next', disabled: curPage >= pageCount - 1,
            onClick: function() { setPage(Math.min(pageCount - 1, curPage + 1)); },
            className: 'px-2 py-1 rounded-md border border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800 disabled:opacity-40 disabled:cursor-default'
          }, isRtl ? '‹' : '›')
        ])
      ]);
    } else if (footBits.length) {
      footer = h('div', { key: 'f', className: 'bow-dt-chrome pt-2 text-xs text-slate-500 dark:text-slate-400', 'data-testid': 'bow-dt-range' }, footBits.join('  ·  '));
    }

    var infoBtn = (props.info && props.viz)
      ? h('div', { key: 'i', className: 'absolute top-0 z-10 ' + (isRtl ? 'left-0' : 'right-0') },
          h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc }))
      : null;

    var empty = !cols.length || !baseRows.length;

    return h('div', {
      dir: dir,
      'data-testid': 'bow-datatable',
      className: 'relative' + (props.className ? ' ' + props.className : ''),
      style: props.style
    }, empty ? [
      infoBtn,
      h('div', { key: 'e', className: 'text-sm text-slate-400 py-8 text-center' }, props.emptyText || 'No data available')
    ] : [
      infoBtn,
      toolbar,
      h('div', { key: 'scroll', className: 'bow-dt-scroll overflow-auto', style: { maxHeight: maxHeight } },
        h('table', { className: 'w-full text-sm' }, [thead, tbody])),
      footer
    ]);
  };

  // ═══════════════════════════════════════════════════════════════════════════
  // FIX 3: FilterSelect — portal dropdown to escape stacking contexts
  // ═══════════════════════════════════════════════════════════════════════════

  window.FilterSelect = function(props) {
    var label = props.label || '';
    var rawOpts = props.options || [];
    // Normalize options to {val, lbl} with string values for consistent comparison
    var opts = rawOpts.map(function(o) {
      return typeof o === 'object' && o !== null
        ? { val: String(o.value), lbl: o.label || String(o.value) }
        : { val: String(o), lbl: String(o) };
    });
    var selected = (props.selected || []).map(String);
    var onChange = props.onChange || function() {};
    // Theme: className OR-replaces defaults (bg/border/text color); structural classes always applied.
    var theme = props.className || 'bg-white border-slate-200 text-slate-900 dark:bg-slate-900 dark:border-slate-700 dark:text-slate-100';
    var searchable = props.searchable !== undefined ? props.searchable : opts.length >= 8;

    var _s = React.useState(false), open = _s[0], setOpen = _s[1];
    var _q = React.useState(''), query = _q[0], setQuery = _q[1];
    var btnRef = React.useRef(null);
    var ddRef = React.useRef(null);
    var searchRef = React.useRef(null);
    var _pos = React.useState(null), pos = _pos[0], setPos = _pos[1];

    // Close on outside click — check both button and portaled dropdown
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

    // Focus search when opened
    React.useEffect(function() {
      if (open && searchable && searchRef.current) searchRef.current.focus();
      if (!open) setQuery('');
    }, [open]);

    // Reposition dropdown on scroll/resize while open
    React.useEffect(function() {
      if (!open || !btnRef.current) return;
      function reposition() {
        if (!btnRef.current) return;
        var rect = btnRef.current.getBoundingClientRect();
        // Flip above if not enough room below
        var spaceBelow = window.innerHeight - rect.bottom;
        var top = spaceBelow > 200 ? rect.bottom + 2 : rect.top - 2;
        var anchor = spaceBelow > 200 ? 'below' : 'above';
        setPos({ top: top, left: rect.left, width: Math.max(rect.width, 200), anchor: anchor });
      }
      reposition();
      window.addEventListener('scroll', reposition, true);
      window.addEventListener('resize', reposition);
      return function() {
        window.removeEventListener('scroll', reposition, true);
        window.removeEventListener('resize', reposition);
      };
    }, [open]);

    function handleToggle() { setOpen(!open); }

    function toggle(val) {
      var idx = selected.indexOf(val);
      onChange(idx >= 0 ? selected.filter(function(v) { return v !== val; }) : selected.concat([val]));
    }

    var filtered = searchable && query
      ? opts.filter(function(o) { return o.lbl.toLowerCase().indexOf(query.toLowerCase()) !== -1; })
      : opts;
    var selLabels = opts.filter(function(o) { return selected.indexOf(o.val) >= 0; }).map(function(o) { return o.lbl; });
    var display = selected.length === 0 ? 'All' : selLabels.length <= 2 ? selLabels.join(', ') : selected.length + ' selected';

    // Build dropdown contents
    var ddChildren = [];
    if (searchable) {
      ddChildren.push(h('div', { key: 'search', className: 'px-2 pt-1 pb-1 sticky top-0 ' + theme }, [
        h('input', {
          ref: searchRef, type: 'text', value: query,
          placeholder: 'Search...',
          onChange: function(e) { setQuery(e.target.value); },
          className: 'w-full rounded border px-2 py-1 text-sm outline-none focus:border-blue-400 ' + theme,
          style: props.style,
          onClick: function(e) { e.stopPropagation(); }
        })
      ]));
    }
    if (selected.length > 0) {
      ddChildren.push(h('button', {
        key: 'clr', type: 'button',
        className: 'w-full text-left px-3 py-1.5 text-xs font-medium opacity-50 hover:opacity-100',
        onClick: function() { onChange([]); }
      }, 'Clear all'));
    }
    filtered.forEach(function(o) {
      var isSelected = selected.indexOf(o.val) >= 0;
      ddChildren.push(h('label', {
        key: 'opt-' + o.val,
        className: 'flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-black/5 dark:hover:bg-white/10'
      }, [
        h('input', {
          key: 'cb', type: 'checkbox', checked: isSelected,
          onChange: function() { toggle(o.val); },
          className: 'rounded border-slate-300 dark:border-slate-600 accent-blue-500'
        }),
        h('span', { key: 'v', className: 'truncate' }, o.lbl)
      ]));
    });

    // Portal the dropdown to document.body so it escapes any overflow/stacking context
    var ddStyle = {
      position: 'fixed',
      zIndex: 99999,
      top: pos && pos.anchor === 'below' ? pos.top : undefined,
      bottom: pos && pos.anchor === 'above' ? (window.innerHeight - pos.top) : undefined,
      left: pos ? pos.left : undefined,
      width: pos ? pos.width : undefined,
      maxHeight: 288
    };
    // Merge user style overrides (e.g. dark background)
    if (props.style) { for (var sk in props.style) ddStyle[sk] = props.style[sk]; }
    var dropdown = (open && pos) ? ReactDOM.createPortal(
      h('div', {
        ref: ddRef,
        className: 'rounded-lg border shadow-lg overflow-auto py-1 ' + theme,
        style: ddStyle
      }, ddChildren),
      document.body
    ) : null;

    return h('div', { className: 'relative inline-block min-w-[140px]' }, [
      label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
      h('button', {
        ref: btnRef, key: 'btn', type: 'button',
        className: 'w-full flex items-center justify-between gap-2 rounded-lg border px-3 py-1.5 text-sm cursor-pointer ' + theme,
        style: props.style,
        onClick: handleToggle
      }, [
        h('span', { key: 't', className: 'truncate' }, display),
        h('svg', { key: 'i', width: 12, height: 12, viewBox: '0 0 12 12', className: 'opacity-50 shrink-0' },
          h('path', { d: 'M3 5l3 3 3-3', stroke: 'currentColor', strokeWidth: 1.5, fill: 'none' }))
      ]),
      dropdown
    ]);
  };

  // ── FilterSearch ────────────────────────────────────────────────────────────
  window.FilterSearch = function(props) {
    var label = props.label || '';
    var value = props.value || '';
    var onChange = props.onChange || function() {};
    var placeholder = props.placeholder || 'Search...';
    var theme = props.className || 'bg-white border-slate-200 text-slate-900 dark:bg-slate-900 dark:border-slate-700 dark:text-slate-100';
    return h('div', { className: 'inline-block min-w-[140px]' }, [
      label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
      h('input', {
        key: 'inp', type: 'text', value: value, placeholder: placeholder,
        onChange: onChange,
        className: 'w-full rounded-lg border px-3 py-1.5 text-sm ' + theme,
        style: props.style
      })
    ]);
  };

  // ── FilterDateRange ─────────────────────────────────────────────────────────
  window.FilterDateRange = function(props) {
    var label = props.label || '';
    var value = props.value || {};
    var onChange = props.onChange || function() {};
    var theme = props.className || 'bg-white border-slate-200 text-slate-900 dark:bg-slate-900 dark:border-slate-700 dark:text-slate-100';
    var inputType = props.type || 'date';
    return h('div', { className: 'inline-block min-w-[200px]' }, [
      label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
      h('div', { key: 'row', className: 'flex items-center gap-2' }, [
        h('input', {
          key: 'from', type: inputType, value: value.from || '',
          onChange: function(e) { onChange({ from: e.target.value || null, to: value.to || null }); },
          className: 'w-full rounded-lg border px-2 py-1.5 text-sm ' + theme,
          style: props.style
        }),
        h('span', { key: 'sep', className: 'text-xs opacity-50' }, '\u2013'),
        h('input', {
          key: 'to', type: inputType, value: value.to || '',
          onChange: function(e) { onChange({ from: value.from || null, to: e.target.value || null }); },
          className: 'w-full rounded-lg border px-2 py-1.5 text-sm ' + theme,
          style: props.style
        })
      ])
    ]);
  };

  // ── BowFile ─────────────────────────────────────────────────────────────────
  // Renders an embedded file (generated image or uploaded image/PDF) by id.
  // Bytes arrive via ARTIFACT_DATA.files as a data: URI (injected by the host,
  // so no auth/URL handling is needed in generated code). Images render inline;
  // PDFs render in a native viewer iframe. `children` are absolutely positioned
  // over the file for annotations/callouts.
  function _bowFindFile(id) {
    var data = window.ARTIFACT_DATA || {};
    var files = Array.isArray(data.files) ? data.files : [];
    for (var i = 0; i < files.length; i++) {
      if (files[i] && String(files[i].id) === String(id)) return files[i];
    }
    return null;
  }

  // A "download / open in new tab" card — shown when a PDF can't render inline
  // (pdf.js unavailable in headless render, or a load error).
  function _bowPdfCard(src, filename) {
    return h('div', {
      className: 'flex flex-col items-center justify-center gap-3 h-full w-full bg-slate-50 border-slate-200 dark:bg-slate-800 dark:border-slate-700 rounded-lg border text-center p-6'
    }, [
      h('svg', { key: 'ic', width: 44, height: 44, viewBox: '0 0 24 24', fill: 'none', stroke: '#ef4444', strokeWidth: 1.5 }, [
        h('path', { key: 'p1', d: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z' }),
        h('path', { key: 'p2', d: 'M14 2v6h6' })
      ]),
      h('div', { key: 'nm', className: 'text-sm font-medium text-slate-700 dark:text-slate-200' }, filename || 'Document.pdf'),
      h('a', {
        key: 'op', href: src, target: '_blank', rel: 'noopener',
        className: 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 text-white text-xs font-medium hover:bg-slate-700'
      }, 'Open PDF')
    ]);
  }

  // Inline PDF viewer using pdf.js — renders pages to canvases in a scroll area.
  // Falls back to _bowPdfCard when pdf.js is missing (e.g. headless thumbnail
  // render) or a document fails to load.
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

      // src is either a signed token URL or an inlined data: URI. Load the raw
      // bytes ourselves (the token URL is same-origin and needs no auth header)
      // and hand pdf.js {data} — avoids pdf.js's relative-URL handling in the
      // sandboxed iframe.
      var src = props.src || '';
      if (!src) { setStatus('error'); return; }
      function loadBytes() {
        if (/^data:/i.test(src)) return Promise.resolve(toBytes(src));
        return fetch(src).then(function(r) {
          if (!r.ok) throw new Error('fetch ' + r.status);
          return r.arrayBuffer();
        }).then(function(buf) { return new Uint8Array(buf); });
      }

      loadBytes().then(function(bytes) {
        return pdfjsLib.getDocument({ data: bytes }).promise;
      }).then(function(pdf) {
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
                canvas.width = viewport.width;
                canvas.height = viewport.height;
                canvas.style.width = '100%';
                canvas.style.display = 'block';
                canvas.style.marginBottom = '8px';
                canvas.style.borderRadius = '6px';
                canvas.style.boxShadow = '0 1px 3px rgba(0,0,0,0.12)';
                containerRef.current.appendChild(canvas);
                return page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise;
              });
            });
          })(p);
        }
        seq.then(function() { if (!cancelled) setStatus('ready'); })
           .catch(function() { if (!cancelled) setStatus('error'); });
      }).catch(function() { if (!cancelled) setStatus('error'); });

      return function() { cancelled = true; };
    }, [props.src]);

    if (status === 'nolib' || status === 'error') {
      return h('div', { style: { height: height } }, _bowPdfCard(props.src, props.filename));
    }
    return h('div', {
      className: 'relative w-full rounded-lg border border-slate-200 bg-slate-100 dark:border-slate-700 dark:bg-slate-800 overflow-y-auto',
      style: { height: height, padding: 8 }
    }, [
      status === 'loading' ? h('div', {
        key: 'ld', className: 'absolute inset-0 flex items-center justify-center text-slate-400 text-sm'
      }, h(window.LoadingSpinner, { size: 28 })) : null,
      h('div', { key: 'pages', ref: containerRef, className: 'w-full' })
    ]);
  };

  window.BowFile = function(props) {
    props = props || {};
    var file = _bowFindFile(props.id);
    var wrapCls = 'relative overflow-hidden ' + (props.className || '');
    var wrapStyle = Object.assign({ width: '100%' }, props.style || {});

    if (!file) {
      return h('div', {
        className: wrapCls + ' flex items-center justify-center bg-slate-50 border-slate-200 dark:bg-slate-800 dark:border-slate-700 border border-dashed rounded-lg text-slate-400 text-sm',
        style: Object.assign({ minHeight: 160 }, wrapStyle)
      }, 'File not found: ' + (props.id || ''));
    }

    var ct = String(file.content_type || '').toLowerCase();
    // Prefer a signed token URL (served without a session, revocable by expiry);
    // fall back to an inlined data URI for the headless thumbnail render.
    var src = file.url || file.dataUri || '';
    var overlay = props.children != null
      ? h('div', { key: 'ov', className: 'absolute inset-0 pointer-events-none' }, props.children)
      : null;

    var media;
    if (ct.indexOf('pdf') !== -1) {
      // Inline PDF via pdf.js (renders pages to canvas — works inside the
      // sandboxed iframe where the native PDF plugin is blocked). Falls back to
      // an "Open PDF" card if pdf.js is unavailable (e.g. headless thumbnail).
      media = h(window.BowPdfViewer, {
        key: 'pdf', src: src, filename: file.filename, height: props.height || 520
      });
    } else if (ct.indexOf('image') !== -1 || !ct) {
      media = h('img', {
        key: 'img', src: src, alt: props.alt || file.filename || '',
        className: 'w-full h-full rounded-lg',
        style: { objectFit: props.fit || 'contain', display: 'block', maxWidth: '100%' }
      });
    } else {
      media = h('a', {
        key: 'dl', href: src, download: file.filename || 'file',
        className: 'inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800 text-sm'
      }, 'Download ' + (file.filename || 'file'));
    }

    return h('div', { className: wrapCls, style: wrapStyle }, overlay ? [media, overlay] : media);
  };

  // ── ECharts 'bow' theme ─────────────────────────────────────────────────────
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

  function _bowIsDark() {
    return document.documentElement.classList.contains('dark');
  }

  // Forced-dark artifacts: generated code that wraps its page in
  // <div className="dark"> means "dark regardless of the host theme". Mirror
  // that onto <html> so everything OUTSIDE the wrapper follows too — portaled
  // dropdowns/popovers (they mount on document.body), the bow-dark chart
  // theme, and the body background. The host's color-mode toggle checks
  // window.__bowForcedDark and never strips the class while a forced-dark
  // root is present.
  (function watchForcedDark() {
    function sync() {
      var root = document.getElementById('root');
      var forced = !!(root && (root.classList.contains('dark') || root.querySelector(':scope .dark')));
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

  // ── EChart wrapper ──────────────────────────────────────────────────────────
  function safeOption(opt) {
    if (opt && opt.tooltip && typeof opt.tooltip.formatter === 'function') {
      var orig = opt.tooltip.formatter;
      opt.tooltip.formatter = function() { try { return orig.apply(this, arguments); } catch(e) { return ''; } };
    }
    return opt;
  }

  window.EChart = function(props) {
    var ref = React.useRef(null);
    var chartRef = React.useRef(null);
    var ht = props.height || 400;
    // Track the host color mode; the iframe shell fires 'bow-colormode' when
    // the host toggles it. The chart re-inits with the matching theme.
    var _dm = React.useState(_bowIsDark()), isDark = _dm[0], setIsDark = _dm[1];
    React.useEffect(function() {
      function onMode() { setIsDark(_bowIsDark()); }
      window.addEventListener('bow-colormode', onMode);
      return function() { window.removeEventListener('bow-colormode', onMode); };
    }, []);
    React.useEffect(function() {
      if (!ref.current) return;
      var chart = echarts.init(ref.current, isDark ? 'bow-dark' : 'bow');
      chartRef.current = chart;
      if (props.option) chart.setOption(safeOption(props.option));
      var ro = new ResizeObserver(function() { chart.resize(); });
      ro.observe(ref.current);
      return function() { ro.disconnect(); chart.dispose(); };
    }, [isDark]);
    React.useEffect(function() {
      if (chartRef.current && props.option) {
        chartRef.current.setOption(safeOption(props.option), true);
      }
    }, [props.option]);
    var chart = h('div', {
      ref: ref,
      style: { width: '100%', height: ht },
      className: props.className || ''
    });
    // When a `viz` is supplied, overlay the built-in info popover so even a bare
    // <EChart> (outside a SectionCard) exposes its data / query / calc.
    if (!props.viz) return chart;
    return h('div', { className: 'relative', style: { width: '100%' } }, [
      h('div', { key: 'info', className: 'absolute top-2 right-2 z-10' },
        h(window.InfoPopover, { viz: props.viz, rows: props.rows, calc: props.calc })),
      chart
    ]);
  };

  // ── resizeAllCharts ─────────────────────────────────────────────────────────
  window.resizeAllCharts = function() {
    if (typeof echarts !== 'undefined') {
      var charts = document.querySelectorAll('[_echarts_instance_]');
      charts.forEach(function(el) {
        var chart = echarts.getInstanceByDom(el);
        if (chart) chart.resize();
      });
    }
  };
  setTimeout(window.resizeAllCharts, 100);
  setTimeout(window.resizeAllCharts, 500);
  window.addEventListener('resize', window.resizeAllCharts);

  // ═══════════════════════════════════════════════════════════════════════════
  // InfoOverlay — per-item info popover for ANY markup, via data attributes.
  // The dashboard (including fully custom divs) annotates each metric/chart/
  // table container with data-bow-viz="<index>" and optional data-bow-calc.
  // A single body-level overlay reads those attributes, draws a small "ⓘ" at
  // each element's corner, and on click shows the same Data/Code/Calc popover.
  // It NEVER mutates the dashboard's own DOM (no React reconciliation conflicts).
  // ═══════════════════════════════════════════════════════════════════════════

  window.InfoOverlay = function() {
    var _tick = React.useState(0), setTick = _tick[1];
    var _open = React.useState(null), openT = _open[0], setOpenT = _open[1]; // {vizIndex, calc, title, rect}
    var _tab = React.useState('data'), tab = _tab[0], setTab = _tab[1];
    var panelRef = React.useRef(null);

    // Re-render (recompute positions) on layout/structure changes.
    React.useEffect(function() {
      var raf = null;
      function ping() { if (raf) return; raf = requestAnimationFrame(function() { raf = null; setTick(function(c) { return c + 1; }); }); }
      var root = document.getElementById('root') || document.body;
      var mo = new MutationObserver(ping);
      mo.observe(root, { childList: true, subtree: true, attributes: true });
      window.addEventListener('scroll', ping, true);
      window.addEventListener('resize', ping);
      var t1 = setTimeout(ping, 150), t2 = setTimeout(ping, 600), t3 = setTimeout(ping, 1500);
      return function() {
        mo.disconnect();
        window.removeEventListener('scroll', ping, true);
        window.removeEventListener('resize', ping);
        clearTimeout(t1); clearTimeout(t2); clearTimeout(t3);
        if (raf) cancelAnimationFrame(raf);
      };
    }, []);

    // Close popover on outside click / Escape.
    React.useEffect(function() {
      if (!openT) return;
      function onDown(e) {
        if (e.target && e.target.closest && e.target.closest('[data-bow-ibtn], [data-bow-panel]')) return;
        setOpenT(null);
      }
      function onKey(e) { if (e.key === 'Escape') setOpenT(null); }
      document.addEventListener('mousedown', onDown);
      document.addEventListener('keydown', onKey);
      return function() { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
    }, [openT]);

    var data = window.ARTIFACT_DATA || {};
    var vizs = Array.isArray(data.visualizations) ? data.visualizations : [];

    // Collect annotated targets and their on-screen rects.
    var targets = [];
    var els = document.querySelectorAll('[data-bow-viz]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var r = el.getBoundingClientRect();
      if ((r.width === 0 && r.height === 0) || r.bottom < 0 || r.top > window.innerHeight) continue;
      targets.push({
        rect: r,
        vizIndex: parseInt(el.getAttribute('data-bow-viz'), 10) || 0,
        calc: el.getAttribute('data-bow-calc') || null,
        title: el.getAttribute('data-bow-title') || null
      });
    }

    // ⓘ markers (fixed, top-right of each annotated element).
    var markers = targets.map(function(t, i) {
      return h('button', {
        key: 'm' + i, type: 'button', 'data-bow-ibtn': '1', 'aria-label': 'Details',
        onClick: function(e) { e.stopPropagation(); setTab('data'); setOpenT(t); },
        style: { position: 'fixed', top: Math.max(2, t.rect.top + 6), left: t.rect.right - 24, zIndex: 99998 },
        className: 'inline-flex items-center justify-center w-5 h-5 rounded-full bg-white/80 backdrop-blur text-slate-400 hover:text-slate-700 hover:bg-white border-slate-200/70 dark:bg-slate-800/80 dark:text-slate-500 dark:hover:text-slate-200 dark:hover:bg-slate-800 dark:border-slate-600/70 shadow-sm border transition-colors'
      }, h('svg', { width: 14, height: 14, viewBox: '0 0 16 16', fill: 'none' }, [
        h('circle', { key: 'c', cx: 8, cy: 8, r: 6.4, stroke: 'currentColor', strokeWidth: 1.2 }),
        h('circle', { key: 'd', cx: 8, cy: 5.2, r: 0.95, fill: 'currentColor' }),
        h('path', { key: 'b', d: 'M8 7.4v4', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round' })
      ]));
    });

    // Popover panel for the open target.
    var panel = null;
    if (openT) {
      var viz = vizs[openT.vizIndex] || {};
      var W = 400;
      var left = Math.min(openT.rect.right - W, window.innerWidth - W - 8); if (left < 8) left = 8;
      var spaceBelow = window.innerHeight - openT.rect.top;
      var below = spaceBelow > 260;
      var vTitle = openT.title || viz.title || 'Details';
      function tabButton(id, label) {
        var active = tab === id;
        return h('button', { key: id, type: 'button', onClick: function() { setTab(id); },
          className: 'px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors ' + (active ? 'border-slate-800 text-slate-800 dark:border-slate-200 dark:text-slate-200' : 'border-transparent text-slate-400 hover:text-slate-600 dark:hover:text-slate-300') }, label);
      }
      panel = h('div', {
        ref: panelRef, 'data-bow-panel': '1',
        className: 'bg-white border border-slate-200 dark:bg-slate-900 dark:border-slate-700 rounded-lg shadow-xl',
        style: {
          position: 'fixed', left: left, width: W, zIndex: 99999, maxHeight: '72vh',
          top: below ? (openT.rect.top + 28) : undefined,
          bottom: below ? undefined : (window.innerHeight - openT.rect.top + 6),
          display: 'flex', flexDirection: 'column'
        }
      }, [
        h('div', { key: 'hd', className: 'flex items-start justify-between gap-2 px-3.5 pt-2.5 pb-1' }, [
          h('div', { key: 't', className: 'text-xs font-semibold text-slate-800 dark:text-slate-200 leading-snug' }, vTitle),
          h('button', { key: 'x', type: 'button', 'aria-label': 'Close', onClick: function() { setOpenT(null); },
            className: 'shrink-0 -mt-0.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300' },
            h('svg', { width: 14, height: 14, viewBox: '0 0 14 14', fill: 'none' }, h('path', { d: 'M3.5 3.5l7 7M10.5 3.5l-7 7', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' })))
        ]),
        h('div', { key: 'tabs', className: 'flex gap-1 px-3 border-b border-slate-100 dark:border-slate-700' }, [tabButton('data', 'Data'), tabButton('code', 'Code')]),
        h('div', { key: 'bd', className: 'px-3.5 py-3 overflow-auto' }, tab === 'code' ? _codeTabBody(viz) : _dataTabBody(viz, { calc: openT.calc })),
        viz.id ? h('div', { key: 'ft', className: 'px-3.5 py-2 border-t border-slate-100 dark:border-slate-700 text-[10px] font-mono text-slate-400 break-all' }, 'ID  ' + viz.id) : null
      ]);
    }

    if (!markers.length && !panel) return null;
    return h('div', { style: { position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 99998 } },
      markers.concat(panel ? [panel] : []).map(function(node, i) {
        return h('div', { key: i, style: { pointerEvents: 'auto' } }, node);
      })
    );
  };

  // Auto-mount the overlay once, unless explicitly disabled (e.g. thumbnails).
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
  //
  // 1. Force Babel to use classic JSX runtime so JSX compiles to React.createElement()
  //    instead of _jsx() calls (which require react/jsx-runtime, not available here).
  //
  // 2. Intercept Node.prototype.appendChild to strip any residual LLM-written
  //    `import` declarations from the script Babel injects into the DOM — Babel
  //    transforms JSX but leaves `import` keywords unchanged, causing the browser
  //    to throw "Cannot use import statement outside a module".
  (function patchBabel() {
    // Patch 1: classic JSX runtime
    if (window.Babel && window.Babel.availablePresets && window.Babel.availablePresets.react) {
      var _origReact = window.Babel.availablePresets.react;
      window.Babel.availablePresets.react = function(api, opts, dir) {
        return _origReact(api, Object.assign({}, opts, { runtime: 'classic' }), dir);
      };
    }

    // Patch 2: strip LLM-written imports from Babel's injected script output
    function stripImports(code) {
      var s = code.replace(/import\s*\{[^}]*\}\s*from\s*['"][^'"]+['"]\s*;?/g, '');
      return s.replace(/^[ \t]*import\b(?!\s*\().*$/gm, '');
    }
    var _origAppendChild = Node.prototype.appendChild;
    Node.prototype.appendChild = function(node) {
      if (node && node.nodeType === 1 && node.tagName === 'SCRIPT' &&
          !node.getAttribute('src') && !node.getAttribute('type') &&
          node.textContent && /\bimport\b/.test(node.textContent)) {
        node.textContent = stripImports(node.textContent);
      }
      return _origAppendChild.call(this, node);
    };
  })();

})();
