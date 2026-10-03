<template>
  <div class="grid-container h-full">
    <ag-grid-vue
      :columnDefs="columnDefs"
      :rowData="rowData"
      :class="[agThemeClass, 'ag-grid']"
      :gridOptions="gridOptions"
      :pagination="paginationEnabled"
      :paginationPageSize="PAGE_SIZE"
      :loadingOverlayComponent="CustomLoadingRenderer"
      :loadingOverlayComponentParams="{ columns: columnCount }">
    </ag-grid-vue>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { AgGridVue } from 'ag-grid-vue3';
import CustomHeader from './CustomHeader.vue'; // Import the CustomHeader component
import CustomLoadingRenderer from './CustomLoadingRenderer.vue'; // Import the CustomLoadingRenderer component
import 'ag-grid-community/styles/ag-grid.css';
//import 'ag-grid-community/styles/ag-theme-alpine.css';
import 'ag-grid-community/styles/ag-theme-balham.css';

// Follow the app color mode so the grid chrome (menus, filter popups, icons)
// doesn't stay light on a dark page. Wrappers that theme via .ag-theme-custom
// CSS variables still win — their rules use !important.
const colorMode = useColorMode();
const agThemeClass = computed(() =>
  colorMode.value === 'dark' ? 'ag-theme-balham-dark' : 'ag-theme-balham'
);

const isLoading = ref(true);

const props = defineProps({
  columnDefs: {
    type: Array,
    required: true
  },
  rowData: {
    type: Array,
    required: true
  }

});

const PAGE_SIZE = 50;

const gridOptions = ref({
  autoHeaderHeight: false,
  suppressServerSideFullWidthLoadingRow: true,
  defaultColDef: {
    loadingCellRenderer: () => '',
    resizable: true,
    sortable: true,
    // Fill the available width so columns aren't stuck at the ~200px default
    // (which showed only 2 columns on a phone); keep a readable minimum and
    // let the grid scroll horizontally when the columns can't all fit.
    flex: 1,
    minWidth: 110,
  },
  loadingOverlayComponent: CustomLoadingRenderer,
  enableCellTextSelection: true
});

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

const columnDefs = ref(props.columnDefs.map(col => ({
  ...col,
  headerComponent: CustomHeader,
  headerComponentParams: {
    displayName: col.headerName,
    description: formatDescription(col.trace)
  }
})));

const rowData = ref(props.rowData);

watch(() => props.columnDefs, (newVal) => {
  columnDefs.value = newVal.map(col => ({
    ...col,
    headerComponent: CustomHeader,
    headerComponentParams: {
      displayName: col.headerName,
      description: formatDescription(col.trace)
    }
  }));
});

watch(() => props.rowData, (newVal) => {
  rowData.value = newVal;
});

// Dynamically set the number of columns for the skeleton loader
const columnCount = ref(0);
onMounted(() => {
  columnCount.value = columnDefs.value.length;
});
</script>

<style>
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
   borders follow the app theme. Everything else (menus, filter popups, icons,
   the active accent) intentionally keeps its per-variant balham value, which
   is already light/dark correct. */
.ag-grid-themed .ag-grid {
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
