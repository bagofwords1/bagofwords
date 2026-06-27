<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-bell" class="w-3 h-3 me-1.5 text-gray-400" />
          Notifying…
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-bell" class="w-3 h-3 me-1.5 text-blue-500" />
          <span dir="auto" class="truncate max-w-[320px]">{{ summaryLine }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 shrink-0 rtl-flip"
          />
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
          <span>Couldn't send notification</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 rtl-flip"
          />
        </span>
      </div>
    </Transition>

    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <div class="border border-gray-150 dark:border-gray-800 rounded-md overflow-hidden">
          <div v-if="subject" class="px-3 py-1.5 bg-gray-50 dark:bg-gray-900 border-b border-gray-150 dark:border-gray-800 flex items-center gap-2">
            <span class="text-[10px] text-gray-600 dark:text-gray-400 font-medium">Subject</span>
            <span dir="auto" class="text-[11px] text-gray-800 dark:text-gray-200 truncate">{{ subject }}</span>
          </div>
          <div v-for="r in results" :key="r.email" class="px-3 py-1 bg-white dark:bg-gray-900 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2">
            <Icon :name="r.delivered && r.delivered.length ? 'heroicons-check-circle' : 'heroicons-x-circle'"
              class="w-3 h-3 shrink-0" :class="r.delivered && r.delivered.length ? 'text-green-500' : 'text-red-500'" />
            <span dir="auto" class="text-[11px] text-gray-700 dark:text-gray-300 truncate">{{ r.email }}<span v-if="r.is_self" class="text-gray-400"> (you)</span></span>
            <span class="text-[10px] text-gray-400 ms-auto">{{ (r.delivered || []).join(', ') || 'failed' }}</span>
          </div>
        </div>

        <div v-if="rejected.length" class="text-[10px] text-amber-600 bg-amber-50 dark:bg-amber-950/40 rounded px-2 py-1">
          Skipped (not members of this org): {{ rejected.join(', ') }}
        </div>
        <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50 dark:bg-red-950 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
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
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
.slide-enter-active, .slide-leave-active { transition: all 0.15s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { opacity: 0; max-height: 0; }
.slide-enter-to, .slide-leave-from { opacity: 1; max-height: 500px; }
</style>
