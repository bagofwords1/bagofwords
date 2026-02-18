<template>
  <div class="mb-2">
    <!-- Main Header -->
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-document-text" class="w-3 h-3 mr-1.5 text-blue-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />

      <span v-if="status === 'running'" class="tool-shimmer">Reading artifact...</span>
      <span v-else-if="status === 'success'" class="text-gray-700">{{ successLabel }}</span>
      <span v-else-if="status === 'error'" class="text-gray-700">Failed to read artifact</span>
      <span v-else class="text-gray-700">Read Artifact</span>

      <!-- Mode Badge -->
      <span
        v-if="artifactMode && status === 'success'"
        :class="[
          'ml-2 px-1.5 py-0.5 rounded text-[10px] font-medium',
          artifactMode === 'slides'
            ? 'bg-purple-100 text-purple-700'
            : 'bg-blue-100 text-blue-700'
        ]"
      >
        {{ artifactMode === 'slides' ? 'Slides' : 'Dashboard' }}
      </span>

      <!-- Version Badge -->
      <span v-if="artifactVersion && status === 'success'" class="ml-1 text-[10px] text-gray-400">
        v{{ artifactVersion }}
      </span>
    </div>

    <!-- Error message -->
    <div v-if="status === 'error' && errorMessage" class="mt-1 ml-4 text-xs text-gray-500">
      {{ errorMessage }}
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed && status === 'success'" class="mt-2 ml-4 space-y-2">
        <!-- Artifact Info Card -->
        <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-md border border-gray-200 max-w-xs">
          <div
            :class="[
              'w-8 h-8 rounded flex-shrink-0 flex items-center justify-center',
              artifactMode === 'slides' ? 'bg-slate-800' : 'bg-blue-50'
            ]"
          >
            <Icon
              :name="artifactMode === 'slides' ? 'heroicons:presentation-chart-bar' : 'heroicons:chart-bar-square'"
              :class="[
                'w-4 h-4',
                artifactMode === 'slides' ? 'text-slate-400' : 'text-blue-500'
              ]"
            />
          </div>
          <div class="flex-1 min-w-0">
            <div class="text-xs font-medium text-gray-700 truncate">{{ artifactTitle || 'Untitled' }}</div>
            <div class="text-[10px] text-gray-400">
              {{ codeStats }}
            </div>
          </div>
        </div>

        <!-- Code Toggle -->
        <div class="mt-2">
          <div
            class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
            @click.stop="toggleCode"
          >
            <Icon :name="isCodeExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1 text-gray-400" />
            <Icon name="heroicons-code-bracket" class="w-3 h-3 mr-1 text-gray-400" />
            <span>{{ isCodeExpanded ? 'Hide code' : 'Show code' }}</span>
            <span class="ml-1 text-gray-400">({{ codeLines }} lines)</span>
          </div>

          <!-- Code Preview -->
          <Transition name="fade">
            <div v-if="isCodeExpanded" class="mt-2">
              <pre class="text-[11px] bg-gray-50 border border-gray-200 rounded-md p-2 overflow-x-auto max-h-80 overflow-y-auto"><code class="text-gray-700">{{ artifactCode }}</code></pre>
            </div>
          </Transition>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: {
      artifact_id?: string
    }
    result_json?: {
      artifact_id?: string
      title?: string
      mode?: string
      code?: string
      version?: number
      visualization_ids?: string[]
      artifact_preview?: {
        title?: string
        mode?: string
        version?: number
        code_stats?: {
          chars?: number
          lines?: number
        }
      }
      code_preview?: {
        code?: string
        language?: string
        collapsed_default?: boolean
      }
      error?: string
    }
    status: string
    result_summary?: string
    duration_ms?: number
  }
  readonly?: boolean
}

const props = defineProps<Props>()

const isCollapsed = ref(true) // Collapsed by default like DescribeTablesTool
const isCodeExpanded = ref(false) // Code also collapsed by default

// Basic computed values
const status = computed(() => props.toolExecution.status)

// Artifact info from result_json or artifact_preview
const artifactPreview = computed(() => props.toolExecution.result_json?.artifact_preview || {})
const codePreview = computed(() => props.toolExecution.result_json?.code_preview || {})

const artifactTitle = computed(() =>
  artifactPreview.value?.title ||
  props.toolExecution.result_json?.title ||
  ''
)

const artifactMode = computed(() =>
  artifactPreview.value?.mode ||
  props.toolExecution.result_json?.mode ||
  'page'
)

const artifactVersion = computed(() =>
  artifactPreview.value?.version ||
  props.toolExecution.result_json?.version ||
  null
)

const artifactCode = computed(() =>
  codePreview.value?.code ||
  props.toolExecution.result_json?.code ||
  ''
)

const codeLines = computed(() => {
  const stats = artifactPreview.value?.code_stats
  if (stats?.lines) return stats.lines
  const code = artifactCode.value
  return code ? code.split('\n').length : 0
})

const codeChars = computed(() => {
  const stats = artifactPreview.value?.code_stats
  if (stats?.chars) return stats.chars
  return artifactCode.value?.length || 0
})

const codeStats = computed(() => {
  return `${codeLines.value} lines, ${formatNumber(codeChars.value)} chars`
})

const successLabel = computed(() => {
  const title = artifactTitle.value
  return title ? `Read "${title}"` : 'Artifact loaded'
})

const errorMessage = computed(() => {
  const rj = props.toolExecution.result_json || {}
  return (rj as any).error || ''
})

// Helpers
function formatNumber(n: number): string {
  return new Intl.NumberFormat().format(n)
}

// Actions
function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

function toggleCode() {
  isCodeExpanded.value = !isCodeExpanded.value
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active { transition: opacity 0.25s ease; }
.fade-enter-from,
.fade-leave-to { opacity: 0; }

@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
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

pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
</style>
