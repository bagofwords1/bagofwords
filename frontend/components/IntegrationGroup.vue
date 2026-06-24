<template>
  <div v-if="items.length" class="mb-2">
    <div class="px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-gray-400">{{ title }}</div>
    <button
      v-for="i in items" :key="i.key"
      @click="$emit('select', i)"
      :class="[
        'w-full flex items-center gap-3 px-3 py-2 text-left transition',
        selected && selected.key === i.key ? 'bg-indigo-50/70' : 'hover:bg-gray-50'
      ]"
    >
      <DataSourceIcon :type="i.type" class="h-6 shrink-0" />
      <div class="min-w-0 flex-1">
        <div class="text-sm text-gray-900 truncate flex items-center gap-1.5">
          {{ i.title }}
          <span v-if="i.kind === 'personal' && i.connected" class="w-1.5 h-1.5 rounded-full bg-green-500 shrink-0"></span>
        </div>
        <div class="text-[11px] text-gray-400 truncate">
          <template v-if="i.kind === 'org'">Workspace data</template>
          <template v-else-if="i.connected">{{ i.tool_count }} {{ i.tool_count === 1 ? 'tool' : 'tools' }}</template>
          <template v-else>Not connected</template>
        </div>
      </div>
    </button>
  </div>
</template>

<script setup lang="ts">
defineProps<{ title: string; items: any[]; selected: any }>()
defineEmits<{ (e: 'select', item: any): void }>()
</script>
