<template>
    <div class="pt-0 pl-2">
        <div v-if="show_title" class="font-bold text-gray-400">{{ widget.title }}</div>
        <div v-if="hasRows" class="text-xl font-bold mt-2">
            {{ displayValue }}
        </div>
        <div v-else>Loading..</div>
    </div>
</template>

<script setup lang="ts">
import { ref, watch, toRefs, computed } from 'vue';

const props = defineProps<{
    widget: any
    data: any
    data_model: any
    show_title: boolean
}>()

// Convert to refs
const { data, widget, show_title } = toRefs(props);

// Make count reactive with ref
const countValue = ref<any>(null);

// Whether rows are present (even zero rows should show "None" rather than hang)
const hasRows = computed(() => {
    const rows = data.value?.rows
    return Array.isArray(rows)
})

const displayValue = computed(() => {
    return (countValue.value ?? 'None') as any
})

// Initial setup
const updateData = () => {
    try {
        const rows = data.value?.rows
        if (Array.isArray(rows) && rows.length > 0) {
            const firstRow = rows[0] || {}
            const firstValue = Object.values(firstRow)[0]
            countValue.value = firstValue as any
        } else if (Array.isArray(rows)) {
            // Rows present but empty
            countValue.value = null
        }
    } catch {
        countValue.value = null
    }
}

// Watch for changes
watch(data, updateData, { deep: true, immediate: true });
</script>