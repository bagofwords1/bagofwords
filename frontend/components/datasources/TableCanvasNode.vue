<template>
  <div class="table-node rounded-lg border bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 shadow-sm text-[11px] leading-normal"
    :class="[data.active ? 'border-gray-300 dark:border-gray-600' : 'border-dashed border-gray-300 dark:border-gray-600 bg-gray-50/90 dark:bg-gray-900/90', data.focused ? 'ring-2 ring-blue-500/50' : '']"
    :data-testid="`erd-node-${data.table.name}`" :data-active="data.active">
    <Handle type="target" :position="Position.Left" :connectable="false" class="!bg-gray-400 !border-white" />
    <div class="flex items-center gap-2 px-3 py-2.5 border-b border-gray-100 dark:border-gray-800">
      <input v-if="data.canUpdate" type="checkbox" :checked="data.active" :aria-label="t('tableErd.selectTable', { name: data.table.name })"
        class="nodrag nopan h-3.5 w-3.5 rounded border-gray-300 accent-blue-500 shrink-0" @click.stop @change="data.toggle(!data.active)" />
      <div class="min-w-0 flex-1">
        <div class="text-[11px] font-medium font-mono truncate" dir="ltr" :title="data.table.name">{{ data.table.name }}</div>
        <div class="text-[9px] text-gray-400 truncate mt-0.5">{{ [data.table.connection_name, data.table.metadata_json?.schema].filter(Boolean).join(' · ') }}</div>
      </div>
      <UIcon v-if="data.active" name="i-heroicons-table-cells" class="w-3.5 h-3.5 text-gray-400" />
      <button v-else-if="data.canUpdate" class="nodrag nopan text-[10px] text-blue-600 dark:text-blue-400 hover:underline" @click.stop="data.toggle(true)">{{ t('tableErd.add') }}</button>
    </div>
    <div class="px-3 py-2 space-y-1 flex-1">
      <div v-for="column in data.keyColumns.slice(0, 3)" :key="column" class="flex items-center gap-2 text-[10px] font-mono" dir="ltr">
        <UIcon name="i-heroicons-key" class="w-3 h-3 text-gray-400" /><span class="truncate">{{ column }}</span>
      </div>
      <span v-if="!data.keyColumns.length" class="text-[10px] text-gray-400">{{ t('tableErd.noKeys') }}</span>
    </div>
    <div class="flex items-center justify-between gap-2 px-3 pb-2 text-[9px] text-gray-500 dark:text-gray-400">
      <span>{{ data.metric || (data.active ? t('tableErd.selected') : t('tableErd.notSelected')) }}</span>
      <button v-if="data.hiddenNeighbors" class="nodrag nopan text-blue-600 dark:text-blue-400 hover:underline" @click.stop="data.expand()">
        {{ t('tableErd.expand', { count: data.hiddenNeighbors }) }}
      </button>
    </div>
    <Handle type="source" :position="Position.Right" :connectable="false" class="!bg-gray-400 !border-white" />
  </div>
</template>
<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
defineProps<{ data: any }>()
const { t } = useI18n()
</script>
<style scoped>
.table-node { width: 232px; height: 140px; display: flex; flex-direction: column; }
</style>
