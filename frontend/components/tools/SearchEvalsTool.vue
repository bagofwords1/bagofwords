<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-magnifying-glass" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ queryLabel ? t('tools.searchEvals.searchingFor', { query: queryLabel }) : t('tools.searchEvals.searching') }}</template>
      <template v-else>{{ queryLabel ? t('tools.searchEvals.searchedFor', { query: queryLabel }) : t('tools.searchEvals.searched') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span v-if="total > 0" class="ms-1.5 text-chat-3 text-ink-muted">· {{ total === 1 ? t('tools.searchEvals.matchSingular', { count: total }) : t('tools.searchEvals.matchPlural', { count: total }) }}</span></template></div>

    <div v-if="items.length" class="text-chat-2 text-gray-600 dark:text-gray-400">
      <ul class="ms-1 space-y-1 leading-snug">
        <li v-for="item in items" :key="item.id" class="flex items-center py-1 px-1 rounded">
          <Icon name="heroicons-beaker" class="w-3 h-3 me-1 text-purple-400 flex-shrink-0" />
          <NuxtLink :to="`/agents`" class="font-medium text-gray-700 dark:text-gray-300 truncate hover:text-blue-600" :title="item.prompt_content || item.name">
            {{ item.name || t('tools.searchEvals.unnamed') }}
          </NuxtLink>
          <span v-if="item.suite_name" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ item.suite_name }}</span>
          <span v-if="item.status === 'draft'" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-amber-100 text-amber-800 flex-shrink-0">{{ t('tools.searchEvals.statusDraft') }}</span>
          <span v-else-if="item.status === 'archived'" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 flex-shrink-0">{{ t('tools.searchEvals.statusArchived') }}</span>
          <span v-if="item.auto_generated" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-purple-100 text-purple-800 flex-shrink-0">{{ t('tools.searchEvals.autoBadge') }}</span>
        </li>
      </ul>
    </div>

    <div v-if="status !== 'running' && !items.length" class="text-chat-2 text-ink-muted ms-1">
      {{ t('tools.searchEvals.empty') }}
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'

const { t } = useResponseI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const q = props.toolExecution?.arguments_json?.query
  if (typeof q === 'string' && q.trim()) return `"${q}"`
  return ''
})

const items = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.items) ? rj.items : []
})

const total = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return typeof rj.total === 'number' ? rj.total : items.value.length
})
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
