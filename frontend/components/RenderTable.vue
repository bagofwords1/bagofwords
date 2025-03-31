<template>
    <div v-if="step?.data?.columns?.length > 0" class="h-full">
        <AgGridComponent class="text-[9px]" :columnDefs="columnDefs" :rowData="rowData" />
    </div>
    <div v-else>
        Loading..
    </div>
</template>

<script setup lang="ts">
import { ref, watch, toRefs } from 'vue';

const props = defineProps<{
    widget: any
    step: any
}>()

// Convert to refs
const { step } = toRefs(props);

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
                    statsText
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