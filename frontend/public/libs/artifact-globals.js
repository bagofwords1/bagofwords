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

  // ── useArtifactData() ───────────────────────────────────────────────────────
  window.useArtifactData = function() {
    return window.ARTIFACT_DATA;
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
    var meta = _infoMeta(viz);
    // Prefer the exact rows the component rendered (e.g. filterRows(...) output)
    // when passed via the `rows` prop; otherwise fall back to the raw query rows.
    var rawRows = Array.isArray(viz.rows) ? viz.rows : [];
    var overrideRows = Array.isArray(props.rows) ? props.rows : null;
    var dataRows = overrideRows != null ? overrideRows : rawRows;
    var cols = _infoCols(viz, dataRows);
    var rawCount = rawRows.length || (viz.row_count != null ? viz.row_count : 0);
    var isFiltered = overrideRows != null && rawCount > 0 && overrideRows.length !== rawCount;
    var MAXR = 100;

    var activeFilters = {};
    try { activeFilters = (window.__filterStore ? window.__filterStore.get() : {}) || {}; } catch (e) {}
    // Only attribute a filter to this viz if it maps onto one of its columns —
    // never claim a filter that doesn't actually touch this data.
    var colFields = cols.map(function(c) { return c.field; });
    var shownFilterKeys = Object.keys(activeFilters).filter(function(k) { return colFields.indexOf(k) !== -1; });

    function tabButton(id, label) {
      var active = tab === id;
      return h('button', {
        key: id, type: 'button', onClick: function() { setTab(id); },
        className: 'px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors '
          + (active ? 'border-slate-800 text-slate-800' : 'border-transparent text-slate-400 hover:text-slate-600')
      }, label);
    }

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

    // DATA tab — the actual rows + a compact metadata line
    var dataBody = h('div', { key: 'data', style: { display: 'flex', flexDirection: 'column', gap: 8 } }, [
      metaBits.length ? h('div', { key: 'm', className: 'text-[11px] text-slate-400' }, metaBits.join('  ·  ')) : null,
      filterNote ? h('div', { key: 'af', className: 'text-[11px] text-slate-500' }, filterNote) : null,
      cols.length ? h('div', {
        key: 'tbl', className: 'border border-slate-100 rounded-md overflow-auto', style: { maxHeight: 300 }
      }, h('table', { className: 'border-collapse', style: { minWidth: '100%' } }, [
        h('thead', { key: 'h' }, h('tr', {}, cols.map(function(c, i) {
          return h('th', {
            key: i,
            className: 'text-left font-medium text-slate-500 bg-slate-50 px-2 py-1.5 border-b border-slate-100 whitespace-nowrap sticky top-0',
            style: { fontSize: 11 }
          }, c.header);
        }))),
        h('tbody', { key: 'b' }, dataRows.slice(0, MAXR).map(function(row, ri) {
          return h('tr', { key: ri, className: ri % 2 ? 'bg-slate-50/40' : '' }, cols.map(function(c, ci) {
            var cell = _infoCell(row[c.field]);
            return h('td', {
              key: ci, title: cell,
              className: 'px-2 py-1 text-slate-700 whitespace-nowrap border-b border-slate-50',
              style: { fontSize: 11, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }
            }, cell);
          }));
        }))
      ])) : h('div', { key: 'nd', className: 'text-xs text-slate-400 py-4 text-center' }, 'No data available'),
      (dataRows.length > MAXR) ? h('div', { key: 'more', className: 'text-[10px] text-slate-400' },
        'Showing ' + MAXR + ' of ' + dataRows.length + ' rows') : null
    ]);

    // CODE tab — the generating query
    var codeBody = h('div', { key: 'code' }, viz.code
      ? h('pre', {
          className: 'text-[11px] font-mono text-slate-700 bg-slate-50 border border-slate-100 rounded-md p-2 overflow-auto',
          style: { maxHeight: 340, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }
        }, viz.code)
      : h('div', { className: 'text-xs text-slate-400 py-4 text-center' }, 'No query available for this visualization.'));

    var panel = (open && pos) ? ReactDOM.createPortal(
      h('div', {
        ref: panelRef,
        className: 'bg-white border border-slate-200 rounded-lg shadow-xl',
        style: {
          position: 'fixed', left: pos.left,
          top: pos.top != null ? pos.top : undefined,
          bottom: pos.bottom != null ? pos.bottom : undefined,
          width: pos.width, zIndex: 99999, maxHeight: '72vh',
          display: 'flex', flexDirection: 'column'
        }
      }, [
        h('div', { key: 'hd', className: 'flex items-start justify-between gap-2 px-3.5 pt-2.5 pb-1' }, [
          h('div', { key: 't', className: 'text-xs font-semibold text-slate-800 leading-snug' }, viz.title || 'Details'),
          h('button', {
            key: 'x', type: 'button', 'aria-label': 'Close',
            onClick: function() { setOpen(false); },
            className: 'shrink-0 -mt-0.5 text-slate-400 hover:text-slate-600'
          }, h('svg', { width: 14, height: 14, viewBox: '0 0 14 14', fill: 'none' },
            h('path', { d: 'M3.5 3.5l7 7M10.5 3.5l-7 7', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' })))
        ]),
        h('div', { key: 'tabs', className: 'flex gap-1 px-3 border-b border-slate-100' }, [
          tabButton('data', 'Data'),
          tabButton('code', 'Code')
        ]),
        h('div', { key: 'bd', className: 'px-3.5 py-3 overflow-auto' }, tab === 'code' ? codeBody : dataBody),
        viz.id ? h('div', {
          key: 'ft', className: 'px-3.5 py-2 border-t border-slate-100 text-[10px] font-mono text-slate-400 break-all'
        }, 'ID  ' + viz.id) : null
      ]),
      document.body
    ) : null;

    return h('span', { className: 'inline-flex align-middle' }, [
      h('button', {
        key: 'btn', ref: btnRef, type: 'button', 'aria-label': 'Details',
        onClick: function(e) { e.stopPropagation(); setOpen(function(o) { return !o; }); },
        className: 'inline-flex items-center justify-center w-5 h-5 rounded-full transition-colors '
          + (open ? 'text-slate-600 bg-slate-100' : 'text-slate-300 hover:text-slate-500 hover:bg-slate-100')
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
    var cls = 'relative rounded-2xl border p-5 shadow-sm overflow-hidden bg-white border-slate-200 text-slate-900'
      + (props.className ? ' ' + props.className : '');
    var titleCls = 'text-xs font-medium uppercase tracking-wider mb-1 text-slate-500'
      + (props.titleClassName ? ' ' + props.titleClassName : '');
    var subtitleCls = 'text-sm mt-1 text-slate-500'
      + (props.subtitleClassName ? ' ' + props.subtitleClassName : '');
    return h('div', { className: cls, style: props.style }, [
      h('div', { key: 'bar', className: 'absolute inset-x-0 top-0 h-1', style: { background: 'linear-gradient(90deg, ' + color + ', ' + color + '99)' } }),
      props.viz ? h('div', { key: 'info', className: 'absolute top-2.5 right-2.5 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows })) : null,
      h('p', { key: 't', className: titleCls }, props.title),
      h('p', { key: 'v', className: 'text-2xl font-semibold' }, props.value),
      props.subtitle ? h('p', { key: 's', className: subtitleCls }, props.subtitle) : null,
    ]);
  };

  window.SectionCard = function(props) {
    var cls = 'relative rounded-2xl border shadow-sm p-6 bg-white border-slate-200'
      + (props.className ? ' ' + props.className : '');
    var titleCls = 'text-lg font-semibold text-slate-800'
      + (props.titleClassName ? ' ' + props.titleClassName : '');
    var subtitleCls = 'text-sm mt-1 text-slate-500'
      + (props.subtitleClassName ? ' ' + props.subtitleClassName : '');
    return h('div', { className: cls, style: props.style }, [
      props.viz ? h('div', { key: 'info', className: 'absolute top-3 right-3 z-10' }, h(window.InfoPopover, { viz: props.viz, rows: props.rows })) : null,
      props.title ? h('div', { key: 'hdr', className: 'mb-4 pr-6' }, [
        h('h2', { key: 't', className: titleCls }, props.title),
        props.subtitle ? h('p', { key: 's', className: subtitleCls }, props.subtitle) : null,
      ]) : null,
      h('div', { key: 'body' }, props.children),
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
    var theme = props.className || 'bg-white border-slate-200 text-slate-900';
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
        className: 'flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-black/5'
      }, [
        h('input', {
          key: 'cb', type: 'checkbox', checked: isSelected,
          onChange: function() { toggle(o.val); },
          className: 'rounded border-slate-300 accent-blue-500'
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
    var theme = props.className || 'bg-white border-slate-200 text-slate-900';
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
    var theme = props.className || 'bg-white border-slate-200 text-slate-900';
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
    React.useEffect(function() {
      if (!ref.current) return;
      var chart = echarts.init(ref.current, 'bow');
      chartRef.current = chart;
      if (props.option) chart.setOption(safeOption(props.option));
      var ro = new ResizeObserver(function() { chart.resize(); });
      ro.observe(ref.current);
      return function() { ro.disconnect(); chart.dispose(); };
    }, []);
    React.useEffect(function() {
      if (chartRef.current && props.option) {
        chartRef.current.setOption(safeOption(props.option), true);
      }
    }, [props.option]);
    return h('div', {
      ref: ref,
      style: { width: '100%', height: ht },
      className: props.className || ''
    });
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

})();
