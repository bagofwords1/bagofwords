<template>
  <div class="mt-1 mb-6" dir="auto">
    <!-- Header: spinner while running, checkmark when done -->
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 mb-3">
      <Spinner v-if="status === 'running'" class="w-3 h-3 me-1.5 text-gray-400" />
      <Icon v-else name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
      <span v-if="status === 'running'" class="tool-shimmer">{{ $t('tools.clarify.clarifying') }}</span>
      <span v-else class="text-gray-500 dark:text-gray-400">{{ $t('tools.clarify.clarifying') }}</span>
    </div>

    <!-- Questions form (only when tool has finished and questions exist) -->
    <div v-if="questions.length && status !== 'running'" class="space-y-4 ms-4">

      <div v-for="(q, i) in questions" :key="i" class="space-y-1.5">
        <p class="text-sm font-medium text-gray-900 dark:text-white" dir="auto">{{ q.text }}</p>
        <p v-if="isMulti(q)" class="text-xs text-gray-400 dark:text-gray-500" dir="auto">{{ $t('tools.clarify.multiHint') }}</p>

        <!-- Lettered options -->
        <div v-if="q.options?.length" class="space-y-1" :role="isMulti(q) ? 'group' : 'radiogroup'" :aria-label="q.text">
          <button
            v-for="(opt, j) in choicesOf(q)"
            :key="j"
            type="button"
            :role="isMulti(q) ? 'checkbox' : 'radio'"
            :aria-checked="isSelected(i, opt)"
            :disabled="isLocked"
            @click="!isLocked && selectOption(i, opt)"
            :class="[
              'flex items-center gap-2.5 w-full text-start px-3 py-2 rounded-lg border transition-all duration-100',
              isSelected(i, opt)
                ? 'border-sky-200 dark:border-sky-800 bg-sky-50 dark:bg-sky-950/40'
                : isLocked
                  ? 'border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 opacity-40 cursor-default'
                  : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 hover:border-gray-300 dark:hover:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer',
            ]"
          >
            <span
              :class="[
                'w-5 h-5 rounded text-[10px] font-bold flex-shrink-0 flex items-center justify-center transition-colors duration-100',
                isSelected(i, opt) ? 'bg-sky-500 text-white' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400',
              ]"
            >
              <Icon v-if="isMulti(q) && isSelected(i, opt)" name="heroicons-check" class="w-3 h-3" />
              <template v-else>{{ String.fromCharCode(65 + j) }}</template>
            </span>
            <span
              dir="auto"
              :class="[
                'text-sm transition-colors duration-100',
                isSelected(i, opt) ? 'text-sky-700 dark:text-sky-300 font-medium' : 'text-gray-600 dark:text-gray-400',
              ]"
            >
              {{ optionLabel(opt) }}
            </span>
          </button>

          <!-- "Other" free-text expander -->
          <Transition name="expand">
            <div
              v-if="isOtherSelected(i) && !isLocked"
              class="px-3 py-2 rounded-lg border border-sky-200 dark:border-sky-800 bg-sky-50 dark:bg-sky-950/40"
            >
              <input
                :ref="(el) => { otherInputEls[i] = el as HTMLInputElement | null }"
                v-model="otherTexts[i]"
                type="text"
                dir="auto"
                :placeholder="$t('tools.clarify.otherPlaceholder')"
                class="w-full text-sm bg-transparent outline-none placeholder-sky-300 text-sky-700 dark:text-sky-300"
                @keydown.enter.prevent="allAnswered && submit()"
              />
            </div>
          </Transition>
          <p v-if="isOtherSelected(i) && isLocked && otherTexts[i]" class="text-sm text-gray-700 dark:text-gray-300 px-3" dir="auto">{{ otherTexts[i] }}</p>
        </div>

        <!-- Free-form question -->
        <div
          v-else-if="!isLocked"
          class="px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 focus-within:border-gray-400 transition-colors"
        >
          <input
            v-model="freeTexts[i]"
            type="text"
            dir="auto"
            :placeholder="$t('tools.clarify.placeholder')"
            class="w-full text-sm bg-transparent outline-none placeholder-gray-400 text-gray-900 dark:text-white"
            @keydown.enter.prevent="allAnswered && submit()"
          />
        </div>
        <p v-else class="text-sm text-gray-700 dark:text-gray-300 px-1" dir="auto">{{ freeTexts[i] || '—' }}</p>
      </div>

      <!-- Submit / skip -->
      <div v-if="!isLocked" class="flex items-center gap-3">
        <button
          type="button"
          :disabled="!allAnswered || saving"
          @click="submit"
          :class="[
            'px-2.5 py-1 text-xs font-medium rounded-md transition-colors duration-100',
            allAnswered && !saving
              ? 'bg-sky-500 text-white hover:bg-sky-600 cursor-pointer'
              : 'bg-gray-100 dark:bg-gray-800 text-gray-400 cursor-not-allowed',
          ]"
        >
          {{ $t('tools.clarify.submit') }}
        </button>
        <button
          type="button"
          :disabled="saving"
          class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
          @click="skip"
        >
          {{ $t('tools.clarify.skip') }}
        </button>
        <span v-if="saveError" class="text-xs text-red-500" role="alert">{{ $t('tools.clarify.saveFailed') }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

interface ClarifyQuestion {
  text: string
  options?: string[]
  multi_select?: boolean
  allow_other?: boolean
}

// A chip entry is a single option (single-pick question) or a list of options
// (multi_select question). Legacy persisted responses are always strings.
type ChipEntry = string | string[]

interface ClarifyResponse {
  selected_chips?: ChipEntry[]
  other_texts?: string[]
  free_texts?: string[]
}

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  arguments_json?: {
    questions?: ClarifyQuestion[]
    context?: string
  }
  result_json?: {
    status?: string
    user_response?: ClarifyResponse | null
  } | null
}

const props = withDefaults(
  defineProps<{
    toolExecution: ToolExecution
    alreadyAnswered?: boolean
    systemCompletionId?: string | null
    /** Transcript-only rendering (the public share page). The exchange itself
        survives whole — the questions come from arguments_json and the answer
        from result_json.user_response — so this only locks the form. Distinct
        from `alreadyAnswered`, which says THIS reader answered; readonly says
        nobody can. Both land on `isLocked`, but an unanswered question must
        still not offer a form to someone who cannot submit it. */
    readonly?: boolean
  }>(),
  { alreadyAnswered: false, systemCompletionId: null, readonly: false }
)

const { t } = useI18n()

// The "Other" choice the UI adds itself when a question sets allow_other.
// Stored under this sentinel so it can never collide with a real option.
const OTHER = '__other__'

const storageKey = computed(() => `clarify:${props.toolExecution.id}`)
const status = computed(() => props.toolExecution.status)

const questions = computed<ClarifyQuestion[]>(
  () => props.toolExecution?.arguments_json?.questions ?? []
)

const persistedResponse = computed<ClarifyResponse | null>(
  () => props.toolExecution?.result_json?.user_response ?? null
)
const hasChip = (v: ChipEntry | undefined) => (Array.isArray(v) ? v.length > 0 : Boolean(v))
const hasPersistedResponse = computed(() => {
  const r = persistedResponse.value
  if (!r) return false
  return (r.selected_chips?.some(hasChip) || r.other_texts?.some(Boolean) || r.free_texts?.some(Boolean)) ?? false
})

const selectedChips = ref<ChipEntry[]>([])
const otherTexts = ref<string[]>([])
const freeTexts = ref<string[]>([])
const submitted = ref(false)
const saving = ref(false)
const saveError = ref(false)
const otherInputEls: (HTMLInputElement | null)[] = []

const isLocked = computed(() =>
  submitted.value || hasPersistedResponse.value || props.alreadyAnswered || props.readonly
)

function isMulti(q: ClarifyQuestion | undefined) {
  return Boolean(q?.multi_select && q?.options?.length)
}

// Options as rendered: deduplicated (selection is keyed by value, so a repeated
// option would toggle together), plus the UI's own "Other" when allow_other.
function choicesOf(q: ClarifyQuestion): string[] {
  const opts = [...new Set((q.options ?? []).filter((o) => typeof o === 'string' && o.trim()))]
  return q.allow_other ? [...opts, OTHER] : opts
}

// Older clarify calls (before allow_other) put an "Other…" entry in options.
// Only a bare "Other" word counts — "Other genres" is a real option.
const LEGACY_OTHER = /^(other|others|something else|אחר|אחרת|otro|otra|otros)\s*(…|\.\.\.|:)?$/i

function isOtherOption(opt: string) {
  return opt === OTHER || LEGACY_OTHER.test(opt.trim())
}

function optionLabel(opt: string) {
  return opt === OTHER ? t('tools.clarify.other') : opt
}

function chipsOf(i: number): string[] {
  const v = selectedChips.value[i]
  if (Array.isArray(v)) return v
  return v ? [v] : []
}

function isSelected(i: number, opt: string) {
  return chipsOf(i).includes(opt)
}

function isOtherSelected(i: number) {
  return chipsOf(i).some(isOtherOption)
}

// The answer for question i as text for the agent, or '' while incomplete.
function effectiveAnswer(i: number): string {
  const q = questions.value[i]
  if (!q) return ''
  if (q.options?.length) {
    const picks = chipsOf(i)
    const parts = picks.filter((o) => !isOtherOption(o))
    if (picks.some(isOtherOption)) {
      const other = otherTexts.value[i]?.trim() ?? ''
      if (!other) return '' // "Other" picked but not described yet
      parts.push(other)
    }
    // One pick per line: options may themselves contain commas.
    return parts.length > 1 ? parts.map((p) => `\n- ${p}`).join('') : (parts[0] ?? '')
  }
  return freeTexts.value[i]?.trim() ?? ''
}

const allAnswered = computed(() =>
  questions.value.length > 0 &&
  questions.value.every((_, i) => effectiveAnswer(i) !== '')
)

function initArrays(qs: ClarifyQuestion[]) {
  selectedChips.value = qs.map((q) => (isMulti(q) ? [] : ''))
  otherTexts.value = Array(qs.length).fill('')
  freeTexts.value = Array(qs.length).fill('')
}

function applyPersistedResponse(r: ClarifyResponse) {
  const qs = questions.value
  const n = qs.length
  const padTexts = (arr: string[] | undefined) => {
    const out = Array(n).fill('')
    ;(arr || []).slice(0, n).forEach((v, idx) => { out[idx] = v ?? '' })
    return out
  }
  // Chip entries may be legacy strings or multi-pick lists — normalize per question.
  selectedChips.value = qs.map((q, idx) => {
    const v = (r.selected_chips || [])[idx]
    if (isMulti(q)) return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : (v ? [v] : [])
    return Array.isArray(v) ? (v[0] ?? '') : (v ?? '')
  })
  otherTexts.value = padTexts(r.other_texts)
  freeTexts.value = padTexts(r.free_texts)
}

function saveDraft() {
  if (props.readonly) return
  try {
    sessionStorage.setItem(
      storageKey.value,
      JSON.stringify({ submitted: submitted.value, selectedChips: selectedChips.value, otherTexts: otherTexts.value, freeTexts: freeTexts.value })
    )
  } catch { /* storage unavailable */ }
}

onMounted(() => {
  initArrays(questions.value)
  // Prefer backend-persisted response (survives reload + cross-device)
  if (hasPersistedResponse.value && persistedResponse.value) {
    applyPersistedResponse(persistedResponse.value)
    return
  }
  // Fall back to sessionStorage for in-flight selections — the report page
  // remounts this component when the turn finishes streaming, so without the
  // draft every pick made in the meantime would be wiped. Never in a
  // transcript: the key is the tool-execution id, so an owner reading their own
  // shared conversation would see their unsent draft from the report tab
  // presented as the answer of record.
  if (props.readonly) return
  try {
    const saved = sessionStorage.getItem(storageKey.value)
    if (saved) {
      const parsed = JSON.parse(saved)
      submitted.value = parsed.submitted ?? false
      if (parsed.selectedChips?.length === questions.value.length) selectedChips.value = parsed.selectedChips
      if (parsed.otherTexts?.length === questions.value.length) otherTexts.value = parsed.otherTexts
      if (parsed.freeTexts?.length === questions.value.length) freeTexts.value = parsed.freeTexts
    }
  } catch { /* ignore */ }
})

watch(questions, (qs) => {
  if (qs.length && selectedChips.value.length !== qs.length) initArrays(qs)
}, { immediate: false })

// If clarify_response_json arrives via SSE after mount, rehydrate the form.
watch(persistedResponse, (r) => {
  if (r && hasPersistedResponse.value) applyPersistedResponse(r)
})

watch([selectedChips, otherTexts, freeTexts], () => {
  if (!isLocked.value) saveDraft()
}, { deep: true })

function selectOption(index: number, option: string) {
  const q = questions.value[index]
  if (isMulti(q)) {
    const cur = chipsOf(index)
    selectedChips.value[index] = cur.includes(option)
      ? cur.filter((o) => o !== option)
      : [...cur, option]
  } else {
    selectedChips.value[index] = selectedChips.value[index] === option ? '' : option
  }
  if (isOtherOption(option) && isOtherSelected(index)) {
    nextTick(() => otherInputEls[index]?.focus())
  }
}

function assemblePrompt(): string {
  return questions.value
    .map((q, i) => `Q: ${q.text}  \nA: ${effectiveAnswer(i)}`)
    .join('\n\n')
}

// Resolves true when the answer is stored (or there is nowhere to store it).
// 409 means another tab already answered: treat it as stored.
async function persistResponseToBackend(): Promise<boolean> {
  if (!props.systemCompletionId) return true
  const res = await useMyFetch(
    `/completions/${props.systemCompletionId}/tool_executions/${props.toolExecution.id}/clarify_response`,
    {
      method: 'POST',
      body: {
        selected_chips: [...selectedChips.value],
        other_texts: [...otherTexts.value],
        free_texts: [...freeTexts.value],
      },
    }
  )
  const err: any = res?.error?.value
  if (err && err.statusCode !== 409) {
    console.warn('Failed to persist clarify response', err)
    return false
  }
  return true
}

async function send(text: string) {
  // Belt and braces: isLocked already hides every path here, but a transcript
  // must never POST an answer or prefill a prompt box it doesn't have.
  if (props.readonly || isLocked.value || saving.value) return
  saving.value = true
  saveError.value = false
  // Store the answer before the agent sees it, so the form never shows
  // unanswered (or a different answer) after the conversation has moved on.
  const ok = await persistResponseToBackend()
  saving.value = false
  if (!ok) {
    saveError.value = true
    return
  }
  submitted.value = true
  saveDraft()
  window.dispatchEvent(new CustomEvent('prompt:prefill', { detail: { text, autoSubmit: true } }))
}

function submit() {
  if (!allAnswered.value) return
  send(assemblePrompt())
}

function skip() {
  send(t('tools.clarify.skipPrompt'))
}
</script>

<style scoped>
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
@keyframes shimmer {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}
.expand-enter-active,
.expand-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.expand-enter-from,
.expand-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
