<template>
    <div 
        v-if="step?.data?.columns?.length > 0" 
        class="h-full"
        :style="{
            backgroundColor: tokens.cardBackground || tokens.background,
            color: tokens.textColor,
            fontFamily: tokens.fontFamily
        }"
    >
        <div
            class="h-full ag-grid-themed ag-theme-custom"
            :style="agGridStyles"
        >
            <AgGridComponent class="text-[9px]" :columnDefs="columnDefs" :rowData="rowData" />
        </div>
    </div>
    <div 
        v-else
        :style="{ color: tokens.textColor }"
    >
        Loading..
    </div>
</template>

<script setup lang="ts">
import { ref, watch, toRefs, computed } from 'vue';
import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'

const props = defineProps<{
    widget: any
    step: any
    view?: Record<string, any> | null
    reportThemeName?: string | null
    reportOverrides?: Record<string, any> | null
}>()

// Convert to refs
const { step, reportThemeName, reportOverrides } = toRefs(props)

// Get theme tokens
const { tokens } = useDashboardTheme(reportThemeName?.value, reportOverrides?.value, props.view || null)

// Create AG Grid themed styles using CSS custom properties
const agGridStyles = computed(() => {
  // Extract primary color from palette (handle both string and gradient objects)
  const primaryColor = (() => {
    const palette = tokens.value.palette
    if (!palette || !palette[0]) return '#60a5fa'
    
    const firstColor = palette[0]
    if (typeof firstColor === 'string') return firstColor
    if (typeof firstColor === 'object' && firstColor && 'colorStops' in firstColor) {
      return (firstColor as any).colorStops?.[0]?.color || '#60a5fa'
    }
    return '#60a5fa'
  })()

  return {
    '--ag-background-color': tokens.value.cardBackground || tokens.value.background,
    '--ag-foreground-color': tokens.value.textColor,
    '--ag-header-background-color': tokens.value.cardBackground || tokens.value.background,
    '--ag-header-foreground-color': tokens.value.textColor,
    '--ag-border-color': tokens.value.cardBorder || tokens.value.axis?.gridLineColor || '#e5e7eb',
    '--ag-row-hover-color': `${primaryColor}20`, // 20% opacity
    '--ag-selected-row-background-color': `${primaryColor}30`, // 30% opacity
    '--ag-odd-row-background-color': tokens.value.cardBackground || tokens.value.background,
    '--ag-even-row-background-color': tokens.value.cardBackground || tokens.value.background,
    '--ag-font-family': tokens.value.fontFamily,
    '--ag-font-size': '9px',
    fontFamily: tokens.value.fontFamily,
  }
})

// Make these reactive with ref
const columnDefs = ref([]);
const rowData = ref([]);

// Initial setup
const updateData = () => {
    if (step.value?.data?.columns) {
        columnDefs.value = step.value.data.columns.map(col => {
            // Get stats info for this column
            const columnInfo = step.value?.data?.info?.column_info?.[col.field];
            let statsText = "";
            if (columnInfo) {
                if (columnInfo.dtype === 'int64' || columnInfo.dtype === 'float64') {
                    statsText = `${columnInfo.dtype}\nmin: ${columnInfo.min}\nmax: ${columnInfo.max}\nmean: ${Number(columnInfo.mean).toFixed(2)}`;
                } else if (columnInfo.dtype === 'object') {
                    statsText = `${columnInfo.dtype}\nunique: ${columnInfo.unique_count}/${columnInfo.count}`;
                }
            }

            return {
                field: col.field,
                headerName: col.headerName,
                sortable: true,
                filter: true,
                headerTooltip: statsText,
                headerComponent: 'CustomHeader',
                headerComponentParams: {
                    statsText,
                    themeTokens: tokens.value
                },
                valueGetter: (params) => {
                    return params.data[col.field];
                }
            };
        });
    }
    
    if (step.value?.data?.rows) {
        // Remove stats row logic and just set the rows directly
        rowData.value = step.value.data.rows;
    }
}

// Watch for changes
watch(step, updateData, { deep: true, immediate: true });
</script>

<style>
/* Custom AG Grid theme that overrides the default Balham theme */
.ag-theme-custom {
  /* Apply CSS custom properties to override AG Grid theme */
  --ag-cell-horizontal-padding: 8px;
  --ag-cell-vertical-padding: 4px;
  --ag-header-cell-hover-background-color: var(--ag-header-background-color);
  --ag-header-cell-moving-background-color: var(--ag-header-background-color);
  --ag-cell-focus-border-color: var(--ag-border-color);
  --ag-range-selection-border-color: var(--ag-border-color);
  --ag-input-focus-border-color: var(--ag-border-color);
}

.ag-theme-custom .ag-root-wrapper {
  border: 1px solid var(--ag-border-color) !important;
  border-radius: 6px !important;
  overflow: hidden !important;
  background-color: var(--ag-background-color) !important;
}

.ag-theme-custom .ag-header {
  background-color: var(--ag-header-background-color) !important;
  border-bottom: 1px solid var(--ag-border-color) !important;
}

.ag-theme-custom .ag-header-cell {
  background-color: var(--ag-header-background-color) !important;
  color: var(--ag-header-foreground-color) !important;
  font-family: var(--ag-font-family) !important;
  font-weight: 500 !important;
  border-right: 1px solid var(--ag-border-color) !important;
}

.ag-theme-custom .ag-header-cell-label {
  color: var(--ag-header-foreground-color) !important;
}

.ag-theme-custom .ag-row {
  background-color: var(--ag-background-color) !important;
  color: var(--ag-foreground-color) !important;
  font-family: var(--ag-font-family) !important;
  border-bottom: 1px solid var(--ag-border-color) !important;
}

.ag-theme-custom .ag-row:hover {
  background-color: var(--ag-row-hover-color) !important;
}

.ag-theme-custom .ag-row-selected {
  background-color: var(--ag-selected-row-background-color) !important;
}

.ag-theme-custom .ag-cell {
  border-right: 1px solid var(--ag-border-color) !important;
  font-family: var(--ag-font-family) !important;
  color: var(--ag-foreground-color) !important;
  background-color: transparent !important;
}

.ag-theme-custom .ag-paging-panel {
  background-color: var(--ag-background-color) !important;
  color: var(--ag-foreground-color) !important;
  border-top: 1px solid var(--ag-border-color) !important;
}

.ag-theme-custom .ag-paging-button {
  color: var(--ag-foreground-color) !important;
  background-color: transparent !important;
  border: 1px solid var(--ag-border-color) !important;
}

.ag-theme-custom .ag-paging-button:not(.ag-disabled):hover {
  background-color: var(--ag-row-hover-color) !important;
}

.ag-theme-custom .ag-paging-description {
  color: var(--ag-foreground-color) !important;
}

.ag-theme-custom .ag-paging-page-summary-panel {
  color: var(--ag-foreground-color) !important;
}

/* Additional overrides for input elements */
.ag-theme-custom .ag-input-field-input {
  background-color: var(--ag-background-color) !important;
  color: var(--ag-foreground-color) !important;
  border: 1px solid var(--ag-border-color) !important;
}

.ag-theme-custom .ag-picker-field-wrapper {
  background-color: var(--ag-background-color) !important;
  color: var(--ag-foreground-color) !important;
  border: 1px solid var(--ag-border-color) !important;
}
</style>