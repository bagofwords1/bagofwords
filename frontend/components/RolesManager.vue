<template>
    <div>
        <!-- Primary action lives in the page header (settings shell). -->
        <Teleport to="#settings-page-actions" defer>
            <UButton v-if="useCan('manage_roles')" color="blue" icon="i-heroicons-plus" @click="openCreateModal">
                {{ $t('rolesManager.newRole') }}
            </UButton>
        </Teleport>

        <div class="mb-4">
            <UInput
                v-model="searchQuery"
                icon="i-heroicons-magnifying-glass"
                :placeholder="$t('rolesManager.searchPlaceholder')"
                class="w-full sm:max-w-xs"
            />
        </div>

        <!-- Roles: a list rather than a table — each row is a role with its
             badges, summary, quota and actions. -->
        <div class="bow-table-card divide-y divide-line-subtle">
            <SettingsLoading v-if="isLoading" />
            <SettingsEmpty
                v-else-if="filteredRoles.length === 0"
                icon="i-heroicons-shield-check"
                :title="$t('rolesManager.noRolesFound')"
                :description="$t('rolesManager.noRolesHint')"
            />
            <div
                v-else
                v-for="role in filteredRoles"
                :key="role.id"
                class="group/role px-5 py-4 flex items-center justify-between gap-4 hover:bg-ink/[0.03] transition-colors"
            >
                <div class="flex items-center gap-3 min-w-0">
                    <span class="flex items-center justify-center w-8 h-8 rounded-md bg-surface-muted text-ink-muted shrink-0">
                        <UIcon :name="role.permissions?.includes('full_admin_access') ? 'i-heroicons-shield-check' : 'i-heroicons-identification'" class="w-4 h-4" />
                    </span>
                    <div class="min-w-0">
                        <div class="flex items-center gap-2">
                            <span class="text-sm font-medium text-ink">{{ cap(role.name) }}</span>
                            <UBadge v-if="isOrgDefaultRole(role)" size="xs" color="gray" variant="subtle">{{ $t('rolesManager.default') }}</UBadge>
                            <UBadge v-else-if="role.is_system" size="xs" color="gray" variant="subtle">{{ $t('rolesManager.system') }}</UBadge>
                            <UBadge v-if="role.permissions?.includes('full_admin_access')" size="xs" color="blue" variant="subtle">
                                {{ $t('rolesManager.fullAdmin') }}
                            </UBadge>
                        </div>
                        <p class="mt-0.5 text-sm text-ink-muted truncate">
                            {{ role.description || $t('rolesManager.permissionsCount', { n: rolePermissionCount(role) }) }}
                        </p>
                    </div>
                </div>
                <div class="flex items-center gap-3 shrink-0">
                    <USelectMenu
                        v-if="showQuotaColumn"
                        :model-value="getDirectQuotaId('role', role.id)"
                        :options="quotaSelectOptions"
                        value-attribute="value"
                        option-attribute="label"
                        size="sm"
                        class="w-44"
                        :ui-menu="{ width: 'w-48' }"
                        :popper="{ placement: 'bottom-start', strategy: 'fixed' }"
                        @update:model-value="updatePrincipalQuota('role', role.id, $event)"
                    >
                        <template #label>
                            <span class="flex gap-1 flex-wrap items-center">
                                <UBadge
                                    v-for="policy in getRoleQuotaPolicies(role).slice(0, 1)"
                                    :key="policy.id"
                                    size="xs"
                                    color="blue"
                                    variant="subtle"
                                >
                                    {{ policy.name }}
                                </UBadge>
                                <span v-if="getRoleQuotaPolicies(role).length === 0" class="text-ink-muted">{{ $t('quotaPolicies.unlimited') }}</span>
                            </span>
                        </template>
                        <template #option="{ option }">
                            <span class="text-sm">{{ option.label }}</span>
                        </template>
                    </USelectMenu>
                    <div class="flex gap-1 w-14 justify-end opacity-60 group-hover/role:opacity-100 focus-within:opacity-100 transition-opacity">
                        <UTooltip v-if="role.is_editable && useCan('manage_roles')" :text="$t('rolesManager.edit')">
                            <UButton
                                color="gray"
                                variant="ghost"
                                size="xs"
                                icon="i-heroicons-pencil-square"
                                :aria-label="$t('rolesManager.edit')"
                                @click="openEditModal(role)"
                            />
                        </UTooltip>
                        <UButton
                            v-if="role.is_deletable && useCan('manage_roles')"
                            variant="ghost"
                            size="xs"
                            color="red"
                            icon="i-heroicons-trash"
                            :aria-label="$t('common.delete')"
                            @click="deleteRole(role)"
                        />
                    </div>
                </div>
            </div>
        </div>

        <!-- Create/Edit Modal -->
        <UModal v-model="showModal" :ui="{ width: 'sm:max-w-xl' }">
            <div class="p-6">
                <h3 class="text-lg font-medium mb-1">
                    {{ editingRole ? $t('rolesManager.editRole') : $t('rolesManager.createRole') }}
                </h3>
                <p v-if="editingDefaultRole" class="text-xs text-ink-muted mb-4">
                    {{ $t('rolesManager.defaultRoleHint') }}
                </p>
                <div v-else class="mb-4"></div>

                <!-- Name + Description -->
                <div class="grid grid-cols-2 gap-3 mb-4">
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.nameLabel') }}</label>
                        <UInput v-model="form.name" :placeholder="$t('rolesManager.namePlaceholder')" size="sm" :disabled="editingDefaultRole" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.descriptionLabel') }}</label>
                        <UInput v-model="form.description" :placeholder="$t('rolesManager.descriptionPlaceholder')" size="sm" />
                    </div>
                </div>

                <!-- Full Admin Toggle -->
                <div class="mb-5 px-3 py-2.5 bg-surface-subtle rounded-lg flex items-center justify-between">
                    <div>
                        <span class="text-sm font-medium">{{ $t('rolesManager.fullAdminAccess') }}</span>
                        <p class="text-xs text-ink-muted">{{ $t('rolesManager.fullAdminBypass') }}</p>
                    </div>
                    <UToggle v-model="isFullAdmin" />
                </div>

                <!-- Permission cards (disabled when full admin) -->
                <div :class="{ 'opacity-40 pointer-events-none': isFullAdmin }" class="space-y-3">

                    <!-- Org-wide card -->
                    <div class="border rounded-lg overflow-hidden">
                        <div class="px-3 py-2 bg-surface-subtle border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UIcon name="i-heroicons-globe-alt" class="w-4 h-4 text-ink-muted" />
                                <span class="text-sm font-medium">{{ $t('rolesManager.allResources') }}</span>
                            </div>
                            <span class="text-xs text-ink-muted">{{ $t('rolesManager.orgWidePermissions') }}</span>
                        </div>
                        <div class="p-3">
                            <div class="grid grid-cols-2 gap-x-4 gap-y-1.5">
                                <label
                                    v-for="perm in flatOrgPermissions"
                                    :key="perm"
                                    class="flex items-center gap-2 text-sm cursor-pointer py-0.5"
                                >
                                    <UCheckbox
                                        :model-value="form.permissions.includes(perm)"
                                        @update:model-value="togglePermission(perm, $event)"
                                        size="xs"
                                    />
                                    <span class="text-ink-2">{{ formatPermission(perm) }}</span>
                                </label>
                            </div>
                        </div>
                    </div>

                    <!-- Per-resource cards -->
                    <div
                        v-for="(grant, idx) in form.resourceGrants"
                        :key="`grant-${idx}`"
                        class="border rounded-lg overflow-hidden"
                    >
                        <div class="px-3 py-2 bg-surface-subtle border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UBadge size="xs" :color="grant.resource_type === 'connection' ? 'gray' : 'blue'">{{ grant.resource_type === 'connection' ? $t('rolesManager.connection') : $t('rolesManager.ds') }}</UBadge>
                                <span class="text-sm font-medium">{{ grant.resource_name }}</span>
                            </div>
                            <UButton
                                variant="ghost"
                                size="xs"
                                color="red"
                                icon="i-heroicons-x-mark"
                                @click="form.resourceGrants.splice(idx, 1)"
                            />
                        </div>
                        <div class="p-3">
                            <!-- Agent grants use the same two access tiers as the agent
                                 panel's "Add people" modal: an EMPTY grant is the query
                                 tier (any grant row on a data source implies `view` in
                                 the backend resolver), `manage` is the owner/manager
                                 superset. Raw checkboxes live in Advanced. -->
                            <template v-if="grant.resource_type === 'data_source'">
                                <div class="space-y-0.5">
                                    <button
                                        v-for="tier in DS_ACCESS_TIERS"
                                        :key="tier.key"
                                        type="button"
                                        class="w-full flex items-start gap-2 px-2 py-1.5 rounded-md text-start hover:bg-ink/[0.03]/50"
                                        @click="selectGrantTier(grant, tier.key)"
                                    >
                                        <span
                                            class="mt-0.5 w-3.5 h-3.5 rounded-full border flex items-center justify-center shrink-0"
                                            :class="grantTier(grant) === tier.key ? 'border-gray-900 dark:border-white' : 'border-line-strong'"
                                        >
                                            <span v-if="grantTier(grant) === tier.key" class="w-1.5 h-1.5 rounded-full bg-gray-900 dark:bg-white" />
                                        </span>
                                        <span class="min-w-0">
                                            <span class="block text-sm text-ink">{{ $t(`rolesManager.tiers.${tier.key}`) }}</span>
                                            <span class="block text-xs text-ink-muted">{{ $t(`rolesManager.tiers.${tier.key}Hint`) }}</span>
                                        </span>
                                    </button>
                                </div>
                                <button
                                    type="button"
                                    class="mt-1 inline-flex items-center gap-1 text-xs text-ink-muted hover:text-gray-700 dark:hover:text-gray-200"
                                    @click="grant.showAdvanced = !grant.showAdvanced"
                                >
                                    {{ $t('rolesManager.advancedPermissions') }}
                                    <UIcon
                                        name="i-heroicons-chevron-down"
                                        class="w-3 h-3 transition-transform"
                                        :class="(grant.showAdvanced || grantTier(grant) === null) ? 'rotate-180' : ''"
                                    />
                                </button>
                            </template>
                            <div
                                v-if="grant.resource_type !== 'data_source' || grant.showAdvanced || grantTier(grant) === null"
                                class="grid grid-cols-2 gap-x-3 gap-y-1"
                                :class="{ 'mt-2': grant.resource_type === 'data_source' }"
                            >
                                <label
                                    v-for="perm in getResourcePermissions(grant.resource_type)"
                                    :key="perm"
                                    class="flex items-center gap-1.5 text-sm cursor-pointer py-0.5"
                                >
                                    <UCheckbox
                                        :model-value="grant.permissions.includes(perm)"
                                        @update:model-value="toggleResourcePerm(grant, perm, $event)"
                                        size="xs"
                                    />
                                    <span class="text-ink-2">{{ formatPermission(perm) }}</span>
                                </label>
                            </div>
                        </div>
                    </div>

                    <!-- Add resource -->
                    <USelectMenu
                        v-model="selectedResource"
                        :options="availableResources"
                        option-attribute="label"
                        value-attribute="value"
                        searchable
                        :placeholder="$t('rolesManager.addAgent')"
                        @update:model-value="addResource"
                        size="sm"
                    />

                    <!-- Model access (enterprise: per-model LLM access control) -->
                    <div v-if="showModelAccess" class="border rounded-lg overflow-hidden">
                        <div class="px-3 py-2 bg-surface-subtle border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UIcon name="i-heroicons-cpu-chip" class="w-4 h-4 text-ink-muted" />
                                <span class="text-sm font-medium">{{ $t('rolesManager.modelAccess') }}</span>
                            </div>
                            <span class="text-xs text-ink-muted">{{ $t('rolesManager.modelAccessHint') }}</span>
                        </div>
                        <div class="p-3">
                            <div v-if="restrictedModels.length === 0" class="text-xs text-ink-muted py-0.5">
                                {{ $t('rolesManager.noRestrictedModels') }}
                            </div>
                            <div v-else class="grid grid-cols-2 gap-x-4 gap-y-1.5">
                                <label
                                    v-for="model in restrictedModels"
                                    :key="model.model_id"
                                    class="flex items-center gap-2 text-sm cursor-pointer py-0.5"
                                >
                                    <UCheckbox
                                        :model-value="model.granted"
                                        :disabled="modelAccessSaving"
                                        @update:model-value="toggleModelAccess(model, $event)"
                                        size="xs"
                                    />
                                    <span class="text-ink-2 truncate">{{ model.name }}</span>
                                </label>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Actions -->
                <div class="flex justify-end gap-2 mt-6">
                    <UButton variant="ghost" @click="showModal = false">{{ $t('rolesManager.cancel') }}</UButton>
                    <UButton color="blue" @click="saveRole" :loading="saving" :disabled="!form.name.trim()">
                        {{ editingRole ? $t('rolesManager.save') : $t('rolesManager.create') }}
                    </UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'

// Role names are stored lowercase ("admin", "member"); show them titled.
function cap(name?: string): string {
    if (!name) return ''
    return name.charAt(0).toUpperCase() + name.slice(1)
}
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useI18n } from 'vue-i18n'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { t } = useI18n()

interface RoleData {
    id: string
    name: string
    description?: string
    permissions: string[]
    resource_grants?: { resource_type: string; resource_id: string; permissions: string[] }[]
    is_system: boolean
    organization_id?: string
    overrides_system_role?: string | null
    is_editable?: boolean
    is_deletable?: boolean
}

// The org-wide default role for new members (settings.default_member_role).
// Drives which role wears the "Default" badge; kept in sync with the control
// in the Members tab. Falls back to "member" when the setting is unset or the
// endpoint is unavailable (e.g. before that feature ships).
const defaultMemberRole = ref('member')

// The role new members get by default carries the "Default" badge, and it
// moves when an admin changes the default_member_role setting.
function isOrgDefaultRole(role: RoleData): boolean {
    return role.name === defaultMemberRole.value
}

// A built-in `member` role (or an org's editable override of it): editable but
// not deletable, and its name is fixed. This is independent of which role is
// the org default — a custom role set as default stays fully editable.
function isBuiltinRole(role: RoleData): boolean {
    return !!role.overrides_system_role || (role.is_system && role.is_editable === true)
}

interface ResourceGrantForm {
    resource_type: string
    resource_id: string
    resource_name: string
    permissions: string[]
    showAdvanced?: boolean
}

interface UsagePolicySummary {
    id: string
    name: string
    enabled: boolean
    assignments: UsagePolicyAssignment[]
}

interface UsagePolicyAssignment {
    principal_type: 'user' | 'group' | 'role'
    principal_id: string
}

const props = defineProps<{
    organization: { id: string; name: string }
}>()

const toast = useToast()

// State
const roles = ref<RoleData[]>([])
const usagePolicies = ref<UsagePolicySummary[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showModal = ref(false)
const editingRole = ref<RoleData | null>(null)
// Editing the built-in `member` role (or its override): its name is fixed,
// only permissions and description are customizable. Not tied to which role is
// the org default.
const editingDefaultRole = computed(() => !!editingRole.value && isBuiltinRole(editingRole.value))
const saving = ref(false)
const selectedResource = ref(null)
const showOrgDetails = ref(false)
const { hasFeature } = useEnterprise()
const showQuotaColumn = computed(() => hasFeature('usage_limits') && useCan('manage_settings'))

// Per-model LLM access control (enterprise). Editing writes grants immediately,
// independent of the role's Save button, so it only applies to a saved role.
interface RestrictedModel {
    model_id: string
    name: string
    provider_name: string
    granted: boolean
    grant_id: string | null
}
const restrictedModels = ref<RestrictedModel[]>([])
const modelAccessSaving = ref(false)
const showModelAccess = computed(() =>
    hasFeature('llm_access_control') && !!editingRole.value && !isFullAdmin.value
)

const form = reactive({
    name: '',
    description: '',
    permissions: [] as string[],
    resourceGrants: [] as ResourceGrantForm[],
})

const isFullAdmin = computed({
    get: () => form.permissions.includes('full_admin_access'),
    set: (val: boolean) => {
        if (val) {
            if (!form.permissions.includes('full_admin_access')) {
                form.permissions.push('full_admin_access')
            }
        } else {
            form.permissions = form.permissions.filter((p) => p !== 'full_admin_access')
        }
    },
})

// ── Registry data from backend ───────────────────────────────────────────

const allCategories = ref<Record<string, string[]>>({})
const mergedCategories = ref<Record<string, string[]>>({})
const resourceScopedGroups = ref<Record<string, Record<string, string[]>>>({})
const resourcePermissions = ref<Record<string, string[]>>({})

// A role's authority is org-wide permissions PLUS its per-resource grants.
// Counting only `permissions` made a correctly-configured role that grants,
// say, "Create agents" on one connection render as "0 permissions" — it looked
// broken when it was merely scoped. An agent grant with no permission strings
// is the query tier (the grant row itself conveys view/query access), so it
// counts as one.
const rolePermissionCount = (role: any) =>
    (role?.permissions?.length || 0) +
    (role?.resource_grants || []).reduce(
        (n: number, g: any) => n + (g?.permissions?.length
            || (g?.resource_type === 'data_source' ? 1 : 0)), 0)

async function loadPermissionsRegistry() {
    try {
        const { data } = await useMyFetch('/permissions/registry')
        if (data.value) {
            const registry = data.value as {
                categories: Record<string, string[]>
                resource_permissions: Record<string, string[]>
                merged_categories: Record<string, string[]>
                resource_scoped_groups: Record<string, Record<string, string[]>>
            }
            allCategories.value = registry.categories
            mergedCategories.value = registry.merged_categories
            resourceScopedGroups.value = registry.resource_scoped_groups
            resourcePermissions.value = registry.resource_permissions
        }
    } catch (e) {
        console.error('Failed to load permissions registry', e)
    }
}

function getResourcePermissions(resourceType: string): string[] {
    return resourcePermissions.value[resourceType] || []
}

const flatOrgPermissions = computed(() => {
    const out: string[] = []
    for (const perms of Object.values(allCategories.value)) {
        out.push(...perms)
    }
    return out
})

// ── Merged category tier logic (org-wide card) ───────────────────────────

function getMergedPerms(catNames: string[]): string[] {
    const perms: string[] = []
    for (const cat of catNames) {
        if (allCategories.value[cat]) {
            perms.push(...allCategories.value[cat])
        }
    }
    return perms
}

function getMergedTier(catNames: string[]): 'none' | 'read' | 'full' | 'custom' {
    const perms = getMergedPerms(catNames)
    if (perms.length === 0) return 'none'
    const selected = perms.filter((p) => form.permissions.includes(p))
    if (selected.length === 0) return 'none'
    if (selected.length === perms.length) return 'full'
    const viewPerms = perms.filter((p) => p.startsWith('view_'))
    if (viewPerms.length > 0 && viewPerms.every((p) => form.permissions.includes(p)) && selected.length === viewPerms.length) {
        return 'read'
    }
    return 'custom'
}

function setMergedTier(catNames: string[], tier: 'read' | 'full') {
    const perms = getMergedPerms(catNames)
    const currentTier = getMergedTier(catNames)

    // If clicking the active tier, toggle it off
    if (currentTier === tier) {
        form.permissions = form.permissions.filter((p) => !perms.includes(p))
        return
    }

    // Remove all perms in these categories first
    form.permissions = form.permissions.filter((p) => !perms.includes(p))

    if (tier === 'read') {
        const viewPerms = perms.filter((p) => p.startsWith('view_'))
        form.permissions.push(...viewPerms)
    } else {
        form.permissions.push(...perms)
    }
}

// ── Resource-scoped permission groups ────────────────────────────────────

function getResourceGroups(resourceType: string): Record<string, string[]> {
    return resourceScopedGroups.value[resourceType] || {}
}

function getResourceGroupTier(grant: ResourceGrantForm, groupPerms: string[]): 'none' | 'read' | 'full' | 'custom' {
    const selected = groupPerms.filter((p) => grant.permissions.includes(p))
    if (selected.length === 0) return 'none'
    if (selected.length === groupPerms.length) return 'full'
    const viewPerms = groupPerms.filter((p) => p.startsWith('view_') || p === 'query' || p === 'view_schema')
    if (viewPerms.length > 0 && viewPerms.every((p) => grant.permissions.includes(p)) && selected.length === viewPerms.length) {
        return 'read'
    }
    return 'custom'
}

function setResourceGroupTier(grant: ResourceGrantForm, groupPerms: string[], tier: 'read' | 'full') {
    const currentTier = getResourceGroupTier(grant, groupPerms)

    // If clicking the active tier, toggle it off
    if (currentTier === tier) {
        grant.permissions = grant.permissions.filter((p) => !groupPerms.includes(p))
        return
    }

    // Remove group perms first
    grant.permissions = grant.permissions.filter((p) => !groupPerms.includes(p))

    if (tier === 'read') {
        const viewPerms = groupPerms.filter((p) => p.startsWith('view_') || p === 'query' || p === 'view_schema')
        grant.permissions.push(...viewPerms)
    } else {
        grant.permissions.push(...groupPerms)
    }
}

function isCheckboxResource(resourceType: string): boolean {
    const groups = resourceScopedGroups.value[resourceType] || {}
    // Checkbox mode when every group has exactly one permission (no read/write split)
    return Object.values(groups).every((perms) => perms.length === 1)
}

function toggleResourcePerm(grant: ResourceGrantForm, perm: string, checked: boolean) {
    if (checked) {
        if (!grant.permissions.includes(perm)) grant.permissions.push(perm)
    } else {
        grant.permissions = grant.permissions.filter((p) => p !== perm)
    }
}

// ── Agent-grant access tiers ─────────────────────────────────────────────
// Mirrors AgentSettingsPanel's "Add people" modal: query (empty grant) or
// manage (`manage` implies the other manage_* perms server-side). Any other
// combination comes from Advanced and matches no tier.

const DS_ACCESS_TIERS: { key: string; perms: string[] }[] = [
    { key: 'query', perms: [] },
    { key: 'manage', perms: ['manage'] },
]

const sameSet = (a: string[], b: string[]) =>
    a.length === b.length && a.every((x) => b.includes(x))

function grantTier(grant: ResourceGrantForm): string | null {
    return DS_ACCESS_TIERS.find((tier) => sameSet(tier.perms, grant.permissions))?.key ?? null
}

function selectGrantTier(grant: ResourceGrantForm, key: string) {
    const tier = DS_ACCESS_TIERS.find((t) => t.key === key)
    if (tier) grant.permissions = [...tier.perms]
}

// ── Available resources for the picker ───────────────────────────────────

const availableResources = ref<{ label: string; value: string; type: string; id: string }[]>([])

async function loadResources() {
    try {
        const dsResult = await useMyFetch(`/data_sources/active`)
        const resources: any[] = []
        if (dsResult.data.value) {
            for (const ds of dsResult.data.value as any[]) {
                resources.push({
                    label: `Agent: ${ds.name}`,
                    value: `data_source:${ds.id}`,
                    type: 'data_source',
                    id: ds.id,
                })
            }
        }
        // Connections — per-connection grants delegate create/manage agents and
        // connection config without org-wide manage_connections.
        const connResult = await useMyFetch(`/connections`)
        if (connResult.data.value) {
            for (const c of connResult.data.value as any[]) {
                resources.push({
                    label: `Connection: ${c.name}`,
                    value: `connection:${c.id}`,
                    type: 'connection',
                    id: c.id,
                })
            }
        }
        availableResources.value = resources
    } catch (e) {
        console.error('Failed to load resources', e)
    }
}

async function loadModelAccess(roleId: string) {
    restrictedModels.value = []
    if (!hasFeature('llm_access_control')) return
    try {
        const { data } = await useMyFetch(
            `/llm/model-access/by-principal?principal_type=role&principal_id=${roleId}`
        )
        if (data.value) restrictedModels.value = data.value as RestrictedModel[]
    } catch (e) {
        console.error('Failed to load model access', e)
    }
}

async function toggleModelAccess(model: RestrictedModel, checked: boolean) {
    if (!editingRole.value) return
    modelAccessSaving.value = true
    try {
        if (checked) {
            const { data, error } = await useMyFetch(`/llm/models/${model.model_id}/access`, {
                method: 'POST',
                body: { principal_type: 'role', principal_id: editingRole.value.id },
            })
            if (error.value) {
                toast.add({ title: error.value.data?.detail || t('rolesManager.failedToSave'), color: 'red' })
                return
            }
            model.grant_id = (data.value as any)?.grant_id ?? null
            model.granted = true
        } else {
            if (!model.grant_id) { model.granted = false; return }
            const { error } = await useMyFetch(
                `/llm/models/${model.model_id}/access/${model.grant_id}`,
                { method: 'DELETE' }
            )
            if (error.value) {
                toast.add({ title: error.value.data?.detail || t('rolesManager.failedToSave'), color: 'red' })
                return
            }
            model.grant_id = null
            model.granted = false
        }
    } finally {
        modelAccessSaving.value = false
    }
}

function addResource(selected: any) {
    if (!selected) return
    const resource = availableResources.value.find((r) => r.value === selected)
    if (!resource) return
    if (form.resourceGrants.some((g) => g.resource_type === resource.type && g.resource_id === resource.id)) {
        selectedResource.value = null
        return
    }
    form.resourceGrants.push({
        resource_type: resource.type,
        resource_id: resource.id,
        resource_name: resource.label.replace(/^(Agent|Data Source|Connection): /, ''),
        permissions: [],
    })
    selectedResource.value = null
}

// ── Helpers ──────────────────────────────────────────────────────────────

function togglePermission(perm: string, checked: boolean) {
    if (checked) {
        if (!form.permissions.includes(perm)) form.permissions.push(perm)
    } else {
        form.permissions = form.permissions.filter((p) => p !== perm)
    }
}

const KNOWN_PERMISSION_KEYS = new Set([
    'manage_files',
    'view_code',
    'run_custom_code',
    'create_data_source',
    'create_connections',
    'manage_connections',
    'manage_instructions',
    'manage_entities',
    'manage_evals',
    'view_members',
    'manage_members',
    'manage_service_accounts',
    'manage_settings',
    'manage_llm',
    'view_audit_logs',
    'manage_identity_providers',
    'view',
    'view_schema',
    'create_entities',
    'manage',
    // connection-scoped
    'manage_connection',
    'create_data_sources',
    'manage_data_sources',
])

function formatPermission(perm: string) {
    if (KNOWN_PERMISSION_KEYS.has(perm)) return t(`rolesManager.permissions.${perm}`)
    // Fallback: snake_case → Title Case
    return perm.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

const filteredRoles = computed(() => {
    const query = searchQuery.value.toLowerCase()
    if (!query) return roles.value
    return roles.value.filter(r =>
        r.name.toLowerCase().includes(query) ||
        (r.description || '').toLowerCase().includes(query)
    )
})

const quotaSelectOptions = computed(() => [
    { value: null, label: t('quotaPolicies.noDirectQuota') },
    ...usagePolicies.value
        .filter(policy => policy.enabled)
        .map(policy => ({ value: policy.id, label: policy.name })),
])

function getRoleQuotaPolicies(role: RoleData): UsagePolicySummary[] {
    if (!showQuotaColumn.value) return []
    return getPrincipalQuotaPolicies('role', role.id)
}

function getPrincipalQuotaPolicies(principalType: UsagePolicyAssignment['principal_type'], principalId: string): UsagePolicySummary[] {
    return usagePolicies.value.filter(policy =>
        policy.enabled &&
        policy.assignments?.some(assignment =>
            assignment.principal_type === principalType &&
            assignment.principal_id === principalId
        )
    )
}

function getDirectQuotaId(principalType: UsagePolicyAssignment['principal_type'], principalId: string): string | null {
    return getPrincipalQuotaPolicies(principalType, principalId)[0]?.id || null
}

function applyLocalQuotaAssignment(principalType: UsagePolicyAssignment['principal_type'], principalId: string, policyId: string | null) {
    usagePolicies.value = usagePolicies.value.map(policy => {
        const assignments = (policy.assignments || []).filter(
            assignment => assignment.principal_type !== principalType || assignment.principal_id !== principalId
        )
        if (policyId && policy.id === policyId) {
            assignments.push({ principal_type: principalType, principal_id: principalId })
        }
        return { ...policy, assignments }
    })
}

async function updatePrincipalQuota(principalType: UsagePolicyAssignment['principal_type'], principalId: string, policyId: string | null) {
    try {
        const { error } = await useMyFetch(`/organizations/${props.organization.id}/usage-policy-assignments/principal`, {
            method: 'PUT',
            body: {
                principal_type: principalType,
                principal_id: principalId,
                policy_id: policyId,
            },
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToSave'), color: 'red' })
            return
        }
        applyLocalQuotaAssignment(principalType, principalId, policyId)
        toast.add({ title: t('quotaPolicies.toastAssignmentUpdated'), color: 'green' })
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('quotaPolicies.failedToSave')
        toast.add({ title: detail, color: 'red' })
    }
}

// ── CRUD ─────────────────────────────────────────────────────────────────

async function loadRoles() {
    isLoading.value = true
    try {
        const { data, error } = await useMyFetch(`/organizations/${props.organization.id}/roles`)
        if (error.value) {
            const detail = (error.value as any)?.data?.detail || t('rolesManager.failedToLoad')
            toast.add({ title: detail, color: 'red' })
        } else if (data.value) {
            roles.value = data.value as RoleData[]
        }
    } finally {
        isLoading.value = false
    }
}

async function loadUsagePolicies() {
    if (!showQuotaColumn.value) return
    try {
        const { data } = await useMyFetch(`/organizations/${props.organization.id}/usage-policies`)
        usagePolicies.value = (data.value || []) as UsagePolicySummary[]
    } catch (e) {
        usagePolicies.value = []
    }
}

// Mirrors permissions_registry.DEFAULT_ON_PERMISSIONS. A new role starts with
// these checked so the editor's default matches the product's: every existing
// role carries them (the migration backfilled them), so a freshly authored role
// that silently lacked them would behave differently from every other role in
// the org for no reason the admin chose.
const DEFAULT_ON_PERMISSIONS = ['view_code', 'run_custom_code']

function openCreateModal() {
    editingRole.value = null
    form.name = ''
    form.description = ''
    form.permissions = [...DEFAULT_ON_PERMISSIONS]
    form.resourceGrants = []
    restrictedModels.value = []
    showOrgDetails.value = false
    showModal.value = true
    loadResources()
}

async function openEditModal(role: RoleData) {
    editingRole.value = role
    form.name = role.name
    form.description = role.description || ''
    form.permissions = [...(role.permissions || [])]
    form.resourceGrants = []
    showOrgDetails.value = false
    showModal.value = true
    await loadResources()
    form.resourceGrants = (role.resource_grants || []).map((g) => {
        const found = availableResources.value.find(
            (r) => r.type === g.resource_type && r.id === g.resource_id
        )
        return {
            resource_type: g.resource_type,
            resource_id: g.resource_id,
            resource_name: found ? found.label.replace(/^(Agent|Data Source|Connection): /, '') : g.resource_id,
            permissions: [...(g.permissions || [])],
        }
    })
    await loadModelAccess(role.id)
}

async function saveRole() {
    saving.value = true
    try {
        const body = {
            name: form.name,
            description: form.description || null,
            permissions: isFullAdmin.value ? ['full_admin_access'] : form.permissions,
            resource_grants: form.resourceGrants.map((g) => ({
                resource_type: g.resource_type,
                resource_id: g.resource_id,
                permissions: g.permissions,
            })),
        }

        if (editingRole.value) {
            const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles/${editingRole.value.id}`, {
                method: 'PUT',
                body,
            })
            if (error.value) {
                const detail = error.value.data?.detail || t('rolesManager.failedToUpdate')
                toast.add({ title: detail, color: 'red' })
                return
            }
            toast.add({ title: t('rolesManager.toastUpdated') })
        } else {
            const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles`, {
                method: 'POST',
                body,
            })
            if (error.value) {
                const detail = error.value.data?.detail || t('rolesManager.failedToCreate')
                toast.add({ title: detail, color: 'red' })
                return
            }
            toast.add({ title: t('rolesManager.toastCreated') })
        }

        showModal.value = false
        await loadRoles()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('rolesManager.failedToSave')
        toast.add({ title: detail, color: 'red' })
    } finally {
        saving.value = false
    }
}

async function deleteRole(role: RoleData) {
    if (!confirm(t('rolesManager.confirmDelete', { name: role.name }))) return
    try {
        const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles/${role.id}`, {
            method: 'DELETE',
        })
        if (error.value) {
            const detail = error.value.data?.detail || t('rolesManager.failedToDelete')
            toast.add({ title: detail, color: 'red' })
            return
        }
        toast.add({ title: t('rolesManager.toastDeleted') })
        await loadRoles()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || t('rolesManager.failedToDelete')
        toast.add({ title: detail, color: 'red' })
    }
}

// The org-wide default role for new members. Missing endpoint / value simply
// leaves the fallback ("member") in place, so this is safe before that setting
// ships.
async function loadDefaultMemberRole() {
    try {
        const { data } = await useMyFetch('/organization/default-member-role')
        const role = (data.value as any)?.role
        if (role) defaultMemberRole.value = role
    } catch (e) {
        // keep the "member" fallback
    }
}

// Load on mount
onMounted(() => {
    loadPermissionsRegistry()
    loadRoles()
    loadUsagePolicies()
    loadDefaultMemberRole()
})

watch(showQuotaColumn, (enabled) => {
    if (enabled && usagePolicies.value.length === 0) {
        loadUsagePolicies()
    }
})
</script>
