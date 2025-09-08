<template>
  <div class="h-full w-full">
    <AgGridComponent v-if="columns.length" class="text-[9px] h-full" :columnDefs="columns" :rowData="rows" />
    <div v-else class="text-xs p-2">Loading..</div>
  </div>
</template>

<script setup lang="ts">
import { toRefs, ref, watch } from 'vue'
import { useDashboardTheme } from '../composables/useDashboardTheme'
import AgGridComponent from '../../AgGridComponent.vue'

const props = defineProps<{
  widget?: any
  step?: any
  view?: Record<string, any> | null
  reportThemeName?: string | null
  reportOverrides?: Record<string, any> | null
}>()

const { reportThemeName, reportOverrides } = toRefs(props)
const { tokens } = useDashboardTheme(reportThemeName?.value, reportOverrides?.value, props.view || null)

const columns = ref<any[]>([])
const rows = ref<any[]>([])

const updateData = () => {
  try {
    const step = props.step || {}
    const data = step?.data || {}
    if (Array.isArray(data.columns)) {
      columns.value = data.columns.map((col: any) => {
        const info = data?.info?.column_info?.[col.field]
        let statsText = ''
        if (info) {
          if (info.dtype === 'int64' || info.dtype === 'float64') {
            statsText = `${info.dtype}\nmin: ${info.min}\nmax: ${info.max}\nmean: ${Number(info.mean).toFixed(2)}`
          } else if (info.dtype === 'object') {
            statsText = `${info.dtype}\nunique: ${info.unique_count}/${info.count}`
          }
        }
        return {
          field: col.field,
          headerName: col.headerName,
          sortable: true,
          filter: true,
          headerTooltip: statsText,
          headerComponent: 'CustomHeader',
          headerComponentParams: { statsText },
          valueGetter: (params: any) => params.data[col.field]
        }
      })
    } else {
      columns.value = []
    }
    rows.value = Array.isArray(data.rows) ? data.rows : []
  } catch {
    columns.value = []
    rows.value = []
  }
}

watch(() => props.step, updateData, { deep: true, immediate: true })
</script>

<style scoped>
</style>


