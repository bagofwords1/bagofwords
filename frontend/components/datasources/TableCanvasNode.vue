<template>
  <div :style="{ width: `${TABLE_NODE_WIDTH}px`, height: `${TABLE_NODE_HEIGHT}px` }" class="table-node rounded-lg border shadow-sm text-[11px] leading-normal"
    :class="[data.active ? 'border-blue-500 dark:border-blue-400 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200' : 'border-dotted border-gray-400 dark:border-gray-500 bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400', data.focused ? 'ring-2 ring-blue-500/20' : '']"
    :data-testid="`erd-node-${data.table.name}`" :data-active="data.active">
    <Handle type="target" :position="Position.Left" :connectable="false" class="!bg-gray-400 !border-white" />
    <div class="flex items-center gap-2 px-3 py-2.5 border-b border-gray-100 dark:border-gray-800">
      <input v-if="data.canUpdate" type="checkbox" :checked="data.active" :aria-label="t('tableErd.selectTable', { name: data.table.name })"
        class="nodrag nopan h-3.5 w-3.5 rounded border-gray-300 accent-blue-500 shrink-0" @click.stop @change="data.toggle(!data.active)" />
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5 min-w-0" :title="data.table.connection_name">
          <span data-testid="erd-connection-icon" class="shrink-0"><DataSourceIcon :type="data.table.connection_type" class="h-3.5 w-3.5" /></span>
          <span class="text-[11px] font-medium font-mono line-clamp-2 break-words leading-4" dir="ltr" :title="data.table.name">{{ data.table.name }}</span>
        </div>
        <div class="text-[9px] text-gray-400 line-clamp-2 break-words leading-3 mt-0.5">{{ [data.table.connection_name, data.table.metadata_json?.schema].filter(Boolean).join(' · ') }}</div>
      </div>
      <UIcon v-if="data.table.custom_query_id" name="i-heroicons-bolt" class="w-3.5 h-3.5 text-gray-400" :title="t('tableErd.customQuery')" data-testid="erd-custom-query" />
      <button v-if="data.editQuery" class="nodrag nopan text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" :aria-label="t('tableErd.editQuery', { name: data.table.name })" @click.stop="data.editQuery()"><UIcon name="i-heroicons-pencil-square" class="w-3.5 h-3.5" /></button>
    </div>
    <div class="px-3 py-2 space-y-1 flex-1">
      <div v-for="column in previewColumns" :key="column" class="flex items-center gap-2 text-[10px] font-mono" dir="ltr">
        <UIcon :name="data.keyColumns.includes(column) ? 'i-heroicons-key' : 'i-heroicons-table-cells'" class="w-3 h-3 text-gray-400" /><span class="truncate">{{ column }}</span>
      </div>
      <div class="flex items-center justify-between gap-2 pt-1">
        <button class="nodrag nopan text-[10px] text-gray-500 hover:text-gray-800 dark:hover:text-gray-200" @click.stop="data.openColumns()">{{ t('tableErd.columns', { count: data.table.columns?.length || 0 }) }} <UIcon name="i-heroicons-chevron-right" class="w-2.5 h-2.5 rtl-flip" /></button>
        <button v-if="data.hiddenNeighbors" class="nodrag nopan text-[9px] text-blue-600 dark:text-blue-400 hover:underline" @click.stop="data.expand()">{{ t('tableErd.expand', { count: data.hiddenNeighbors }) }}</button>
      </div>
    </div>
    <div class="px-3 pb-2 text-[9px] text-gray-500 dark:text-gray-400">
      <TableMetrics v-if="data.showStats" :table="data.table" />
      <span v-else>{{ t(data.active ? 'tableErd.selected' : 'tableErd.notSelected') }}</span>
    </div>
    <Handle type="source" :position="Position.Right" :connectable="false" class="!bg-gray-400 !border-white" />
  </div>
</template>
<script setup lang="ts">
import { TABLE_NODE_WIDTH, TABLE_NODE_HEIGHT } from '~/utils/tableGraph'
import { Handle, Position } from '@vue-flow/core'
import DataSourceIcon from '@/components/DataSourceIcon.vue'
import TableMetrics from './TableMetrics.vue'
const props = defineProps<{ data: any }>()
const previewColumns = computed(() => [...new Set<string>([...props.data.keyColumns, ...(props.data.table.columns || []).map((column: { name: string }) => column.name)])].slice(0, 3))
const { t } = useI18n()
</script>
<style scoped>
.table-node { display: flex; flex-direction: column; }
</style>
