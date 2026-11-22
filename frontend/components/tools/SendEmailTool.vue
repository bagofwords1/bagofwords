<template>
  <div class="min-w-0">
    <!-- Status header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-envelope" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.sendEmail.sending') }}</template>
      <template v-else-if="isSuccess">{{ t('tools.sendEmail.sentPrefix', { subject: truncatedSubject }) }}</template>
      <template v-else>{{ t('tools.sendEmail.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="toolHeaderExpanded && status !== 'running'" class="mt-2 space-y-2">
        <div class="border border-gray-150 rounded-md overflow-hidden">
          <div class="px-3 py-1.5 bg-gray-50 dark:bg-gray-900 border-b border-gray-150 flex items-center gap-2">
            <span class="text-chat-3 text-gray-600 dark:text-gray-400 font-medium">{{ t('tools.sendEmail.subject') }}</span>
            <span dir="auto" class="text-chat-2 text-gray-800 dark:text-gray-200 truncate">{{ subject }}</span>
          </div>
          <div v-if="recipient" class="px-3 py-1 bg-white dark:bg-gray-900 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2">
            <span class="text-chat-3 text-gray-500 dark:text-gray-400">{{ t('tools.sendEmail.to') }}</span>
            <span dir="auto" class="text-chat-2 text-gray-700 dark:text-gray-300">{{ recipient }}</span>
          </div>
          <div v-if="body" class="px-3 py-2 bg-white dark:bg-gray-900">
            <pre dir="auto" class="text-chat-2 text-gray-700 dark:text-gray-300 whitespace-pre-wrap font-sans m-0">{{ truncatedBody }}</pre>
          </div>
        </div>

        <!-- Error message -->
        <div v-if="errorMessage" class="text-chat-2 text-red-500 dark:text-red-300 bg-red-50 dark:bg-red-950 rounded px-2 py-1">
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

const { t } = useResponseI18n()

import { computed, ref } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
  duration_ms?: number
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const isExpanded = ref(false)

const status = computed<string>(() => props.toolExecution?.status || '')

const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

// Prefer the input arguments for subject/body (always present), fall back to output.
const subject = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  const rj = props.toolExecution?.result_json || {}
  return args.subject || rj.subject || ''
})

const body = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.body || ''
})

const recipient = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.recipient || ''
})

const truncatedSubject = computed(() => {
  const s = subject.value
  if (!s) return ''
  return s.length > 60 ? s.substring(0, 57) + '...' : s
})

const truncatedBody = computed(() => {
  const b = body.value
  if (!b) return ''
  return b.length > 1000 ? b.substring(0, 1000) + '…' : b
})

const errorMessage = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  if (status.value === 'error') {
    return rj.error || rj.message || ''
  }
  if (status.value === 'success' && rj.success === false) {
    return rj.error || ''
  }
  return ''
})

function toggleExpanded() {
  if (status.value !== 'running') {
    isExpanded.value = !isExpanded.value
  }
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}

.slide-enter-active, .slide-leave-active {
  transition: all 0.15s ease;
  overflow: hidden;
}
.slide-enter-from, .slide-leave-to {
  opacity: 0;
  max-height: 0;
}
.slide-enter-to, .slide-leave-from {
  opacity: 1;
  max-height: 500px;
}
</style>
