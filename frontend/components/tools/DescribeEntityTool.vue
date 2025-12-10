<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleDetails">
        <Icon :name="detailsCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1 text-gray-400" />
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 mr-1 text-gray-400" />
          Loading from catalog: "{{ entityTitle }}"…
        </span>
        <span v-else class="flex items-center" :class="hasErrors ? 'text-red-600' : 'text-gray-700'">
          <Icon v-if="hasErrors" name="heroicons-exclamation-triangle" class="w-3 h-3 mr-1 text-red-500" />
          <Icon v-else-if="isActionMode && isSuccess" name="heroicons-check" class="w-3 h-3 mr-1 text-green-500" />
          <Icon v-else name="heroicons-cube" class="w-3 h-3 mr-1 text-gray-400" />
          <span class="align-middle">{{ statusLabel }}</span>
        </span>
      </div>
    </Transition>

    <!-- Entity Details Section (collapsible) -->
    <Transition name="fade">
      <div v-if="!detailsCollapsed && entityInfo" class="ml-4 text-xs text-gray-600 space-y-2">
        <!-- Description -->
        <div v-if="entityInfo.description" class="text-gray-500 leading-relaxed">
          {{ entityInfo.description }}
        </div>

        <!-- Data Profile Section -->
        <div v-if="dataProfile" class="mt-2">
          <div 
            class="flex items-center text-gray-500 cursor-pointer hover:text-gray-700"
            @click.stop="toggleProfile"
          >
            <Icon :name="profileCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1" />
            <Icon name="heroicons-table-cells" class="w-3 h-3 mr-1" />
            <span>Data Profile</span>
            <span class="ml-2 text-gray-400">{{ dataProfile.row_count?.toLocaleString() || 0 }} rows, {{ dataProfile.column_count || 0 }} columns</span>
          </div>
          <Transition name="fade">
            <div v-if="!profileCollapsed" class="mt-2 ml-4">
              <!-- Columns table -->
              <table v-if="profileColumns.length" class="min-w-0 text-[11px]">
                <thead class="text-gray-400">
                  <tr>
                    <th class="text-left pr-4 font-normal">Column</th>
                    <th class="text-left pr-4 font-normal">Type</th>
                    <th class="text-left pr-4 font-normal">Unique</th>
                    <th class="text-left font-normal">Nulls</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(col, idx) in profileColumns.slice(0, 10)" :key="idx">
                    <td class="pr-4 text-gray-600">{{ col.name }}</td>
                    <td class="pr-4 text-gray-400">{{ col.dtype || '—' }}</td>
                    <td class="pr-4 text-gray-400">{{ col.unique_count ?? '—' }}</td>
                    <td class="text-gray-400">{{ col.null_count ?? '—' }}</td>
                  </tr>
                  <tr v-if="profileColumns.length > 10">
                    <td colspan="4" class="text-gray-400">… {{ profileColumns.length - 10 }} more</td>
                  </tr>
                </tbody>
              </table>
              <!-- Sample rows -->
              <div v-if="sampleRows.length" class="mt-2">
                <div class="text-gray-400 mb-1">Sample:</div>
                <pre class="bg-gray-50 rounded px-2 py-1 text-[10px] text-gray-600 overflow-x-auto max-h-24">{{ JSON.stringify(sampleRows, null, 2) }}</pre>
              </div>
            </div>
          </Transition>
        </div>

        <!-- Code Section -->
        <div v-if="entityCode" class="mt-2">
          <div 
            class="flex items-center text-gray-500 cursor-pointer hover:text-gray-700"
            @click.stop="toggleCode"
          >
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1" />
            <Icon name="heroicons-code-bracket" class="w-3 h-3 mr-1" />
            <span>Code</span>
          </div>
          <Transition name="fade">
            <div v-if="!codeCollapsed" class="mt-1 ml-4">
              <pre class="bg-gray-50 rounded px-3 py-2 text-[11px] text-gray-700 overflow-x-auto max-h-48 whitespace-pre-wrap">{{ entityCode }}</pre>
            </div>
          </Transition>
        </div>

        <!-- Execution info -->
        <div v-if="wasRerun" class="mt-2 flex items-center text-gray-400">
          <Icon name="heroicons-arrow-path" class="w-3 h-3 mr-1" />
          <span>Re-executed with fresh data</span>
        </div>

        <!-- Errors -->
        <div v-if="errors.length" class="mt-2 text-red-500">
          <div v-for="(err, idx) in errors" :key="idx">{{ err }}</div>
        </div>
      </div>
    </Transition>

    <!-- Widget Preview (only in action mode with step created) -->
    <div v-if="isActionMode && hasPreview" class="mt-2">
      <ToolWidgetPreview 
        :tool-execution="enhancedToolExecution" 
        @addWidget="onAddWidget" 
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
  created_widget_id?: string
  created_step_id?: string
  created_widget?: any
  created_step?: any
  created_visualizations?: any[]
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()
const emit = defineEmits(['addWidget', 'toggleSplitScreen', 'editQuery'])

// Collapsed state
const detailsCollapsed = ref(true)
const profileCollapsed = ref(true)
const codeCollapsed = ref(true)

const status = computed<string>(() => props.toolExecution?.status || '')

// Check if this is action mode (should_create=true)
// Derive from result_json since arguments_json may not be persisted after refresh
const isActionMode = computed<boolean>(() => {
  // First check arguments_json (available during SSE streaming)
  const args = (props.toolExecution as any)?.arguments_json
  if (args?.should_create === true) return true
  
  // Fallback: check result_json for action mode indicators (persisted data)
  const rj = props.toolExecution?.result_json || {}
  return !!(rj.step_id || rj.data_model || rj.view || rj.data)
})

// Check if code was re-executed
const wasRerun = computed<boolean>(() => {
  // Check arguments_json (available during SSE)
  const args = (props.toolExecution as any)?.arguments_json
  if (args?.should_rerun === true) return true
  
  // Fallback: check if execution_log exists in result (indicates code was run)
  const rj = props.toolExecution?.result_json || {}
  return !!rj.execution_log
})

// Entity info from result
const entityInfo = computed<any>(() => {
  const rj = props.toolExecution?.result_json || {}
  return {
    entity_id: rj.entity_id,
    entity_type: rj.entity_type,
    title: rj.title,
    description: rj.description,
  }
})

const entityTitle = computed<string>(() => {
  return entityInfo.value?.title || props.toolExecution?.arguments_json?.name_or_id || 'entity'
})

const entityCode = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.code || ''
})

const dataProfile = computed<any>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.data_profile || null
})

const profileColumns = computed<any[]>(() => {
  return dataProfile.value?.columns || []
})

const sampleRows = computed<any[]>(() => {
  return dataProfile.value?.head_rows || []
})

const errors = computed<string[]>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.errors || []
})

const hasErrors = computed<boolean>(() => errors.value.length > 0)

const isSuccess = computed<boolean>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.success === true
})

const statusLabel = computed<string>(() => {
  const title = entityTitle.value
  if (hasErrors.value) {
    return `Failed to load entity "${title}"`
  }
  return isActionMode.value ? `Created from entity "${title}"` : `Described entity "${title}"`
})

// Enhanced tool execution with created_step constructed from result_json if not present
// This is needed because during SSE streaming, we only get IDs, not full objects
const enhancedToolExecution = computed(() => {
  const te: any = props.toolExecution
  if (!te) return te
  
  // If created_step already exists with data, use as-is
  if (te.created_step?.data) return te
  
  // Otherwise construct from result_json
  const rj = te.result_json || {}
  const hasStepData = rj.data?.rows || rj.data?.columns
  
  if (!hasStepData && !rj.data_model && !rj.view) return te
  
  // Build a synthetic step object from result_json
  const syntheticStep = {
    id: te.created_step_id || rj.step_id || `entity-step-${Date.now()}`,
    title: rj.title || entityTitle.value,
    code: rj.code || '',
    data: rj.data || {},
    data_model: rj.data_model || { type: 'table' },
    view: rj.view || { type: rj.data_model?.type || 'table' },
    status: 'success',
  }
  
  return {
    ...te,
    created_step: te.created_step || syntheticStep,
    result_json: {
      ...rj,
      widget_title: rj.title || entityTitle.value,
    }
  }
})

// Check if we have a widget/step to preview
const hasPreview = computed<boolean>(() => {
  const te: any = props.toolExecution
  const hasStep = !!(te?.created_step || te?.created_step_id)
  const hasViz = Array.isArray(te?.created_visualizations) && te.created_visualizations.length > 0
  const hasData = !!(te?.result_json?.data?.rows)
  return hasStep || hasViz || hasData
})

// Toggle functions
function toggleDetails() {
  detailsCollapsed.value = !detailsCollapsed.value
}

function toggleProfile() {
  profileCollapsed.value = !profileCollapsed.value
}

function toggleCode() {
  codeCollapsed.value = !codeCollapsed.value
}

function onAddWidget(payload: { widget?: any, step?: any }) {
  emit('addWidget', payload)
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
