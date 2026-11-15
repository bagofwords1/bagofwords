<template>
    <div>
        <!-- Primary action lives in the page header (settings shell). -->
        <Teleport to="#settings-page-actions" defer>
            <UButton v-if="useCan('manage_settings')" color="blue" icon="i-heroicons-plus" @click="openCreateModal">
                {{ $t('quotaPolicies.newQuota') }}
            </UButton>
        </Teleport>

        <div class="mb-4">
            <UInput
                v-model="searchQuery"
                icon="i-heroicons-magnifying-glass"
                :placeholder="$t('quotaPolicies.searchPlaceholder')"
                class="w-full sm:max-w-xs"
            />
        </div>

        <div class="bow-table-card">
            <div class="overflow-x-auto">
                <table class="bow-table">
                    <thead v-if="isLoading || filteredPolicies.length">
                        <tr>
                            <th>{{ $t('quotaPolicies.colPolicy') }}</th>
                            <th>{{ $t('quotaPolicies.colLimits') }}</th>
                            <th>{{ $t('quotaPolicies.colAssignments') }}</th>
                            <th>{{ $t('quotaPolicies.colOverrides') }}</th>
                            <th>{{ $t('quotaPolicies.colStatus') }}</th>
                            <th v-if="useCan('manage_settings')" class="text-end"><span class="sr-only">{{ $t('quotaPolicies.colActions') }}</span></th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-if="isLoading" class="bow-table-static">
                            <td :colspan="useCan('manage_settings') ? 6 : 5" class="text-center">
                                <div class="flex items-center justify-center text-ink-muted">
                                    <Spinner class="w-4 h-4 me-2" />
                                    <span class="text-sm">{{ $t('quotaPolicies.loading') }}</span>
                                </div>
                            </td>
                        </tr>
                        <template v-else>
                            <tr v-for="policy in filteredPolicies" :key="policy.id">
                                <td>
                                    <div class="flex items-center gap-3">
                                        <span class="flex items-center justify-center w-7 h-7 rounded-md bg-surface-muted text-ink-muted shrink-0">
                                            <Icon name="heroicons:chart-bar-square" class="h-4 w-4" />
                                        </span>
                                        <div class="min-w-0">
                                            <div class="bow-cell-primary">{{ policy.name }}</div>
                                            <div v-if="policy.description" class="bow-cell-secondary truncate">{{ policy.description }}</div>
                                        </div>
                                    </div>
                                </td>
                                <td>
                                    <dl class="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs whitespace-nowrap">
                                        <div class="flex gap-1.5"><dt class="text-ink-muted">{{ $t('quotaPolicies.tokensShort') }}</dt><dd class="text-ink-2 tabular-nums">{{ formatCountLimit(policy.monthly_token_limit) }}</dd></div>
                                        <div class="flex gap-1.5"><dt class="text-ink-muted">{{ $t('quotaPolicies.queriesShort') }}</dt><dd class="text-ink-2 tabular-nums">{{ formatCountLimit(policy.monthly_query_limit) }}</dd></div>
                                        <div class="flex gap-1.5"><dt class="text-ink-muted">{{ $t('quotaPolicies.dataShort') }}</dt><dd class="text-ink-2 tabular-nums">{{ formatBytesLimit(policy.monthly_data_bytes_limit) }}</dd></div>
                                        <div class="flex gap-1.5"><dt class="text-ink-muted">{{ $t('quotaPolicies.spendShort') }}</dt><dd class="text-ink-2 tabular-nums">{{ formatUsdLimit(policy.monthly_spend_limit_usd) }}</dd></div>
                                    </dl>
                                </td>
                                <td>
                                    <div class="flex gap-1 flex-wrap items-center">
                                        <UBadge
                                            v-for="item in assignmentSummary(policy)"
                                            :key="item.key"
                                            size="xs"
                                            color="blue"
                                            variant="subtle"
                                        >
                                            {{ item.label }}
                                        </UBadge>
                                        <span v-if="policy.assignments.length === 0" class="bow-cell-empty">{{ $t('quotaPolicies.none') }}</span>
                                    </div>
                                </td>
                                <td>
                                    <div class="flex gap-1 flex-wrap items-center">
                                        <UBadge
                                            v-for="label in policy.connection_overrides.map(formatOverride).slice(0, 2)"
                                            :key="label"
                                            size="xs"
                                            color="gray"
                                            variant="subtle"
                                        >
                                            {{ label }}
                                        </UBadge>
                                        <UBadge
                                            v-if="policy.connection_overrides.length > 2"
                                            size="xs"
                                            color="gray"
                                            variant="subtle"
                                        >
                                            +{{ policy.connection_overrides.length - 2 }}
                                        </UBadge>
                                        <span v-if="policy.connection_overrides.length === 0" class="bow-cell-empty">{{ $t('quotaPolicies.none') }}</span>
                                    </div>
                                </td>
                                <td class="whitespace-nowrap">
                                    <UBadge
                                        size="xs"
                                        :color="policy.enabled ? 'green' : 'gray'"
                                        variant="subtle"
                                    >
                                        {{ policy.enabled ? $t('quotaPolicies.enabled') : $t('quotaPolicies.disabled') }}
                                    </UBadge>
                                </td>
                                <td v-if="useCan('manage_settings')" class="whitespace-nowrap text-end">
                                    <div class="bow-row-actions inline-flex gap-1">
                                        <UButton
                                            color="gray"
                                            variant="ghost"
                                            size="xs"
                                            icon="i-heroicons-pencil-square"
                                            :aria-label="$t('common.edit')"
                                            @click="openEditModal(policy)"
                                        />
                                        <UButton
                                            variant="ghost"
                                            size="xs"
                                            color="red"
                                            icon="i-heroicons-trash"
                                            :aria-label="$t('common.delete')"
                                            @click="deletePolicy(policy)"
                                        />
                                    </div>
                                </td>
                            </tr>
                            <tr v-if="filteredPolicies.length === 0" class="bow-table-static">
                                <td :colspan="useCan('manage_settings') ? 6 : 5">
                                    <SettingsEmpty
                                        icon="i-heroicons-chart-bar-square"
                                        :title="$t('quotaPolicies.noPoliciesFound')"
                                        :description="$t('quotaPolicies.noPoliciesHint')"
                                    />
                                </td>
                            </tr>
                        </template>
                    </tbody>
                </table>
            </div>
        </div>

        <UModal v-model="showModal" :ui="{ width: 'sm:max-w-3xl' }">
            <div class="p-6">
                <div class="flex items-start justify-between gap-3 mb-5">
                    <div>
                        <h3 class="text-lg font-medium">
                            {{ editingPolicy ? $t('quotaPolicies.editQuota') : $t('quotaPolicies.createQuota') }}
                        </h3>
                    </div>
                    <UToggle v-model="form.enabled" />
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.nameLabel') }}</label>
                        <UInput v-model="form.name" :placeholder="$t('quotaPolicies.namePlaceholder')" size="sm" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.descriptionLabel') }}</label>
                        <UInput v-model="form.description" :placeholder="$t('quotaPolicies.descriptionPlaceholder')" size="sm" />
                    </div>
                </div>

                <div class="grid grid-cols-1 gap-3 mb-5">
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.monthlyTokenLimit') }}</label>
                        <UInput v-model="form.monthlyTokenLimit" type="number" min="0" :placeholder="$t('quotaPolicies.unlimited')" size="sm" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.monthlyQueryLimit') }}</label>
                        <UInput v-model="form.monthlyQueryLimit" type="number" min="0" :placeholder="$t('quotaPolicies.unlimited')" size="sm" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.monthlyDataLimitMb') }}</label>
                        <UInput v-model="form.monthlyDataLimitMb" type="number" min="0" step="0.01" :placeholder="$t('quotaPolicies.unlimited')" size="sm" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.monthlySpendLimitUsd') }}</label>
                        <UInput v-model="form.monthlySpendLimitUsd" type="number" min="0" step="0.01" :placeholder="$t('quotaPolicies.unlimited')" size="sm" />
                    </div>
                </div>

                <div class="border rounded-lg">
                    <div class="px-3 py-2 bg-surface-subtle border-b flex items-center justify-between">
                        <span class="text-sm font-medium">{{ $t('quotaPolicies.connectionOverrides') }}</span>
                    </div>
                    <div class="p-3 space-y-3">
                        <div class="grid grid-cols-1 gap-3">
                            <div>
                                <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.connection') }}</label>
                                <select
                                    v-model="overrideConnection"
                                    class="block w-full rounded-md border border-line-strong bg-surface-elevated px-3 py-2 text-sm text-ink shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
                                >
                                    <option :value="null" disabled>{{ $t('quotaPolicies.selectConnection') }}</option>
                                    <option
                                        v-for="option in connectionOptions"
                                        :key="option.value"
                                        :value="option.value"
                                    >
                                        {{ option.label }}
                                    </option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.queries') }}</label>
                                <UInput v-model="overrideQueryLimit" type="number" min="0" :placeholder="$t('quotaPolicies.unlimited')" size="sm" />
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('quotaPolicies.dataMb') }}</label>
                                <UInput v-model="overrideDataLimitMb" type="number" min="0" step="0.01" :placeholder="$t('quotaPolicies.unlimited')" size="sm" />
                            </div>
                            <UButton
                                color="blue"
                                variant="solid"
                                size="sm"
                                icon="i-heroicons-plus"
                                class="justify-center md:w-fit"
                                :disabled="!overrideConnection"
                                @click="addOverride"
                            >
                                {{ editingOverrideConnection ? $t('quotaPolicies.update') : $t('quotaPolicies.add') }}
                            </UButton>
                            <UButton
                                v-if="editingOverrideConnection"
                                variant="ghost"
                                size="sm"
                                class="justify-center md:w-fit"
                                @click="resetOverrideEditor"
                            >
                                {{ $t('quotaPolicies.cancelEdit') }}
                            </UButton>
                        </div>
                        <div class="flex flex-col gap-2">
                            <div
                                v-for="override in form.connectionOverrides"
                                :key="override.connection_id"
                                class="flex items-center justify-between gap-3 border border-line-subtle rounded-md px-3 py-2"
                            >
                                <div class="min-w-0 text-sm">
                                    <div class="font-medium text-ink truncate">{{ connectionName(override.connection_id) }}</div>
                                    <div class="text-ink-muted">
                                        {{ $t('quotaPolicies.queriesShort') }}: {{ formatCountLimit(override.monthly_query_limit) }}
                                        <span class="mx-1">/</span>
                                        {{ $t('quotaPolicies.dataShort') }}: {{ formatBytesLimit(override.monthly_data_bytes_limit) }}
                                    </div>
                                </div>
                                <div class="flex items-center gap-1">
                                    <UButton
                                        variant="ghost"
                                        size="xs"
                                        icon="i-heroicons-pencil"
                                        @click="editOverride(override)"
                                    />
                                    <UButton
                                        variant="ghost"
                                        size="xs"
                                        color="red"
                                        icon="i-heroicons-x-mark"
                                        @click="removeOverride(override.connection_id)"
                                    />
                                </div>
                            </div>
                            <span v-if="form.connectionOverrides.length === 0" class="bow-cell-empty">{{ $t('quotaPolicies.none') }}</span>
                        </div>
                    </div>
                </div>

                <div class="flex justify-end gap-2 mt-6">
                    <UButton variant="ghost" @click="showModal = false">{{ $t('quotaPolicies.cancel') }}</UButton>
                    <UButton color="blue" :loading="saving" :disabled="!form.name.trim()" @click="savePolicy">
                        {{ editingPolicy ? $t('quotaPolicies.save') : $t('quotaPolicies.create') }}
                    </UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useI18n } from 'vue-i18n'

type PrincipalType = 'user' | 'group' | 'role'

interface UsagePolicyAssignment {
    id?: string
    principal_type: PrincipalType
    principal_id: string
}

interface UsagePolicyConnectionOverride {
    id?: string
    connection_id: string
    monthly_query_limit: number | null
    monthly_data_bytes_limit: number | null
}

interface UsagePolicy {
    id: string
    organization_id: string
    name: string
    description?: string | null
    monthly_token_limit: number | null
    monthly_query_limit: number | null
    monthly_data_bytes_limit: number | null
    monthly_spend_limit_usd: number | null
    enabled: boolean
    assignments: UsagePolicyAssignment[]
    connection_overrides: UsagePolicyConnectionOverride[]
}

interface NamedOption {
    id: string
    name: string
}

const props = defineProps<{
    organization: { id: string; name?: string }
}>()

const { t } = useI18n()
const toast = useToast()
const organizationId = props.organization.id

const policies = ref<UsagePolicy[]>([])
const connections = ref<NamedOption[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showModal = ref(false)
const editingPolicy = ref<UsagePolicy | null>(null)
const saving = ref(false)
const overrideConnection = ref<string | null>(null)
const overrideQueryLimit = ref('')
const overrideDataLimitMb = ref('')
const editingOverrideConnection = ref<string | null>(null)

const form = reactive({
    name: '',
    description: '',
    monthlyTokenLimit: '',
    monthlyQueryLimit: '',
    monthlyDataLimitMb: '',
    monthlySpendLimitUsd: '',
    enabled: true,
    connectionOverrides: [] as UsagePolicyConnectionOverride[],
})

const connectionOptions = computed(() =>
    connections.value.map(connection => ({ value: connection.id, label: connection.name }))
)

const filteredPolicies = computed(() => {
    const query = searchQuery.value.toLowerCase()
    if (!query) return policies.value
    return policies.value.filter(policy =>
        policy.name.toLowerCase().includes(query) ||
        (policy.description || '').toLowerCase().includes(query)
    )
})

function parseOptionalInt(value: string | number | null | undefined): number | null {
    if (value === '' || value == null) return null
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed < 0) return null
    return Math.floor(parsed)
}

function parseOptionalMb(value: string | number | null | undefined): number | null {
    if (value === '' || value == null) return null
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed < 0) return null
    return Math.floor(parsed * 1024 * 1024)
}

function parseOptionalUsd(value: string | number | null | undefined): number | null {
    if (value === '' || value == null) return null
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed < 0) return null
    return Math.round(parsed * 100) / 100
}

function bytesToMbInput(value: number | null | undefined): string {
    if (value == null) return ''
    const mb = value / 1024 / 1024
    return Number.isInteger(mb) ? String(mb) : String(Number(mb.toFixed(2)))
}

function formatCountLimit(value: number | null | undefined): string {
    return value == null ? t('quotaPolicies.unlimited') : new Intl.NumberFormat().format(value)
}

function formatBytesLimit(value: number | null | undefined): string {
    return value == null ? t('quotaPolicies.unlimited') : formatBytes(value)
}

function formatUsdLimit(value: number | null | undefined): string {
    if (value == null) return t('quotaPolicies.unlimited')
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(value)
}

function formatBytes(value: number): string {
    if (value < 1024) return `${value} B`
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
    if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
    return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`
}

function connectionName(id: string): string {
    return connections.value.find(connection => connection.id === id)?.name || id
}

function assignmentSummary(policy: UsagePolicy): { key: string; label: string }[] {
    const counts = policy.assignments.reduce(
        (acc, assignment) => {
            acc[assignment.principal_type] += 1
            return acc
        },
        { user: 0, group: 0, role: 0 } as Record<PrincipalType, number>
    )
    return (Object.keys(counts) as PrincipalType[])
        .filter(type => counts[type] > 0)
        .map(type => ({
            key: type,
            label: t(`quotaPolicies.assignmentCounts.${type}`, { n: counts[type] }),
        }))
}

function formatOverride(override: UsagePolicyConnectionOverride): string {
    return `${connectionName(override.connection_id)} (${t('quotaPolicies.queriesShort')}: ${formatCountLimit(override.monthly_query_limit)}, ${t('quotaPolicies.dataShort')}: ${formatBytesLimit(override.monthly_data_bytes_limit)})`
}

async function loadPolicies() {
    const { data, error } = await useMyFetch(`/organizations/${organizationId}/usage-policies`)
    if (error.value) {
        toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToLoad'), color: 'red' })
        return
    }
    policies.value = (data.value || []) as UsagePolicy[]
}

async function loadConnections() {
    try {
        const { data } = await useMyFetch('/connections')
        connections.value = ((data.value || []) as any[]).map(connection => ({ id: connection.id, name: connection.name }))
    } catch (e) {
        connections.value = []
    }
}

async function loadAll() {
    isLoading.value = true
    try {
        await Promise.all([loadPolicies(), loadConnections()])
    } finally {
        isLoading.value = false
    }
}

function resetForm() {
    form.name = ''
    form.description = ''
    form.monthlyTokenLimit = ''
    form.monthlyQueryLimit = ''
    form.monthlyDataLimitMb = ''
    form.monthlySpendLimitUsd = ''
    form.enabled = true
    form.connectionOverrides = []
    overrideConnection.value = null
    overrideQueryLimit.value = ''
    overrideDataLimitMb.value = ''
    editingOverrideConnection.value = null
}

function openCreateModal() {
    editingPolicy.value = null
    resetForm()
    showModal.value = true
}

function openEditModal(policy: UsagePolicy) {
    editingPolicy.value = policy
    form.name = policy.name
    form.description = policy.description || ''
    form.monthlyTokenLimit = policy.monthly_token_limit == null ? '' : String(policy.monthly_token_limit)
    form.monthlyQueryLimit = policy.monthly_query_limit == null ? '' : String(policy.monthly_query_limit)
    form.monthlyDataLimitMb = bytesToMbInput(policy.monthly_data_bytes_limit)
    form.monthlySpendLimitUsd = policy.monthly_spend_limit_usd == null ? '' : String(policy.monthly_spend_limit_usd)
    form.enabled = policy.enabled
    form.connectionOverrides = policy.connection_overrides.map(override => ({
        connection_id: override.connection_id,
        monthly_query_limit: override.monthly_query_limit,
        monthly_data_bytes_limit: override.monthly_data_bytes_limit,
    }))
    overrideConnection.value = null
    overrideQueryLimit.value = ''
    overrideDataLimitMb.value = ''
    editingOverrideConnection.value = null
    showModal.value = true
}

function addOverride() {
    if (!overrideConnection.value) return
    upsertOverride(form.connectionOverrides, overrideFromEditor(overrideConnection.value))
    resetOverrideEditor()
}

function editOverride(override: UsagePolicyConnectionOverride) {
    editingOverrideConnection.value = override.connection_id
    overrideConnection.value = override.connection_id
    overrideQueryLimit.value = override.monthly_query_limit == null ? '' : String(override.monthly_query_limit)
    overrideDataLimitMb.value = bytesToMbInput(override.monthly_data_bytes_limit)
}

function resetOverrideEditor() {
    overrideConnection.value = null
    overrideQueryLimit.value = ''
    overrideDataLimitMb.value = ''
    editingOverrideConnection.value = null
}

function removeOverride(connectionId: string) {
    form.connectionOverrides = form.connectionOverrides.filter(override => override.connection_id !== connectionId)
    if (editingOverrideConnection.value === connectionId) {
        resetOverrideEditor()
    }
}

function overrideFromEditor(connectionId: string): UsagePolicyConnectionOverride {
    return {
        connection_id: connectionId,
        monthly_query_limit: parseOptionalInt(overrideQueryLimit.value),
        monthly_data_bytes_limit: parseOptionalMb(overrideDataLimitMb.value),
    }
}

function upsertOverride(overrides: UsagePolicyConnectionOverride[], next: UsagePolicyConnectionOverride) {
    const idx = overrides.findIndex(override => override.connection_id === next.connection_id)
    if (idx >= 0) {
        overrides.splice(idx, 1, next)
    } else {
        overrides.push(next)
    }
}

function hasPendingOverrideEditorValue(): boolean {
    return Boolean(
        overrideConnection.value &&
        (
            editingOverrideConnection.value ||
            String(overrideQueryLimit.value).trim() !== '' ||
            String(overrideDataLimitMb.value).trim() !== ''
        )
    )
}

function connectionOverridesForSave(): UsagePolicyConnectionOverride[] {
    const overrides = form.connectionOverrides.map(override => ({
        connection_id: override.connection_id,
        monthly_query_limit: override.monthly_query_limit,
        monthly_data_bytes_limit: override.monthly_data_bytes_limit,
    }))
    if (overrideConnection.value && hasPendingOverrideEditorValue()) {
        upsertOverride(overrides, overrideFromEditor(overrideConnection.value))
    }
    return overrides
}

function policyPayload() {
    return {
        name: form.name.trim(),
        description: form.description.trim() || null,
        monthly_token_limit: parseOptionalInt(form.monthlyTokenLimit),
        monthly_query_limit: parseOptionalInt(form.monthlyQueryLimit),
        monthly_data_bytes_limit: parseOptionalMb(form.monthlyDataLimitMb),
        monthly_spend_limit_usd: parseOptionalUsd(form.monthlySpendLimitUsd),
        enabled: form.enabled,
        connection_overrides: connectionOverridesForSave(),
    }
}

async function savePolicy() {
    saving.value = true
    try {
        const url = editingPolicy.value
            ? `/organizations/${organizationId}/usage-policies/${editingPolicy.value.id}`
            : `/organizations/${organizationId}/usage-policies`
        const method = editingPolicy.value ? 'PUT' : 'POST'
        const { error } = await useMyFetch(url, {
            method,
            body: policyPayload(),
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToSave'), color: 'red' })
            return
        }
        toast.add({ title: editingPolicy.value ? t('quotaPolicies.toastUpdated') : t('quotaPolicies.toastCreated'), color: 'green' })
        showModal.value = false
        await loadPolicies()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('quotaPolicies.failedToSave')
        toast.add({ title: detail, color: 'red' })
    } finally {
        saving.value = false
    }
}

async function deletePolicy(policy: UsagePolicy) {
    if (!confirm(t('quotaPolicies.confirmDelete', { name: policy.name }))) return
    try {
        const { error } = await useMyFetch(`/organizations/${organizationId}/usage-policies/${policy.id}`, {
            method: 'DELETE',
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToDelete'), color: 'red' })
            return
        }
        toast.add({ title: t('quotaPolicies.toastDeleted'), color: 'green' })
        await loadPolicies()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('quotaPolicies.failedToDelete')
        toast.add({ title: detail, color: 'red' })
    }
}

onMounted(loadAll)
</script>
