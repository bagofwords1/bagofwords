<template>
    <div data-testid="sandbox-computes">
        <!-- Primary action lives in the page header (settings shell). -->
        <Teleport to="#settings-page-actions" defer>
            <UButton color="blue" icon="i-heroicons-plus" data-testid="sandbox-add-compute" @click="openCreate">
                {{ $t('sandbox.computes.add') }}
            </UButton>
        </Teleport>

        <p v-if="loadError" class="mb-4 rounded-lg border border-line p-4 text-sm text-red-600 dark:text-red-400" role="alert" data-testid="sandbox-computes-error">
            {{ loadError }}
        </p>

        <div class="bow-table-card">
            <div class="overflow-x-auto">
                <table class="bow-table">
                    <thead v-if="loading || computes.length">
                        <tr>
                            <th>{{ $t('sandbox.computes.col.name') }}</th>
                            <th>{{ $t('sandbox.computes.col.status') }}</th>
                            <th>{{ $t('sandbox.computes.col.supports') }}</th>
                            <th>{{ $t('sandbox.computes.col.bootstrap') }}</th>
                            <th>{{ $t('sandbox.computes.col.sandboxes') }}</th>
                            <th>{{ $t('sandbox.computes.col.enabled') }}</th>
                            <th class="text-end"><span class="sr-only">{{ $t('sandbox.computes.col.actions') }}</span></th>
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
                            <tr v-for="compute in computes" :key="compute.id" data-testid="sandbox-compute-row" :data-compute-name="compute.name">
                                <!-- Name, type, labels -->
                                <td>
                                    <div class="flex items-start gap-2">
                                        <span class="flex items-center justify-center w-7 h-7 rounded-md bg-surface-muted text-ink-muted shrink-0">
                                            <UIcon :name="compute.type === 'fake' ? 'i-heroicons-beaker' : 'i-heroicons-server'" class="h-4 w-4" />
                                        </span>
                                        <div class="min-w-[9rem]">
                                            <div class="bow-cell-primary whitespace-nowrap" data-testid="sandbox-compute-name-cell">{{ compute.name }}</div>
                                            <div class="text-xs text-ink-muted max-w-[12rem]" data-testid="sandbox-compute-type-cell">{{ typeTitle(compute.type) }}</div>
                                            <div v-if="compute.labels?.length" class="mt-1 flex flex-wrap gap-1">
                                                <UBadge v-for="label in compute.labels" :key="label" size="xs" color="gray">{{ label }}</UBadge>
                                            </div>
                                        </div>
                                    </div>
                                </td>

                                <!-- Health -->
                                <td>
                                    <UBadge size="xs" variant="subtle" :color="statusColor(compute.status)" data-testid="sandbox-compute-status">
                                        {{ $t(`sandbox.computes.status.${statusKey(compute.status)}`) }}
                                    </UBadge>
                                    <div
                                        v-if="compute.last_error && compute.status !== 'healthy'"
                                        dir="auto"
                                        class="mt-1 max-w-[12rem] text-xs text-red-600 dark:text-red-400 line-clamp-2 break-words"
                                        :title="compute.last_error"
                                        data-testid="sandbox-compute-last-error"
                                    >{{ compute.last_error }}</div>
                                    <div class="mt-1 text-xs text-ink-muted whitespace-nowrap nums" data-testid="sandbox-compute-last-check">
                                        <span v-if="compute.last_checked_at" :title="formatDate(compute.last_checked_at)">{{ $t('sandbox.computes.checkedAgo', { when: timeAgo(compute.last_checked_at) }) }}</span>
                                        <span v-else>{{ $t('sandbox.computes.neverChecked') }}</span>
                                    </div>
                                </td>

                                <!-- What it supports -->
                                <td>
                                    <template v-if="isolationOf(compute)">
                                        <UTooltip
                                            :text="isolationOf(compute) === 'strong' ? $t('sandbox.computes.isolation.strongHint') : (compute.info?.isolation_detail || $t('sandbox.computes.isolation.standardHint'))"
                                            :ui="{ base: 'h-auto whitespace-normal max-w-xs' }"
                                        >
                                            <UBadge
                                                size="xs"
                                                variant="subtle"
                                                :color="isolationOf(compute) === 'strong' ? 'green' : 'amber'"
                                                class="whitespace-nowrap"
                                                data-testid="sandbox-compute-isolation"
                                                :data-isolation="isolationOf(compute)"
                                            >
                                                <UIcon
                                                    :name="isolationOf(compute) === 'strong' ? 'i-heroicons-shield-check' : 'i-heroicons-exclamation-triangle'"
                                                    class="w-3 h-3 me-1"
                                                />
                                                {{ $t(`sandbox.computes.isolation.${isolationOf(compute)}`) }}
                                            </UBadge>
                                        </UTooltip>
                                        <div
                                            v-if="isolationOf(compute) === 'standard' && compute.info?.isolation_detail"
                                            dir="auto"
                                            class="mt-1 max-w-[11rem] text-xs text-amber-700 dark:text-amber-300 line-clamp-2"
                                            :title="compute.info.isolation_detail"
                                        >{{ compute.info.isolation_detail }}</div>
                                        <div v-if="otherCapabilities(compute).length" class="mt-1 w-40 text-xs text-ink-muted" data-testid="sandbox-compute-capabilities">
                                            {{ otherCapabilities(compute).join(' · ') }}
                                        </div>
                                    </template>
                                    <span v-else class="bow-cell-empty">—</span>
                                </td>

                                <!-- Bootstrap -->
                                <td class="whitespace-nowrap">
                                    <button
                                        type="button"
                                        class="inline-flex items-center gap-1.5 text-sm hover:underline"
                                        :class="bootstrapLook(compute).class"
                                        data-testid="sandbox-compute-bootstrap-status"
                                        :data-bootstrap="compute.bootstrap_status"
                                        @click="openBootstrap(compute, false)"
                                    >
                                        <Spinner v-if="compute.bootstrap_status === 'running'" class="w-3.5 h-3.5" />
                                        <UIcon v-else :name="bootstrapLook(compute).icon" class="w-4 h-4" />
                                        {{ $t(`sandbox.computes.bootstrapStatus.${bootstrapKey(compute.bootstrap_status)}`) }}
                                    </button>
                                </td>

                                <!-- Running sandboxes out of capacity -->
                                <td class="whitespace-nowrap nums" data-testid="sandbox-compute-usage">
                                    <span dir="ltr">{{ compute.running_sandboxes }} / {{ compute.capacity }}</span>
                                </td>

                                <!-- Enable / disable. Enabling needs a finished bootstrap. -->
                                <td>
                                    <UTooltip
                                        :text="enableTooltip(compute)"
                                        :prevent="!enableTooltip(compute)"
                                        :ui="{ base: 'h-auto whitespace-normal max-w-xs' }"
                                    >
                                        <UToggle
                                            :model-value="compute.enabled"
                                            :disabled="!canToggle(compute) || togglingId === compute.id"
                                            :aria-label="compute.enabled ? $t('sandbox.computes.disable') : $t('sandbox.computes.enable')"
                                            data-testid="sandbox-compute-enabled"
                                            @update:model-value="setEnabled(compute, $event)"
                                        />
                                    </UTooltip>
                                </td>

                                <td class="whitespace-nowrap text-end">
                                    <div class="inline-flex items-center gap-1">
                                        <UTooltip :text="$t('sandbox.computes.testConnection')">
                                            <UButton
                                                color="gray"
                                                variant="soft"
                                                size="xs"
                                                icon="i-heroicons-signal"
                                                :aria-label="$t('sandbox.computes.testConnection')"
                                                data-testid="sandbox-compute-test"
                                                @click="openTest(compute)"
                                            >
                                                {{ $t('sandbox.computes.testShort') }}
                                            </UButton>
                                        </UTooltip>
                                        <UButton
                                            color="gray"
                                            variant="soft"
                                            size="xs"
                                            icon="i-heroicons-wrench-screwdriver"
                                            data-testid="sandbox-compute-bootstrap"
                                            @click="openBootstrap(compute, !isBootstrapped(compute))"
                                        >
                                            {{ isBootstrapped(compute) ? $t('sandbox.computes.repair') : $t('sandbox.computes.bootstrapAction') }}
                                        </UButton>
                                        <UTooltip :text="$t('common.edit')">
                                            <UButton
                                                color="gray"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-pencil-square"
                                                :aria-label="$t('common.edit')"
                                                data-testid="sandbox-compute-edit"
                                                @click="openEdit(compute)"
                                            />
                                        </UTooltip>
                                        <UTooltip :text="$t('common.delete')">
                                            <UButton
                                                color="red"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-trash"
                                                :aria-label="$t('common.delete')"
                                                data-testid="sandbox-compute-delete"
                                                @click="openDelete(compute)"
                                            />
                                        </UTooltip>
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="computes.length === 0 && !loadError" class="bow-table-static">
                                <td colspan="7">
                                    <SettingsEmpty
                                        icon="i-heroicons-server-stack"
                                        :title="$t('sandbox.computes.emptyTitle')"
                                        :description="$t('sandbox.computes.emptyHint')"
                                    >
                                        <UButton color="blue" icon="i-heroicons-plus" data-testid="sandbox-add-compute-empty" @click="openCreate">
                                            {{ $t('sandbox.computes.add') }}
                                        </UButton>
                                    </SettingsEmpty>
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>

        <SandboxComputeFormModal v-model="showForm" :types="types" :compute="editing" @saved="upsert" />
        <SandboxComputeTestModal v-model="showTest" :compute="selected" @updated="upsert" />
        <SandboxComputeBootstrapModal v-model="showBootstrap" :compute="selected" :auto-start="bootstrapAutoStart" @updated="upsert" />
        <SandboxComputeDeleteModal v-model="showDelete" :compute="selected" @deleted="onDeleted" />
    </div>
</template>

<script setup lang="ts">
import type { SandboxCompute, SandboxComputeType } from '~/composables/useSandboxApi'
import Spinner from '~/components/Spinner.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SandboxComputeFormModal from '~/components/sandbox/SandboxComputeFormModal.vue'
import SandboxComputeTestModal from '~/components/sandbox/SandboxComputeTestModal.vue'
import SandboxComputeBootstrapModal from '~/components/sandbox/SandboxComputeBootstrapModal.vue'
import SandboxComputeDeleteModal from '~/components/sandbox/SandboxComputeDeleteModal.vue'

const { t, te, locale } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const computes = ref<SandboxCompute[]>([])
const types = ref<SandboxComputeType[]>([])
const loading = ref(true)
const loadError = ref('')

const showForm = ref(false)
const showTest = ref(false)
const showBootstrap = ref(false)
const showDelete = ref(false)
const bootstrapAutoStart = ref(false)
const editing = ref<SandboxCompute | null>(null)
const selected = ref<SandboxCompute | null>(null)
const togglingId = ref<string | null>(null)

async function load() {
    loadError.value = ''
    try {
        const [list, computeTypes] = await Promise.all([
            call<SandboxCompute[]>('/computes'),
            call<SandboxComputeType[]>('/compute-types'),
        ])
        computes.value = list || []
        types.value = computeTypes || []
    } catch (error) {
        loadError.value = getErrorMessage(error, t('sandbox.computes.loadFailed'))
    } finally {
        loading.value = false
    }
}

// A bootstrap may be running without its dialog open (started in another tab,
// or the dialog was closed): keep those rows fresh until it ends.
let refreshTimer: ReturnType<typeof setTimeout> | undefined
function scheduleRefresh() {
    clearTimeout(refreshTimer)
    if (!computes.value.some(compute => compute.bootstrap_status === 'running')) return
    refreshTimer = setTimeout(async () => {
        try { computes.value = (await call<SandboxCompute[]>('/computes')) || [] } catch { /* next round */ }
        scheduleRefresh()
    }, 3000)
}
watch(computes, scheduleRefresh, { deep: false })
onBeforeUnmount(() => clearTimeout(refreshTimer))

function upsert(compute: SandboxCompute) {
    const index = computes.value.findIndex(item => item.id === compute.id)
    computes.value = index === -1
        ? [...computes.value, compute]
        : computes.value.map(item => (item.id === compute.id ? compute : item))
    if (selected.value?.id === compute.id) selected.value = compute
}
function onDeleted(compute: SandboxCompute) {
    computes.value = computes.value.filter(item => item.id !== compute.id)
}

function openCreate() { editing.value = null; showForm.value = true }
function openEdit(compute: SandboxCompute) { editing.value = compute; showForm.value = true }
function openTest(compute: SandboxCompute) { selected.value = compute; showTest.value = true }
function openDelete(compute: SandboxCompute) { selected.value = compute; showDelete.value = true }
function openBootstrap(compute: SandboxCompute, autoStart: boolean) {
    selected.value = compute
    bootstrapAutoStart.value = autoStart && compute.bootstrap_status !== 'running'
    showBootstrap.value = true
}

const typeTitle = (name: string) => types.value.find(type => type.name === name)?.title || name

const isBootstrapped = (compute: SandboxCompute) =>
    compute.bootstrap_status === 'ready' || !!compute.bootstrapped_at

// Disabling is always possible; enabling needs a finished bootstrap (the API
// answers sandbox.compute_not_ready otherwise).
const canToggle = (compute: SandboxCompute) => compute.enabled || compute.bootstrap_status === 'ready'
const enableTooltip = (compute: SandboxCompute) =>
    canToggle(compute) ? '' : t('sandbox.computes.enableNeedsBootstrap')

async function setEnabled(compute: SandboxCompute, enabled: boolean) {
    if (togglingId.value) return
    togglingId.value = compute.id
    try {
        upsert(await call<SandboxCompute>(`/computes/${compute.id}`, { method: 'PUT', body: { enabled } }))
        toast.add({ title: enabled ? t('sandbox.computes.toast.enabled') : t('sandbox.computes.toast.disabled'), color: 'green' })
    } catch (error) {
        toast.add({ title: getErrorMessage(error, t('sandbox.computes.toast.saveFailed')), color: 'red' })
    } finally {
        togglingId.value = null
    }
}

const statusKey = (status: string) => (['healthy', 'unreachable'].includes(status) ? status : 'unknown')
const statusColor = (status: string) => (status === 'healthy' ? 'green' : status === 'unreachable' ? 'red' : 'gray')

function isolationOf(compute: SandboxCompute): 'strong' | 'standard' | '' {
    if (compute.info?.isolation === 'strong' || compute.info?.isolation === 'standard') return compute.info.isolation
    // Before the first check there is nothing to say; after a bootstrap the
    // capability list is the fact.
    if (compute.bootstrap_status !== 'ready') return ''
    return compute.capabilities?.includes('strong_isolation') ? 'strong' : 'standard'
}
function otherCapabilities(compute: SandboxCompute) {
    return (compute.capabilities || [])
        .filter(capability => capability !== 'strong_isolation')
        .map(capability => (te(`sandbox.capabilities.${capability}`) ? t(`sandbox.capabilities.${capability}`) : capability.replace(/_/g, ' ')))
}

const bootstrapKey = (status: string) => (['running', 'ready', 'failed'].includes(status) ? status : 'none')
function bootstrapLook(compute: SandboxCompute) {
    switch (compute.bootstrap_status) {
        case 'ready': return { icon: 'i-heroicons-check-circle', class: 'text-green-700 dark:text-green-400' }
        case 'failed': return { icon: 'i-heroicons-x-circle', class: 'text-red-600 dark:text-red-400' }
        case 'running': return { icon: '', class: 'text-blue-600 dark:text-blue-400' }
        default: return { icon: 'i-heroicons-minus-circle', class: 'text-ink-muted' }
    }
}

// The API's timestamps are UTC without a zone suffix.
function parseDate(value: string) {
    return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
}
function formatDate(value: string) {
    return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(parseDate(value))
}
function timeAgo(value: string) {
    const seconds = Math.round((parseDate(value).getTime() - Date.now()) / 1000)
    const format = new Intl.RelativeTimeFormat(locale.value, { numeric: 'auto' })
    const abs = Math.abs(seconds)
    if (abs < 60) return format.format(Math.min(seconds, 0), 'second')
    if (abs < 3600) return format.format(Math.round(seconds / 60), 'minute')
    if (abs < 86400) return format.format(Math.round(seconds / 3600), 'hour')
    return format.format(Math.round(seconds / 86400), 'day')
}

onMounted(load)
</script>
