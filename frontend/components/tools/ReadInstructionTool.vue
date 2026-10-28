<template>
  <div class="mb-2">
    <div
      class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
      @click="toggleOpen"
      :aria-expanded="expandable ? open : undefined"
    >
      <span v-if="status === 'running'" class="tool-shimmer inline-flex items-center">
        <Spinner class="w-3 h-3 me-1.5 text-ink-muted" />
        {{ t('tools.readInstruction.reading') }}
      </span>
      <template v-else>
        <Icon
          v-if="succeeded"
          :name="open ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
          class="w-3 h-3 me-1 text-ink-muted flex-shrink-0 rtl-flip"
        />
        <Icon v-else name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-red-500 flex-shrink-0" />
        <Icon :name="isSkill ? 'heroicons-sparkles' : 'heroicons-cube'" class="w-3 h-3 me-1 text-indigo-400 flex-shrink-0" />
        <span v-if="succeeded" class="text-gray-700 dark:text-gray-300 truncate">
          {{ isSkill ? t('tools.readInstruction.readSkill') : t('tools.readInstruction.read') }}<template v-if="title"> · {{ title }}</template>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 truncate">
          {{ message || t('tools.readInstruction.failed') }}
        </span>
        <!-- Window badge: which slice of the body the agent actually saw. A
             plain full read shows nothing, exactly as before. -->
        <span
          v-if="succeeded && windowLabel"
          class="ms-2 text-2xs px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 whitespace-nowrap"
          dir="ltr"
        >{{ windowLabel }}</span>
        <span v-if="succeeded && truncated" class="ms-2 text-2xs text-yellow-600 whitespace-nowrap">{{ t('tools.readInstruction.truncated') }}</span>
        <span v-if="succeeded && grepTruncated" class="ms-2 text-2xs text-yellow-600 whitespace-nowrap">{{ t('tools.readInstruction.moreMatches') }}</span>
      </template>
    </div>

    <Transition name="fade">
      <div v-if="open && succeeded" class="mt-1 ms-4 space-y-1.5">
        <!-- GREP READ: matching lines with line numbers; click one for its context. -->
        <template v-if="readMode === 'grep'">
          <ul v-if="matches.length" class="space-y-0.5 leading-snug text-xs text-gray-600 dark:text-gray-400">
            <li v-for="(m, idx) in matches.slice(0, MAX_MATCHES_SHOWN)" :key="idx">
              <div
                class="flex items-center py-0.5 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                @click.stop="toggleMatch(idx)"
                :aria-expanded="isMatchOpen(idx)"
              >
                <Icon :name="isMatchOpen(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-ink-muted me-1 rtl-flip flex-shrink-0" />
                <span class="font-mono text-2xs text-ink-muted flex-shrink-0" dir="ltr">:{{ m.line_no }}</span>
                <span class="ms-2 truncate font-mono text-2xs text-gray-600 dark:text-gray-400">{{ m.line }}</span>
              </div>
              <Transition name="fade">
                <div v-if="isMatchOpen(idx)" class="ps-5 pe-1 pb-1">
                  <pre class="text-2xs bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap" dir="ltr">{{ contextBlock(m) }}</pre>
                  <div v-if="m.col_start != null" class="mt-0.5 text-2xs text-yellow-600">
                    {{ t('tools.readInstruction.longLine', { start: m.col_start, end: m.col_end, length: m.line_length }) }}
                  </div>
                </div>
              </Transition>
            </li>
            <li v-if="matches.length > MAX_MATCHES_SHOWN" class="ps-1 text-2xs text-ink-muted">
              {{ t('tools.readInstruction.moreShown', { count: matches.length - MAX_MATCHES_SHOWN }) }}
            </li>
          </ul>
          <div v-else class="text-2xs text-ink-muted">{{ t('tools.readInstruction.noMatches') }}</div>
        </template>

        <!-- FULL / RANGE / HEAD READ: the verbatim text the agent received. -->
        <template v-else-if="text">
          <div
            class="px-2 py-1.5 rounded border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/40 text-2xs leading-snug text-gray-600 dark:text-gray-400 whitespace-pre-wrap max-h-48 overflow-y-auto"
          >{{ text }}</div>
        </template>

        <!-- Outline: the line-numbered map the agent navigates a long body by.
             Sent on a head read and on a grep with no matches. -->
        <div v-if="outline">
          <div
            class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300 py-0.5"
            @click.stop="outlineOpen = !outlineOpen"
            :aria-expanded="outlineOpen"
          >
            <Icon :name="outlineOpen ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 me-1 text-ink-muted rtl-flip" />
            <Icon name="heroicons-list-bullet" class="w-3 h-3 me-1 text-ink-muted" />
            <span>{{ t('tools.readInstruction.outline') }}</span>
          </div>
          <Transition name="fade">
            <pre v-if="outlineOpen" class="mt-1 text-2xs bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap" dir="ltr">{{ outline }}</pre>
          </Transition>
        </div>

        <ToolCallParams :params="toolExecution?.arguments_json" />
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

interface GrepMatch {
  line_no: number
  line: string
  before?: string[]
  after?: string[]
  col_start?: number
  col_end?: number
  line_length?: number
}

const MAX_MATCHES_SHOWN = 10

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const succeeded = computed(() => status.value === 'success' && result.value?.success !== false)
const title = computed<string>(() => result.value?.title || result.value?.short_id || '')
// The tool reads instructions and skills alike and reports which it resolved,
// so the row can name the right thing. Only the result knows: the call carries
// a short id and nothing else, which is why the running state stays neutral.
const isSkill = computed(() => result.value?.kind === 'skill')
const text = computed<string>(() => result.value?.text || '')
const message = computed<string>(() => result.value?.message || '')

// Windowed reads (see backend read_instruction._window_read): 'full' returns
// the whole body with no extra fields; 'head' is a plain read of a body too
// long to return whole; 'range' is offset/limit; 'grep' is grep_pattern.
const readMode = computed<string>(() => result.value?.read_mode || 'full')
const startLine = computed<number | undefined>(() => result.value?.start_line)
const endLine = computed<number | undefined>(() => result.value?.end_line)
const totalLines = computed<number | undefined>(() => result.value?.total_lines)
const truncated = computed(() => !!result.value?.truncated)
const outline = computed<string>(() => result.value?.outline || '')
const matches = computed<GrepMatch[]>(() => Array.isArray(result.value?.matches) ? result.value.matches : [])
const grepTotal = computed<number>(() => result.value?.grep_total_matches ?? matches.value.length)
const grepTruncated = computed(() => !!result.value?.grep_truncated)
const grepPattern = computed<string>(() => args.value?.grep_pattern || '')

const windowLabel = computed<string>(() => {
  if (readMode.value === 'grep') {
    const n = grepTotal.value
    const count = t(n === 1 ? 'tools.readInstruction.matchSingular' : 'tools.readInstruction.matchPlural', { count: n })
    return `${t('tools.readInstruction.grep', { pattern: grepPattern.value })} · ${count}`
  }
  if ((readMode.value === 'range' || readMode.value === 'head') && startLine.value != null) {
    return t('tools.readInstruction.lines', {
      start: startLine.value,
      end: endLine.value ?? startLine.value,
      total: totalLines.value ?? '?',
    })
  }
  return ''
})

const expandable = computed(() => succeeded.value)

const open = ref(false)
function toggleOpen() {
  if (expandable.value) open.value = !open.value
}

const outlineOpen = ref(false)

const openMatches = ref<Set<number>>(new Set())
function isMatchOpen(idx: number) { return openMatches.value.has(idx) }
function toggleMatch(idx: number) {
  const next = new Set(openMatches.value)
  next.has(idx) ? next.delete(idx) : next.add(idx)
  openMatches.value = next
}

/** Numbered before/match/after block, the way grep -n -B -A prints it. */
function contextBlock(m: GrepMatch): string {
  const before = m.before || []
  const after = m.after || []
  const rows: string[] = []
  before.forEach((l, i) => rows.push(`${m.line_no - before.length + i}  ${l}`))
  rows.push(`${m.line_no}> ${m.line}`)
  after.forEach((l, i) => rows.push(`${m.line_no + 1 + i}  ${l}`))
  return rows.join('\n')
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
.fade-enter-active, .fade-leave-active { transition: opacity 0.15s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
