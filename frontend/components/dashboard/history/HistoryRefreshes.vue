<template>
    <!-- The dashboard's refreshes, newest first (refhist01): when, what
         started it, whose credentials it ran with, how it went, and under a
         run that failed (or partly failed) the first error. Reads its own
         data; owner and editors only (the route checks). -->
    <div data-testid="history-refreshes">
        <div v-if="loading && !runs.length" class="flex items-center gap-2 py-10 justify-center text-sm text-gray-400">
            <Spinner class="w-4 h-4" /> {{ $t('common.loading') }}
        </div>
        <div v-else-if="loadError && !runs.length" class="py-10 flex flex-col items-center gap-2 text-sm text-gray-500 dark:text-gray-400"
            data-testid="history-refreshes-error">
            <span>{{ loadError }}</span>
            <UButton size="xs" color="gray" variant="soft" @click="load()">{{ $t('common.retry') }}</UButton>
        </div>
        <HistoryEmpty v-else-if="!runs.length" icon="heroicons:arrow-path" :text="$t('dashboardCollab.refreshes.empty')" />
        <template v-else>
            <div class="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-md">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="bg-gray-50 dark:bg-gray-800/60 text-xs text-gray-500 dark:text-gray-400">
                            <th class="text-start font-medium px-3 py-2">{{ $t('dashboardCollab.refreshes.colTime') }}</th>
                            <th class="text-start font-medium px-3 py-2">{{ $t('dashboardCollab.refreshes.colTrigger') }}</th>
                            <th class="text-start font-medium px-3 py-2">{{ $t('dashboardCollab.refreshes.colVersion') }}</th>
                            <th class="text-start font-medium px-3 py-2">{{ $t('dashboardCollab.refreshes.colResult') }}</th>
                        </tr>
                    </thead>
                    <tbody>
                        <template v-for="run in runs" :key="run.id">
                            <tr :class="['border-t border-gray-100 dark:border-gray-800', rowTint(run)]" data-testid="history-refresh">
                                <td class="px-3 py-2 whitespace-nowrap tabular-nums text-gray-900 dark:text-gray-100 align-top">
                                    {{ formatDateTime(run.started_at) }}
                                </td>
                                <td class="px-3 py-2 align-top">
                                    <div class="text-gray-700 dark:text-gray-300 whitespace-nowrap">
                                        <!-- Whose access it ran with: on hover. -->
                                        <UTooltip v-if="run.run_as?.name" :text="$t('dashboardCollab.refreshes.ranAs', { name: run.run_as.name })">
                                            <span class="cursor-default" data-testid="history-refresh-trigger">{{ triggerLabel(run) }}</span>
                                        </UTooltip>
                                        <span v-else data-testid="history-refresh-trigger">{{ triggerLabel(run) }}</span>
                                    </div>
                                </td>
                                <!-- Which version it refreshed: what readers see on /d, or the
                                     draft in the editor. -->
                                <td class="px-3 py-2 align-top whitespace-nowrap text-gray-500 dark:text-gray-400" data-testid="history-refresh-version">
                                    {{ versionLabel(run) }}
                                </td>
                                <td class="px-3 py-2 align-top whitespace-nowrap">
                                    <!-- A run with an error: the status opens it (hidden until asked). -->
                                    <button v-if="hasError(run)" type="button" :aria-expanded="isOpen(run)"
                                        :aria-label="$t('dashboardCollab.refreshes.showError')"
                                        :class="['inline-flex items-center gap-1.5 font-medium rounded outline-none focus-visible:ring-2 focus-visible:ring-primary-500', statusColor(run.status)]"
                                        data-testid="history-refresh-status" @click="toggle(run)">
                                        <span class="w-1.5 h-1.5 rounded-full bg-current" aria-hidden="true" />
                                        {{ statusLabel(run.status) }}
                                        <Icon name="heroicons:chevron-down" :class="['w-3.5 h-3.5 transition-transform', isOpen(run) ? 'rotate-180' : '']" />
                                    </button>
                                    <span v-else :class="['inline-flex items-center gap-1.5 font-medium', statusColor(run.status)]" data-testid="history-refresh-status">
                                        <Spinner v-if="run.status === 'running'" class="w-3 h-3" />
                                        <span v-else class="w-1.5 h-1.5 rounded-full bg-current" aria-hidden="true" />
                                        {{ statusLabel(run.status) }}
                                    </span>
                                    <span v-if="run.status === 'partial' && run.steps_total" class="ms-1.5 text-xs text-gray-500 dark:text-gray-400 tabular-nums">
                                        {{ $t('dashboardCollab.refreshes.ofTotal', { done: run.steps_succeeded, total: run.steps_total }) }}
                                    </span>
                                    <div v-if="run.status === 'skipped' && run.skip_reason" class="text-xs text-gray-400">
                                        {{ skipLabel(run.skip_reason) }}
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="hasError(run) && isOpen(run)" :class="rowTint(run)">
                                <td colspan="4" class="px-3 pb-2.5 pt-0">
                                    <div :class="['flex items-start gap-2 text-xs leading-relaxed', run.status === 'failed' ? 'text-red-800 dark:text-red-300' : 'text-orange-800 dark:text-orange-300']"
                                        data-testid="history-refresh-error">
                                        <Icon name="heroicons:exclamation-circle" class="w-4 h-4 shrink-0 mt-px" />
                                        <!-- The query or connection (when the run knows it), then the
                                             reason: each isolated, so a Hebrew name does not turn an
                                             English reason around. -->
                                        <span class="break-words min-w-0">
                                            <template v-if="run.error_source">
                                                <bdi class="font-semibold">{{ run.error_source }}</bdi>
                                                <span class="mx-1" aria-hidden="true">·</span>
                                            </template>
                                            <bdi>{{ run.error }}</bdi>
                                        </span>
                                    </div>
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
            <div class="flex items-center justify-between gap-2 mt-2">
                <p class="text-[11px] text-gray-400">{{ $t('dashboardCollab.refreshes.kept') }}</p>
                <UButton v-if="hasMore" size="xs" color="gray" variant="ghost" :loading="loading" :disabled="loading" data-testid="history-refreshes-more"
                    @click="load(true)">
                    {{ $t('dashboardCollab.refreshes.showMore') }}
                </UButton>
            </div>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import HistoryEmpty from './HistoryEmpty.vue'

// `active`: the tab is showing -- each time it is, the list loads again
// (the settings window keeps the tab mounted).
const props = defineProps<{ dashboardId: string; active?: boolean }>()
const { t } = useI18n()
const { formatDateTime } = useFormatDate()
const { getErrorMessage } = useErrorMessage()

const PAGE = 20
const runs = ref<any[]>([])
const hasMore = ref(false)
const nextBefore = ref<string | null>(null)
// Only the newest request lands: a "Show more" still in flight when the tab
// reloads the first page (or the dashboard changes) must not append a page
// cut from the old list.
let request = 0
const loading = ref(false)
const loadError = ref<string | null>(null)

async function load(more = false) {
    const id = props.dashboardId
    const mine = ++request
    loading.value = true
    loadError.value = null
    try {
        const params = new URLSearchParams({ limit: String(PAGE) })
        if (more && nextBefore.value) params.set('before', nextBefore.value)
        const { data, error } = await useMyFetch(`/artifacts/${id}/refresh-runs?${params}`)
        if (mine !== request || id !== props.dashboardId) return
        if (error.value) throw error.value
        const body = (data.value || {}) as any
        runs.value = more ? [...runs.value, ...(body.runs || [])] : (body.runs || [])
        hasMore.value = !!body.has_more
        nextBefore.value = body.next_before || null
    } catch (e: any) {
        if (mine === request) loadError.value = getErrorMessage(e, t('dashboardCollab.refreshes.loadFailed'))
    } finally {
        if (mine === request) loading.value = false
    }
}

watch(() => props.dashboardId, () => { runs.value = []; hasMore.value = false; nextBefore.value = null })
watch(() => [props.dashboardId, props.active] as const, ([, active]) => { if (active !== false) load() }, { immediate: true })

function triggerLabel(run: any) {
    if (run.trigger === 'schedule') return t('dashboardCollab.refreshes.triggerSchedule')
    if (run.trigger === 'view') return t('dashboardCollab.refreshes.triggerView')
    return t('dashboardCollab.refreshes.triggerManual')
}

function versionLabel(run: any) {
    if (run.kind === 'published') return t('dashboardCollab.refreshes.versionPublished')
    if (run.kind === 'draft') return t('dashboardCollab.refreshes.versionDraft')
    return '—'
}

const STATUS_KEYS: Record<string, string> = {
    success: 'statusSuccess', partial: 'statusPartial', failed: 'statusFailed',
    skipped: 'statusSkipped', running: 'statusRunning', interrupted: 'statusInterrupted',
}
function statusLabel(status: string) {
    return t(`dashboardCollab.refreshes.${STATUS_KEYS[status] || 'statusSkipped'}`)
}
function statusColor(status: string) {
    if (status === 'success') return 'text-green-700 dark:text-green-400'
    if (status === 'partial') return 'text-orange-600 dark:text-orange-400'
    if (status === 'failed') return 'text-red-600 dark:text-red-400'
    return 'text-gray-400'
}
// The error is behind a click; an open one is tinted with its row.
const opened = ref<Set<string>>(new Set())
const hasError = (run: any) => !!run.error && (run.status === 'failed' || run.status === 'partial')
const isOpen = (run: any) => opened.value.has(run.id)
function toggle(run: any) {
    const next = new Set(opened.value)
    next.has(run.id) ? next.delete(run.id) : next.add(run.id)
    opened.value = next
}
function rowTint(run: any) {
    if (!hasError(run) || !isOpen(run)) return ''
    if (run.status === 'failed') return 'bg-red-50 dark:bg-red-950/30'
    return 'bg-orange-50 dark:bg-orange-950/30'
}

const SKIP_KEYS: Record<string, string> = {
    no_runner: 'skipNoRunner', publishing: 'skipPublishing', replaced: 'skipReplaced',
    stopped: 'skipStopped', nothing_to_run: 'skipNothing',
}
function skipLabel(reason: string) {
    return SKIP_KEYS[reason] ? t(`dashboardCollab.refreshes.${SKIP_KEYS[reason]}`) : reason
}
</script>
