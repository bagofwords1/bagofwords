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
                                        <UTooltip v-if="sandbox.pinned" :text="$t('sandbox.sandboxes.pinnedHint')" :ui="{ base: 'h-auto whitespace-normal max-w-xs' }" class="mt-1">
                                            <UBadge size="xs" color="blue" variant="subtle" class="gap-1" data-testid="sandbox-pinned">
                                                <UIcon name="i-heroicons-bookmark" class="w-3 h-3" />
                                                {{ $t('sandbox.sandboxes.pinned') }}
                                            </UBadge>
                                        </UTooltip>
                                    </div>
                                </td>

                                <!-- Environment and what it may reach -->
                                <td>
                                    <div class="text-sm text-ink-2 whitespace-nowrap" dir="auto" data-testid="sandbox-environment">{{ sandbox.environment_name || '—' }}</div>
                                    <div v-if="networkLabel(sandbox)" class="mt-0.5 flex items-start gap-1 max-w-[9rem] text-xs text-ink-muted" data-testid="sandbox-network">
                                        <UIcon :name="networkIcon(sandbox)" class="w-3.5 h-3.5 shrink-0 mt-0.5" />
                                        {{ networkLabel(sandbox) }}
                                    </div>
                                    <div
                                        v-if="mountsOf(sandbox).length"
                                        class="mt-0.5 flex items-center gap-1 text-xs text-ink-muted max-w-[12rem]"
                                        :title="mountsTitle(sandbox)"
                                        data-testid="sandbox-mounts"
                                    >
                                        <UIcon name="i-heroicons-circle-stack" class="w-3.5 h-3.5 shrink-0" />
                                        <span class="truncate" dir="ltr">{{ mountsOf(sandbox).map(mount => mount.volume).join(', ') }}</span>
                                    </div>
                                    <UTooltip v-if="sandbox.setup_outdated" :text="$t('sandbox.sandboxes.setupOutdatedHint')" :ui="{ base: 'h-auto whitespace-normal max-w-xs' }" class="mt-1">
                                        <UBadge size="xs" color="amber" variant="subtle" class="gap-1" data-testid="sandbox-setup-outdated">
                                            <UIcon name="i-heroicons-clock" class="w-3 h-3" />
                                            {{ $t('sandbox.sandboxes.setupOutdated') }}
                                        </UBadge>
                                    </UTooltip>
                                </td>

                                <!-- The server it runs on -->
                                <td class="whitespace-nowrap">
                                    <span v-if="sandbox.compute_name" class="text-sm text-ink-2" dir="auto" data-testid="sandbox-runs-on">{{ sandbox.compute_name }}</span>
                                    <span v-else class="bow-cell-empty">—</span>
                                </td>

                                <!-- State and what it is doing -->
                                <td class="min-w-[9rem]">
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
                                    <div
                                        v-if="keptWarm(sandbox)"
                                        class="mt-0.5 flex items-start gap-1 max-w-[10rem] text-xs text-ink-muted nums"
                                        :title="formatDate(sandbox.keep_warm_until)"
                                        data-testid="sandbox-keep-warm"
                                    >
                                        <UIcon name="i-heroicons-fire" class="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-500" />
                                        {{ $t('sandbox.sandboxes.keptWarm', { when: relative(sandbox.keep_warm_until!, now) }) }}
                                    </div>
                                </td>

                                <!-- Last used, and when it is removed -->
                                <td>
                                    <div class="max-w-[11rem]">
                                    <div v-if="sandbox.last_used_at" class="text-sm text-ink-2 nums whitespace-nowrap" :title="formatDate(sandbox.last_used_at)" data-testid="sandbox-last-used">
                                        {{ relative(sandbox.last_used_at, now) }}
                                    </div>
                                    <div v-else class="bow-cell-empty">—</div>
                                    <!-- When it is deleted on its own, and why -->
                                    <div
                                        v-if="sandbox.deletes_at"
                                        class="mt-0.5 text-xs nums"
                                        :class="deletesSoon(sandbox) ? 'text-amber-700 dark:text-amber-300' : 'text-ink-muted'"
                                        :title="formatDate(sandbox.deletes_at)"
                                        data-testid="sandbox-deletes"
                                        :data-reason="sandbox.delete_reason || ''"
                                    >
                                        <div>{{ $t('sandbox.sandboxes.deletes', { when: relative(sandbox.deletes_at, now) }) }}</div>
                                        <div v-if="deleteReason(sandbox)" class="text-ink-muted">{{ deleteReason(sandbox) }}</div>
                                    </div>
                                    <div v-else-if="sandbox.pinned" class="mt-0.5 text-xs text-ink-muted" data-testid="sandbox-not-deleted">
                                        {{ $t('sandbox.sandboxes.notDeleted') }}
                                    </div>
                                    <div
                                        v-if="sandbox.snapshot_at"
                                        class="mt-0.5 flex items-start gap-1 text-xs text-ink-muted nums"
                                        :title="formatDate(sandbox.snapshot_at)"
                                        data-testid="sandbox-snapshot"
                                    >
                                        <UIcon name="i-heroicons-camera" class="w-3.5 h-3.5 shrink-0 mt-0.5" />
                                        <span>{{ snapshotLabel(sandbox) }}</span>
                                    </div>
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
                                        <UDropdown :items="moreActions(sandbox)" :popper="{ placement: 'bottom-end', strategy: 'fixed' }" :ui="{ width: 'w-60' }">
                                            <UButton
                                                color="gray"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-ellipsis-horizontal"
                                                :loading="acting.endsWith(`:${sandbox.id}`) && isMenuAction(acting)"
                                                :aria-label="$t('sandbox.sandboxes.more')"
                                                data-testid="sandbox-more"
                                            />
                                        </UDropdown>
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
        <SandboxAmountModal v-model="showAmount" :sandbox="selected" :mode="amountMode" @done="upsert" @gone="onDeleted" />
    </div>
</template>

<script setup lang="ts">
import { parseSandboxDate, type SandboxFleetRow } from '~/composables/useSandboxApi'
import { sandboxStateColor, SANDBOX_STATES } from '~/composables/useSandboxFormat'
import Spinner from '~/components/Spinner.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SandboxUsageBars from '~/components/sandbox/SandboxUsageBars.vue'
import SandboxDetailModal from '~/components/sandbox/SandboxDetailModal.vue'
import SandboxDeleteModal from '~/components/sandbox/SandboxDeleteModal.vue'
import SandboxAmountModal from '~/components/sandbox/SandboxAmountModal.vue'

// Live usage costs one call to each server, so the list is never asked for
// more often than this.
const REFRESH_MS = 10_000

const { t, te } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()
const { formatDate, relative, between, bytes } = useSandboxFormat()

const sandboxes = ref<SandboxFleetRow[]>([])
const loading = ref(true)
const refreshing = ref(false)
const loadError = ref('')
const loadedAt = ref(0)
// `${action}:${sandbox id}` while a request for that row is out.
const acting = ref('')

const showDetail = ref(false)
const showDelete = ref(false)
const showAmount = ref(false)
const amountMode = ref<'keepWarm' | 'extend'>('keepWarm')
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

// The actions answer without live usage: the row keeps what it showed.
function upsert(row: SandboxFleetRow) {
    sandboxes.value = sandboxes.value.map(item => (item.id === row.id ? { ...row, usage: row.usage ?? item.usage ?? null } : item))
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

// ── The row's menu: start, keep warm, pin, extend, snapshot ──────────────

const MENU_ACTIONS = ['start', 'warm', 'pin', 'snapshot']
const isMenuAction = (key: string) => MENU_ACTIONS.includes(key.split(':')[0])

const keptWarm = (sandbox: SandboxFleetRow) =>
    !!sandbox.keep_warm_until && parseSandboxDate(sandbox.keep_warm_until).getTime() > now.value

function openAmount(sandbox: SandboxFleetRow, mode: 'keepWarm' | 'extend') {
    selected.value = sandbox
    amountMode.value = mode
    showAmount.value = true
}

// One request, the fresh row back, a line saying what happened.
async function act(sandbox: SandboxFleetRow, key: string, path: string, body: Record<string, any> | undefined, done: string, failed: string) {
    if (acting.value) return
    acting.value = `${key}:${sandbox.id}`
    try {
        upsert(await call<SandboxFleetRow>(`/sandboxes/${sandbox.id}/${path}`, { method: 'POST', body }))
        toast.add({ title: t(done), color: 'green' })
    } catch (error) {
        if (!gone(error, sandbox)) {
            // "Wait for it to finish" is written for the person whose run it is.
            const message = getErrorCode(error) === 'sandbox.busy'
                ? t('sandbox.sandboxes.toast.stopBusy')
                : getErrorMessage(error, t(failed))
            toast.add({ title: message, color: 'red' })
        }
    } finally {
        acting.value = ''
        load()
    }
}

function moreActions(sandbox: SandboxFleetRow) {
    const busy = !!acting.value
    const alive = !['failed', 'deleted'].includes(sandbox.state)
    const lifecycle: any[] = []
    if (sandbox.state === 'stopped') {
        lifecycle.push({
            label: t('sandbox.sandboxes.start'), icon: 'i-heroicons-play', disabled: busy,
            click: () => act(sandbox, 'start', 'start', undefined, 'sandbox.sandboxes.toast.started', 'sandbox.sandboxes.toast.startFailed'),
        })
    }
    if (alive) {
        lifecycle.push({
            label: t(keptWarm(sandbox) ? 'sandbox.sandboxes.keepWarmChange' : 'sandbox.sandboxes.keepWarmAction'),
            icon: 'i-heroicons-fire', disabled: busy, click: () => openAmount(sandbox, 'keepWarm'),
        })
        if (keptWarm(sandbox)) {
            lifecycle.push({
                label: t('sandbox.sandboxes.keepWarmClear'), icon: 'i-heroicons-x-mark', disabled: busy,
                click: () => act(sandbox, 'warm', 'keep-warm', { hours: 0 }, 'sandbox.sandboxes.toast.keepWarmCleared', 'sandbox.sandboxes.keepWarm.failed'),
            })
        }
    }
    const keeping: any[] = [{
        label: t(sandbox.pinned ? 'sandbox.sandboxes.unpin' : 'sandbox.sandboxes.pin'),
        icon: sandbox.pinned ? 'i-heroicons-bookmark-slash' : 'i-heroicons-bookmark', disabled: busy,
        click: () => act(sandbox, 'pin', 'pin', { pinned: !sandbox.pinned },
            sandbox.pinned ? 'sandbox.sandboxes.toast.unpinned' : 'sandbox.sandboxes.toast.pinned', 'sandbox.sandboxes.toast.pinFailed'),
    }]
    if (alive) {
        keeping.push({
            label: t('sandbox.sandboxes.extendAction'), icon: 'i-heroicons-calendar-days', disabled: busy,
            click: () => openAmount(sandbox, 'extend'),
        })
    }
    const snapshot: any[] = []
    if (['ready', 'idle', 'stopped', 'busy'].includes(sandbox.state)) {
        snapshot.push({
            label: t('sandbox.sandboxes.snapshotNow'), icon: 'i-heroicons-camera', disabled: busy,
            click: () => act(sandbox, 'snapshot', 'snapshot', undefined, 'sandbox.sandboxes.toast.snapshotTaken', 'sandbox.sandboxes.toast.snapshotFailed'),
        })
    }
    return [lifecycle, keeping, snapshot].filter(group => group.length)
}

// ── What the row says ────────────────────────────────────────────────────

const mountsOf = (sandbox: SandboxFleetRow): Array<{ volume: string; path: string; read_only?: boolean }> =>
    (Array.isArray(sandbox.limits?.mounts) ? sandbox.limits.mounts : []).filter((mount: any) => mount?.volume)
const mountsTitle = (sandbox: SandboxFleetRow) => mountsOf(sandbox)
    .map(mount => `${mount.volume} → ${mount.path} (${t(mount.read_only === false ? 'sandbox.sandboxes.mountWritable' : 'sandbox.sandboxes.mountReadOnly')})`)
    .join('\n')

const DELETE_REASONS: Record<string, string> = { expired: 'expired', 'stopped too long': 'stoppedTooLong' }
function deleteReason(sandbox: SandboxFleetRow) {
    const reason = sandbox.delete_reason || ''
    return reason in DELETE_REASONS ? t(`sandbox.sandboxes.deleteReason.${DELETE_REASONS[reason]}`) : reason
}
// Within a day: the deletion has been, or is about to be, announced.
const deletesSoon = (sandbox: SandboxFleetRow) =>
    !!sandbox.deletes_at && parseSandboxDate(sandbox.deletes_at).getTime() - now.value < 24 * 3600 * 1000

function snapshotLabel(sandbox: SandboxFleetRow) {
    const when = relative(sandbox.snapshot_at!, now.value)
    return typeof sandbox.snapshot_bytes === 'number'
        ? t('sandbox.sandboxes.snapshotWithSize', { when, size: bytes(sandbox.snapshot_bytes) })
        : t('sandbox.sandboxes.snapshot', { when })
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
