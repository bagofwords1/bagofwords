<template>
  <div class="mt-1">
    <!-- Status header (always collapsible, collapsed by default) -->
    <Transition name="fade" appear>
      <div
        class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="detailsCollapsed = !detailsCollapsed"
      >
        <Icon
          :name="detailsCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'"
          class="w-3 h-3 me-1 text-gray-400 rtl-flip"
        />
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-play-circle" class="w-3 h-3 me-1 text-gray-400" />
          {{ runningLabel }}
        </span>
        <span
          v-else
          class="flex items-center"
          :class="hasError ? 'text-red-600' : 'text-gray-700 dark:text-gray-300'"
        >
          <Icon v-if="hasError" name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-red-500" />
          <Icon v-else name="heroicons-play-circle" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">{{ statusLabel }}</span>
          <UTooltip v-if="isCached" :text="$t('tools.runQuery.cachedHint')">
            <Icon name="heroicons-bolt" class="w-2.5 h-2.5 ms-1 text-amber-500 flex-shrink-0" />
          </UTooltip>
        </span>
      </div>
    </Transition>

    <!-- Result preview. Rendered from THIS run's rows — never re-hydrated from
         the stored step, whose snapshot answers the query's default values. -->
    <Transition name="fade">
      <div v-if="!detailsCollapsed && isSuccess && hasData">
        <ToolWidgetPreview
          :tool-execution="enhancedExecution"
          :readonly="readonly"
          :can-expand="canExpand"
          @editQuery="$emit('editQuery', $event)"
          @openDataPanel="$emit('openDataPanel', $event)"
        />
      </div>
    </Transition>

    <!-- Missing parameters: what the agent still needs from the user -->
    <div v-if="!detailsCollapsed && missingParams.length" class="ms-4 mt-1 text-xs text-gray-500 dark:text-gray-400">
      {{ $t('tools.runQuery.needsValues') }}
      <span class="font-medium">{{ missingParams.map((p: any) => p.name).join(', ') }}</span>
    </div>

    <!-- Error -->
    <div v-if="!detailsCollapsed && hasError" class="ms-4 mt-1 text-xs text-red-500">
      {{ errorMessage }}
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
}

interface Props {
  toolExecution: ToolExecution
  readonly?: boolean
  canExpand?: boolean
}

const props = defineProps<Props>()
defineEmits(['editQuery', 'openDataPanel'])

const { t } = useI18n()

// Always collapsed by default — the header line carries the values, the data
// is one click away.
const detailsCollapsed = ref(true)

const status = computed<string>(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})

const isSuccess = computed<boolean>(() => rj.value.success === true)
const isCached = computed<boolean>(() => rj.value.cached === true)
const errorMessage = computed<string>(() => rj.value.error || '')
const hasError = computed<boolean>(() => !!errorMessage.value)
const missingParams = computed<any[]>(() => rj.value.missing_params || [])

const title = computed<string>(() => rj.value.title || args.value.query_id || '')

/** The values this run actually executed with — server-resolved, so it
 *  includes defaults and identity bindings, not just what the model sent. */
const appliedParams = computed<Record<string, any>>(
  () => rj.value.applied_params || args.value.params || {}
)

function formatValue(v: any): string {
  if (v === null || v === undefined) return 'all'
  if (Array.isArray(v)) return v.length > 2 ? `${v.slice(0, 2).join(', ')} +${v.length - 2}` : v.join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const paramsLabel = computed<string>(() => {
  const entries = Object.entries(appliedParams.value)
  if (!entries.length) return ''
  const shown = entries.slice(0, 3).map(([k, v]) => `${k} = ${formatValue(v)}`)
  const more = entries.length - shown.length
  return shown.join(', ') + (more > 0 ? ` +${more}` : '')
})

const runningLabel = computed<string>(() =>
  paramsLabel.value
    ? t('tools.runQuery.runningWith', { title: title.value, params: paramsLabel.value })
    : t('tools.runQuery.running', { title: title.value })
)

const statusLabel = computed<string>(() => {
  if (hasError.value || missingParams.value.length) {
    return missingParams.value.length
      ? t('tools.runQuery.needsInput', { title: title.value })
      : t('tools.runQuery.failed', { title: title.value })
  }
  return paramsLabel.value
    ? t('tools.runQuery.ranWith', { title: title.value, params: paramsLabel.value })
    : t('tools.runQuery.ran', { title: title.value })
})

const hasData = computed<boolean>(() => {
  const d = rj.value.data || {}
  const p = rj.value.data_preview || {}
  return !!(d.rows || d.columns || p.rows || p.columns)
})

/**
 * ToolWidgetPreview renders from a step, but this run deliberately created
 * none. Hand it a synthetic one carrying THIS run's rows.
 *
 * The id is intentionally NOT the executed step's id: ReadQueryTool hydrates a
 * real step_id from /api/steps/{id}, which would replace these rows with the
 * stored snapshot — i.e. the default values — while the header still claimed
 * the requested ones.
 */
const enhancedExecution = computed<any>(() => {
  const te: any = props.toolExecution
  const preview = rj.value.data_preview || {}
  const previewData = {
    rows: preview.rows || [],
    columns: preview.columns || [],
    info: { total_rows: preview.row_count ?? (preview.rows || []).length },
    truncated: !!preview.truncated,
    total_rows: preview.row_count,
  }

  const syntheticStep = {
    id: `run-query-${te?.id || 'result'}`,
    title: title.value || 'Untitled',
    data: rj.value.data || previewData,
    data_model: rj.value.data_model || { type: 'table' },
    view: rj.value.view || { type: rj.value.data_model?.type || 'table' },
    status: 'success',
  }

  return {
    ...te,
    created_step: syntheticStep,
    result_json: {
      ...rj.value,
      widget_title: title.value || 'Untitled',
    },
  }
})
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
