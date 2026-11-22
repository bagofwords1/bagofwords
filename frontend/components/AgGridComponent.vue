<template>
  <div ref="containerEl" class="grid-container h-full">
    <!-- Open the whole table over the page (same control as a create_data
         result's "open in panel"). -->
    <div v-if="expandable" class="grid-toolbar">
      <UTooltip :text="t('dataGrid.expand')">
        <button
          type="button"
          class="grid-toolbar__btn"
          :aria-label="t('dataGrid.expand')"
          data-testid="grid-expand"
          @click="setExpanded(true)">
          <Icon name="heroicons:arrows-pointing-out" class="w-3.5 h-3.5" />
        </button>
      </UTooltip>
    </div>
    <!-- Expanded: the SAME grid moves to <body> (Teleport keeps the instance,
         so sort / filter / widths stay) — a fixed overlay inside a widget
         would be trapped by any transformed ancestor (dashboard tiles). The
         wrapper's theme tokens are copied along, since the overlay leaves the
         themed wrapper behind. -->
    <Teleport to="body" :disabled="!expanded">
      <div v-if="expanded" class="grid-overlay-backdrop" @click="setExpanded(false)" />
      <div
        class="grid-shell"
        :class="expanded ? ['grid-shell--expanded', 'ag-grid-themed', 'ag-theme-custom'] : []"
        :style="expanded ? expandedVars : undefined"
        :role="expanded ? 'dialog' : undefined"
        :aria-modal="expanded ? 'true' : undefined">
        <div v-if="expanded" class="grid-toolbar grid-toolbar--expanded">
          <UTooltip :text="t('dataGrid.collapse')">
            <button
              type="button"
              class="grid-toolbar__btn"
              :aria-label="t('dataGrid.collapse')"
              data-testid="grid-collapse"
              @click="setExpanded(false)">
              <Icon name="heroicons:arrows-pointing-in" class="w-3.5 h-3.5" />
            </button>
          </UTooltip>
        </div>
    <!-- Data, columns and pagination go in through gridOptions and are updated
         through this grid's own API (see the watchers below), never as reactive
         props: ag-grid-vue3 v31 batches prop changes in one module-level buffer
         shared by every grid, so when two grids change in the same tick (e.g.
         a filter applied to a result shown in both the chat and the side
         panel) all changes land on the first grid and the second never
         updates. -->
    <!-- :key — ag-grid reads localeText only at creation, so a language
         switch builds fresh options and mounts a new grid. -->
    <ag-grid-vue
      ref="gridRef"
      :key="gridKey"
      :class="[agThemeClass, 'ag-grid']"
      :gridOptions="gridOptions">
    </ag-grid-vue>
    <!-- Only while a filter hides rows: how many are left, and a way back. -->
    <div v-if="filterStatus" class="grid-filter-status" data-testid="grid-filter-status">
      <span>{{ t('dataGrid.filteredRows', filterStatus) }}</span>
      <button type="button" class="grid-filter-status__clear" @click="clearFilters">
        {{ t('dataGrid.clearFilters') }}
      </button>
    </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, computed, watch, toRaw, inject, onBeforeUnmount } from 'vue';
import { AgGridVue } from 'ag-grid-vue3';
import CustomHeader from './CustomHeader.vue'; // Import the CustomHeader component
import CustomLoadingRenderer from './CustomLoadingRenderer.vue'; // Import the CustomLoadingRenderer component
import ValueSetFilter from './grid/ValueSetFilter.vue';
import { GRID_FILTER_BRIDGE, type GridFilterBridge } from '~/utils/valueFilter';
import 'ag-grid-community/styles/ag-grid.css';
//import 'ag-grid-community/styles/ag-theme-alpine.css';
import 'ag-grid-community/styles/ag-theme-balham.css';
import { loadGridState, saveGridState } from '~/utils/gridState';
import { localeTextDir } from '~/utils/textDirection';

// Follow the app color mode so the grid chrome (menus, filter popups, icons)
// doesn't stay light on a dark page. Wrappers that theme via .ag-theme-custom
// CSS variables still win — their rules use !important.
const colorMode = useColorMode();
const agThemeClass = computed(() =>
  colorMode.value === 'dark' ? 'ag-theme-balham-dark' : 'ag-theme-balham'
);

const isLoading = ref(true);
const { t, locale } = useI18n();

// A cell whose whole value is an http(s) URL (SharePoint/Outlook web links,
// monday `item_url`, …) renders as a short "Open ↗" link in a new tab instead
// of a long raw URL. Built as a DOM node — never innerHTML — so cell text can't
// inject markup; anything that isn't a single absolute URL stays plain text.
const URL_RE = /^https?:\/\/\S+$/i;
const linkCellRenderer = (params: any) => {
  const value = params.value;
  if (typeof value !== 'string' || !URL_RE.test(value.trim())) {
    return value ?? '';
  }
  const a = document.createElement('a');
  a.href = value.trim();
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.title = value.trim();
  a.className = 'ag-link-cell';
  a.textContent = `${t('common.openLink')} ↗`;
  return a;
};

const props = defineProps({
  columnDefs: {
    type: Array,
    required: true
  },
  rowData: {
    type: Array,
    required: true
  },
  // Stable id (widget / query) under which this viewer's column layout —
  // widths, order, sort — is remembered in the browser. Empty: in memory only.
  stateKey: {
    type: String,
    default: ''
  },
  // Show the "expand to full screen" button (off where the host already has
  // its own, e.g. a create_data result's "open in panel").
  expandable: {
    type: Boolean,
    default: true
  }
});

// ---- Expand to full screen -----------------------------------------------
const containerEl = ref<HTMLElement | null>(null);
const expanded = ref(false);
const expandedVars = ref<Record<string, string>>({});
// The tokens RenderTable / TableAgGrid set on their themed wrapper.
const THEME_VARS = [
  '--ag-background-color', '--ag-foreground-color', '--ag-header-background-color',
  '--ag-header-foreground-color', '--ag-border-color', '--ag-row-hover-color',
  '--ag-selected-row-background-color', '--ag-odd-row-background-color',
  '--ag-even-row-background-color', '--ag-font-family', '--ag-font-size',
];
const onEscape = (e: KeyboardEvent) => {
  // Leave Esc to an open filter popup first.
  if (e.key === 'Escape' && !document.querySelector('.ag-popup .ag-menu')) setExpanded(false);
};
const setExpanded = (on: boolean) => {
  if (on === expanded.value) return;
  if (on) {
    const host = containerEl.value?.parentElement;
    const cs = host ? getComputedStyle(host) : null;
    const vars: Record<string, string> = {};
    for (const name of THEME_VARS) {
      const value = cs?.getPropertyValue(name).trim();
      if (value) vars[name] = value;
    }
    vars.fontFamily = cs?.fontFamily || '';
    expandedVars.value = vars;
    window.addEventListener('keydown', onEscape);
  } else {
    window.removeEventListener('keydown', onEscape);
  }
  expanded.value = on;
  // The grid's width just changed a lot: lay the columns out again for it
  // (content widths, filling the space) — the viewer's own order, sort and
  // hand-set widths are re-applied by layoutColumns.
  relayoutOnResize = true;
};
onBeforeUnmount(() => window.removeEventListener('keydown', onEscape));
// A host with its own control (a tab row, a widget header) opens it directly.
defineExpose({ setExpanded });

const PAGE_SIZE = 50;
// Auto-sized columns stay within this range; a viewer can still drag any
// column wider or narrower by hand.
const MIN_COL_WIDTH = 80;
const MAX_AUTO_WIDTH = 300;
// The VALUES decide a column's width; a header longer than its values gets
// at most this much room and is cut with "…" ("Duration in milliseconds"
// over six-digit numbers stays narrow instead of 205px wide).
const MAX_HEADER_WIDTH = 140;

const gridRef = ref<any>(null);
const gridApi = () => gridRef.value?.api;

// The paging footer ("Page Size", "1 to 50 of 236", "Page 1 of 5") in the
// viewer's language instead of ag-grid's built-in English.
const PAGER_KEYS = ['page', 'to', 'of', 'more', 'pageSizeSelectorLabel', 'firstPage', 'previousPage', 'nextPage', 'lastPage'];
// The empty state once a filter hides every row. (The column filter itself is
// our ValueSetFilter, which translates its own strings.)
const FILTER_KEYS = ['noRowsToShow'];
const pagerText = () => Object.fromEntries([...PAGER_KEYS, ...FILTER_KEYS].map((key) => [key, t(`dataGrid.${key}`)]));

// ---- Filter status -------------------------------------------------------
// "24 of 120 rows · Clear filters" under the grid while any filter is on.
const filterStatus = ref<{ shown: number; total: number } | null>(null);
let memoryFilter: Record<string, any> | null = null;  // survives a language switch

const syncFilterStatus = (api: any) => {
  if (!api?.isAnyFilterPresent?.()) { filterStatus.value = null; return; }
  filterStatus.value = { shown: api.getDisplayedRowCount(), total: (rowData.value || []).length };
};
const clearFilters = () => {
  const api = gridApi();
  api?.setFilterModel(null);
  syncFilterStatus(api);
};

// The filter popup names the column it filters (ag-grid's has no title).
const titleFilterPopup = (e: any) => {
  const host = e?.eGui;
  if (!host || host.querySelector(':scope > .grid-filter-title')) return;
  const title = document.createElement('div');
  title.className = 'grid-filter-title';
  title.textContent = e.column?.getColDef?.()?.headerName || e.column?.getColId?.() || '';
  host.prepend(title);
};

// ---- Column layout -------------------------------------------------------
// No `flex`: a flex column gives back width to its neighbours, so dragging
// one column wider used to shrink all the others and nothing stuck.
//
// Widths come from the CONTENT on every load (sized within MIN..MAX), except
// the columns the viewer resized by hand. Only what the viewer actually did
// is remembered — the order after a drag, the sort, and those hand-set
// widths — never a snapshot of every column: a snapshot froze automatic
// widths (or a wrong one) as if the viewer had chosen them.
type GridLayout = {
  order: string[];                                  // empty until a column is dragged
  sort: { colId: string; sort: string; sortIndex?: number | null }[];
  widths: Record<string, number>;                   // hand-resized columns only
};
const emptyLayout = (): GridLayout => ({ order: [], sort: [], widths: {} });
let layout: GridLayout = emptyLayout();
let layoutLoaded = false;   // read from storage once; then `layout` is the truth
let needsLayout = false;    // the column set changed under a live grid
let saveTimer: ReturnType<typeof setTimeout> | null = null;

const currentLayout = (): GridLayout => {
  if (!layoutLoaded) {
    layout = (loadGridState(props.stateKey) as GridLayout | null) || emptyLayout();
    layoutLoaded = true;
  }
  return layout;
};

// Size every column to its content, within MIN_COL_WIDTH..MAX_AUTO_WIDTH.
const sizeToContent = (api: any) => {
  const cols = api.getColumns() || [];
  // Pass 1: what the header AND values need (ag-grid's default auto-size).
  api.autoSizeAllColumns(false);
  const withHeader = new Map(cols.map((c: any) => [c.getColId(), c.getActualWidth()]));
  // Pass 2: what the values alone need.
  api.autoSizeAllColumns(true);
  const state = cols.map((c: any) => {
    const values = c.getActualWidth();
    const both = withHeader.get(c.getColId()) as number;
    // When the header is the wider part (both > values), give it up to
    // MAX_HEADER_WIDTH; past that it truncates.
    const width = Math.max(values, Math.min(both, MAX_HEADER_WIDTH));
    return { colId: c.getColId(), width: Math.min(Math.max(width, MIN_COL_WIDTH), MAX_AUTO_WIDTH) };
  });
  if (state.length) api.applyColumnState({ state });
  // What each column would need to show everything (header and values).
  return withHeader as Map<string, number>;
};

// Spare width (a narrow result, or full screen) is handed out in two steps:
// 1. columns that are cut off — a long value or a truncated header — open up
//    toward their full size first, the smallest shortfalls first;
// 2. whatever is left is spread in proportion to the widths, so a wide
//    column stays wide and a narrow one narrow.
// (Not ag-grid's sizeColumnsToFit: it evens every column out to one width.)
const fillSpareWidth = (api: any, full: Map<string, number>) => {
  const viewport = gridRef.value?.$el?.querySelector?.('.ag-center-cols-viewport') as HTMLElement | null;
  const available = viewport?.clientWidth || gridRef.value?.$el?.clientWidth || 0;
  const cols = api.getColumns() || [];
  const widths = new Map<string, number>(cols.map((c: any) => [c.getColId(), c.getActualWidth()]));
  const used = [...widths.values()].reduce((a, b) => a + b, 0);
  let spare = available - used - 2;
  if (!available || spare <= 0) return;
  const cut = cols
    .map((c: any) => ({ id: c.getColId(), need: Math.max(0, (full.get(c.getColId()) || 0) - (widths.get(c.getColId()) || 0)) }))
    .filter((x: any) => x.need > 0);
  // Smallest needs first, each taking at most an equal share of what's
  // left: a header short by 30px opens fully before a long text column
  // takes the rest.
  cut.sort((a: any, b: any) => a.need - b.need);
  cut.forEach((x: any, i: number) => {
    const add = Math.min(x.need, Math.floor(spare / (cut.length - i)));
    widths.set(x.id, (widths.get(x.id) || 0) + add);
    spare -= add;
  });
  if (spare > 0) {
    const total = [...widths.values()].reduce((a, b) => a + b, 0);
    for (const [id, w] of widths) widths.set(id, w + Math.floor(spare * w / total));
  }
  api.applyColumnState({ state: [...widths].map(([colId, width]) => ({ colId, width })) });
};

// Width the grid had at the last layout: 0 means it was laid out while
// hidden (a closed tab / collapsed panel), where nothing can be measured.
let laidOutWidth = -1;
// Set when full screen opens / closes: the next size change lays out again,
// once the moved grid has rendered at its new width (measuring earlier sees
// empty cells and spreads every column to the same width).
let relayoutOnResize = false;

const layoutColumns = (api: any) => {
  needsLayout = false;
  laidOutWidth = gridRef.value?.$el?.clientWidth || 0;
  const saved = currentLayout();
  const full = sizeToContent(api);
  const present = new Set((api.getColumns() || []).map((c: any) => c.getColId()));
  const handWidths = Object.entries(saved.widths).filter(([id]) => present.has(id));
  // Narrow results still fill the card (what `flex: 1` used to do) — a
  // one-off stretch, and only when the viewer hasn't sized columns by hand.
  if (!handWidths.length) fillSpareWidth(api, full);
  const sortBy = new Map(saved.sort.filter((s) => present.has(s.colId)).map((s) => [s.colId, s]));
  const order = saved.order.filter((id) => present.has(id));
  const ids = order.length ? order : [...present] as string[];
  const state = ids.map((colId) => {
    const width = saved.widths[colId];
    const sort = sortBy.get(colId);
    return { colId, ...(width ? { width } : {}), sort: sort?.sort ?? null, sortIndex: sort?.sortIndex ?? null };
  });
  // Columns the saved order doesn't know (the query gained one) stay after it.
  api.applyColumnState({ state, applyOrder: order.length > 0 });
};

const persistLayout = () => {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveGridState(props.stateKey, layout), 250);
};
const rememberWidths = (e: any) => {
  const cols = e.columns || (e.column ? [e.column] : []);
  for (const c of cols) currentLayout().widths[c.getColId()] = c.getActualWidth();
  persistLayout();
};
const rememberOrder = (api: any) => {
  currentLayout().order = (api.getColumnState() || []).map((s: any) => s.colId);
  persistLayout();
};
const rememberSort = (api: any) => {
  currentLayout().sort = (api.getColumnState() || [])
    .filter((s: any) => s.sort)
    .map((s: any) => ({ colId: s.colId, sort: s.sort, sortIndex: s.sortIndex ?? null }));
  persistLayout();
};

// Only the viewer's own actions count as a layout — not the auto-size, the
// fit, or a re-applied state (those carry non-"ui" sources).
const fromViewer = (e: any) => typeof e?.source === 'string' && e.source.startsWith('ui');

// Read once at mount by ag-grid-vue; later changes go through gridApi().
const baseOptions = () => ({
  autoHeaderHeight: false,
  suppressServerSideFullWidthLoadingRow: true,
  // A data refresh with the same columns keeps the viewer's column order.
  maintainColumnOrder: true,
  suppressMovableColumns: false,
  // Auto-size measures every column, not just the ones scrolled into view;
  // past ~40 columns keep virtualisation for speed (those keep the default).
  suppressColumnVirtualisation: props.columnDefs.length <= 40,
  // Hebrew & co.: columns run right to left and the grid scrolls from the
  // right. Read at creation; a language switch remounts the grid (below).
  enableRtl: localeTextDir(locale.value) === 'rtl',
  defaultColDef: {
    loadingCellRenderer: () => '',
    cellRenderer: linkCellRenderer,
    resizable: true,
    sortable: true,
    minWidth: MIN_COL_WIDTH,
  },
  loadingOverlayComponent: CustomLoadingRenderer,
  loadingOverlayComponentParams: { columns: props.columnDefs.length },
  enableCellTextSelection: true,
  paginationPageSize: PAGE_SIZE,
  onFirstDataRendered: (e: any) => {
    layoutColumns(e.api);
    if (memoryFilter) { e.api.setFilterModel(memoryFilter); memoryFilter = null; }
  },
  onFilterOpened: titleFilterPopup,
  onFilterChanged: (e: any) => syncFilterStatus(e.api),
  onRowDataUpdated: (e: any) => {
    if (needsLayout) layoutColumns(e.api);
    syncFilterStatus(e.api);
  },
  // Laid out while hidden: size again the first time it has a real width.
  onGridSizeChanged: (e: any) => {
    if (relayoutOnResize && e.clientWidth > 0) {
      relayoutOnResize = false;
      requestAnimationFrame(() => requestAnimationFrame(() => layoutColumns(e.api)));
    } else if (laidOutWidth === 0 && e.clientWidth > 0) {
      layoutColumns(e.api);
    }
  },
  onColumnResized: (e: any) => { if (e.finished && fromViewer(e)) rememberWidths(e); },
  onColumnMoved: (e: any) => { if (fromViewer(e)) rememberOrder(e.api); },
  onSortChanged: (e: any) => { if (fromViewer(e)) rememberSort(e.api); },
} as Record<string, any>);

// Only paginate when there are actually more rows than a page. A small preview
// (the common case for a data tool result) otherwise gets ag-grid's paging
// footer, which overlaps itself on narrow/mobile widths.
const paginationEnabled = computed(() => (rowData.value?.length || 0) > PAGE_SIZE);

const formatDescription = (trace) => {
  if (typeof trace === 'object') {
    return Object.entries(trace).map(([key, value]) => `${key}: ${value}`).join('<br />');
  }
  return trace;
};

// Inside a report (chat / side panel) the host hands down a bridge: the
// column filter then writes to the report's shared filters, so the chart and
// the Filters popover follow. Elsewhere the grid filters its own rows.
const filterBridge = inject<GridFilterBridge | null>(GRID_FILTER_BRIDGE, null);

const withHeader = (cols: any[]) => cols.map(col => ({
  ...col,
  // Every column filters by picking values (or From / To for a numeric
  // column with many values) — see components/grid/ValueSetFilter.vue.
  filter: col.filter === false ? false : ValueSetFilter,
  filterParams: {
    ...(col.filterParams || {}),
    numeric: col.type === 'numericColumn',
    bridge: filterBridge,
    t,
  },
  headerComponent: CustomHeader,
  headerComponentParams: {
    ...(col.headerComponentParams || {}),
    displayName: col.headerName,
    description: formatDescription(col.trace),
    filterBridge,
  }
}));

const columnDefs = ref(withHeader(props.columnDefs as any[]));

const rowData = ref(props.rowData);

const buildOptions = () => ({
  ...baseOptions(),
  localeText: pagerText(),
  columnDefs: toRaw(columnDefs.value),
  rowData: toRaw(rowData.value),
  pagination: paginationEnabled.value,
});
const gridOptions = shallowRef(buildOptions());
const gridKey = ref(0);
// A language switch (profile menu, or the org locale arriving after mount):
// keep the viewer's layout, rebuild with the current data, the new pager
// text and direction, and remount.
watch(locale, () => {
  const filters = gridApi()?.getFilterModel?.();
  memoryFilter = filters && Object.keys(filters).length ? filters : null;
  gridOptions.value = buildOptions();
  gridKey.value++;
});

const fieldsOf = (cols: any[]) => cols.map((c: any) => c.field).join('\u0000');

watch(() => props.columnDefs, (newVal) => {
  const changed = fieldsOf(newVal as any[]) !== fieldsOf(columnDefs.value);
  columnDefs.value = withHeader(newVal as any[]);
  if (changed) {
    // A different result (other columns): size again once its rows render,
    // or right away if the rows are already in place.
    needsLayout = true;
    setTimeout(() => { const api = gridApi(); if (needsLayout && api) layoutColumns(api); }, 0);
  }
});

watch(() => props.rowData, (newVal) => {
  rowData.value = newVal;
});

watch(columnDefs, (defs) => gridApi()?.setGridOption('columnDefs', toRaw(defs)));
watch(rowData, (rows) => gridApi()?.setGridOption('rowData', toRaw(rows)));
watch(paginationEnabled, (on) => gridApi()?.setGridOption('pagination', on));
</script>

<style>
.ag-link-cell {
  color: rgb(37 99 235);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.dark .ag-link-cell {
  color: rgb(96 165 250);
}

/* ---- Column filter popup ------------------------------------------------
   Quiet by design: the grid's own surface and border tokens (so it follows
   the light/dark/report theme), soft radius and shadow, muted secondary
   text. The popup lives inside the grid element, so the --ag-* tokens reach
   it. */
.ag-grid .ag-menu {
  min-width: 208px;
  border: 1px solid var(--ag-border-color);
  border-radius: 8px;
  background-color: var(--ag-background-color);
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.08), 0 1px 3px rgba(15, 23, 42, 0.06);
  overflow: hidden;
}
.grid-filter-title {
  padding: 9px 10px 0;
  font-size: 11px;
  font-weight: 500;
  color: var(--ag-foreground-color);
  opacity: 0.55;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}
.ag-grid .ag-filter-body-wrapper {
  padding: 8px 10px 4px;
  gap: 6px;
}
.ag-grid .ag-filter .ag-picker-field-wrapper,
.ag-grid .ag-filter input[class^='ag-'][type='text'],
.ag-grid .ag-filter input[class^='ag-'][type='number'] {
  min-height: 28px;
  border: 1px solid var(--ag-border-color);
  border-radius: 6px;
  background-color: var(--ag-background-color);
  color: var(--ag-foreground-color);
  padding-inline: 8px;
  box-shadow: none;
}
.ag-grid .ag-filter .ag-picker-field-wrapper:focus-within,
.ag-grid .ag-filter input[class^='ag-']:focus {
  border-color: rgb(96 165 250);
  box-shadow: 0 0 0 2px rgb(96 165 250 / 0.18);
  outline: none;
}
.ag-grid .ag-filter-apply-panel {
  padding: 2px 10px 8px;
  border-top: none;
  justify-content: flex-end;
}
.ag-grid .ag-filter-apply-panel .ag-standard-button {
  margin: 0;
  padding: 2px 4px;
  border: none;
  background: transparent;
  color: var(--ag-foreground-color);
  opacity: 0.55;
  font-size: 11px;
  font-weight: 500;
  box-shadow: none;
}
.ag-grid .ag-filter-apply-panel .ag-standard-button:hover {
  opacity: 0.9;
}
/* The operator dropdown ("Contains", "Equals", …). */
.ag-grid .ag-select-list {
  border: 1px solid var(--ag-border-color);
  border-radius: 6px;
  background-color: var(--ag-background-color);
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.08);
  padding: 3px 0;
}
.ag-grid .ag-select-list-item {
  padding-inline: 10px;
}
.ag-grid .ag-select-list-item.ag-active-item,
.ag-grid .ag-select-list-item:hover {
  background-color: color-mix(in srgb, var(--ag-foreground-color) 6%, transparent);
}

/* ---- "24 of 120 rows · Clear filters" ------------------------------------ */
.grid-filter-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 4px 0;
  font-size: 11px;
  line-height: 16px;
  opacity: 0.6;
}
.grid-filter-status__clear {
  color: inherit;
  text-decoration: underline;
  text-underline-offset: 2px;
  text-decoration-color: color-mix(in srgb, currentColor 40%, transparent);
}
.grid-filter-status__clear:hover {
  text-decoration-color: currentColor;
}
.grid-filter-status:hover {
  opacity: 0.85;
}

/* ---- Expand button + full-screen overlay --------------------------------- */
.grid-toolbar {
  display: flex;
  justify-content: flex-end;
  padding-bottom: 4px;
  flex: none;
}
.grid-toolbar__btn {
  display: inline-flex;
  align-items: center;
  color: rgb(156 163 175);
  transition: color 0.15s;
}
.grid-toolbar__btn:hover {
  color: rgb(75 85 99);
}
.dark .grid-toolbar__btn:hover {
  color: rgb(209 213 219);
}
.grid-shell {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  width: 100%;
}
.grid-overlay-backdrop {
  position: fixed;
  inset: 0;
  z-index: 70;
  background: rgb(15 23 42 / 0.35);
}
.grid-shell--expanded {
  position: fixed;
  inset: 24px;
  z-index: 71;
  width: auto;
  padding: 10px 12px 12px;
  border-radius: 10px;
  background-color: var(--ag-background-color, #fff);
  color: var(--ag-foreground-color, inherit);
  box-shadow: 0 20px 50px rgb(15 23 42 / 0.25);
}
@media (max-width: 640px) {
  .grid-shell--expanded { inset: 8px; }
}

.grid-container {
  width: 100%;
  display: flex;
  flex-direction: column;
  height: 100%;
}

.ag-grid {
  flex: 1;
  width: 100%;
}

/* ag-theme-balham declares the whole `--ag-*` palette ON the element carrying
   the theme class — which is this grid, not the wrapper. A themed wrapper
   (.ag-grid-themed in RenderTable / TableAgGrid) sets its token values as
   inline custom properties one level up, and those were being shadowed for
   everything inside the grid: the app/report tokens never reached a single
   cell. In light mode that went unnoticed (balham is white too); in dark mode
   the grid rendered balham-dark's own greys (#2d3436 rows on a #1c1c1c header)
   inside a #111827 panel — three different surfaces in one card.

   Re-inherit the tokens the wrappers actually set, so the surface, text and
   borders follow the app theme. The filter popup's wrapper (.ag-popup) gets
   the theme class again from ag-grid, so it re-inherits too — otherwise the
   popup is balham-dark grey on a navy dark-mode grid. Everything else
   (icons, the active accent) keeps its per-variant balham value, which is
   already light/dark correct. */
.ag-grid-themed .ag-grid,
.ag-grid-themed .ag-popup {
  --ag-background-color: inherit;
  --ag-foreground-color: inherit;
  --ag-header-background-color: inherit;
  --ag-header-foreground-color: inherit;
  --ag-border-color: inherit;
  --ag-odd-row-background-color: inherit;
  --ag-row-hover-color: inherit;
  --ag-selected-row-background-color: inherit;
  --ag-font-family: inherit;
  --ag-font-size: inherit;
  /* Not set by the wrappers — derive them so row/column rules stop using
     balham's own greys once --ag-border-color follows the theme. */
  --ag-row-border-color: var(--ag-border-color);
  --ag-header-column-separator-color: var(--ag-border-color);
  /* The wrappers declare these as var(--ag-header-background-color), but that
     resolves on the WRAPPER — i.e. to the flat card background, not to the
     tinted header below. Re-declare them here so they resolve against this
     element's header color and hovering a header cell doesn't flash it flat. */
  --ag-header-cell-hover-background-color: var(--ag-header-background-color);
  --ag-header-cell-moving-background-color: var(--ag-header-background-color);
}

/* Both wrappers set --ag-header-background-color to the same token as the body
   (cardBackground), so once the tokens actually reached the grid the header row
   collapsed onto the rows and the only separation left was the border under
   .ag-header. Tint it toward the text color instead of picking a literal: that
   darkens the header in light mode and lifts it in dark mode while staying in
   the card's own hue family, so it also holds for the named report themes
   (retro, hacker, …) without giving each one a header token of its own.

   Gated on @supports because a custom property accepts any token stream at
   parse time — without the gate, a browser that can't do color-mix would take
   this declaration and then drop the property at computed-value time, leaving
   the header with no background at all rather than the plain inherit above. */
@supports (color: color-mix(in srgb, red 6%, blue)) {
  .ag-grid-themed .ag-grid {
    --ag-header-background-color: color-mix(
      in srgb,
      var(--ag-foreground-color) 6%,
      var(--ag-background-color)
    );
  }
}
</style>
