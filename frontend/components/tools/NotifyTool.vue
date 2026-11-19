<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-envelope" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">
          Notifying…
        </template>
      <template v-else-if="isSuccess">{{ summaryLine }}</template>
      <template v-else>Couldn't send notification</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">

    <Transition name="slide">
      <div v-if="toolHeaderExpanded && status !== 'running'" class="mt-2 space-y-2">
        <div class="border border-gray-150 dark:border-gray-800 rounded-md overflow-hidden">
          <div v-if="subject" class="px-3 py-1.5 bg-gray-50 dark:bg-gray-900 border-b border-gray-150 dark:border-gray-800 flex items-center gap-2">
            <span class="text-chat-3 text-gray-600 dark:text-gray-400 font-medium">Subject</span>
            <span dir="auto" class="text-chat-2 text-gray-800 dark:text-gray-200 truncate">{{ subject }}</span>
          </div>
          <div v-for="r in results" :key="r.email" class="px-3 py-1 bg-white dark:bg-gray-900 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2">
            <Icon :name="r.delivered && r.delivered.length ? 'heroicons-check-circle' : 'heroicons-x-circle'"
              class="w-3 h-3 shrink-0" :class="r.delivered && r.delivered.length ? 'text-green-500' : 'text-red-500'" />
            <span dir="auto" class="text-chat-2 text-gray-700 dark:text-gray-300 truncate">{{ r.email }}<span v-if="r.is_self" class="text-ink-muted"> (you)</span></span>
            <span class="text-chat-3 text-ink-muted ms-auto">{{ (r.delivered || []).join(', ') || 'failed' }}</span>
          </div>
        </div>

        <div v-if="rejected.length" class="text-chat-2 text-amber-600 bg-amber-50 dark:bg-amber-950/40 rounded px-2 py-1">
          Skipped (not members of this org): {{ rejected.join(', ') }}
        </div>
        <div v-if="errorMessage" class="text-chat-2 text-red-500 bg-red-50 dark:bg-red-950 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const isExpanded = ref(false)

const status = computed<string>(() => props.toolExecution?.status || '')
const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

const subject = computed<string>(() => props.toolExecution?.result_json?.subject || props.toolExecution?.arguments_json?.subject || '')
const results = computed<any[]>(() => props.toolExecution?.result_json?.results || [])
const rejected = computed<string[]>(() => props.toolExecution?.result_json?.rejected || [])

const summaryLine = computed(() => {
  const reached = results.value.filter((r) => (r.delivered || []).length).length
  const subj = subject.value ? `: ${subject.value}` : ''
  return `Notified ${reached} recipient${reached === 1 ? '' : 's'}${subj}`
})

const errorMessage = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  if (status.value === 'error') return rj.error || rj.message || ''
  if (status.value === 'success' && rj.success === false) return rj.error || ''
  return ''
})

function toggleExpanded() {
  if (status.value !== 'running') isExpanded.value = !isExpanded.value
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
.slide-enter-active, .slide-leave-active { transition: all 0.15s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { opacity: 0; max-height: 0; }
.slide-enter-to, .slide-leave-from { opacity: 1; max-height: 500px; }
</style>
