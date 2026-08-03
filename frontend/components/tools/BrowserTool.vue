<template>
  <div class="mt-1">
    <!-- Status header (click to expand) -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="flex items-center min-w-0">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer truncate">{{ runningLabel }}</span>
        </span>
        <span v-else class="flex items-center min-w-0" :class="isError ? 'text-gray-600 dark:text-gray-400' : 'text-gray-600 dark:text-gray-400'">
          <Icon :name="headerIcon" class="w-3 h-3 me-1.5 shrink-0" :class="iconColor" />
          <span class="truncate">{{ doneLabel }}</span>
          <span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[280px] text-gray-400 dark:text-gray-500">{{ displayUrl }}</span>
          <span v-if="blockedReason" class="ms-1.5 text-[10px] text-amber-500 shrink-0">{{ blockedReason }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 dark:text-gray-500 rtl-flip shrink-0"
          />
        </span>
      </div>
    </Transition>

    <!-- Expanded content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 ms-5 space-y-1.5">
        <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 dark:bg-red-950 rounded px-2 py-1">
          {{ errorMessage }}
        </div>

        <!-- Downloads -->
        <div v-if="downloads.length" class="flex flex-col gap-0.5">
          <div v-for="d in downloads" :key="d.file_id" class="flex items-center gap-1 text-[11px] text-gray-600 dark:text-gray-300">
            <Icon name="heroicons-arrow-down-tray" class="w-3 h-3 text-gray-400" />
            <span class="truncate">{{ d.filename }}</span>
          </div>
        </div>

        <!-- Extracted text -->
        <div v-if="text" class="max-h-40 overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800">
          <pre class="text-[10px] leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words">{{ text }}</pre>
        </div>

        <!-- Accessibility snapshot -->
        <div v-if="snapshot" class="group">
          <div
            class="flex items-center text-[11px] text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-600 dark:hover:text-gray-300 mb-0.5"
            @click.stop="showSnapshot = !showSnapshot"
          >
            <Icon :name="showSnapshot ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-2.5 h-2.5 me-1 text-gray-400 dark:text-gray-500 rtl-flip" />
            <span>{{ $t('tools.browser.snapshot') }}</span>
          </div>
          <div v-if="showSnapshot" class="max-h-48 overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800">
            <pre class="text-[10px] leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ snapshot }}</pre>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const isExpanded = ref(false)
const showSnapshot = ref(false)

const status = computed(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const toolName = computed(() => props.toolExecution?.tool_name || '')

const isSuccess = computed(() => status.value === 'success' && rj.value?.success === true)
const isError = computed(() => !isSuccess.value && status.value !== 'running')

const displayUrl = computed(() => rj.value?.url || args.value?.url || '')
const snapshot = computed(() => rj.value?.snapshot || '')
const text = computed(() => rj.value?.text || '')
const blockedReason = computed(() => rj.value?.blocked_reason || '')
const downloads = computed<any[]>(() => Array.isArray(rj.value?.downloads) ? rj.value.downloads : [])
const errorMessage = computed(() => (isError.value ? (rj.value?.error_message || '') : ''))

const verb = computed(() => {
  switch (toolName.value) {
    case 'browser_navigate': return { running: t('tools.browser.navigating'), done: t('tools.browser.navigated') }
    case 'browser_snapshot': return { running: t('tools.browser.snapshotting'), done: t('tools.browser.snapshotted') }
    case 'browser_extract': return { running: t('tools.browser.extracting'), done: t('tools.browser.extracted') }
    case 'browser_act': return { running: t('tools.browser.acting'), done: t('tools.browser.acted') }
    default: return { running: t('tools.browser.working'), done: t('tools.browser.done') }
  }
})

const runningLabel = computed(() => args.value?.title || verb.value.running)
const doneLabel = computed(() => {
  if (isError.value) return t('tools.browser.failed')
  return args.value?.title || verb.value.done
})
const headerIcon = computed(() => isError.value ? 'heroicons-globe-alt' : 'heroicons-globe-alt')
const iconColor = computed(() => {
  if (isError.value) return 'text-orange-500'
  if (blockedReason.value) return 'text-amber-500'
  return 'text-green-500'
})

function toggleExpanded() {
  if (status.value !== 'running') isExpanded.value = !isExpanded.value
}
</script>

<style scoped>
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
.slide-enter-active, .slide-leave-active { transition: all 0.15s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { opacity: 0; max-height: 0; }
.slide-enter-to, .slide-leave-from { opacity: 1; max-height: 400px; }
</style>
