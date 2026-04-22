<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
        @click="toggleDetails"
      >
        <Icon :name="detailsCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1 text-gray-400" />
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-document-text" class="w-3 h-3 me-1 text-gray-400" />
          Writing CSV…
        </span>
        <span v-else class="flex items-center" :class="hasError ? 'text-red-600' : 'text-gray-700'">
          <Icon v-if="hasError" name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-red-500" />
          <Icon v-else name="heroicons-check" class="w-3 h-3 me-1 text-green-500" />
          <span class="align-middle">{{ statusLabel }}</span>
        </span>
        <span v-if="formatDuration" class="ms-1.5 text-gray-400">{{ formatDuration }}</span>
      </div>
    </Transition>

    <!-- Collapsible details -->
    <Transition name="fade">
      <div v-if="!detailsCollapsed" class="ms-4 text-xs text-gray-600 space-y-2">
        <!-- Error -->
        <div v-if="errorMessage" class="text-red-500">{{ errorMessage }}</div>

        <!-- Code section -->
        <div v-if="generatedCode">
          <div
            class="flex items-center text-gray-500 cursor-pointer hover:text-gray-700"
            @click.stop="codeCollapsed = !codeCollapsed"
          >
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1" />
            <Icon name="heroicons-code-bracket" class="w-3 h-3 me-1" />
            <span>{{ $t('tools.common.code') }}</span>
          </div>
          <Transition name="fade">
            <div v-if="!codeCollapsed" class="mt-1 ms-4">
              <pre class="bg-gray-50 rounded px-3 py-2 text-[11px] text-gray-700 overflow-x-auto max-h-48 whitespace-pre-wrap">{{ generatedCode }}</pre>
            </div>
          </Transition>
        </div>
      </div>
    </Transition>

    <!-- Widget Preview -->
    <div v-if="hasPreview" class="mt-2">
      <ToolWidgetPreview
        :tool-execution="enhancedToolExecution"
        :readonly="readonly"
        @toggleSplitScreen="$emit('toggleSplitScreen')"
        @editQuery="$emit('editQuery', $event)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
  duration_ms?: number
  created_widget_id?: string
  created_step_id?: string
  created_widget?: any
  created_step?: any
  created_visualizations?: any[]
}

const props = defineProps<{
  toolExecution: ToolExecution
  readonly?: boolean
}>()
defineEmits(['toggleSplitScreen', 'editQuery'])

const detailsCollapsed = ref(true)
const codeCollapsed = ref(true)

const status = computed(() => props.toolExecution?.status || '')
const rj = computed(() => props.toolExecution?.result_json || {})

const hasError = computed(() => rj.value.success === false)
const errorMessage = computed(() => rj.value.error_message || '')
const generatedCode = computed(() => rj.value.code || '')

const rowCount = computed(() => rj.value.row_count || 0)
const columns = computed<string[]>(() => rj.value.columns || [])

const statusLabel = computed(() => {
  if (hasError.value) return 'CSV generation failed'
  if (rowCount.value) return `CSV written — ${rowCount.value} rows, ${columns.value.length} columns`
  return 'CSV written'
})

const formatDuration = computed(() => {
  const ms = props.toolExecution?.duration_ms
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
})

// Build enhanced tool execution with synthetic step from result_json
const enhancedToolExecution = computed(() => {
  const te: any = props.toolExecution
  if (!te) return te

  // If created_step already exists with data, use as-is
  if (te.created_step?.data) return te

  const r = te.result_json || {}
  const hasStepData = r.data?.rows || r.data?.columns

  if (!hasStepData && !r.data_model && !r.view) return te

  const title = te.arguments_json?.title || 'Generated CSV'
  const syntheticStep = {
    id: te.created_step_id || r.step_id || `write-csv-step-${Date.now()}`,
    title,
    code: r.code || '',
    data: r.data || {},
    data_model: r.data_model || { type: 'table' },
    view: r.view || { type: 'table' },
    status: 'success',
  }

  return {
    ...te,
    created_step: te.created_step || syntheticStep,
    result_json: {
      ...r,
      widget_title: title,
    },
  }
})

const hasPreview = computed(() => {
  const te: any = props.toolExecution
  const hasStep = !!(te?.created_step || te?.created_step_id)
  const hasViz = Array.isArray(te?.created_visualizations) && te.created_visualizations.length > 0
  const hasData = !!(te?.result_json?.data?.rows)
  return hasStep || hasViz || hasData
})

function toggleDetails() {
  detailsCollapsed.value = !detailsCollapsed.value
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

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(2px);
}
</style>
