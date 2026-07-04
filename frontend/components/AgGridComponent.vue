<template>
  <div class="grid-container h-full">
    <ag-grid-vue
      :columnDefs="columnDefs"
      :rowData="rowData"
      class="ag-theme-balham ag-grid"
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
</style>
