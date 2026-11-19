<template>
  <div class="min-w-0">
    <!-- Main Header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ runningLabel }}</template>
      <template v-else-if="status === 'success'">{{ successLabel }}</template>
      <template v-else-if="status === 'stopped'">{{ runningLabel }}</template>
      <template v-else-if="status === 'error'">{{ errorLabel }}</template>
      <template v-else>{{ t('tools.createArtifact.create') }}</template>
      <template v-if="artifactMode">{{ artifactMode === 'slides' ? t('tools.createArtifact.slides') : t('tools.createArtifact.dashboard') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted"><span v-if="status === 'stopped'" class="ms-1.5 text-chat-3 text-ink-muted">· {{ t('reportView.stopped') }}</span><span
        v-if="notRenderChecked"
        data-testid="not-render-checked"
        class="ms-1.5 px-1.5 py-0.5 rounded text-chat-3 font-medium whitespace-nowrap shrink-0 bg-amber-100 dark:bg-amber-950 text-amber-700 dark:text-amber-300"
        :title="t('tools.createArtifact.notRenderCheckedDesc')"
      >{{ t('tools.createArtifact.notRenderChecked') }}</span><span v-if="formatDuration" class="ms-1.5 text-ink-muted">{{ formatDuration }}</span></div>

    <!-- Expanded content -->
    <template v-if="toolHeaderExpanded">
      <!-- Plan prompt -->
      <div v-if="artifactPrompt" class="mt-0.5 ms-[18px] text-chat-2 text-ink-muted max-w-lg">
        <span>{{ t('tools.createArtifact.plan') }}</span>
        <span :class="{ 'line-clamp-1': !promptExpanded }">{{ artifactPrompt }}</span>
        <button
          v-if="artifactPrompt.length > 80"
          class="ms-1 text-blue-400 hover:text-blue-600 text-chat-2"
          @click="promptExpanded = !promptExpanded"
        >
          {{ promptExpanded ? t('tools.createArtifact.less') : t('tools.createArtifact.more') }}
        </button>
      </div>

      <!-- Resolved viz badges -->
      <div v-if="resolvedVisualizations.length > 0 && progressStage !== 'awaiting_confirmation'" class="mt-1 ms-[18px] flex flex-wrap gap-1">
        <span
          v-for="viz in resolvedVisualizations"
          :key="viz.id"
          class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-chat-3 font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400"
        >
          {{ viz.title }}
        </span>
      </div>

      <div v-if="notRenderChecked" class="mt-1 ms-[18px] text-chat-2 text-amber-700 dark:text-amber-500 max-w-lg">
        {{ t('tools.createArtifact.notRenderCheckedDesc') }}
      </div>

      <!-- Stopped/Error message -->

      <div v-if="status === 'error' && errorMessage" class="mt-1 ms-[18px] text-chat-2 text-gray-500 dark:text-gray-400">
        {{ errorMessage }}
      </div>

      <!-- Progress stages -->
      <div v-if="status === 'running'" class="mt-1 ms-[18px]">
        <!-- Slides mode -->
        <div v-if="artifactMode === 'slides' && slideProgress.length > 0" class="space-y-1">
          <div class="text-chat-3 text-ink-muted mb-1">{{ t('tools.createArtifact.slidesLabel') }}</div>
          <div class="flex flex-wrap gap-1">
            <div
              v-for="(slide, idx) in slideProgress"
              :key="idx"
              :class="[
                'w-6 h-6 rounded flex items-center justify-center text-chat-3 font-medium transition-all',
                slide.status === 'done'
                  ? 'bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300'
                  : slide.status === 'generating'
                    ? 'bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 animate-pulse'
                    : 'bg-gray-100 dark:bg-gray-800 text-ink-muted'
              ]"
            >
              {{ Number(idx) + 1 }}
            </div>
          </div>
        </div>
        <!-- Page mode progress -->
        <div v-else class="text-chat-2 text-ink-muted">
          <div v-if="progressStage === 'generating' || progressStage === 'generating_code'">
            <span>{{ t('tools.createArtifact.generatingCode') }}</span>
            <span v-if="progressChars" class="ms-1 text-ink-muted">{{ t('tools.createArtifact.charsCount', { n: progressChars }) }}</span>
          </div>
          <div v-else-if="progressStage === 'validating'" class="flex items-center gap-1.5">
            <span>{{ t('tools.createArtifact.validating') }}</span>
            <span v-if="validationAttempt" class="text-ink-muted">{{ t('tools.createArtifact.attempt', { n: validationAttempt, max: validationMaxAttempts }) }}</span>
          </div>
          <div v-else-if="progressStage === 'fixing_errors'" class="space-y-1">
            <div class="flex items-center gap-1.5 text-amber-500">
              <Icon name="heroicons:wrench-screwdriver" class="w-3 h-3" />
              <span>{{ t('tools.createArtifact.fixingErrors') }}</span>
              <span class="text-ink-muted">{{ t('tools.createArtifact.attempt', { n: validationAttempt, max: validationMaxAttempts }) }}</span>
            </div>
            <div v-if="fixingErrors.length > 0" class="ms-4 text-chat-2 text-ink-muted space-y-0.5">
              <div v-for="(err, idx) in fixingErrors.slice(0, 2)" :key="idx" class="truncate max-w-xs">{{ err }}</div>
            </div>
          </div>
          <div v-else-if="progressStage === 'saving_artifact'"><span>{{ t('tools.createArtifact.savingArtifact') }}</span></div>
          <div v-else><span>{{ t('tools.createArtifact.processing') }}</span></div>
        </div>
      </div>

      <!-- Confirmation card -->
      <div v-if="confirmation && progressStage === 'awaiting_confirmation'" class="mt-2 ms-[18px] rounded-md border border-amber-200 bg-amber-50 dark:bg-amber-950 p-2.5 space-y-2">
        <div class="text-chat-2 font-medium text-gray-700 dark:text-gray-300">{{ t('tools.createArtifact.confirm') }}</div>
        <div v-if="confirmation.visualizations?.length" class="flex flex-wrap gap-1">
          <span
            v-for="viz in confirmation.visualizations"
            :key="viz.id"
            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-chat-3 font-medium bg-white dark:bg-gray-900 border border-amber-200 text-gray-600 dark:text-gray-400"
          >
            {{ viz.title }}
          </span>
        </div>
        <input
          v-model="editableTitle"
          class="w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-900 focus:outline-none focus:border-blue-400"
          :placeholder="t('tools.createArtifact.titlePlaceholder')"
        />
        <div class="flex items-center gap-2">
          <button class="px-2.5 py-1 text-chat-2 font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors" @click="approveConfirmation">{{ t('tools.createArtifact.approve') }}</button>
          <button class="px-2.5 py-1 text-chat-2 font-medium text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors" @click="rejectConfirmation">{{ t('tools.createArtifact.cancel') }}</button>
          <span class="text-chat-3 text-ink-muted">{{ t('tools.createArtifact.autoApprovingIn', { n: confirmationCountdown }) }}</span>
        </div>
      </div>
      <!-- Preview Card -->
      <div
        v-if="(status === 'success' && createdArtifact) || status === 'running'"
        class="mt-1.5 ms-[18px] cursor-pointer group"
        @click="openArtifact"
      >
      <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-md border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800 transition-all max-w-xs">
        <!-- Thumbnail -->
        <div
          :class="[
            'w-10 h-10 rounded flex-shrink-0 overflow-hidden flex items-center justify-center',
            artifactMode === 'slides' ? 'bg-slate-800' : 'bg-blue-50 dark:bg-blue-950'
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
          <div class="text-chat-2 font-medium text-gray-700 dark:text-gray-300 truncate">{{ artifactTitle || t('tools.createArtifact.untitled') }}</div>
          <div class="text-chat-3 text-ink-muted">
            <span v-if="status === 'running'">{{ t('tools.createArtifact.generating') }}</span>
            <span v-else>{{ artifactMode === 'slides' ? t('tools.createArtifact.presentation') : t('tools.createArtifact.dashboard') }}</span>
          </div>
          <button
            v-if="createdArtifact && toolHeaderExpanded"
            @click.stop="copyArtifactId"
            class="flex items-center gap-0.5 text-chat-3 text-ink-muted hover:text-gray-600 dark:hover:text-gray-400 font-mono mt-0.5"
            :title="t('tools.createArtifact.copyIdTooltip')"
          >
            <Icon name="heroicons:clipboard-document" class="w-3 h-3" />
            {{ createdArtifact.slice(0, 8) }}
          </button>
        </div>
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3.5 h-3.5 text-ink-muted group-hover:text-ink-muted flex-shrink-0" />
      </div>
    </div>
    </template>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref, watch, onUnmounted } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import Spinner from '~/components/Spinner.vue'
import { toolErrorText } from '~/utils/toolError'

const { t } = useResponseI18n()

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

const { markConfirmationAnswered } = useToolConfirmations()

async function approveConfirmation() {
  if (!confirmation.value?.confirmation_id) return
  if (countdownInterval) { clearInterval(countdownInterval); countdownInterval = null }
  try {
    await $fetch(`/api/artifacts/confirm/${confirmation.value.confirmation_id}`, {
      method: 'POST',
      body: { approved: true, title: editableTitle.value || null },
    })
    markConfirmationAnswered(confirmation.value?.confirmation_id)
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
    markConfirmationAnswered(confirmation.value?.confirmation_id)
  } catch {}
}

// Labels
const runningLabel = computed(() => artifactMode.value === 'slides' ? t('tools.createArtifact.creatingPresentation') : t('tools.createArtifact.creatingDashboard'))
const successLabel = computed(() => artifactMode.value === 'slides' ? t('tools.createArtifact.presentationCreated') : t('tools.createArtifact.dashboardCreated'))
const errorLabel = computed(() => artifactMode.value === 'slides' ? t('tools.createArtifact.failedPresentation') : t('tools.createArtifact.failedDashboard'))
const errorMessage = computed(() => toolErrorText(props.toolExecution.result_json?.error))

// The page was saved without being rendered on the server, so a tick alone
// would promise more than was checked.
const notRenderChecked = computed(() =>
  status.value === 'success' && (props.toolExecution.result_json as any)?.render_checked === false
)

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
    toast.add({ title: t('tools.createArtifact.copied'), description: t('tools.createArtifact.copiedDesc'), color: 'green' })
  } catch {
    toast.add({ title: t('tools.createArtifact.copyFailed'), color: 'red' })
  }
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active { transition: opacity 0.3s ease; }
.fade-enter-from,
.fade-leave-to { opacity: 0; }

</style>
