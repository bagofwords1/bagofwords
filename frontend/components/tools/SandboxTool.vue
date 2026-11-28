<template>
  <!-- create_sandbox, sandbox_status and delete_sandbox: one line each, with the
       sandbox's state, environment and network on it. run_in_sandbox has its own
       block (RunInSandboxTool.vue). -->
  <div class="min-w-0 text-chat-2" :data-testid="`sandbox-tool-${kind}`" :data-outcome="outcome">
    <ToolCallHeader :icon="icon" :label="label" :running="outcome === 'running' || outcome === 'awaiting'"
      :error="outcome === 'error'" :expanded="expanded" :expandable="hasDetails" @toggle="expanded = !expanded">
      <template #actions>
        <span v-if="state || environment || network" class="flex min-w-0 flex-wrap items-center gap-1.5 text-chat-3 text-ink-muted" data-testid="sandbox-tool-meta">
          <span v-if="state" class="rounded px-1 py-0.5 text-2xs" :class="stateClass" data-testid="sandbox-state-chip">{{ stateLabel }}</span>
          <bdi v-if="environment" data-testid="sandbox-tool-environment">{{ environment }}</bdi>
          <span v-if="environment && network" aria-hidden="true">·</span>
          <span v-if="network" data-testid="sandbox-tool-network">{{ network }}</span>
        </span>
      </template>
    </ToolCallHeader>

    <SandboxConfirmCard v-if="outcome === 'awaiting'" :confirmation="confirmation" :system-completion-id="systemCompletionId"
      :readonly="readonly" @answered="answered = true" />

    <div v-if="expanded && hasDetails" class="mt-2 space-y-2 rounded-lg border border-line bg-surface-subtle p-3 text-chat-2" data-testid="sandbox-tool-details">
      <div v-if="errorMessage" dir="auto" class="break-words text-start text-amber-600 dark:text-amber-500" data-testid="sandbox-tool-error">{{ errorMessage }}</div>
      <dl v-if="facts.length" class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-ink-muted">
        <template v-for="fact in facts" :key="fact.key">
          <dt>{{ fact.label }}</dt>
          <dd class="min-w-0 break-words" :class="{ 'font-mono': fact.mono }" :dir="fact.ltr ? 'ltr' : undefined" :title="fact.title">{{ fact.value }}</dd>
        </template>
      </dl>
      <div v-if="recentRuns.length" data-testid="sandbox-tool-runs">
        <div class="mb-1 text-ink-muted">{{ t('tools.sandbox.detail.recentRuns') }}</div>
        <ul class="m-0 list-none space-y-0.5 p-0 text-ink-muted">
          <li v-for="run in recentRuns" :key="run.run_id" class="flex flex-wrap items-baseline gap-x-2">
            <span class="text-ink-2">{{ languageLabel(run.language) }}</span>
            <span>{{ runStatusLabel(run.status) }}</span>
            <span v-if="run.exit_code != null && run.exit_code !== 0" class="nums">{{ t('tools.sandbox.exitCode', { code: run.exit_code }) }}</span>
            <span v-if="run.artifacts" class="nums">{{ t('tools.sandbox.fileCount', { count: run.artifacts }, { plural: run.artifacts }) }}</span>
            <span v-if="run.started_at" class="text-chat-3" :title="formatDate(run.started_at)">{{ relative(run.started_at) }}</span>
          </li>
        </ul>
      </div>
      <div v-if="environments.length" data-testid="sandbox-tool-environments">
        <div class="mb-1 text-ink-muted">{{ t('tools.sandbox.detail.environments') }}</div>
        <ul class="m-0 list-none space-y-0.5 p-0 text-ink-muted">
          <li v-for="env in environments" :key="env.name" class="flex flex-wrap items-baseline gap-x-2">
            <bdi class="text-ink-2">{{ env.name }}</bdi>
            <span v-if="env.is_default" class="rounded bg-gray-100 px-1 py-0.5 text-2xs dark:bg-gray-800">{{ t('sandbox.environments.defaultBadge') }}</span>
            <bdi v-if="env.description" class="min-w-0 break-words">{{ env.description }}</bdi>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import SandboxConfirmCard from '~/components/tools/SandboxConfirmCard.vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import { SANDBOX_STATES, sandboxStateColor } from '~/composables/useSandboxFormat'
import { SANDBOX_LANGUAGES, SANDBOX_NETWORK_POLICIES, sandboxActionOutcome } from '~/utils/sandboxTool'

const props = withDefaults(defineProps<{
  toolExecution: Record<string, any>
  runStatus?: string
  systemCompletionId?: string
  /** The shared link: an approval cannot be answered there. */
  readonly?: boolean
}>(), { runStatus: '', readonly: false })

const { t, locale } = useResponseI18n()
const { formatDate, relative, duration } = useSandboxFormat(locale)

const kind = computed<'create' | 'status' | 'delete'>(() => {
  const name = props.toolExecution?.tool_name
  return name === 'create_sandbox' ? 'create' : name === 'delete_sandbox' ? 'delete' : 'status'
})
const result = computed<Record<string, any>>(() => props.toolExecution?.result_json || {})
const confirmation = computed<Record<string, any>>(() => props.toolExecution?.confirmation || {})
const answered = ref(false)
const outcome = computed(() => sandboxActionOutcome(props.toolExecution, props.runStatus, answered.value))
const expanded = ref(false)

const icon = computed(() => (kind.value === 'delete' ? 'heroicons-trash' : 'heroicons-cube'))

const label = computed(() => {
  const base = `tools.sandbox.${kind.value}`
  const o = outcome.value
  if (o === 'done') {
    if (kind.value === 'create') return t(result.value.created ? `${base}.created` : `${base}.existing`)
    if (kind.value === 'delete') return t(result.value.deleted ? `${base}.deleted` : `${base}.nothing`)
    return t(result.value.exists ? `${base}.exists` : `${base}.none`)
  }
  if (o === 'error') return t(`${base}.failed`)
  // Only a deletion asks first.
  if (kind.value !== 'delete' && o !== 'running' && o !== 'cancelled') return t(`${base}.${o === 'awaiting' ? 'running' : 'failed'}`)
  return t(`${base}.${o}`)
})

const done = computed(() => outcome.value === 'done')
const state = computed<string>(() => (done.value && kind.value !== 'delete' ? (result.value.state || '') : ''))
const stateLabel = computed(() => (SANDBOX_STATES.includes(state.value) ? t(`sandbox.sandboxes.state.${state.value}`) : state.value))
const STATE_CLASSES: Record<string, string> = {
  green: 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300',
  blue: 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
  red: 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300',
  amber: 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300',
  gray: 'bg-gray-100 text-ink-muted dark:bg-gray-800',
}
const stateClass = computed(() => STATE_CLASSES[sandboxStateColor(state.value)])
const environment = computed<string>(() => result.value.environment || confirmation.value.environment || '')
const network = computed(() => {
  const policy = result.value.network_policy
  return SANDBOX_NETWORK_POLICIES.includes(policy) ? t(`sandbox.network.${policy}`) : (policy || '')
})

// A refusal's own sentence repeats the row's label.
const errorMessage = computed(() => (outcome.value === 'error' ? (result.value.error_message || props.toolExecution?.error_message || '') : ''))

interface Fact { key: string; label: string; value: string; title?: string; ltr?: boolean; mono?: boolean }
const facts = computed<Fact[]>(() => {
  if (!done.value || kind.value === 'delete') return []
  const out: Fact[] = []
  const add = (key: string, label: string, value: unknown, extra: Partial<Fact> = {}) => {
    if (value === null || value === undefined || value === '') return
    out.push({ key, label, value: String(value), ...extra })
  }
  const fact = (key: string) => t(`sandbox.sandboxes.detail.fact.${key}`)
  add('environment', fact('environment'), environment.value)
  add('network', fact('network'), network.value)
  if (Array.isArray(result.value.languages) && result.value.languages.length) {
    add('languages', t('tools.sandbox.detail.languages'), result.value.languages.map(languageLabel).join(', '))
  }
  const limits = result.value.limits || {}
  const resources = [
    limits.cpus != null ? t('sandbox.sandboxes.detail.cpus', { count: limits.cpus }) : '',
    limits.memory_mb != null ? t('sandbox.sandboxes.detail.memory', { mb: limits.memory_mb }) : '',
    limits.disk_mb != null ? t('sandbox.sandboxes.detail.disk', { mb: limits.disk_mb }) : '',
  ].filter(Boolean)
  add('limits', fact('limits'), resources.join(' · '))
  if (limits.run_timeout_seconds != null) add('runTimeout', fact('runTimeout'), duration(Number(limits.run_timeout_seconds) * 1000))
  if (result.value.uptime_seconds != null) add('uptime', t('tools.sandbox.detail.uptime'), duration(Number(result.value.uptime_seconds) * 1000))
  if (result.value.expires_at) add('expires', fact('expires'), relative(result.value.expires_at), { title: formatDate(result.value.expires_at) })
  add('id', fact('id'), result.value.sandbox_id, { ltr: true, mono: true })
  return out
})

const recentRuns = computed<any[]>(() => (done.value && Array.isArray(result.value.recent_runs) ? result.value.recent_runs : []))
const environments = computed<any[]>(() => (done.value && Array.isArray(result.value.available_environments) ? result.value.available_environments : []))
const hasDetails = computed(() => !!errorMessage.value || facts.value.length > 0 || recentRuns.value.length > 0 || environments.value.length > 0)

function languageLabel(language: string) {
  return SANDBOX_LANGUAGES.includes(language) ? t(`tools.sandbox.lang.${language}`) : language
}
const RUN_STATUSES = ['running', 'succeeded', 'failed', 'timeout', 'cancelled']
const runStatusLabel = (status: string) => (RUN_STATUSES.includes(status) ? t(`sandbox.sandboxes.runStatus.${status}`) : status)
</script>
