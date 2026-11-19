<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="isRunning">{{ t('tools.readArtifactActivity.reading') }}</template>
      <template v-else>{{ headline }}
          </template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="isRunning"></template>
      <template v-else><span v-if="countLabel" class="text-ink-muted ms-1">· {{ countLabel }}</span><span v-if="failures > 0" class="text-red-500 ms-1">· {{ t('tools.readArtifactActivity.failed', { n: failures }, failures) }}</span></template></div>

    <p v-if="!isRunning && errorMessage" class="ms-4 mt-0.5 text-chat-2 text-ink-muted">{{ errorMessage }}</p>

    <Transition name="slide">
      <div
        v-if="toolHeaderExpanded && canExpand"
        class="mt-1.5 ms-4 max-h-48 overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800"
        dir="ltr"
      >
        <!-- One event's detail -->
        <div v-if="single" class="p-2 space-y-1 text-chat-2 font-mono leading-snug">
          <div :class="isFailedEvent(single) ? 'text-red-600 dark:text-red-400' : 'text-gray-700 dark:text-gray-300'">{{ single.line }}</div>
          <pre v-if="singleDetail" class="m-0 whitespace-pre-wrap break-words text-gray-500 dark:text-gray-400">{{ singleDetail }}</pre>
        </div>
        <!-- Event list -->
        <ul v-else class="py-1 text-chat-2 font-mono leading-snug">
          <li
            v-for="ev in events"
            :key="ev.id"
            class="px-2 py-0.5 whitespace-pre-wrap break-words"
            :class="ev.failed ? 'text-red-600 dark:text-red-400' : 'text-gray-600 dark:text-gray-400'"
          >{{ ev.line }}</li>
        </ul>
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
import { useResponseI18n } from '~/composables/useResponseI18n'

const { t } = useResponseI18n()

const props = defineProps<{
  toolExecution: { status: string; result_json?: any; arguments_json?: any }
  readonly?: boolean
}>()

const isExpanded = ref(false)

const status = computed(() => props.toolExecution?.status || '')
const isRunning = computed(() => status.value === 'running' || status.value === 'in_progress')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})

const events = computed<any[]>(() => (Array.isArray(rj.value?.events) ? rj.value.events : []))
const single = computed<any>(() => rj.value?.event || null)
const failures = computed(() => (single.value ? 0 : Number(rj.value?.failures) || 0))
const errorMessage = computed(() => rj.value?.error || '')

const canExpand = computed(() => !isRunning.value && (!!single.value || events.value.length > 0))

const headline = computed(() => {
  if (single.value || args.value?.event_id) return t('tools.readArtifactActivity.checkedEvent')
  if (args.value?.only_errors) return t('tools.readArtifactActivity.checkedErrors')
  return t('tools.readArtifactActivity.checked')
})

const countLabel = computed(() => {
  if (single.value) return single.value.kind || ''
  if (errorMessage.value) return ''
  const n = events.value.length
  return n ? t('tools.readArtifactActivity.events', { n }, n) : t('tools.readArtifactActivity.none')
})

function isFailedEvent(ev: any) {
  return ev?.status === 'error' || !!ev?.error_code || ev?.kind === 'error'
}

// Message + stack for one event; falls back to the raw detail.
const singleDetail = computed(() => {
  const d = single.value?.detail || {}
  const parts = [d.message, d.stack].filter(Boolean)
  if (parts.length) return parts.join('\n')
  return Object.keys(d).length ? JSON.stringify(d, null, 2) : ''
})
</script>

<style scoped>
.slide-enter-active,
.slide-leave-active { transition: all 0.2s ease; }
.slide-enter-from,
.slide-leave-to { opacity: 0; transform: translateY(-4px); }

</style>
