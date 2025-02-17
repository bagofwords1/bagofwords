<template>
    <div v-if="step?.data?.rows?.length > 0" class="h-full">
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
        columnDefs.value = step.value.data.columns.map(col => ({
            field: col.field,
            headerName: col.headerName,
            sortable: true,
            filter: true,
            valueGetter: (params) => {
                // Handle dot notation in field names
                return params.data[col.field];
            }
        }));
    }
    
    if (step.value?.data?.rows) {
        rowData.value = step.value.data.rows;
    }
}

// Watch for changes
watch(step, updateData, { deep: true, immediate: true });
</script>