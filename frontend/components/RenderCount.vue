<template>
    <div class="pt-0 pl-2">
        <div v-if="hasRows" class="text-2xl font-bold mt-2">
            {{ formattedValue }}
        </div>
        <div v-else class="text-gray-400">Loading..</div>
    </div>
</template>

<script setup lang="ts">
import { ref, watch, toRefs, computed } from 'vue';

const props = defineProps<{
    widget: any
    data: any
    data_model: any
    view?: Record<string, any> | null
}>()

const { data } = toRefs(props);

// Extract view config (v2 schema)
const viewConfig = computed(() => props.view?.view || {})

// Get value column from view or first column
const valueColumn = computed(() => {
    const v = viewConfig.value?.value
    if (v) return v.toLowerCase()
    return null
})

// Raw value
const rawValue = ref<any>(null)

const hasRows = computed(() => {
    const rows = data.value?.rows
    return Array.isArray(rows)
})

// Formatting
const formatType = computed(() => viewConfig.value?.format || 'number')
const prefix = computed(() => viewConfig.value?.prefix || '')
const suffix = computed(() => viewConfig.value?.suffix || '')

function formatNumber(val: any): string {
    if (val === null || val === undefined) return '—'
    
    const num = typeof val === 'number' ? val : parseFloat(String(val))
    if (isNaN(num)) return String(val)
    
    switch (formatType.value) {
        case 'currency':
            return new Intl.NumberFormat('en-US', { 
                style: 'currency', 
                currency: 'USD',
                minimumFractionDigits: 0,
                maximumFractionDigits: 2
            }).format(num)
        
        case 'percent':
            return new Intl.NumberFormat('en-US', { 
                style: 'percent',
                minimumFractionDigits: 0,
                maximumFractionDigits: 1
            }).format(num / 100)
        
        case 'compact':
            return new Intl.NumberFormat('en-US', { 
                notation: 'compact',
                maximumFractionDigits: 1
            }).format(num)
        
        default:
            return new Intl.NumberFormat('en-US', {
                minimumFractionDigits: 0,
                maximumFractionDigits: 2
            }).format(num)
    }
}

const formattedValue = computed(() => {
    const formatted = formatNumber(rawValue.value)
    if (formatted === '—') return formatted
    return `${prefix.value}${formatted}${suffix.value}`
})

const updateData = () => {
    try {
        const rows = data.value?.rows
        if (Array.isArray(rows) && rows.length > 0) {
            const firstRow = rows[0] || {}
            
            if (valueColumn.value) {
                const key = Object.keys(firstRow).find(k => k.toLowerCase() === valueColumn.value)
                if (key) {
                    rawValue.value = firstRow[key]
                    return
                }
            }
            
            // Fallback to first value
            rawValue.value = Object.values(firstRow)[0]
        } else if (Array.isArray(rows)) {
            rawValue.value = null
        }
    } catch {
        rawValue.value = null
    }
}

watch(data, updateData, { deep: true, immediate: true });
</script>
