<template>
    <div data-testid="sandbox-environments">
        <Teleport to="#settings-page-actions" defer>
            <UButton color="blue" icon="i-heroicons-plus" data-testid="sandbox-add-environment" @click="openCreate">
                {{ $t('sandbox.environments.add') }}
            </UButton>
        </Teleport>

        <p v-if="loadError" class="mb-4 rounded-lg border border-line p-4 text-sm text-red-600 dark:text-red-400" role="alert" data-testid="sandbox-environments-error">
            {{ loadError }}
        </p>

        <div class="bow-table-card">
            <div class="overflow-x-auto">
                <table class="bow-table">
                    <thead v-if="loading || environments.length">
                        <tr>
                            <th>{{ $t('sandbox.environments.col.name') }}</th>
                            <th>{{ $t('sandbox.environments.col.image') }}</th>
                            <th>{{ $t('sandbox.environments.col.network') }}</th>
                            <th>{{ $t('sandbox.environments.col.resources') }}</th>
                            <th>{{ $t('sandbox.environments.col.access') }}</th>
                            <th>{{ $t('sandbox.environments.col.enabled') }}</th>
                            <th class="text-end"><span class="sr-only">{{ $t('sandbox.environments.col.actions') }}</span></th>
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
                            <tr v-for="env in environments" :key="env.id" data-testid="sandbox-environment-row" :data-environment-name="env.name">
                                <td>
                                    <div class="flex items-center gap-2">
                                        <span class="bow-cell-primary whitespace-nowrap" data-testid="sandbox-environment-name-cell">{{ env.name }}</span>
                                        <UBadge v-if="env.is_default" size="xs" color="blue" variant="subtle" data-testid="sandbox-environment-default">
                                            {{ $t('sandbox.environments.defaultBadge') }}
                                        </UBadge>
                                        <UTooltip v-if="env.warnings?.length" :ui="{ base: 'h-auto whitespace-normal max-w-sm' }">
                                            <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 text-amber-500 dark:text-amber-400" data-testid="sandbox-environment-warning" :aria-label="env.warnings.join(' ')" />
                                            <template #text>
                                                <span v-for="warning in env.warnings" :key="warning" class="block">{{ warning }}</span>
                                            </template>
                                        </UTooltip>
                                    </div>
                                    <div v-if="env.description" dir="auto" class="mt-0.5 max-w-xs text-xs text-ink-muted line-clamp-2" :title="env.description">{{ env.description }}</div>
                                </td>
                                <td>
                                    <code dir="ltr" class="font-mono text-xs text-ink-2 break-all">{{ env.image }}</code>
                                </td>
                                <td class="whitespace-nowrap">
                                    <UBadge size="xs" variant="subtle" :color="networkColor(env.network_policy)" data-testid="sandbox-environment-network" :data-policy="env.network_policy">
                                        {{ networkLabel(env.network_policy) }}
                                    </UBadge>
                                    <UBadge v-if="env.require_strong_isolation" size="xs" color="gray" class="ms-1" :title="$t('sandbox.environments.form.strongIsolation')">
                                        <UIcon name="i-heroicons-shield-check" class="w-3 h-3" />
                                    </UBadge>
                                </td>
                                <td class="text-xs text-ink-2 nums">
                                    <div class="whitespace-nowrap">{{ $t('sandbox.environments.resources', { cpus: formatNumber(env.cpus), memory: formatSize(env.memory_mb), disk: formatSize(env.disk_mb) }) }}</div>
                                    <div class="whitespace-nowrap text-ink-muted">{{ $t('sandbox.environments.timeout', { duration: formatDuration(env.run_timeout_seconds) }) }}</div>
                                </td>
                                <td class="whitespace-nowrap">
                                    <button
                                        type="button"
                                        class="text-sm hover:underline"
                                        :class="env.access?.length ? 'text-blue-600 dark:text-blue-400 font-medium' : 'text-ink-muted'"
                                        data-testid="sandbox-environment-access"
                                        :data-count="env.access?.length || 0"
                                        @click="openEdit(env)"
                                    >
                                        {{ accessSummary(env) }}
                                    </button>
                                </td>
                                <td>
                                    <UToggle
                                        :model-value="env.enabled"
                                        :disabled="togglingId === env.id"
                                        :aria-label="$t('sandbox.environments.form.enabled')"
                                        data-testid="sandbox-environment-enabled"
                                        @update:model-value="setEnabled(env, $event)"
                                    />
                                </td>
                                <td class="whitespace-nowrap text-end">
                                    <div class="bow-row-actions inline-flex gap-1">
                                        <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-pencil-square" :aria-label="$t('common.edit')" data-testid="sandbox-environment-edit" @click="openEdit(env)" />
                                        <UButton color="red" variant="ghost" size="xs" icon="i-heroicons-trash" :aria-label="$t('common.delete')" data-testid="sandbox-environment-delete" @click="confirmDelete = env" />
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="environments.length === 0 && !loadError" class="bow-table-static">
                                <td colspan="7">
                                    <SettingsEmpty icon="i-heroicons-cube" :title="$t('sandbox.environments.emptyTitle')" :description="$t('sandbox.environments.emptyHint')" />
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>

        <SandboxEnvironmentFormModal
            v-model="showForm"
            :environment="editing"
            :roles="roles"
            :groups="groups"
            :users="users"
            @saved="upsert"
        />

        <!-- Delete confirmation -->
        <UModal :model-value="!!confirmDelete" @update:model-value="!$event && (confirmDelete = null)">
            <div class="p-6" data-testid="sandbox-delete-environment-modal">
                <h3 class="text-lg font-semibold text-ink">{{ $t('sandbox.environments.delete.title', { name: confirmDelete?.name || '' }) }}</h3>
                <p class="mt-1 text-sm text-ink-muted leading-relaxed">{{ $t('sandbox.environments.delete.hint') }}</p>
                <p v-if="deleteError" class="mt-3 text-sm text-red-600 dark:text-red-400 break-words" role="alert">{{ deleteError }}</p>
                <p v-if="deleteInUse" class="mt-1 text-xs text-ink-muted leading-relaxed" data-testid="sandbox-delete-environment-in-use">{{ $t('sandbox.environments.delete.inUseHint') }}</p>
                <div class="flex justify-end gap-2 pt-5">
                    <UButton variant="ghost" color="gray" :disabled="deleting" @click="confirmDelete = null">{{ $t('common.cancel') }}</UButton>
                    <UButton color="red" :loading="deleting" data-testid="sandbox-delete-environment-confirm" @click="remove(deleteInUse)">
                        {{ deleteInUse ? $t('sandbox.environments.delete.withSandboxes') : $t('sandbox.environments.delete.confirm') }}
                    </UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import type { SandboxEnvironment, SandboxPrincipalOption } from '~/composables/useSandboxApi'
import Spinner from '~/components/Spinner.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SandboxEnvironmentFormModal from '~/components/sandbox/SandboxEnvironmentFormModal.vue'

const { t, locale } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()
const { organization } = useOrganization()

const environments = ref<SandboxEnvironment[]>([])
const loading = ref(true)
const loadError = ref('')
const roles = ref<SandboxPrincipalOption[]>([])
const groups = ref<SandboxPrincipalOption[]>([])
const users = ref<SandboxPrincipalOption[]>([])

const showForm = ref(false)
const editing = ref<SandboxEnvironment | null>(null)
const confirmDelete = ref<SandboxEnvironment | null>(null)
const deleting = ref(false)
const deleteError = ref('')
// The environment still has sandboxes: the next confirm deletes them with it.
const deleteInUse = ref(false)
const togglingId = ref<string | null>(null)

async function load() {
    loadError.value = ''
    try {
        environments.value = (await call<SandboxEnvironment[]>('/environments')) || []
    } catch (error) {
        loadError.value = getErrorMessage(error, t('sandbox.environments.loadFailed'))
    } finally {
        loading.value = false
    }
}

const cap = (name?: string) => (name ? name.charAt(0).toUpperCase() + name.slice(1) : '')

// Who can be granted an environment: the same lists the Members, Roles and
// Groups pages read. Each is optional (a deployment without custom roles has
// no groups); a list that fails to load just offers nothing.
async function loadPrincipals() {
    const organizationId = organization.value?.id
    if (!organizationId) return
    const base = `/organizations/${organizationId}`
    const [roleList, groupList, memberList] = await Promise.all(
        ['roles', 'groups', 'members'].map(async (path) => {
            try {
                const { data } = await useMyFetch(`${base}/${path}`)
                return (data.value as any[]) || []
            } catch { return [] }
        })
    )
    roles.value = roleList.map(role => ({ id: role.id, label: cap(role.name), hint: role.description || '' }))
    groups.value = groupList.map(group => ({ id: group.id, label: group.name, hint: group.description || '' }))
    users.value = memberList
        .filter(member => member.user_id)
        .map(member => ({
            id: member.user_id,
            label: member.user?.name || member.user?.email || member.email || member.user_id,
            hint: member.user?.name ? (member.user?.email || '') : '',
        }))
}

function upsert(environment: SandboxEnvironment) {
    const known = environments.value.some(item => item.id === environment.id)
    // Only one environment is the default: the server clears the others.
    const rest = environments.value.map(item =>
        item.id === environment.id ? environment
            : (environment.is_default && item.is_default ? { ...item, is_default: false } : item))
    environments.value = known ? rest : [...rest, environment]
}

function openCreate() { editing.value = null; showForm.value = true }
function openEdit(environment: SandboxEnvironment) { editing.value = environment; showForm.value = true }

async function setEnabled(environment: SandboxEnvironment, enabled: boolean) {
    if (togglingId.value) return
    togglingId.value = environment.id
    try {
        const saved = await call<SandboxEnvironment>(`/environments/${environment.id}`, { method: 'PUT', body: { enabled } })
        upsert(saved)
    } catch (error) {
        toast.add({ title: getErrorMessage(error, t('sandbox.environments.toast.saveFailed')), color: 'red' })
    } finally {
        togglingId.value = null
    }
}

async function remove(withSandboxes = false) {
    const environment = confirmDelete.value
    if (!environment || deleting.value) return
    deleting.value = true
    deleteError.value = ''
    try {
        await call(`/environments/${environment.id}`, {
            method: 'DELETE',
            query: withSandboxes ? { delete_sandboxes: 'true' } : undefined,
        })
        environments.value = environments.value.filter(item => item.id !== environment.id)
        toast.add({ title: t('sandbox.environments.toast.deleted'), color: 'green' })
        confirmDelete.value = null
    } catch (error) {
        deleteError.value = getErrorMessage(error, t('sandbox.environments.toast.deleteFailed'))
        deleteInUse.value = getErrorCode(error) === 'sandbox.environment_in_use' && !withSandboxes
    } finally {
        deleting.value = false
    }
}
watch(confirmDelete, () => { deleteError.value = ''; deleteInUse.value = false })

const NETWORK_COLORS: Record<string, string> = { none: 'gray', registries: 'blue', allowlist: 'blue', full: 'amber' }
const networkColor = (policy: string) => (NETWORK_COLORS[policy] || 'gray') as any
const networkLabel = (policy: string) => (policy in NETWORK_COLORS ? t(`sandbox.network.${policy}`) : policy)

function accessSummary(environment: SandboxEnvironment) {
    const count = environment.access?.length || 0
    if (!count) return t('sandbox.environments.accessNone')
    return count === 1 ? t('sandbox.environments.accessOne') : t('sandbox.environments.accessMany', { n: count })
}

const formatNumber = (value: number) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 2 }).format(value)
function formatSize(megabytes: number) {
    const unit = megabytes >= 1024 ? 'gigabyte' : 'megabyte'
    const value = megabytes >= 1024 ? megabytes / 1024 : megabytes
    return new Intl.NumberFormat(locale.value, { style: 'unit', unit, unitDisplay: 'short', maximumFractionDigits: 1 }).format(value)
}
function formatDuration(seconds: number) {
    const unit = seconds >= 60 && seconds % 60 === 0 ? 'minute' : 'second'
    const value = unit === 'minute' ? seconds / 60 : seconds
    return new Intl.NumberFormat(locale.value, { style: 'unit', unit, unitDisplay: 'short' }).format(value)
}

onMounted(() => { load(); loadPrincipals() })
</script>
