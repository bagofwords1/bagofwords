<template>
    <div data-testid="sandbox-sandboxes">
        <!-- Refresh lives in the page header (settings shell). The list also
             refreshes itself every 10 s while the tab is visible. -->
        <Teleport to="#settings-page-actions" defer>
            <span v-if="loadedAt" class="hidden sm:inline text-xs text-ink-muted whitespace-nowrap nums" data-testid="sandbox-sandboxes-updated">
                {{ $t('sandbox.sandboxes.updated', { when: relative(new Date(loadedAt).toISOString(), now) }) }}
            </span>
            <UButton
                color="gray"
                icon="i-heroicons-arrow-path"
                :loading="refreshing"
                data-testid="sandbox-sandboxes-refresh"
                @click="load(true)"
            >
                {{ $t('sandbox.sandboxes.refresh') }}
            </UButton>
        </Teleport>

        <p v-if="loadError" class="mb-4 rounded-lg border border-line p-4 text-sm text-red-600 dark:text-red-400" role="alert" data-testid="sandbox-sandboxes-error">
            {{ loadError }}
        </p>

        <div class="bow-table-card">
            <div class="overflow-x-auto">
                <table class="bow-table">
                    <thead v-if="loading || sandboxes.length">
                        <tr>
                            <th>{{ $t('sandbox.sandboxes.col.report') }}</th>
                            <th>{{ $t('sandbox.sandboxes.col.environment') }}</th>
                            <th>{{ $t('sandbox.sandboxes.col.runsOn') }}</th>
                            <th>{{ $t('sandbox.sandboxes.col.state') }}</th>
                            <th>{{ $t('sandbox.sandboxes.col.lastUsed') }}</th>
                            <th>{{ $t('sandbox.sandboxes.col.usage') }}</th>
                            <th class="text-end"><span class="sr-only">{{ $t('sandbox.sandboxes.col.actions') }}</span></th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-if="loading" class="bow-table-static">
                            <td colspan="7" class="text-center">
                                <div class="flex items-center justify-center text-ink-muted">
                                    <Spinner class="w-4 h-4 me-2" />
                                    <span class="text-sm">{{ $t('common.loading') }}</span>
                                </div>
                            </td>
                        </tr>
                        <template v-else>
                            <tr
                                v-for="sandbox in sandboxes"
                                :key="sandbox.id"
                                data-testid="sandbox-row"
                                :data-sandbox-id="sandbox.id"
                                :data-state="sandbox.state"
                                :data-report-title="sandbox.report_title || ''"
                            >
                                <!-- Report and whose it is -->
                                <td>
                                    <div class="min-w-[8rem] max-w-[14rem]">
                                        <NuxtLink
                                            :to="`/reports/${sandbox.report_id}`"
                                            class="bow-cell-primary hover:underline line-clamp-2 break-words"
                                            dir="auto"
                                            data-testid="sandbox-report-link"
                                        >{{ sandbox.report_title || $t('sandbox.sandboxes.untitledReport') }}</NuxtLink>
                                        <div class="mt-0.5 flex items-center gap-1 text-xs text-ink-muted" data-testid="sandbox-user">
                                            <UIcon name="i-heroicons-user" class="w-3.5 h-3.5 shrink-0" />
                                            <span class="truncate" dir="auto">{{ sandbox.user_name || $t('sandbox.sandboxes.noUser') }}</span>
                                        </div>
                                    </div>
                                </td>

                                <!-- Environment and what it may reach -->
                                <td>
                                    <div class="text-sm text-ink-2 whitespace-nowrap" dir="auto" data-testid="sandbox-environment">{{ sandbox.environment_name || '—' }}</div>
                                    <div v-if="networkLabel(sandbox)" class="mt-0.5 flex items-center gap-1 text-xs text-ink-muted whitespace-nowrap" data-testid="sandbox-network">
                                        <UIcon :name="networkIcon(sandbox)" class="w-3.5 h-3.5 shrink-0" />
                                        {{ networkLabel(sandbox) }}
                                    </div>
                                </td>

                                <!-- The server it runs on -->
                                <td class="whitespace-nowrap">
                                    <span v-if="sandbox.backend_name" class="text-sm text-ink-2" dir="auto" data-testid="sandbox-runs-on">{{ sandbox.backend_name }}</span>
                                    <span v-else class="bow-cell-empty">—</span>
                                </td>

                                <!-- State and what it is doing -->
                                <td>
                                    <UBadge size="xs" variant="subtle" :color="sandboxStateColor(sandbox.state)" data-testid="sandbox-state">
                                        <Spinner v-if="sandbox.state === 'busy' || sandbox.state === 'provisioning'" class="w-3 h-3 me-1" />
                                        {{ stateLabel(sandbox.state) }}
                                    </UBadge>
                                    <div
                                        v-if="sandbox.state === 'failed' && sandbox.error"
                                        dir="auto"
                                        class="mt-1 max-w-[14rem] text-xs text-red-600 dark:text-red-400 line-clamp-2 break-words"
                                        :title="sandbox.error"
                                        data-testid="sandbox-error"
                                    >{{ sandbox.error }}</div>
                                    <div
                                        v-else
                                        class="mt-1 max-w-[10rem] text-xs text-ink-muted nums"
                                        :title="activityTitle(sandbox)"
                                        data-testid="sandbox-activity"
                                    >{{ activity(sandbox) }}</div>
                                </td>

                                <!-- Last used, and when it is removed -->
                                <td class="whitespace-nowrap">
                                    <div v-if="sandbox.last_used_at" class="text-sm text-ink-2 nums" :title="formatDate(sandbox.last_used_at)" data-testid="sandbox-last-used">
                                        {{ relative(sandbox.last_used_at, now) }}
                                    </div>
                                    <div v-else class="bow-cell-empty">—</div>
                                    <div v-if="sandbox.expires_at" class="mt-0.5 text-xs text-ink-muted nums" :title="formatDate(sandbox.expires_at)" data-testid="sandbox-expires">
                                        {{ $t('sandbox.sandboxes.expires', { when: relative(sandbox.expires_at, now) }) }}
                                    </div>
                                </td>

                                <td>
                                    <SandboxUsageBars :sandbox="sandbox" />
                                </td>

                                <td class="whitespace-nowrap text-end">
                                    <div class="inline-flex items-center gap-1">
                                        <UButton
                                            v-if="sandbox.state === 'busy'"
                                            color="red"
                                            variant="soft"
                                            size="xs"
                                            icon="i-heroicons-stop-circle"
                                            :loading="acting === `kill:${sandbox.id}`"
                                            :disabled="!!acting"
                                            data-testid="sandbox-stop-run"
                                            @click="stopRun(sandbox)"
                                        >
                                            {{ $t('sandbox.sandboxes.stopRun') }}
                                        </UButton>
                                        <UTooltip v-if="canStop(sandbox)" :text="$t('sandbox.sandboxes.stopHint')" :ui="{ base: 'h-auto whitespace-normal max-w-xs' }">
                                            <UButton
                                                color="gray"
                                                variant="soft"
                                                size="xs"
                                                icon="i-heroicons-pause-circle"
                                                :loading="acting === `stop:${sandbox.id}`"
                                                :disabled="!!acting"
                                                data-testid="sandbox-stop"
                                                @click="stop(sandbox)"
                                            >
                                                {{ $t('sandbox.sandboxes.stop') }}
                                            </UButton>
                                        </UTooltip>
                                        <UTooltip :text="$t('sandbox.sandboxes.details')">
                                            <UButton
                                                color="gray"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-document-magnifying-glass"
                                                :aria-label="$t('sandbox.sandboxes.details')"
                                                data-testid="sandbox-details"
                                                @click="openDetail(sandbox)"
                                            />
                                        </UTooltip>
                                        <UTooltip :text="$t('common.delete')">
                                            <UButton
                                                color="red"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-trash"
                                                :aria-label="$t('common.delete')"
                                                :disabled="!!acting"
                                                data-testid="sandbox-delete"
                                                @click="openDelete(sandbox)"
                                            />
                                        </UTooltip>
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="sandboxes.length === 0 && !loadError" class="bow-table-static">
                                <td colspan="7">
                                    <SettingsEmpty
                                        icon="i-heroicons-cube-transparent"
                                        :title="$t('sandbox.sandboxes.emptyTitle')"
                                        :description="$t('sandbox.sandboxes.emptyHint')"
                                        data-testid="sandbox-sandboxes-empty"
                                    />
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>

        <SandboxDetailModal v-model="showDetail" :sandbox="selected" />
        <SandboxDeleteModal v-model="showDelete" :sandbox="selected" @deleted="onDeleted" />
    </div>
</template>

<script setup lang="ts">
import type { SandboxFleetRow } from '~/composables/useSandboxApi'
import { sandboxStateColor, SANDBOX_STATES } from '~/composables/useSandboxFormat'
import Spinner from '~/components/Spinner.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SandboxUsageBars from '~/components/sandbox/SandboxUsageBars.vue'
import SandboxDetailModal from '~/components/sandbox/SandboxDetailModal.vue'
import SandboxDeleteModal from '~/components/sandbox/SandboxDeleteModal.vue'

// Live usage costs one call to each server, so the list is never asked for
// more often than this.
const REFRESH_MS = 10_000

const { t, te } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()
const { formatDate, relative, between } = useSandboxFormat()

const sandboxes = ref<SandboxFleetRow[]>([])
const loading = ref(true)
const refreshing = ref(false)
const loadError = ref('')
const loadedAt = ref(0)
// `${action}:${sandbox id}` while a Stop or Stop run request is out.
const acting = ref('')

const showDetail = ref(false)
const showDelete = ref(false)
const selected = ref<SandboxFleetRow | null>(null)

let inFlight: Promise<void> | null = null
function load(manual = false) {
    if (inFlight) return inFlight
    if (manual) refreshing.value = true
    inFlight = (async () => {
        try {
            sandboxes.value = (await call<SandboxFleetRow[]>('/sandboxes', { query: { usage: 'true' } })) || []
            loadError.value = ''
            loadedAt.value = Date.now()
            if (selected.value) {
                selected.value = sandboxes.value.find(item => item.id === selected.value!.id) || selected.value
            }
        } catch (error) {
            // The rows already on screen stay; the line above the table says they are stale.
            loadError.value = getErrorMessage(error, t('sandbox.sandboxes.loadFailed'))
        } finally {
            loading.value = false
            refreshing.value = false
            inFlight = null
        }
    })()
    return inFlight
}

// One clock for the live durations and "… ago" texts, and the 10 s refresh.
// Both stand still while the browser tab is hidden.
const now = ref(Date.now())
let clock: ReturnType<typeof setInterval> | undefined
let lastAttempt = 0
const visible = () => typeof document === 'undefined' || document.visibilityState === 'visible'
function tick() {
    if (!visible()) return
    now.value = Date.now()
    if (now.value - lastAttempt >= REFRESH_MS) {
        lastAttempt = now.value
        load()
    }
}
function onVisibility() { if (visible()) tick() }

onMounted(() => {
    lastAttempt = Date.now()
    load()
    clock = setInterval(tick, 1000)
    document.addEventListener('visibilitychange', onVisibility)
})
onBeforeUnmount(() => {
    clearInterval(clock)
    document.removeEventListener('visibilitychange', onVisibility)
})

function upsert(row: SandboxFleetRow) {
    sandboxes.value = sandboxes.value.map(item => (item.id === row.id ? { ...row, usage: row.usage ?? null } : item))
    if (selected.value?.id === row.id) selected.value = row
}
function drop(id: string) {
    sandboxes.value = sandboxes.value.filter(item => item.id !== id)
}
function onDeleted(sandbox: SandboxFleetRow) {
    drop(sandbox.id)
}

function openDetail(sandbox: SandboxFleetRow) { selected.value = sandbox; showDetail.value = true }
function openDelete(sandbox: SandboxFleetRow) { selected.value = sandbox; showDelete.value = true }

const canStop = (sandbox: SandboxFleetRow) => sandbox.state === 'ready' || sandbox.state === 'idle'

// A sandbox that went away between the list and the click is not an error to
// act on: say so and take the row out.
function gone(error: unknown, sandbox: SandboxFleetRow) {
    if (getErrorCode(error) !== 'sandbox.not_found') return false
    drop(sandbox.id)
    toast.add({ title: getErrorMessage(error, t('sandbox.sandboxes.toast.stopFailed')), color: 'amber' })
    return true
}

async function stop(sandbox: SandboxFleetRow) {
    if (acting.value) return
    acting.value = `stop:${sandbox.id}`
    try {
        const row = await call<SandboxFleetRow>(`/sandboxes/${sandbox.id}/stop`, { method: 'POST' })
        upsert(row)
        if (row.state === 'stopped') toast.add({ title: t('sandbox.sandboxes.toast.stopped'), color: 'green' })
        else toast.add({ title: t('sandbox.sandboxes.toast.notStopped'), description: row.error || undefined, color: 'amber' })
    } catch (error) {
        if (!gone(error, sandbox)) {
            // The catalog line for sandbox.busy is written for the person whose
            // run it is ("wait for it to finish"); the admin has another way out.
            const message = getErrorCode(error) === 'sandbox.busy'
                ? t('sandbox.sandboxes.toast.stopBusy')
                : getErrorMessage(error, t('sandbox.sandboxes.toast.stopFailed'))
            toast.add({ title: message, color: 'red' })
        }
    } finally {
        acting.value = ''
        load()
    }
}

// Asks the run to end and waits for it (up to about five seconds).
async function stopRun(sandbox: SandboxFleetRow) {
    if (acting.value) return
    acting.value = `kill:${sandbox.id}`
    try {
        const row = await call<SandboxFleetRow>(`/sandboxes/${sandbox.id}/kill-run`, { method: 'POST' })
        upsert(row)
        toast.add({
            title: row.state === 'busy' ? t('sandbox.sandboxes.toast.runStopping') : t('sandbox.sandboxes.toast.runStopped'),
            color: 'green',
        })
    } catch (error) {
        if (!gone(error, sandbox)) toast.add({ title: getErrorMessage(error, t('sandbox.sandboxes.toast.stopRunFailed')), color: 'red' })
    } finally {
        acting.value = ''
        // The worker that owns the run gives the sandbox back a moment later.
        await load()
        setTimeout(() => load(), 2000)
    }
}

const stateLabel = (state: string) =>
    (SANDBOX_STATES.includes(state) ? t(`sandbox.sandboxes.state.${state}`) : state)

const NETWORK_ICONS: Record<string, string> = {
    none: 'i-heroicons-no-symbol',
    full: 'i-heroicons-globe-alt',
}
const networkIcon = (sandbox: SandboxFleetRow) => NETWORK_ICONS[sandbox.limits?.network_policy] || 'i-heroicons-funnel'
function networkLabel(sandbox: SandboxFleetRow) {
    const policy = sandbox.limits?.network_policy
    if (!policy) return ''
    return te(`sandbox.network.${policy}`) ? t(`sandbox.network.${policy}`) : String(policy)
}

function activity(sandbox: SandboxFleetRow) {
    switch (sandbox.state) {
        case 'busy':
            return sandbox.running_since
                ? t('sandbox.sandboxes.activity.running', { duration: between(sandbox.running_since, null, now.value) })
                : t('sandbox.sandboxes.activity.working')
        case 'ready': return t('sandbox.sandboxes.activity.ready')
        case 'idle': return t('sandbox.sandboxes.activity.idle')
        case 'provisioning': return t('sandbox.sandboxes.activity.provisioning')
        case 'stopped':
            return sandbox.stopped_at
                ? t('sandbox.sandboxes.activity.stopped', { when: relative(sandbox.stopped_at, now.value) })
                : t('sandbox.sandboxes.activity.stoppedPlain')
        default: return ''
    }
}
function activityTitle(sandbox: SandboxFleetRow) {
    if (sandbox.state === 'busy' && sandbox.running_since) return formatDate(sandbox.running_since)
    if (sandbox.state === 'stopped' && sandbox.stopped_at) return formatDate(sandbox.stopped_at)
    return undefined
}
</script>
