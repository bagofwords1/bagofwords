<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleDetails">
        <Icon :name="detailsCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1 text-gray-400" />
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-document-magnifying-glass" class="w-3 h-3 mr-1 text-gray-400" />
          Reading {{ queryCount > 1 ? `${queryCount} queries` : `query "${queryTitle}"` }}…
        </span>
        <span v-else class="flex items-center" :class="hasErrors ? 'text-red-600' : 'text-gray-700'">
          <Icon v-if="hasErrors" name="heroicons-exclamation-triangle" class="w-3 h-3 mr-1 text-red-500" />
          <Icon v-else name="heroicons-document-magnifying-glass" class="w-3 h-3 mr-1 text-gray-400" />
          <span class="align-middle">{{ statusLabel }}</span>
        </span>
      </div>
    </Transition>

    <!-- Widget Previews (collapsed by default, one per result) -->
    <Transition name="fade">
      <div v-if="!detailsCollapsed && isSuccess" class="space-y-2">
        <div v-for="(result, idx) in successResults" :key="idx">
          <ToolWidgetPreview
            v-if="hasResultData(result)"
            :tool-execution="buildEnhancedExecution(result)"
            :readonly="readonly"
          />
          <div v-else-if="result.error" class="ml-4 text-xs text-red-500">
            {{ result.error }}
          </div>
        </div>
      </div>
    </Transition>

    <!-- Global errors -->
    <div v-if="hasErrors && !detailsCollapsed" class="ml-4 mt-2 text-xs text-red-500">
      <div v-for="(err, idx) in globalErrors" :key="idx">{{ err }}</div>
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
  created_widget_id?: string
  created_step_id?: string
  created_widget?: any
  created_step?: any
  created_visualizations?: any[]
}

interface Props {
  toolExecution: ToolExecution
  readonly?: boolean
}

const props = defineProps<Props>()

// Always collapsed by default
const detailsCollapsed = ref(true)

const status = computed<string>(() => props.toolExecution?.status || '')

const rj = computed<any>(() => props.toolExecution?.result_json || {})

const isSuccess = computed<boolean>(() => rj.value.success === true)

const results = computed<any[]>(() => rj.value.results || [])
const successResults = computed<any[]>(() => results.value.filter((r: any) => !r.error))
const globalErrors = computed<string[]>(() => rj.value.errors || [])
const hasErrors = computed<boolean>(() => globalErrors.value.length > 0)

const queryCount = computed<number>(() => {
  const args = props.toolExecution?.arguments_json || {}
  return (args.query_ids?.length || 0) + (args.visualization_ids?.length || 0)
})

const queryTitle = computed<string>(() => {
  // For single query, show its title
  if (successResults.value.length === 1) {
    return successResults.value[0].title || 'query'
  }
  const args = props.toolExecution?.arguments_json || {}
  return args.query_ids?.[0] || args.visualization_ids?.[0] || 'query'
})

const statusLabel = computed<string>(() => {
  const count = successResults.value.length
  if (hasErrors.value && count === 0) return 'Failed to read queries'
  if (count === 1) return `Read query "${successResults.value[0].title || 'Untitled'}"`
  return `Read ${count} queries`
})

function hasResultData(result: any): boolean {
  return !!(result.data?.rows || result.data?.columns)
}

function buildEnhancedExecution(result: any): any {
  const te: any = props.toolExecution

  const syntheticStep = {
    id: result.step_id || `read-query-step-${Date.now()}`,
    title: result.title || 'Untitled',
    code: result.code || '',
    data: result.data || {},
    data_model: result.data_model || { type: 'table' },
    view: result.view || { type: result.data_model?.type || 'table' },
    status: 'success',
  }

  return {
    ...te,
    created_step: syntheticStep,
    result_json: {
      ...result,
      widget_title: result.title || 'Untitled',
    },
  }
}

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
