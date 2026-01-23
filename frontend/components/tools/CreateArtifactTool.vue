<template>
  <div class="mb-2">
    <!-- Main Header: Creating Artifact (always collapsible) -->
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />

      <span v-if="status === 'running'" class="tool-shimmer">{{ runningLabel }}</span>
      <span v-else-if="status === 'success'" class="text-gray-700">{{ successLabel }}</span>
      <span v-else-if="status === 'error'" class="text-gray-700">{{ errorLabel }}</span>
      <span v-else class="text-gray-700">Create Artifact</span>

      <!-- Mode Badge -->
      <span
        v-if="artifactMode"
        :class="[
          'ml-2 px-1.5 py-0.5 rounded text-[10px] font-medium',
          artifactMode === 'slides'
            ? 'bg-purple-100 text-purple-700'
            : 'bg-blue-100 text-blue-700'
        ]"
      >
        {{ artifactMode === 'slides' ? 'Slides' : 'Dashboard' }}
      </span>

      <span v-if="formatDuration" class="ml-1.5 text-gray-400">{{ formatDuration }}</span>
    </div>

    <!-- Error message below header -->
    <div v-if="status === 'error' && errorMessage" class="mt-1 ml-4 text-xs text-gray-500">
      {{ errorMessage }}
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed" class="mt-2 ml-4 space-y-2">
        <!-- Title display -->
        <div v-if="artifactTitle" class="text-xs text-gray-600">
          <span class="text-gray-400">Title:</span> {{ artifactTitle }}
        </div>

        <!-- Progress stages for slides mode -->
        <div v-if="artifactMode === 'slides' && slideProgress.length > 0" class="space-y-1">
          <div class="text-xs text-gray-400 mb-1">Slides:</div>
          <div class="flex flex-wrap gap-1">
            <div
              v-for="(slide, idx) in slideProgress"
              :key="idx"
              :class="[
                'w-6 h-6 rounded flex items-center justify-center text-[10px] font-medium transition-all',
                slide.status === 'done'
                  ? 'bg-green-100 text-green-700'
                  : slide.status === 'generating'
                    ? 'bg-blue-100 text-blue-700 animate-pulse'
                    : 'bg-gray-100 text-gray-400'
              ]"
            >
              {{ idx + 1 }}
            </div>
          </div>
        </div>

        <!-- Generation progress for page mode -->
        <div v-else-if="status === 'running'" class="text-xs text-gray-400">
          <span v-if="progressStage === 'generating'">Generating code...</span>
          <span v-else-if="progressStage === 'saving_artifact'">Saving artifact...</span>
          <span v-else>Processing...</span>
          <span v-if="progressChars" class="ml-1">({{ progressChars }} chars)</span>
        </div>

        <!-- Preview Card (on success) -->
        <div
          v-if="status === 'success' && createdArtifact"
          class="mt-1 cursor-pointer group"
          @click="openArtifact"
        >
          <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-md border border-gray-200 hover:border-gray-300 hover:bg-gray-50 transition-all max-w-xs">
            <!-- Small thumbnail -->
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
            <!-- Title and action -->
            <div class="flex-1 min-w-0">
              <div class="text-xs font-medium text-gray-700 truncate">{{ artifactTitle || 'Untitled' }}</div>
              <div class="text-[10px] text-gray-400">{{ artifactMode === 'slides' ? 'Presentation' : 'Dashboard' }}</div>
            </div>
            <Icon name="heroicons:arrow-top-right-on-square" class="w-3.5 h-3.5 text-gray-400 group-hover:text-gray-600 flex-shrink-0" />
          </div>
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
      title?: string
      mode?: string
      user_prompt?: string
    }
    result_json?: {
      artifact_id?: string
      title?: string
      mode?: string
      error?: string
    }
    status: string
    result_summary?: string
    duration_ms?: number
    progress_stage?: string
    progress_payload?: any
  }
  readonly?: boolean
}

const props = defineProps<Props>()
const emit = defineEmits(['openArtifact', 'toggleSplitScreen'])

const isCollapsed = ref(false)

// Basic computed values
const status = computed(() => props.toolExecution.status)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')
const progressPayload = computed(() => (props.toolExecution as any).progress_payload || {})
const progressChars = computed(() => progressPayload.value?.chars)

// Artifact info
const artifactTitle = computed(() =>
  props.toolExecution.result_json?.title ||
  props.toolExecution.arguments_json?.title ||
  ''
)
const artifactMode = computed(() =>
  props.toolExecution.result_json?.mode ||
  props.toolExecution.arguments_json?.mode ||
  'page'
)
const createdArtifact = computed(() => props.toolExecution.result_json?.artifact_id)

// Slide progress tracking
const slideProgress = computed(() => {
  const slides = (props.toolExecution as any).progress_slides || []
  return slides
})

// Labels
const runningLabel = computed(() => {
  if (artifactMode.value === 'slides') {
    return 'Creating Presentation'
  }
  return 'Creating Dashboard'
})

const successLabel = computed(() => {
  if (artifactMode.value === 'slides') {
    return 'Presentation Created'
  }
  return 'Dashboard Created'
})

const errorLabel = computed(() => {
  if (artifactMode.value === 'slides') {
    return 'Failed to Create Presentation'
  }
  return 'Failed to Create Dashboard'
})

const errorMessage = computed(() => props.toolExecution.result_json?.error || '')

const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})

// Actions
function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

function openArtifact() {
  if (!createdArtifact.value) return

  // Always open in artifact pane (for both slides and pages)
  emit('openArtifact', { artifactId: createdArtifact.value })
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active { transition: opacity 0.3s ease; }
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
</style>
