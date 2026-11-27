<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="p-6 relative" data-testid="sandbox-detail-modal" :data-state="current?.state">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>

            <div class="flex items-start gap-3 pe-8">
                <div class="min-w-0">
                    <h3 class="text-lg font-semibold text-ink break-words" dir="auto">
                        {{ current?.report_title || $t('sandbox.sandboxes.untitledReport') }}
                    </h3>
                    <p class="text-sm text-ink-muted">{{ $t('sandbox.sandboxes.detail.hint') }}</p>
                </div>
                <UBadge v-if="current" size="xs" variant="subtle" :color="sandboxStateColor(current.state)" class="mt-1.5 shrink-0" data-testid="sandbox-detail-state">
                    {{ stateLabel(current.state) }}
                </UBadge>
            </div>
            <hr class="my-4 border-line" />

            <p v-if="errorMessage" class="mb-4 text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-detail-error">
                {{ errorMessage }}
            </p>

            <div v-if="current" class="space-y-6 max-h-[70vh] overflow-y-auto pe-1">
                <!-- The row's facts -->
                <section>
                    <div
                        v-if="current.error"
                        dir="auto"
                        class="mb-3 rounded-lg border border-red-200 dark:border-red-900/60 bg-red-50 dark:bg-red-950/40 px-4 py-3 text-sm text-red-700 dark:text-red-300 break-words"
                        data-testid="sandbox-detail-failure"
                    >{{ current.error }}</div>
                    <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm" data-testid="sandbox-detail-facts">
                        <div v-for="fact in facts" :key="fact.key" class="flex items-baseline gap-2 min-w-0" :data-fact="fact.key">
                            <dt class="w-28 shrink-0 text-ink-muted">{{ fact.label }}</dt>
                            <dd class="min-w-0 text-ink-2 break-words nums" :dir="fact.ltr ? 'ltr' : 'auto'" :title="fact.title">
                                <NuxtLink v-if="fact.to" :to="fact.to" class="text-blue-600 dark:text-blue-400 hover:underline">{{ fact.value }}</NuxtLink>
                                <span v-else-if="fact.mono" class="font-mono text-xs">{{ fact.value }}</span>
                                <template v-else>{{ fact.value }}</template>
                            </dd>
                        </div>
                    </dl>
                    <div v-if="current.usage" class="mt-3 max-w-xs">
                        <SandboxUsageBars :sandbox="current" />
                    </div>
                </section>

                <!-- Recent runs -->
                <section data-testid="sandbox-detail-runs">
                    <h4 class="text-sm font-semibold text-ink">{{ $t('sandbox.sandboxes.detail.runs') }}</h4>
                    <p class="text-xs text-ink-muted">{{ $t('sandbox.sandboxes.detail.runsHint') }}</p>
                    <p
                        v-if="detail && detail.runs.length && !canViewCode"
                        class="mt-2 rounded-lg border border-line bg-surface-subtle px-3 py-2 text-xs text-ink-muted flex items-start gap-2"
                        data-testid="sandbox-detail-code-hidden"
                    >
                        <UIcon name="i-heroicons-eye-slash" class="w-4 h-4 shrink-0" />
                        <span>{{ $t('sandbox.sandboxes.detail.codeHidden') }}</span>
                    </p>
                    <div v-if="loading && !detail" class="mt-3 flex items-center text-sm text-ink-muted">
                        <Spinner class="w-4 h-4 me-2" />{{ $t('common.loading') }}
                    </div>
                    <p v-else-if="detail && !detail.runs.length" class="mt-3 text-sm text-ink-muted">{{ $t('sandbox.sandboxes.detail.noRuns') }}</p>
                    <ul v-else-if="detail" class="mt-3 rounded-lg border border-line divide-y divide-line">
                        <li v-for="run in detail.runs" :key="run.id" data-testid="sandbox-run" :data-status="run.status">
                            <component
                                :is="canViewCode ? 'button' : 'div'"
                                :type="canViewCode ? 'button' : undefined"
                                class="w-full flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-start text-sm no-press"
                                :class="canViewCode ? 'hover:bg-surface-subtle' : ''"
                                :aria-expanded="canViewCode ? expanded.has(run.id) : undefined"
                                data-testid="sandbox-run-toggle"
                                @click="canViewCode && toggle(run.id)"
                            >
                                <UIcon
                                    v-if="canViewCode"
                                    :name="expanded.has(run.id) ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'"
                                    class="w-4 h-4 shrink-0 text-ink-muted"
                                />
                                <UBadge size="xs" variant="subtle" :color="runColor(run.status)" data-testid="sandbox-run-status">
                                    {{ runStatusLabel(run.status) }}
                                </UBadge>
                                <span v-if="run.exit_code !== null && run.exit_code !== undefined" class="text-xs text-ink-muted nums whitespace-nowrap" data-testid="sandbox-run-exit">
                                    {{ $t('sandbox.sandboxes.detail.exitCode', { code: run.exit_code }) }}
                                </span>
                                <span v-if="run.language" class="text-xs text-ink-muted" dir="ltr">{{ run.language }}</span>
                                <span v-if="run.artifact_file_ids?.length" class="text-xs text-ink-muted whitespace-nowrap">
                                    {{ $t('sandbox.sandboxes.detail.filesSaved', { count: run.artifact_file_ids.length }, run.artifact_file_ids.length) }}
                                </span>
                                <span class="ms-auto text-xs text-ink-muted nums whitespace-nowrap" :title="formatDate(run.started_at)">
                                    <template v-if="run.started_at">{{ relative(run.started_at, now) }} · </template>{{ runDuration(run) }}
                                </span>
                            </component>
                            <div v-if="canViewCode && expanded.has(run.id)" class="px-3 pb-3 space-y-2" data-testid="sandbox-run-body">
                                <template v-for="block in blocks(run)" :key="block.key">
                                    <div class="text-2xs font-medium uppercase tracking-wide text-ink-muted">{{ block.label }}</div>
                                    <pre
                                        dir="ltr"
                                        class="max-h-56 overflow-auto rounded-md bg-surface-muted px-3 py-2 text-xs font-mono whitespace-pre-wrap break-words text-start"
                                        :class="block.key === 'stderr' ? 'text-red-700 dark:text-red-300' : 'text-ink-2'"
                                        :data-block="block.key"
                                    >{{ block.text }}</pre>
                                </template>
                                <p v-if="!blocks(run).length" class="text-xs text-ink-muted">{{ $t('sandbox.sandboxes.detail.nothingRecorded') }}</p>
                            </div>
                        </li>
                    </ul>
                </section>

                <!-- Timeline, newest first -->
                <section data-testid="sandbox-detail-events">
                    <h4 class="text-sm font-semibold text-ink">{{ $t('sandbox.sandboxes.detail.timeline') }}</h4>
                    <p v-if="detail && !detail.events.length" class="mt-3 text-sm text-ink-muted">{{ $t('sandbox.sandboxes.detail.noEvents') }}</p>
                    <ol v-else-if="detail" class="mt-3 space-y-2">
                        <li v-for="event in detail.events" :key="event.id" class="flex items-start gap-3 text-sm" data-testid="sandbox-event" :data-type="event.type">
                            <span class="mt-1.5 w-2 h-2 rounded-full shrink-0" :class="eventDot(event.type)" />
                            <div class="min-w-0 flex-1">
                                <span class="text-ink-2">{{ eventLabel(event.type) }}</span>
                                <span v-if="eventNote(event)" class="text-xs text-ink-muted break-words" dir="auto"> · {{ eventNote(event) }}</span>
                            </div>
                            <span v-if="event.created_at" class="shrink-0 text-xs text-ink-muted nums whitespace-nowrap" :title="formatDate(event.created_at)">
                                {{ relative(event.created_at, now) }}
                            </span>
                        </li>
                    </ol>
                </section>
            </div>

            <div class="flex justify-end gap-2 pt-5">
                <UButton variant="ghost" color="gray" icon="i-heroicons-arrow-path" :loading="loading" data-testid="sandbox-detail-refresh" @click="load">
                    {{ $t('sandbox.sandboxes.refresh') }}
                </UButton>
                <UButton color="gray" @click="open = false">{{ $t('common.close') }}</UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxDetail, SandboxEvent, SandboxFleetRow, SandboxRun } from '~/composables/useSandboxApi'
import { sandboxStateColor, SANDBOX_STATES } from '~/composables/useSandboxFormat'
import { useCanViewCode } from '~/composables/usePermissions'
import Spinner from '~/components/Spinner.vue'
import SandboxUsageBars from '~/components/sandbox/SandboxUsageBars.vue'

const props = defineProps<{ modelValue: boolean; sandbox: SandboxFleetRow | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const { t, te } = useI18n()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()
const { formatDate, relative, between, duration } = useSandboxFormat()
const canViewCode = useCanViewCode()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const detail = ref<SandboxDetail | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const expanded = ref(new Set<string>())
const now = ref(Date.now())

// The list's row until the full record lands, so the dialog never opens empty.
const current = computed<SandboxFleetRow | null>(() =>
    (detail.value && detail.value.id === props.sandbox?.id ? detail.value : props.sandbox))

async function load() {
    const id = props.sandbox?.id
    if (!id || loading.value) return
    loading.value = true
    errorMessage.value = ''
    try {
        const fresh = await call<SandboxDetail>(`/sandboxes/${id}`)
        if (props.sandbox?.id === id) detail.value = fresh
    } catch (error) {
        errorMessage.value = getErrorMessage(error, t('sandbox.sandboxes.detail.loadFailed'))
    } finally {
        loading.value = false
        now.value = Date.now()
    }
}

function toggle(id: string) {
    const next = new Set(expanded.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    expanded.value = next
}

const stateLabel = (state: string) =>
    (SANDBOX_STATES.includes(state) ? t(`sandbox.sandboxes.state.${state}`) : state)

interface Fact { key: string; label: string; value: string; title?: string; to?: string; ltr?: boolean; mono?: boolean }

const facts = computed<Fact[]>(() => {
    const row = current.value
    if (!row) return []
    const limits = row.limits || {}
    const out: Fact[] = []
    const add = (key: string, value: unknown, extra: Partial<Fact> = {}) => {
        if (value === null || value === undefined || value === '') return
        out.push({ key, label: t(`sandbox.sandboxes.detail.fact.${key}`), value: String(value), ...extra })
    }
    const when = (key: string, value?: string | null) => {
        if (value) add(key, relative(value, now.value), { title: formatDate(value) })
    }
    add('report', row.report_title || t('sandbox.sandboxes.untitledReport'), { to: `/reports/${row.report_id}` })
    add('user', row.user_name || t('sandbox.sandboxes.noUser'))
    add('environment', row.environment_name)
    const policy = limits.network_policy
    add('network', policy && te(`sandbox.network.${policy}`) ? t(`sandbox.network.${policy}`) : policy)
    add('runsOn', row.backend_name)
    if (row.state === 'busy' && row.running_since) {
        add('running', between(row.running_since, null, now.value), { title: formatDate(row.running_since) })
    }
    when('created', row.created_at)
    when('lastUsed', row.last_used_at)
    when('stopped', row.stopped_at)
    when('expires', row.expires_at)
    const resources = [
        limits.cpus != null ? t('sandbox.sandboxes.detail.cpus', { count: limits.cpus }) : '',
        limits.memory_mb != null ? t('sandbox.sandboxes.detail.memory', { mb: limits.memory_mb }) : '',
        limits.disk_mb != null ? t('sandbox.sandboxes.detail.disk', { mb: limits.disk_mb }) : '',
    ].filter(Boolean)
    add('limits', resources.join(' · '))
    if (limits.run_timeout_seconds != null) add('runTimeout', duration(Number(limits.run_timeout_seconds) * 1000))
    add('image', row.image_digest, { ltr: true, mono: true })
    add('id', row.id, { ltr: true, mono: true })
    return out
})

const RUN_STATUSES = ['running', 'succeeded', 'failed', 'timeout', 'cancelled']
const runStatusLabel = (status: string) =>
    (RUN_STATUSES.includes(status) ? t(`sandbox.sandboxes.runStatus.${status}`) : status)
function runColor(status: string) {
    switch (status) {
        case 'succeeded': return 'green'
        case 'running': return 'blue'
        case 'failed': return 'red'
        case 'timeout': return 'amber'
        default: return 'gray'
    }
}
const runDuration = (run: SandboxRun) => between(run.started_at, run.finished_at, now.value)

function blocks(run: SandboxRun) {
    const out: Array<{ key: string; label: string; text: string }> = []
    const add = (key: string, text?: string | null) => {
        if (text) out.push({ key, label: t(`sandbox.sandboxes.detail.block.${key}`), text })
    }
    add('code', run.code)
    // A command run has no code; the wrapper command of a code run says nothing.
    if (!run.code) add('command', run.command?.join(' '))
    add('stdout', run.stdout)
    add('stderr', run.stderr)
    return out
}

const EVENT_TYPES = [
    'created', 'resumed', 'ready', 'busy', 'idle', 'stopped', 'failed', 'deleted',
    'run_started', 'process_started', 'run_finished', 'run_killed', 'setup_script', 'packages_installed',
]
const eventLabel = (type: string) =>
    (EVENT_TYPES.includes(type) ? t(`sandbox.sandboxes.event.${type}`) : type.replace(/_/g, ' '))
function eventDot(type: string) {
    if (type === 'failed') return 'bg-red-500'
    if (type === 'run_killed' || type === 'stopped' || type === 'deleted') return 'bg-amber-500'
    if (type === 'created' || type === 'resumed') return 'bg-green-500'
    if (type.startsWith('run_') || type === 'process_started') return 'bg-blue-500'
    return 'bg-line-strong'
}
// The few details worth a glance; the reason is the server's own sentence.
function eventNote(event: SandboxEvent) {
    const details = event.details || {}
    const parts: string[] = []
    if (details.status) parts.push(runStatusLabel(String(details.status)))
    if (details.exit_code !== null && details.exit_code !== undefined) {
        parts.push(t('sandbox.sandboxes.detail.exitCode', { code: details.exit_code }))
    }
    if (typeof details.duration_seconds === 'number') parts.push(duration(details.duration_seconds * 1000))
    if (Array.isArray(details.packages) && details.packages.length) parts.push(details.packages.join(', '))
    if (details.environment) parts.push(String(details.environment))
    if (details.reason) parts.push(String(details.reason))
    if (details.error) parts.push(String(details.error))
    return parts.join(' · ')
}

watch(() => [props.modelValue, props.sandbox?.id] as const, ([isOpen]) => {
    if (!isOpen) return
    if (detail.value?.id !== props.sandbox?.id) {
        detail.value = null
        expanded.value = new Set()
    }
    load()
})
</script>
