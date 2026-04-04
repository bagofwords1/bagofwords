<template>
  <div class="mb-2">
    <!-- Main Header -->
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />

      <span v-if="status === 'running'" class="tool-shimmer">{{ runningLabel }}</span>
      <span v-else-if="status === 'success'" class="text-gray-700">{{ successLabel }}</span>
      <span v-else-if="status === 'stopped'" class="text-gray-700 italic">{{ runningLabel }}</span>
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

    <!-- Expanded content -->
    <template v-if="!isCollapsed">
      <!-- Plan prompt -->
      <div v-if="artifactPrompt" class="mt-0.5 ml-[18px] text-xs text-gray-400 max-w-lg">
        <span>Plan: </span>
        <span :class="{ 'line-clamp-1': !promptExpanded }">{{ artifactPrompt }}</span>
        <button
          v-if="artifactPrompt.length > 80"
          class="ml-1 text-blue-400 hover:text-blue-600 text-[11px]"
          @click="promptExpanded = !promptExpanded"
        >
          {{ promptExpanded ? 'less' : 'more' }}
        </button>
      </div>

      <!-- Resolved viz badges -->
      <div v-if="resolvedVisualizations.length > 0 && progressStage !== 'awaiting_confirmation'" class="mt-1 ml-[18px] flex flex-wrap gap-1">
        <span
          v-for="viz in resolvedVisualizations"
          :key="viz.id"
          class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-600"
        >
          {{ viz.title }}
        </span>
      </div>

      <!-- Stopped/Error message -->
      <div v-if="status === 'stopped'" class="mt-1 ml-[18px] text-xs text-gray-400 italic">Generation stopped</div>
      <div v-else-if="status === 'error' && errorMessage" class="mt-1 ml-[18px] text-xs text-gray-500">
        {{ errorMessage }}
      </div>

      <!-- Progress stages -->
      <div v-if="status === 'running'" class="mt-1 ml-[18px]">
        <!-- Slides mode -->
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
              {{ Number(idx) + 1 }}
            </div>
          </div>
        </div>
        <!-- Page mode progress -->
        <div v-else class="text-xs text-gray-400">
          <div v-if="progressStage === 'generating' || progressStage === 'generating_code'">
            <span>Generating code...</span>
            <span v-if="progressChars" class="ml-1 text-gray-300">({{ progressChars }} chars)</span>
          </div>
          <div v-else-if="progressStage === 'validating'" class="flex items-center gap-1.5">
            <span>Validating</span>
            <span v-if="validationAttempt" class="text-gray-300">(attempt {{ validationAttempt }}/{{ validationMaxAttempts }})</span>
          </div>
          <div v-else-if="progressStage === 'fixing_errors'" class="space-y-1">
            <div class="flex items-center gap-1.5 text-amber-500">
              <Icon name="heroicons:wrench-screwdriver" class="w-3 h-3" />
              <span>Fixing errors</span>
              <span class="text-gray-300">(attempt {{ validationAttempt }}/{{ validationMaxAttempts }})</span>
            </div>
            <div v-if="fixingErrors.length > 0" class="ml-4 text-[10px] text-gray-400 space-y-0.5">
              <div v-for="(err, idx) in fixingErrors.slice(0, 2)" :key="idx" class="truncate max-w-xs">{{ err }}</div>
            </div>
          </div>
          <div v-else-if="progressStage === 'saving_artifact'"><span>Saving artifact...</span></div>
          <div v-else><span>Processing...</span></div>
        </div>
      </div>

      <!-- Confirmation card -->
      <div v-if="confirmation && progressStage === 'awaiting_confirmation'" class="mt-2 ml-[18px] rounded-md border border-amber-200 bg-amber-50 p-2.5 space-y-2">
        <div class="text-xs font-medium text-gray-700">Confirm artifact creation</div>
        <div v-if="confirmation.visualizations?.length" class="flex flex-wrap gap-1">
          <span
            v-for="viz in confirmation.visualizations"
            :key="viz.id"
            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-white border border-amber-200 text-gray-600"
          >
            {{ viz.title }}
          </span>
        </div>
        <input
          v-model="editableTitle"
          class="w-full px-2 py-1 text-xs border border-gray-200 rounded bg-white focus:outline-none focus:border-blue-400"
          placeholder="Artifact title"
        />
        <div class="flex items-center gap-2">
          <button class="px-2.5 py-1 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors" @click="approveConfirmation">Approve</button>
          <button class="px-2.5 py-1 text-xs font-medium text-gray-600 bg-white border border-gray-200 rounded hover:bg-gray-50 transition-colors" @click="rejectConfirmation">Cancel</button>
          <span class="text-[10px] text-gray-400">Auto-approving in {{ confirmationCountdown }}s</span>
        </div>
      </div>
      <!-- Preview Card -->
      <div
        v-if="(status === 'success' && createdArtifact) || status === 'running'"
        class="mt-1.5 ml-[18px] cursor-pointer group"
        @click="openArtifact"
      >
      <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-md border border-gray-200 hover:border-gray-300 hover:bg-gray-50 transition-all max-w-xs">
        <!-- Thumbnail -->
        <div
          :class="[
            'w-10 h-10 rounded flex-shrink-0 overflow-hidden flex items-center justify-center',
            artifactMode === 'slides' ? 'bg-slate-800' : 'bg-blue-50'
          ]"
        >
          <img
            v-if="thumbnailUrl && !thumbnailError"
            :src="thumbnailUrl"
            :alt="artifactTitle"
            class="w-full h-full object-cover"
            @error="thumbnailError = true"
          />
          <template v-else>
            <Spinner v-if="status === 'running'" class="w-4 h-4 text-blue-500" />
            <Icon
              v-else
              :name="artifactMode === 'slides' ? 'heroicons:presentation-chart-bar' : 'heroicons:chart-bar-square'"
              :class="['w-4 h-4', artifactMode === 'slides' ? 'text-slate-400' : 'text-blue-500']"
            />
          </template>
        </div>
        <!-- Title and info -->
        <div class="flex-1 min-w-0">
          <div class="text-xs font-medium text-gray-700 truncate">{{ artifactTitle || 'Untitled' }}</div>
          <div class="text-[10px] text-gray-400">
            <span v-if="status === 'running'">Generating...</span>
            <span v-else>{{ artifactMode === 'slides' ? 'Presentation' : 'Dashboard' }}</span>
          </div>
          <button
            v-if="createdArtifact && !isCollapsed"
            @click.stop="copyArtifactId"
            class="flex items-center gap-0.5 text-[10px] text-gray-400 hover:text-gray-600 font-mono mt-0.5"
            title="Click to copy ID"
          >
            <Icon name="heroicons:clipboard-document" class="w-3 h-3" />
            {{ createdArtifact.slice(0, 8) }}
          </button>
        </div>
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3.5 h-3.5 text-gray-400 group-hover:text-gray-600 flex-shrink-0" />
      </div>
    </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: {
      title?: string
      mode?: string
      prompt?: string
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
const toast = useToast()

const isCollapsed = ref(false)
const promptExpanded = ref(false)
const thumbnailError = ref(false)

// Basic computed values
const status = computed(() => props.toolExecution.status)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')
const progressPayload = computed(() => (props.toolExecution as any).progress_payload || {})
const progressChars = computed(() => progressPayload.value?.chars)

// Validation progress
const validationAttempt = computed(() => progressPayload.value?.attempt || 0)
const validationMaxAttempts = computed(() => progressPayload.value?.max_attempts || 3)
const fixingErrors = computed(() => progressPayload.value?.errors || [])

// Artifact info
const artifactPrompt = computed(() => props.toolExecution.arguments_json?.prompt || '')
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
const pendingArtifactId = computed(() => (props.toolExecution as any).pending_artifact_id)

const config = useRuntimeConfig()
const thumbnailUrl = computed(() => {
  const id = createdArtifact.value
  if (!id) return null
  return `${config.public.baseURL}/thumbnails/${id}.png`
})

// Slide progress tracking
const slideProgress = computed(() => (props.toolExecution as any).progress_slides || [])

// Confirmation state
const confirmation = computed(() => (props.toolExecution as any).confirmation || null)
const resolvedVisualizations = computed(() => (props.toolExecution as any).progress_visualizations || [])
const editableTitle = ref('')
const confirmationCountdown = ref(5)
let countdownInterval: ReturnType<typeof setInterval> | null = null

watch(confirmation, (val) => {
  if (val) {
    editableTitle.value = val.title || ''
    confirmationCountdown.value = 5
    if (countdownInterval) clearInterval(countdownInterval)
    countdownInterval = setInterval(() => {
      confirmationCountdown.value--
      if (confirmationCountdown.value <= 0) {
        if (countdownInterval) clearInterval(countdownInterval)
        countdownInterval = null
      }
    }, 1000)
  }
}, { immediate: true })

onUnmounted(() => {
  if (countdownInterval) clearInterval(countdownInterval)
})

async function approveConfirmation() {
  if (!confirmation.value?.confirmation_id) return
  if (countdownInterval) { clearInterval(countdownInterval); countdownInterval = null }
  try {
    await $fetch(`/api/artifacts/confirm/${confirmation.value.confirmation_id}`, {
      method: 'POST',
      body: { approved: true, title: editableTitle.value || null },
    })
  } catch {}
}

async function rejectConfirmation() {
  if (!confirmation.value?.confirmation_id) return
  if (countdownInterval) { clearInterval(countdownInterval); countdownInterval = null }
  try {
    await $fetch(`/api/artifacts/confirm/${confirmation.value.confirmation_id}`, {
      method: 'POST',
      body: { approved: false },
    })
  } catch {}
}

// Labels
const runningLabel = computed(() => artifactMode.value === 'slides' ? 'Creating Presentation' : 'Creating Dashboard')
const successLabel = computed(() => artifactMode.value === 'slides' ? 'Presentation Created' : 'Dashboard Created')
const errorLabel = computed(() => artifactMode.value === 'slides' ? 'Failed to Create Presentation' : 'Failed to Create Dashboard')
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
  if (createdArtifact.value) {
    emit('openArtifact', { artifactId: createdArtifact.value })
  } else if (status.value === 'running' && pendingArtifactId.value) {
    emit('openArtifact', { artifactId: pendingArtifactId.value })
  } else if (status.value === 'running') {
    emit('openArtifact', { loading: true })
  }
}

async function copyArtifactId() {
  if (!createdArtifact.value) return
  try {
    await navigator.clipboard.writeText(createdArtifact.value)
    toast.add({ title: 'Copied', description: 'Artifact ID copied to clipboard', color: 'green' })
  } catch {
    toast.add({ title: 'Failed to copy', color: 'red' })
  }
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
