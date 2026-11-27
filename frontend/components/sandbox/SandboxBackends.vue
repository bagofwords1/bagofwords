<template>
    <div data-testid="sandbox-backends">
        <!-- Primary action lives in the page header (settings shell). -->
        <Teleport to="#settings-page-actions" defer>
            <UButton color="blue" icon="i-heroicons-plus" data-testid="sandbox-add-backend" @click="openCreate">
                {{ $t('sandbox.backends.add') }}
            </UButton>
        </Teleport>

        <p v-if="loadError" class="mb-4 rounded-lg border border-line p-4 text-sm text-red-600 dark:text-red-400" role="alert" data-testid="sandbox-backends-error">
            {{ loadError }}
        </p>

        <div class="bow-table-card">
            <div class="overflow-x-auto">
                <table class="bow-table">
                    <thead v-if="loading || backends.length">
                        <tr>
                            <th>{{ $t('sandbox.backends.col.name') }}</th>
                            <th>{{ $t('sandbox.backends.col.status') }}</th>
                            <th>{{ $t('sandbox.backends.col.supports') }}</th>
                            <th>{{ $t('sandbox.backends.col.bootstrap') }}</th>
                            <th>{{ $t('sandbox.backends.col.sandboxes') }}</th>
                            <th>{{ $t('sandbox.backends.col.enabled') }}</th>
                            <th class="text-end"><span class="sr-only">{{ $t('sandbox.backends.col.actions') }}</span></th>
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
                            <tr v-for="backend in backends" :key="backend.id" data-testid="sandbox-backend-row" :data-backend-name="backend.name">
                                <!-- Name, type, labels -->
                                <td>
                                    <div class="flex items-start gap-2">
                                        <span class="flex items-center justify-center w-7 h-7 rounded-md bg-surface-muted text-ink-muted shrink-0">
                                            <UIcon :name="backend.type === 'fake' ? 'i-heroicons-beaker' : 'i-heroicons-server'" class="h-4 w-4" />
                                        </span>
                                        <div class="min-w-[9rem]">
                                            <div class="bow-cell-primary whitespace-nowrap" data-testid="sandbox-backend-name-cell">{{ backend.name }}</div>
                                            <div class="text-xs text-ink-muted max-w-[12rem]" data-testid="sandbox-backend-type-cell">{{ typeTitle(backend.type) }}</div>
                                            <div v-if="backend.labels?.length" class="mt-1 flex flex-wrap gap-1">
                                                <UBadge v-for="label in backend.labels" :key="label" size="xs" color="gray">{{ label }}</UBadge>
                                            </div>
                                        </div>
                                    </div>
                                </td>

                                <!-- Health -->
                                <td>
                                    <UBadge size="xs" variant="subtle" :color="statusColor(backend.status)" data-testid="sandbox-backend-status">
                                        {{ $t(`sandbox.backends.status.${statusKey(backend.status)}`) }}
                                    </UBadge>
                                    <div
                                        v-if="backend.last_error && backend.status !== 'healthy'"
                                        dir="auto"
                                        class="mt-1 max-w-[12rem] text-xs text-red-600 dark:text-red-400 line-clamp-2 break-words"
                                        :title="backend.last_error"
                                        data-testid="sandbox-backend-last-error"
                                    >{{ backend.last_error }}</div>
                                    <div class="mt-1 text-xs text-ink-muted whitespace-nowrap nums" data-testid="sandbox-backend-last-check">
                                        <span v-if="backend.last_checked_at" :title="formatDate(backend.last_checked_at)">{{ $t('sandbox.backends.checkedAgo', { when: timeAgo(backend.last_checked_at) }) }}</span>
                                        <span v-else>{{ $t('sandbox.backends.neverChecked') }}</span>
                                    </div>
                                </td>

                                <!-- What it supports -->
                                <td>
                                    <template v-if="isolationOf(backend)">
                                        <UTooltip
                                            :text="isolationOf(backend) === 'strong' ? $t('sandbox.backends.isolation.strongHint') : (backend.info?.isolation_detail || $t('sandbox.backends.isolation.standardHint'))"
                                            :ui="{ base: 'h-auto whitespace-normal max-w-xs' }"
                                        >
                                            <UBadge
                                                size="xs"
                                                variant="subtle"
                                                :color="isolationOf(backend) === 'strong' ? 'green' : 'amber'"
                                                class="whitespace-nowrap"
                                                data-testid="sandbox-backend-isolation"
                                                :data-isolation="isolationOf(backend)"
                                            >
                                                <UIcon
                                                    :name="isolationOf(backend) === 'strong' ? 'i-heroicons-shield-check' : 'i-heroicons-exclamation-triangle'"
                                                    class="w-3 h-3 me-1"
                                                />
                                                {{ $t(`sandbox.backends.isolation.${isolationOf(backend)}`) }}
                                            </UBadge>
                                        </UTooltip>
                                        <div
                                            v-if="isolationOf(backend) === 'standard' && backend.info?.isolation_detail"
                                            dir="auto"
                                            class="mt-1 max-w-[11rem] text-xs text-amber-700 dark:text-amber-300 line-clamp-2"
                                            :title="backend.info.isolation_detail"
                                        >{{ backend.info.isolation_detail }}</div>
                                        <div v-if="otherCapabilities(backend).length" class="mt-1 w-40 text-xs text-ink-muted" data-testid="sandbox-backend-capabilities">
                                            {{ otherCapabilities(backend).join(' · ') }}
                                        </div>
                                    </template>
                                    <span v-else class="bow-cell-empty">—</span>
                                </td>

                                <!-- Bootstrap -->
                                <td class="whitespace-nowrap">
                                    <button
                                        type="button"
                                        class="inline-flex items-center gap-1.5 text-sm hover:underline"
                                        :class="bootstrapLook(backend).class"
                                        data-testid="sandbox-backend-bootstrap-status"
                                        :data-bootstrap="backend.bootstrap_status"
                                        @click="openBootstrap(backend, false)"
                                    >
                                        <Spinner v-if="backend.bootstrap_status === 'running'" class="w-3.5 h-3.5" />
                                        <UIcon v-else :name="bootstrapLook(backend).icon" class="w-4 h-4" />
                                        {{ $t(`sandbox.backends.bootstrapStatus.${bootstrapKey(backend.bootstrap_status)}`) }}
                                    </button>
                                </td>

                                <!-- Running sandboxes out of capacity -->
                                <td class="whitespace-nowrap nums" data-testid="sandbox-backend-usage">
                                    <span dir="ltr">{{ backend.running_sandboxes }} / {{ backend.capacity }}</span>
                                </td>

                                <!-- Enable / disable. Enabling needs a finished bootstrap. -->
                                <td>
                                    <UTooltip
                                        :text="enableTooltip(backend)"
                                        :prevent="!enableTooltip(backend)"
                                        :ui="{ base: 'h-auto whitespace-normal max-w-xs' }"
                                    >
                                        <UToggle
                                            :model-value="backend.enabled"
                                            :disabled="!canToggle(backend) || togglingId === backend.id"
                                            :aria-label="backend.enabled ? $t('sandbox.backends.disable') : $t('sandbox.backends.enable')"
                                            data-testid="sandbox-backend-enabled"
                                            @update:model-value="setEnabled(backend, $event)"
                                        />
                                    </UTooltip>
                                </td>

                                <td class="whitespace-nowrap text-end">
                                    <div class="inline-flex items-center gap-1">
                                        <UTooltip :text="$t('sandbox.backends.testConnection')">
                                            <UButton
                                                color="gray"
                                                variant="soft"
                                                size="xs"
                                                icon="i-heroicons-signal"
                                                :aria-label="$t('sandbox.backends.testConnection')"
                                                data-testid="sandbox-backend-test"
                                                @click="openTest(backend)"
                                            >
                                                {{ $t('sandbox.backends.testShort') }}
                                            </UButton>
                                        </UTooltip>
                                        <UButton
                                            color="gray"
                                            variant="soft"
                                            size="xs"
                                            icon="i-heroicons-wrench-screwdriver"
                                            data-testid="sandbox-backend-bootstrap"
                                            @click="openBootstrap(backend, !isBootstrapped(backend))"
                                        >
                                            {{ isBootstrapped(backend) ? $t('sandbox.backends.repair') : $t('sandbox.backends.bootstrapAction') }}
                                        </UButton>
                                        <UTooltip :text="$t('common.edit')">
                                            <UButton
                                                color="gray"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-pencil-square"
                                                :aria-label="$t('common.edit')"
                                                data-testid="sandbox-backend-edit"
                                                @click="openEdit(backend)"
                                            />
                                        </UTooltip>
                                        <UTooltip :text="$t('common.delete')">
                                            <UButton
                                                color="red"
                                                variant="ghost"
                                                size="xs"
                                                icon="i-heroicons-trash"
                                                :aria-label="$t('common.delete')"
                                                data-testid="sandbox-backend-delete"
                                                @click="openDelete(backend)"
                                            />
                                        </UTooltip>
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="backends.length === 0 && !loadError" class="bow-table-static">
                                <td colspan="7">
                                    <SettingsEmpty
                                        icon="i-heroicons-server-stack"
                                        :title="$t('sandbox.backends.emptyTitle')"
                                        :description="$t('sandbox.backends.emptyHint')"
                                    >
                                        <UButton color="blue" icon="i-heroicons-plus" data-testid="sandbox-add-backend-empty" @click="openCreate">
                                            {{ $t('sandbox.backends.add') }}
                                        </UButton>
                                    </SettingsEmpty>
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>

        <SandboxBackendFormModal v-model="showForm" :types="types" :backend="editing" @saved="upsert" />
        <SandboxBackendTestModal v-model="showTest" :backend="selected" @updated="upsert" />
        <SandboxBackendBootstrapModal v-model="showBootstrap" :backend="selected" :auto-start="bootstrapAutoStart" @updated="upsert" />
        <SandboxBackendDeleteModal v-model="showDelete" :backend="selected" @deleted="onDeleted" />
    </div>
</template>

<script setup lang="ts">
import type { SandboxBackend, SandboxBackendType } from '~/composables/useSandboxApi'
import Spinner from '~/components/Spinner.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SandboxBackendFormModal from '~/components/sandbox/SandboxBackendFormModal.vue'
import SandboxBackendTestModal from '~/components/sandbox/SandboxBackendTestModal.vue'
import SandboxBackendBootstrapModal from '~/components/sandbox/SandboxBackendBootstrapModal.vue'
import SandboxBackendDeleteModal from '~/components/sandbox/SandboxBackendDeleteModal.vue'

const { t, te, locale } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const backends = ref<SandboxBackend[]>([])
const types = ref<SandboxBackendType[]>([])
const loading = ref(true)
const loadError = ref('')

const showForm = ref(false)
const showTest = ref(false)
const showBootstrap = ref(false)
const showDelete = ref(false)
const bootstrapAutoStart = ref(false)
const editing = ref<SandboxBackend | null>(null)
const selected = ref<SandboxBackend | null>(null)
const togglingId = ref<string | null>(null)

async function load() {
    loadError.value = ''
    try {
        const [list, backendTypes] = await Promise.all([
            call<SandboxBackend[]>('/backends'),
            call<SandboxBackendType[]>('/backend-types'),
        ])
        backends.value = list || []
        types.value = backendTypes || []
    } catch (error) {
        loadError.value = getErrorMessage(error, t('sandbox.backends.loadFailed'))
    } finally {
        loading.value = false
    }
}

// A bootstrap may be running without its dialog open (started in another tab,
// or the dialog was closed): keep those rows fresh until it ends.
let refreshTimer: ReturnType<typeof setTimeout> | undefined
function scheduleRefresh() {
    clearTimeout(refreshTimer)
    if (!backends.value.some(backend => backend.bootstrap_status === 'running')) return
    refreshTimer = setTimeout(async () => {
        try { backends.value = (await call<SandboxBackend[]>('/backends')) || [] } catch { /* next round */ }
        scheduleRefresh()
    }, 3000)
}
watch(backends, scheduleRefresh, { deep: false })
onBeforeUnmount(() => clearTimeout(refreshTimer))

function upsert(backend: SandboxBackend) {
    const index = backends.value.findIndex(item => item.id === backend.id)
    backends.value = index === -1
        ? [...backends.value, backend]
        : backends.value.map(item => (item.id === backend.id ? backend : item))
    if (selected.value?.id === backend.id) selected.value = backend
}
function onDeleted(backend: SandboxBackend) {
    backends.value = backends.value.filter(item => item.id !== backend.id)
}

function openCreate() { editing.value = null; showForm.value = true }
function openEdit(backend: SandboxBackend) { editing.value = backend; showForm.value = true }
function openTest(backend: SandboxBackend) { selected.value = backend; showTest.value = true }
function openDelete(backend: SandboxBackend) { selected.value = backend; showDelete.value = true }
function openBootstrap(backend: SandboxBackend, autoStart: boolean) {
    selected.value = backend
    bootstrapAutoStart.value = autoStart && backend.bootstrap_status !== 'running'
    showBootstrap.value = true
}

const typeTitle = (name: string) => types.value.find(type => type.name === name)?.title || name

const isBootstrapped = (backend: SandboxBackend) =>
    backend.bootstrap_status === 'ready' || !!backend.bootstrapped_at

// Disabling is always possible; enabling needs a finished bootstrap (the API
// answers sandbox.backend_not_ready otherwise).
const canToggle = (backend: SandboxBackend) => backend.enabled || backend.bootstrap_status === 'ready'
const enableTooltip = (backend: SandboxBackend) =>
    canToggle(backend) ? '' : t('sandbox.backends.enableNeedsBootstrap')

async function setEnabled(backend: SandboxBackend, enabled: boolean) {
    if (togglingId.value) return
    togglingId.value = backend.id
    try {
        upsert(await call<SandboxBackend>(`/backends/${backend.id}`, { method: 'PUT', body: { enabled } }))
        toast.add({ title: enabled ? t('sandbox.backends.toast.enabled') : t('sandbox.backends.toast.disabled'), color: 'green' })
    } catch (error) {
        toast.add({ title: getErrorMessage(error, t('sandbox.backends.toast.saveFailed')), color: 'red' })
    } finally {
        togglingId.value = null
    }
}

const statusKey = (status: string) => (['healthy', 'unreachable'].includes(status) ? status : 'unknown')
const statusColor = (status: string) => (status === 'healthy' ? 'green' : status === 'unreachable' ? 'red' : 'gray')

function isolationOf(backend: SandboxBackend): 'strong' | 'standard' | '' {
    if (backend.info?.isolation === 'strong' || backend.info?.isolation === 'standard') return backend.info.isolation
    // Before the first check there is nothing to say; after a bootstrap the
    // capability list is the fact.
    if (backend.bootstrap_status !== 'ready') return ''
    return backend.capabilities?.includes('strong_isolation') ? 'strong' : 'standard'
}
function otherCapabilities(backend: SandboxBackend) {
    return (backend.capabilities || [])
        .filter(capability => capability !== 'strong_isolation')
        .map(capability => (te(`sandbox.capabilities.${capability}`) ? t(`sandbox.capabilities.${capability}`) : capability.replace(/_/g, ' ')))
}

const bootstrapKey = (status: string) => (['running', 'ready', 'failed'].includes(status) ? status : 'none')
function bootstrapLook(backend: SandboxBackend) {
    switch (backend.bootstrap_status) {
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
