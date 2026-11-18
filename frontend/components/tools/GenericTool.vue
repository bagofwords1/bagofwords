<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-wrench-screwdriver" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ toolTitle }}</template>
      <template v-else>{{ toolTitle }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted"><span v-if="showDuration" class="ms-2 text-ink-muted">{{ formatDuration }}</span></div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="toolHeaderExpanded" class="mt-2 ms-4 space-y-3">
        <!-- Input arguments - Full Display -->
        <div v-if="hasInput">
          <div class="text-chat-3 uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1.5">{{ t('tools.common.input') }}</div>
          <div class="border rounded-md overflow-hidden">
            <!-- Structured Input Fields -->
            <div class="divide-y divide-gray-100 dark:divide-gray-800">
              <div v-for="(value, key) in inputArgs" :key="key" class="px-3 py-2">
                <div class="flex items-start gap-2">
                  <span class="text-chat-3 font-medium text-gray-500 dark:text-gray-400 min-w-[80px] pt-0.5">{{ key }}</span>
                  <div class="flex-1 min-w-0">
                    <!-- String values -->
                    <template v-if="typeof value === 'string'">
                      <div v-if="value.length > 150" class="space-y-1">
                        <div class="text-chat-2 text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words">
                          {{ showFullInput[key] ? value : value.slice(0, 150) + '...' }}
                        </div>
                        <button
                          class="text-chat-2 text-blue-600 hover:text-blue-800"
                          @click.stop="toggleFullInput(key)"
                        >
                          {{ showFullInput[key] ? t('tools.common.showLess') : t('tools.common.showAll', { count: value.length }) }}
                        </button>
                      </div>
                      <div v-else class="text-chat-2 text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words">{{ value }}</div>
                    </template>
                    <!-- Number/Boolean values -->
                    <template v-else-if="typeof value === 'number' || typeof value === 'boolean'">
                      <span class="text-chat-2 text-gray-800 dark:text-gray-200 font-mono">{{ value }}</span>
                    </template>
                    <!-- Array values -->
                    <template v-else-if="Array.isArray(value)">
                      <div class="space-y-1">
                        <div class="flex flex-wrap gap-1">
                          <span
                            v-for="(item, idx) in (showFullInput[key] ? value : value.slice(0, 5))"
                            :key="idx"
                            class="inline-flex items-center px-1.5 py-0.5 rounded text-chat-3 bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                          >
                            {{ typeof item === 'object' ? JSON.stringify(item) : item }}
                          </span>
                          <span v-if="!showFullInput[key] && value.length > 5" class="text-chat-3 text-ink-muted">
                            +{{ value.length - 5 }} more
                          </span>
                        </div>
                        <button
                          v-if="value.length > 5"
                          class="text-chat-2 text-blue-600 hover:text-blue-800"
                          @click.stop="toggleFullInput(key)"
                        >
                          {{ showFullInput[key] ? t('tools.common.showLess') : t('tools.common.showAll', { count: value.length }) }}
                        </button>
                      </div>
                    </template>
                    <!-- Object values -->
                    <template v-else-if="typeof value === 'object' && value !== null">
                      <div class="space-y-1">
                        <pre class="text-chat-2 text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words bg-gray-50 dark:bg-gray-900 rounded px-2 py-1 overflow-x-auto max-h-32" :class="{ 'max-h-none': showFullInput[key] }">{{ formatObject(value, showFullInput[key]) }}</pre>
                        <button
                          v-if="JSON.stringify(value).length > 200"
                          class="text-chat-2 text-blue-600 hover:text-blue-800"
                          @click.stop="toggleFullInput(key)"
                        >
                          {{ showFullInput[key] ? t('tools.common.collapse') : t('tools.common.expand') }}
                        </button>
                      </div>
                    </template>
                    <!-- Null/undefined -->
                    <template v-else>
                      <span class="text-chat-2 text-ink-muted italic">null</span>
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Status -->
        <div class="text-chat-2">
          <span v-if="status === 'running'" class="tool-shimmer">{{ t('tools.generic.running') }}</span>
          <span v-else-if="status === 'success'" class="text-green-600 font-medium">✓ {{ t('tools.common.success') }}</span>
          <span v-else-if="status === 'error'" class="text-red-600 font-medium">✗ {{ statusReason || t('tools.common.failed') }}</span>
          <span v-else>{{ status }}</span>
        </div>

        <!-- Error message fallback (when result_json is empty but error_message exists) -->
        <div v-if="status === 'error' && toolExecution.error_message && !hasOutput"
             class="text-chat-2 text-red-700 bg-red-50 dark:bg-red-950 border border-red-200 rounded px-3 py-2 whitespace-pre-wrap break-words font-mono">
          {{ toolExecution.error_message }}
        </div>

        <!-- Output - Full Display -->
        <div v-if="hasOutput">
          <div class="text-chat-3 uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1.5">{{ t('tools.common.output') }}</div>
          <div class="border rounded-md overflow-hidden">
            <!-- Result Summary (if available) -->
            <div v-if="toolExecution.result_summary" class="px-3 py-2 bg-green-50 dark:bg-green-950 border-b border-green-100">
              <div class="text-chat-2 text-green-800">{{ toolExecution.result_summary }}</div>
            </div>

            <!-- Full Result Data -->
            <div v-if="outputData" class="px-3 py-2">
              <!-- String output -->
              <template v-if="typeof outputData === 'string'">
                <div class="text-chat-2 text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words">
                  {{ showFullOutput ? outputData : truncateString(outputData, 300) }}
                </div>
                <button
                  v-if="outputData.length > 300"
                  class="text-chat-2 text-blue-600 hover:text-blue-800 mt-1"
                  @click.stop="showFullOutput = !showFullOutput"
                >
                  {{ showFullOutput ? t('tools.common.showLess') : t('tools.common.showAll', { count: outputData.length }) }}
                </button>
              </template>
              <!-- Object output -->
              <template v-else-if="typeof outputData === 'object'">
                <div class="space-y-2">
                  <!-- Show key fields prominently -->
                  <div v-if="outputData.summary" class="text-chat-2 text-gray-800 dark:text-gray-200">
                    <span class="text-gray-500 dark:text-gray-400">{{ t('tools.common.summary') }}</span> {{ outputData.summary }}
                  </div>
                  <div v-if="outputData.error" class="text-chat-2 text-red-700 bg-red-50 dark:bg-red-950 rounded px-2 py-1">
                    <span class="font-medium">{{ t('tools.common.errorLabel') }}</span>
                    {{ typeof outputData.error === 'string' ? outputData.error : outputData.error.message || JSON.stringify(outputData.error) }}
                  </div>

                  <!-- Full JSON (collapsible) -->
                  <div>
                    <button
                      class="text-chat-2 text-blue-600 hover:text-blue-800 flex items-center gap-1"
                      @click.stop="showFullOutput = !showFullOutput"
                    >
                      <Icon :name="showFullOutput ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
                      {{ showFullOutput ? t('tools.common.hideFullOutput') : t('tools.common.showFullOutput') }}
                    </button>
                    <Transition name="fade">
                      <pre v-if="showFullOutput" class="mt-1 text-chat-2 text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words bg-gray-50 dark:bg-gray-900 rounded px-2 py-1.5 overflow-x-auto max-h-64 overflow-y-auto">{{ JSON.stringify(outputData, null, 2) }}</pre>
                    </Transition>
                  </div>
                </div>
              </template>
              <!-- Other types -->
              <template v-else>
                <div class="text-chat-2 text-gray-800 dark:text-gray-200">{{ outputData }}</div>
              </template>
            </div>
          </div>
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

import { computed, ref, reactive } from 'vue'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: any
    result_json?: any
    error_message?: string | null
    status: string
    status_reason?: string
    result_summary?: string
    duration_ms?: number
  }
}

const props = defineProps<Props>()

const showFullInput = reactive<Record<string, boolean>>({})
const showFullOutput = ref(false)

const status = computed(() => props.toolExecution.status)
const statusReason = computed(() => props.toolExecution.status_reason)

const toolTitle = computed(() => {
  // Prefer a model-authored, human-readable label when the tool provides one
  // (e.g. connection tools that set `title` — "Searching Notion for customer").
  const t = props.toolExecution.arguments_json?.title
  if (typeof t === 'string' && t.trim()) return t.trim()
  const name = props.toolExecution.tool_name
  const action = props.toolExecution.tool_action
  return action ? `${name} → ${action}` : name
})

// Input handling
const inputArgs = computed(() => {
  return props.toolExecution.arguments_json || {}
})

const hasInput = computed(() => {
  const args = props.toolExecution.arguments_json
  return args && Object.keys(args).length > 0
})

// Output handling
const outputData = computed(() => {
  return props.toolExecution.result_json
})

const hasOutput = computed(() => {
  return props.toolExecution.result_json !== undefined && props.toolExecution.result_json !== null
})

// Show duration if > 2 seconds
const showDuration = computed(() => {
  return props.toolExecution.duration_ms && props.toolExecution.duration_ms > 2000
})

const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})


function toggleFullInput(key: string) {
  showFullInput[key] = !showFullInput[key]
}

function formatObject(obj: any, full: boolean = false): string {
  try {
    const str = JSON.stringify(obj, null, 2)
    if (!full && str.length > 200) {
      return str.slice(0, 200) + '\n...'
    }
    return str
  } catch {
    return String(obj)
  }
}

function truncateString(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str
  return str.slice(0, maxLen) + '...'
}


</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@keyframes shimmer {
	0% { background-position: -100% 0; }
	100% { background-position: 100% 0; }
}

.tool-shimmer {
	background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
	background-size: 200% 100%;
	-webkit-background-clip: text;
	background-clip: text;
	color: transparent;
	animation: shimmer 2s linear infinite;
	font-weight: 400;
	opacity: 1;
}
</style>
