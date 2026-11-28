<template>
  <!-- A `run_in_sandbox` call. Built on the SSH shell block (RemoteShellTool.vue):
       one line that says what ran, details behind it. What the run saved to the
       report and anything the reader must know about the sandbox stay on the
       collapsed row. -->
  <div class="min-w-0 text-chat-2" data-testid="run-in-sandbox-tool" :data-outcome="outcome">
    <ToolCallHeader icon="heroicons-command-line" :running="outcome === 'running' || outcome === 'awaiting'"
      :error="isProblem" :expanded="expanded" @toggle="expanded = !expanded">
      <template #label>
        <template v-if="summary">
          <span data-testid="sandbox-run-language">{{ languageLabel }}</span>
          <span class="mx-1.5 text-ink-subtle" aria-hidden="true">·</span>
          <span dir="ltr" data-testid="sandbox-run-summary">{{ summary }}</span>
        </template>
        <!-- No command and no code to quote (or the viewer may not see code). -->
        <span v-else data-testid="sandbox-run-summary">{{ t('tools.sandbox.codeOf', { language: languageLabel }) }}</span>
      </template>
      <template #actions>
        <span class="flex min-w-0 flex-wrap items-center gap-1.5 text-chat-3 text-ink-muted" data-testid="sandbox-run-meta">
          <span v-if="outcome === 'running' || outcome === 'awaiting'" data-testid="sandbox-run-stage">{{ stageLabel }}</span>
          <span v-else-if="outcome !== 'succeeded'" :class="chipClass(true)" data-testid="sandbox-run-outcome">{{ outcomeLabel }}</span>
          <span v-if="durationLabel" class="nums">{{ durationLabel }}</span>
        </span>
      </template>
    </ToolCallHeader>
    <!-- What changed about the sandbox itself (recreated, about to be deleted…):
         under the row, so it is read without opening the details. -->
    <div v-if="chips.length" class="ms-5 flex flex-wrap items-center gap-1.5 text-chat-3" data-testid="sandbox-run-chips">
      <span v-for="chip in chips" :key="chip.kind" :class="chipClass(chip.warning)" :title="chip.text"
        :data-testid="`sandbox-chip-${chip.kind}`">{{ t(`tools.sandbox.chip.${chip.kind}`) }}</span>
    </div>

    <SandboxConfirmCard v-if="outcome === 'awaiting'" :confirmation="confirmation" :system-completion-id="systemCompletionId"
      :readonly="readonly" @answered="answered = true" />

    <!-- While the approval card is up it already shows what would run. -->
    <div v-if="expanded && outcome !== 'awaiting'" class="mt-2 space-y-2 rounded-lg border border-line bg-surface-subtle p-3 text-chat-2" data-testid="sandbox-run-details">
      <div class="text-ink-muted">
        <span data-testid="sandbox-run-status">{{ outcomeLabel }}</span>
        <template v-if="outcome === 'running' && stageLabel !== outcomeLabel"><span class="mx-1.5" aria-hidden="true">·</span><span>{{ stageLabel }}</span></template>
        <span v-if="durationLabel" class="nums ms-2">{{ durationLabel }}</span>
      </div>
      <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-ink-muted">
        <template v-if="environment">
          <dt>{{ t('tools.sandbox.environment') }}</dt><dd class="min-w-0 break-words" data-testid="sandbox-run-environment"><bdi>{{ environment }}</bdi></dd>
        </template>
        <template v-if="network">
          <dt>{{ t('tools.sandbox.network') }}</dt><dd class="min-w-0 break-words" data-testid="sandbox-run-network">{{ network }}</dd>
        </template>
        <template v-if="packages.length">
          <dt>{{ t('tools.sandbox.packages') }}</dt><dd class="min-w-0 break-words font-mono" dir="ltr">{{ packages.join(' ') }}</dd>
        </template>
        <template v-if="inputCount">
          <dt>{{ t('tools.sandbox.inputs') }}</dt><dd>{{ t('tools.sandbox.inputFiles', { count: inputCount }, { plural: inputCount }) }}</dd>
        </template>
        <template v-if="args.timeout_seconds">
          <dt>{{ t('tools.sandbox.timeLimit') }}</dt><dd class="nums">{{ duration(Number(args.timeout_seconds) * 1000) }}</dd>
        </template>
      </dl>

      <template v-if="codeHidden">
        <div class="text-ink-muted" data-testid="sandbox-run-code-hidden">{{ t('tools.sandbox.codeHidden') }}</div>
      </template>
      <template v-else>
        <div v-if="args.command">
          <div class="mb-1 text-ink-muted">{{ t('tools.sandbox.command') }}</div>
          <pre dir="ltr" class="m-0 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono text-ink" data-testid="sandbox-run-command">{{ args.command }}</pre>
        </div>
        <div v-if="args.code">
          <div class="mb-1 text-ink-muted">{{ t('tools.sandbox.code') }}</div>
          <pre dir="ltr" class="m-0 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono text-ink" data-testid="sandbox-run-code">{{ args.code }}</pre>
        </div>
        <div v-for="file in extraFiles" :key="file.path">
          <div class="mb-1 text-ink-muted"><bdi dir="ltr" class="font-mono">{{ file.path }}</bdi></div>
          <pre dir="ltr" class="m-0 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono text-ink">{{ file.content }}</pre>
        </div>
      </template>

      <div v-if="errorMessage" dir="auto" class="break-words text-start text-amber-600 dark:text-amber-500" data-testid="sandbox-run-error">{{ errorMessage }}</div>

      <template v-for="stream in STREAMS" :key="stream">
        <div v-if="output[stream]" :data-testid="`sandbox-run-${stream}`">
          <div class="mb-1 text-ink-muted">
            {{ t(`tools.sandbox.${stream}`) }}
            <span v-if="truncated[stream]" class="ms-2 text-chat-3" :data-testid="`sandbox-run-${stream}-truncated`">{{ t('tools.sandbox.truncated') }}</span>
          </div>
          <pre :ref="(el) => setStreamEl(stream, el)" dir="ltr"
            class="m-0 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono"
            :class="stream === 'stderr' ? 'text-amber-700 dark:text-amber-400' : 'text-ink'">{{ output[stream] }}</pre>
        </div>
      </template>
      <div v-if="outcome === 'running' && !output.stdout && !output.stderr" class="text-ink-muted" data-testid="sandbox-run-no-output">{{ t('tools.sandbox.noOutputYet') }}</div>
      <div v-else-if="isFinished && !output.stdout && !output.stderr" class="text-ink-muted" data-testid="sandbox-run-no-output">{{ t('tools.sandbox.noOutput') }}</div>

      <div v-if="result.exit_code != null" class="text-ink-muted" data-testid="sandbox-run-exit-code">{{ t('tools.sandbox.exitCode', { code: result.exit_code }) }}</div>
      <p v-if="outcome === 'timeout'" class="m-0 text-ink-muted" data-testid="sandbox-run-timeout-note">{{ t('tools.sandbox.timedOutNote') }}</p>
      <p v-else-if="outcome === 'cancelled'" class="m-0 text-ink-muted" data-testid="sandbox-run-cancelled-note">{{ t('tools.sandbox.cancelledNote') }}</p>

      <div v-if="skipped.length" data-testid="sandbox-run-skipped">
        <div class="mb-1 text-ink-muted">{{ t('tools.sandbox.skipped') }}</div>
        <ul class="m-0 list-none space-y-0.5 p-0">
          <li v-for="file in skipped" :key="file.name" class="flex flex-wrap items-baseline gap-x-2 text-ink-muted">
            <bdi dir="ltr" class="font-mono text-ink-2">{{ file.name }}</bdi>
            <span v-if="file.size != null" class="nums text-chat-3" dir="ltr">{{ formatBytes(file.size) }}</span>
            <span dir="auto">{{ file.reason }}</span>
          </li>
        </ul>
      </div>

      <ul v-if="shownNotes.length" class="m-0 list-none space-y-1 p-0 text-ink-muted" data-testid="sandbox-run-notes">
        <li v-for="note in shownNotes" :key="note.text" class="flex items-start gap-1.5">
          <Icon :name="note.warning ? 'heroicons-exclamation-triangle' : 'heroicons-information-circle'"
            class="mt-0.5 h-3.5 w-3.5 shrink-0" :class="{ 'text-amber-600 dark:text-amber-500': note.warning }" aria-hidden="true" />
          <!-- Server sentences are English whatever the response language. -->
          <span class="min-w-0 break-words text-start" dir="auto">{{ note.text }}</span>
        </li>
      </ul>
    </div>

    <!-- Files the run saved to the report: the result, so never behind the toggle. -->
    <div v-if="artifacts.length" class="mt-1.5 space-y-1.5" data-testid="sandbox-run-files">
      <ul class="m-0 flex list-none flex-wrap gap-1.5 p-0">
        <li v-for="file in artifacts" :key="file.file_id || file.name" class="min-w-0">
          <component :is="canOpen(file) ? 'button' : 'span'" :type="canOpen(file) ? 'button' : undefined"
            class="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line bg-surface px-2 py-1 text-start text-ink-2"
            :class="canOpen(file) ? 'transition-colors hover:border-line-strong hover:text-ink focus:outline-none focus-visible:ring-1 focus-visible:ring-blue-500' : ''"
            :title="canOpen(file) ? t(opensInPanel(file) ? 'tools.sandbox.openFile' : 'tools.sandbox.downloadFile', { name: file.name }) : undefined"
            :disabled="canOpen(file) ? downloading === file.file_id : undefined"
            data-testid="sandbox-run-file" :data-file-id="file.file_id" @click="canOpen(file) && openFile(file)">
            <Icon :name="fileIcon(file)" class="h-3.5 w-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
            <bdi dir="ltr" class="min-w-0 truncate">{{ file.name }}</bdi>
            <span class="nums shrink-0 text-chat-3 text-ink-muted" dir="ltr">{{ formatBytes(file.size) }}</span>
            <Icon v-if="canOpen(file) && !opensInPanel(file)" name="heroicons-arrow-down-tray" class="h-3 w-3 shrink-0 text-ink-muted" aria-hidden="true" />
          </component>
        </li>
      </ul>
      <div v-if="!readonly && images.length" class="flex flex-wrap gap-2" data-testid="sandbox-run-images">
        <AuthenticatedImage v-for="file in images" :key="file.file_id" :file-id="file.file_id" :alt="file.name"
          img-class="max-h-48 w-auto cursor-zoom-in rounded-lg border border-line bg-white object-contain"
          @click="openFile(file)" />
      </div>
      <div v-if="downloadError" class="text-amber-600 dark:text-amber-500">{{ t('tools.sandbox.downloadFailed') }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import SandboxConfirmCard from '~/components/tools/SandboxConfirmCard.vue'
import AuthenticatedImage from '~/components/AuthenticatedImage.vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import { formatBytes } from '~/utils/fileTree'
import {
  SANDBOX_LANGUAGES, SANDBOX_NETWORK_POLICIES, SANDBOX_RUN_STAGES, SANDBOX_WARNING_NOTES,
  sandboxCodeRedacted, sandboxFileKind, sandboxNoteKind, sandboxRunOutcome, sandboxRunSummary,
} from '~/utils/sandboxTool'

const STREAMS = ['stdout', 'stderr'] as const
type Stream = typeof STREAMS[number]

const props = withDefaults(defineProps<{
  toolExecution: Record<string, any>
  runStatus?: string
  systemCompletionId?: string
  /** The host page has a side panel a file can open in. */
  canExpand?: boolean
  /** The shared link: nobody is signed in, so files cannot be fetched and an
   *  approval cannot be answered. */
  readonly?: boolean
}>(), { runStatus: '', canExpand: false, readonly: false })
const emit = defineEmits<{ (e: 'openFilePreview', payload: any): void }>()

const { t, locale } = useResponseI18n()
const canViewCode = useCanViewCode()
const { duration } = useSandboxFormat(locale)

const args = computed<Record<string, any>>(() => props.toolExecution?.arguments_json || {})
const result = computed<Record<string, any>>(() => props.toolExecution?.result_json || {})
const confirmation = computed<Record<string, any>>(() => props.toolExecution?.confirmation || {})
const answered = ref(false)
const outcome = computed(() => sandboxRunOutcome(props.toolExecution, props.runStatus, answered.value))
const isFinished = computed(() => !['running', 'awaiting'].includes(outcome.value))
const isProblem = computed(() => ['failed', 'timeout', 'error'].includes(outcome.value))

// Open while it runs, so the output is seen arriving; a stored run starts closed.
const expanded = ref(outcome.value === 'running')

const languageLabel = computed(() => {
  const language = args.value.language || confirmation.value.language || 'python'
  return SANDBOX_LANGUAGES.includes(language) ? t(`tools.sandbox.lang.${language}`) : String(language)
})
// The server already blanks the code for a viewer without `view_code`; the
// permission check also covers a row cached before the role changed.
const codeHidden = computed(() => !canViewCode.value || sandboxCodeRedacted(args.value))
const summary = computed(() => (codeHidden.value ? '' : sandboxRunSummary(args.value)))
const extraFiles = computed<{ path: string; content: string }[]>(() =>
  (Array.isArray(args.value.files) ? args.value.files : []).filter((f: any) => f && typeof f.path === 'string'))
const packages = computed<string[]>(() => Array.isArray(args.value.packages) ? args.value.packages : [])
const inputCount = computed(() => (Array.isArray(args.value.input_file_ids) ? args.value.input_file_ids.length : 0))

const environment = computed(() => result.value.environment || confirmation.value.environment || '')
const network = computed(() => {
  const policy = result.value.network_policy || confirmation.value.network_policy
  return SANDBOX_NETWORK_POLICIES.includes(policy) ? t(`sandbox.network.${policy}`) : (policy || '')
})

const stageLabel = computed(() => {
  if (outcome.value === 'awaiting') return t('tools.sandbox.run.awaiting')
  const stage = props.toolExecution?.progress_stage
  return t(`tools.sandbox.stage.${SANDBOX_RUN_STAGES.includes(stage) ? stage : 'starting'}`)
})
const outcomeLabel = computed(() => t(`tools.sandbox.run.${outcome.value}`))
const durationLabel = computed(() => {
  const seconds = Number(result.value.duration_seconds)
  return isFinished.value && result.value.duration_seconds != null && Number.isFinite(seconds) ? duration(seconds * 1000) : ''
})
// A refusal's own sentence repeats the status line; anything else is said as sent.
const errorMessage = computed(() => (outcome.value === 'error' ? (result.value.error_message || props.toolExecution?.error_message || '') : ''))

// While the run works the output is what `tool.stdout` has delivered so far;
// afterwards it is the result's own (the last 20 KB of each stream).
const live = computed<Record<Stream, string>>(() => props.toolExecution?.live_output || { stdout: '', stderr: '' })
const output = computed<Record<Stream, string>>(() => ({
  stdout: isFinished.value && result.value.stdout != null ? String(result.value.stdout) : (live.value.stdout || ''),
  stderr: isFinished.value && result.value.stderr != null ? String(result.value.stderr) : (live.value.stderr || ''),
}))
const truncated = computed<Record<Stream, boolean>>(() => ({
  stdout: !!result.value.stdout_truncated, stderr: !!result.value.stderr_truncated,
}))

// Follow the output while it streams, as a terminal does.
const streamEls: Partial<Record<Stream, HTMLElement>> = {}
function setStreamEl(stream: Stream, el: any) {
  if (el) streamEls[stream] = el as HTMLElement
  else delete streamEls[stream]
}
watch(() => [live.value.stdout, live.value.stderr], () => {
  if (isFinished.value) return
  nextTick(() => { for (const el of Object.values(streamEls)) if (el) el.scrollTop = el.scrollHeight })
})

const artifacts = computed<any[]>(() => (Array.isArray(result.value.artifacts) ? result.value.artifacts : []))
const skipped = computed<any[]>(() => (Array.isArray(result.value.skipped_artifacts) ? result.value.skipped_artifacts : []))
const images = computed(() => artifacts.value.filter((f) => f.file_id && sandboxFileKind(f) === 'image'))

const notes = computed(() => (Array.isArray(result.value.notes) ? result.value.notes : [])
  .map((text: string) => {
    const kind = sandboxNoteKind(text)
    return { text, kind, warning: SANDBOX_WARNING_NOTES.includes(kind) }
  }))
// The status line and the skipped list already say these three.
const shownNotes = computed(() => notes.value.filter((n: any) => !['timeout', 'cancelled', 'skipped'].includes(n.kind)))
const chips = computed(() => notes.value.filter((n: any) => ['fresh', 'restored', 'deletion', 'background', 'created', 'resumed'].includes(n.kind)))

function chipClass(warning: boolean): string {
  return [
    'rounded px-1 py-0.5 text-2xs',
    warning ? 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300' : 'bg-gray-100 text-ink-muted dark:bg-gray-800',
  ].join(' ')
}

function fileIcon(file: any): string {
  const kind = sandboxFileKind(file)
  if (kind === 'image') return 'heroicons-photo'
  if (/\.(csv|tsv|xlsx?|parquet)$/i.test(file.name || '')) return 'heroicons-table-cells'
  return 'heroicons-document-text'
}
const canOpen = (file: any) => !props.readonly && !!file.file_id
// Pictures and PDFs open in the report's file panel (as a read_file card does);
// everything else, and every file where there is no panel, is downloaded.
const opensInPanel = (file: any) => props.canExpand && sandboxFileKind(file) !== 'file'

const downloading = ref('')
const downloadError = ref(false)
async function openFile(file: any) {
  if (!canOpen(file)) return
  if (opensInPanel(file)) {
    emit('openFilePreview', { fileId: file.file_id, kind: sandboxFileKind(file), name: file.name, imageFileIds: null, targetPage: 1, pagesTotal: null })
    return
  }
  downloading.value = file.file_id
  downloadError.value = false
  try {
    const { data, error } = await useMyFetch<any>(`/files/${file.file_id}/content`, { method: 'GET', responseType: 'blob' as any })
    if (error.value || !data.value) throw error.value || new Error('empty')
    const url = URL.createObjectURL(data.value as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = file.name || 'file'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch {
    downloadError.value = true
  } finally {
    downloading.value = ''
  }
}
</script>
