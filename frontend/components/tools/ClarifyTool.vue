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
        <div v-if="q.options?.length" class="space-y-1">
          <button
            v-for="(opt, j) in q.options"
            :key="opt"
            type="button"
            :disabled="isLocked"
            @click="!isLocked && selectOption(i, opt)"
            :class="[
              'flex items-center gap-2.5 w-full text-start px-3 py-2 rounded-lg border transition-all duration-100',
              isSelected(i, opt)
                ? 'border-sky-200 bg-sky-50'
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
                isSelected(i, opt) ? 'text-sky-700 font-medium' : 'text-gray-600 dark:text-gray-400',
              ]"
            >
              {{ opt }}
            </span>
          </button>

          <!-- "Other…" free-text expander -->
          <Transition name="expand">
            <div
              v-if="isOtherSelected(i) && !isLocked"
              class="px-3 py-2 rounded-lg border border-sky-200 bg-sky-50"
            >
              <input
                ref="otherInputRefs"
                v-model="otherTexts[i]"
                type="text"
                dir="auto"
                placeholder="Describe…"
                class="w-full text-sm bg-transparent outline-none placeholder-sky-300 text-sky-700"
                @keydown.enter.prevent="allAnswered && submit()"
              />
            </div>
          </Transition>
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

      <!-- Submit -->
      <button
        v-if="!isLocked"
        type="button"
        :disabled="!allAnswered"
        @click="submit"
        :class="[
          'px-2.5 py-1 text-xs font-medium rounded-md transition-colors duration-100',
          allAnswered
            ? 'bg-sky-500 text-white hover:bg-sky-600 cursor-pointer'
            : 'bg-gray-100 dark:bg-gray-800 text-gray-400 cursor-not-allowed',
        ]"
      >
        {{ $t('tools.clarify.submit') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface ClarifyQuestion {
  text: string
  options?: string[]
  multi_select?: boolean
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
  }>(),
  { alreadyAnswered: false, systemCompletionId: null }
)

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
const otherInputRefs = ref<HTMLInputElement[]>([])

const isLocked = computed(() =>
  submitted.value || hasPersistedResponse.value || props.alreadyAnswered
)

function isMulti(q: ClarifyQuestion | undefined) {
  return Boolean(q?.multi_select && q?.options?.length)
}

function isOtherOption(opt: string) {
  return /^other/i.test(opt.trim())
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

function effectiveAnswer(i: number): string {
  const q = questions.value[i]
  if (!q) return ''
  if (q.options?.length) {
    const picks = chipsOf(i)
    const parts = picks.filter((o) => !isOtherOption(o))
    if (picks.some(isOtherOption)) {
      const other = otherTexts.value[i]?.trim() ?? ''
      if (!other) return '' // "Other…" picked but not described yet
      parts.push(other)
    }
    return parts.join(', ')
  }
  return freeTexts.value[i]?.trim() ?? ''
}

const allAnswered = computed(() =>
  questions.value.length > 0 &&
  questions.value.every((_, i) => effectiveAnswer(i) !== '')
)

// Auto-submit when all questions are single-pick chips (no free-form or
// multi-select pending — for multi-select we can't know when the user is done).
const allChipBased = computed(() =>
  questions.value.every((q, i) => (q.options?.length ?? 0) > 0 && !isMulti(q) && !isOtherSelected(i))
)

watch(allAnswered, (val) => {
  if (val && allChipBased.value && !isLocked.value) submit()
})

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

onMounted(() => {
  initArrays(questions.value)
  // Prefer backend-persisted response (survives reload + cross-device)
  if (hasPersistedResponse.value && persistedResponse.value) {
    applyPersistedResponse(persistedResponse.value)
    return
  }
  // Fall back to sessionStorage for in-flight, unsaved selections
  const saved = sessionStorage.getItem(storageKey.value)
  if (saved) {
    try {
      const parsed = JSON.parse(saved)
      submitted.value = parsed.submitted ?? false
      if (parsed.selectedChips) selectedChips.value = parsed.selectedChips
      if (parsed.otherTexts) otherTexts.value = parsed.otherTexts
      if (parsed.freeTexts) freeTexts.value = parsed.freeTexts
    } catch { /* ignore */ }
  }
})

watch(questions, (qs) => {
  if (qs.length && selectedChips.value.length === 0) initArrays(qs)
}, { immediate: false })

// If clarify_response_json arrives via SSE after mount, rehydrate the form.
watch(persistedResponse, (r) => {
  if (r && hasPersistedResponse.value) applyPersistedResponse(r)
})

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
    nextTick(() => otherInputRefs.value[0]?.focus())
  }
}

function assemblePrompt(): string {
  return questions.value
    .map((q, i) => `Q: ${q.text}  \nA: ${effectiveAnswer(i)}`)
    .join('\n\n')
}

async function persistResponseToBackend() {
  if (!props.systemCompletionId) return
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
  if (res?.error?.value) {
    // Non-blocking: sessionStorage still holds the answer for this tab.
    console.warn('Failed to persist clarify response', res.error.value)
  }
}

function submit() {
  if (!allAnswered.value) return
  submitted.value = true
  sessionStorage.setItem(
    storageKey.value,
    JSON.stringify({ submitted: true, selectedChips: [...selectedChips.value], otherTexts: [...otherTexts.value], freeTexts: [...freeTexts.value] })
  )
  persistResponseToBackend()
  window.dispatchEvent(new CustomEvent('prompt:prefill', { detail: { text: assemblePrompt(), autoSubmit: true } }))
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
