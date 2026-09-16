<template>
    <div class="mt-4">
        <!-- Header with search and actions -->
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div class="flex-1 max-w-md w-full">
                <div class="relative">
                    <input
                        v-model="searchQuery"
                        type="text"
                        :placeholder="$t('rolesManager.searchPlaceholder')"
                        class="w-full ps-10 pe-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    />
                    <UIcon
                        name="i-heroicons-magnifying-glass"
                        class="absolute start-3 top-2.5 h-4 w-4 text-gray-400"
                    />
                </div>
            </div>
            <div class="flex items-center justify-end gap-2 w-full md:w-auto">
                <UButton
                    v-if="useCan('manage_roles')"
                    color="blue"
                    variant="solid"
                    size="xs"
                    icon="i-heroicons-plus"
                    @click="openCreateModal"
                >
                    {{ $t('rolesManager.newRole') }}
                </UButton>
            </div>
        </div>

        <!-- Role cards -->
        <div class="bg-white dark:bg-gray-900 shadow-sm border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden divide-y divide-gray-200 dark:divide-gray-700">
            <div v-if="isLoading" class="px-6 py-12 text-center">
                <div class="flex items-center justify-center text-gray-500 dark:text-gray-400">
                    <Spinner class="w-4 h-4 me-2" />
                    <span class="text-sm">{{ $t('rolesManager.loading') }}</span>
                </div>
            </div>
            <div v-else-if="filteredRoles.length === 0" class="px-6 py-12 text-center">
                <div class="flex flex-col items-center">
                    <Icon name="heroicons:shield-check" class="mx-auto h-12 w-12 text-gray-400" />
                    <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">{{ $t('rolesManager.noRolesFound') }}</h3>
                    <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ $t('rolesManager.noRolesHint') }}</p>
                </div>
            </div>
            <div
                v-else
                v-for="role in filteredRoles"
                :key="role.id"
                class="p-4 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-800"
            >
                <div>
                    <div class="flex items-center gap-2">
                        <span class="font-medium">{{ role.name }}</span>
                        <UBadge v-if="role.is_system" size="xs" color="gray">{{ $t('rolesManager.system') }}</UBadge>
                        <UBadge
                            v-if="role.permissions?.includes('full_admin_access')"
                            size="xs"
                            color="blue"
                        >
                            {{ $t('rolesManager.fullAdmin') }}
                        </UBadge>
                    </div>
                    <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
                        {{ role.description || $t('rolesManager.permissionsCount', { n: rolePermissionCount(role) }) }}
                    </p>
                </div>
                <div class="flex items-center gap-3">
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
                                <span v-if="getRoleQuotaPolicies(role).length === 0" class="text-gray-400 text-sm italic">{{ $t('quotaPolicies.unlimited') }}</span>
                            </span>
                        </template>
                        <template #option="{ option }">
                            <span class="text-sm">{{ option.label }}</span>
                        </template>
                    </USelectMenu>
                    <div class="flex gap-2">
                        <UButton
                            v-if="!role.is_system && useCan('manage_roles')"
                            variant="ghost"
                            size="xs"
                            icon="i-heroicons-pencil"
                            @click="openEditModal(role)"
                        />
                        <UButton
                            v-if="!role.is_system && useCan('manage_roles')"
                            variant="ghost"
                            size="xs"
                            color="red"
                            icon="i-heroicons-trash"
                            @click="deleteRole(role)"
                        />
                    </div>
                </div>
            </div>
        </div>

        <!-- Create/Edit Modal -->
        <UModal v-model="showModal" :ui="{ width: 'sm:max-w-xl' }">
            <div class="p-6">
                <h3 class="text-lg font-medium mb-4">
                    {{ editingRole ? $t('rolesManager.editRole') : $t('rolesManager.createRole') }}
                </h3>

                <!-- Name + Description -->
                <div class="grid grid-cols-2 gap-3 mb-4">
                    <div>
                        <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{{ $t('rolesManager.nameLabel') }}</label>
                        <UInput v-model="form.name" :placeholder="$t('rolesManager.namePlaceholder')" size="sm" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{{ $t('rolesManager.descriptionLabel') }}</label>
                        <UInput v-model="form.description" :placeholder="$t('rolesManager.descriptionPlaceholder')" size="sm" />
                    </div>
                </div>

                <!-- Full Admin Toggle -->
                <div class="mb-5 px-3 py-2.5 bg-gray-50 dark:bg-gray-900 rounded-lg flex items-center justify-between">
                    <div>
                        <span class="text-sm font-medium">{{ $t('rolesManager.fullAdminAccess') }}</span>
                        <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('rolesManager.fullAdminBypass') }}</p>
                    </div>
                    <UToggle v-model="isFullAdmin" />
                </div>

                <!-- Permission cards (disabled when full admin) -->
                <div :class="{ 'opacity-40 pointer-events-none': isFullAdmin }" class="space-y-3">

                    <!-- Org-wide card -->
                    <div class="border rounded-lg overflow-hidden">
                        <div class="px-3 py-2 bg-gray-50 dark:bg-gray-900 border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UIcon name="i-heroicons-globe-alt" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
                                <span class="text-sm font-medium">{{ $t('rolesManager.allResources') }}</span>
                            </div>
                            <span class="text-xs text-gray-400">{{ $t('rolesManager.orgWidePermissions') }}</span>
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
                                    <span class="text-gray-700 dark:text-gray-300">{{ formatPermission(perm) }}</span>
                                </label>
                            </div>
                        </div>
                    </div>

                    <!-- Agent access + Connection access. Both sections render from
                         the same markup (accessSections) so they look and behave
                         identically: compact rows with an access-level select, and an
                         inline add panel that never leaves the role form. Everything
                         here only stages form state — nothing is saved until Save. -->
                    <div
                        v-for="sec in accessSections"
                        :key="sec.type"
                        class="border rounded-lg overflow-hidden"
                    >
                        <div class="px-3 py-2 bg-gray-50 dark:bg-gray-900 border-b flex items-center justify-between gap-2">
                            <div class="flex items-center gap-2 min-w-0">
                                <UIcon :name="sec.icon" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
                                <span class="text-sm font-medium">{{ sec.title }}</span>
                                <span v-if="sec.count" class="text-xs tabular-nums text-gray-400">{{ sec.count }}</span>
                            </div>
                            <UButton
                                v-if="!(picker.open && picker.type === sec.type)"
                                size="2xs"
                                variant="ghost"
                                color="gray"
                                icon="i-heroicons-plus"
                                :disabled="!sec.resources.length"
                                @click="openPicker(sec.type)"
                            >
                                {{ sec.addLabel }}
                            </UButton>
                        </div>

                        <!-- Inline add panel -->
                        <div v-if="picker.open && picker.type === sec.type" class="p-3 border-b bg-blue-50/40 dark:bg-blue-500/5 space-y-3">
                            <div class="flex items-center gap-2">
                                <UInput
                                    v-model="picker.search"
                                    :placeholder="sec.searchPlaceholder"
                                    icon="i-heroicons-magnifying-glass"
                                    size="xs"
                                    class="flex-1 min-w-0"
                                    autofocus
                                />
                                <USelect
                                    v-if="sec.type === 'data_source'"
                                    v-model="picker.stage"
                                    :options="pickerStageFilters"
                                    option-attribute="label"
                                    value-attribute="value"
                                    size="xs"
                                    class="w-44 shrink-0"
                                />
                            </div>

                            <div class="border rounded-md bg-white dark:bg-gray-900 overflow-hidden">
                                <label
                                    class="flex items-center gap-2 px-2.5 h-8 border-b text-xs text-gray-600 dark:text-gray-300"
                                    :class="pickerSelectableVisible.length ? 'cursor-pointer' : 'opacity-50'"
                                >
                                    <UCheckbox
                                        :model-value="pickerAllVisibleSelected"
                                        :disabled="!pickerSelectableVisible.length"
                                        size="xs"
                                        @update:model-value="toggleAllVisible($event)"
                                    />
                                    <span class="font-medium">{{ $t('rolesManager.bulk.selectAllVisible', { n: pickerSelectableVisible.length }) }}</span>
                                </label>
                                <div class="max-h-52 overflow-y-auto">
                                    <div v-if="!pickerVisible.length" class="px-3 py-4 text-xs text-center text-gray-400">
                                        {{ sec.noMatches }}
                                    </div>
                                    <label
                                        v-for="r in pickerVisible"
                                        :key="r.id"
                                        class="flex items-center gap-2 px-2.5 h-8 text-sm"
                                        :class="isInRole(r) ? 'text-gray-400 dark:text-gray-500' : 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 text-gray-800 dark:text-gray-200'"
                                    >
                                        <UCheckbox
                                            :model-value="isInRole(r) || picker.selected.has(r.id)"
                                            :disabled="isInRole(r)"
                                            size="xs"
                                            @update:model-value="togglePickerSelected(r.id, $event)"
                                        />
                                        <span class="min-w-0 truncate">{{ r.name }}</span>
                                        <span v-if="r.subtitle" class="text-[11px] text-gray-400 dark:text-gray-500 truncate">{{ r.subtitle }}</span>
                                        <span class="flex-1" />
                                        <span v-if="isInRole(r)" class="text-[11px] shrink-0">{{ $t('rolesManager.bulk.alreadyInRole') }}</span>
                                        <span
                                            v-else-if="picker.stage === 'all' && r.stage"
                                            :class="['inline-flex items-center h-4 px-1.5 rounded-full border text-[9px] font-semibold uppercase tracking-wider shrink-0', stageMeta(r.stage).badge]"
                                        >{{ $t(`agentsPage.stage.${r.stage}`) }}</span>
                                    </label>
                                </div>
                            </div>

                            <div>
                                <div class="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('rolesManager.bulk.accessLevel') }}</div>
                                <div class="inline-flex flex-wrap rounded-md border bg-white dark:bg-gray-900 p-0.5">
                                    <button
                                        v-for="opt in accessOptions(sec.type)"
                                        :key="opt.value"
                                        type="button"
                                        class="h-6 px-3 rounded text-xs"
                                        :class="picker.tier === opt.value ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900' : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'"
                                        @click="picker.tier = opt.value"
                                    >
                                        {{ opt.label }}
                                    </button>
                                </div>
                                <p class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">{{ accessHint(sec.type, picker.tier) }}</p>
                                <div v-if="picker.tier === 'custom'" class="grid grid-cols-2 gap-x-3 gap-y-1 mt-2">
                                    <label
                                        v-for="perm in getResourcePermissions(sec.type)"
                                        :key="perm"
                                        class="flex items-center gap-1.5 text-xs cursor-pointer text-gray-700 dark:text-gray-300"
                                    >
                                        <UCheckbox
                                            :model-value="picker.customPerms.includes(perm)"
                                            size="xs"
                                            @update:model-value="togglePickerCustomPerm(perm, $event)"
                                        />
                                        {{ formatPermission(perm) }}
                                    </label>
                                </div>
                            </div>

                            <div class="flex justify-end gap-2">
                                <UButton size="xs" variant="ghost" color="gray" @click="closePicker">{{ $t('rolesManager.cancel') }}</UButton>
                                <UButton size="xs" color="blue" :disabled="!picker.selected.size" @click="addPickerSelected">
                                    {{ $t(sec.addCountKey, { n: picker.selected.size }, picker.selected.size) }}
                                </UButton>
                            </div>
                        </div>

                        <div v-if="!sec.count && !(picker.open && picker.type === sec.type)" class="px-3 py-4 text-xs text-center text-gray-400">
                            {{ sec.emptyText }}
                        </div>

                        <template v-for="group in sec.groups" :key="group.key">
                            <div v-if="group.stage && sec.groups.length > 1" class="px-3 pt-3 pb-1 flex items-center gap-2">
                                <span :class="['inline-flex items-center h-5 px-2 rounded-full border text-[10px] font-semibold uppercase tracking-wider', stageMeta(group.stage).badge]">{{ $t(`agentsPage.stage.${group.stage}`) }}</span>
                                <span class="text-[11px] tabular-nums text-gray-400">{{ group.items.length }}</span>
                            </div>
                            <div
                                v-for="{ grant, idx } in group.items"
                                :key="`${grant.resource_type}-${grant.resource_id}`"
                                class="px-3 py-1.5"
                            >
                                <div class="flex items-center gap-2">
                                    <span class="min-w-0 truncate text-sm text-gray-800 dark:text-gray-200">{{ grant.resource_name }}</span>
                                    <span v-if="resourceSubtitle(grant)" class="text-[11px] text-gray-400 dark:text-gray-500 truncate">{{ resourceSubtitle(grant) }}</span>
                                    <span class="flex-1" />
                                    <USelect
                                        :model-value="grantAccess(grant)"
                                        :options="accessOptions(sec.type)"
                                        option-attribute="label"
                                        value-attribute="value"
                                        size="2xs"
                                        class="w-40 shrink-0"
                                        @update:model-value="setGrantAccess(grant, $event)"
                                    />
                                    <UButton
                                        variant="ghost"
                                        size="2xs"
                                        color="gray"
                                        icon="i-heroicons-x-mark"
                                        :aria-label="$t('rolesManager.remove')"
                                        @click="form.resourceGrants.splice(idx, 1)"
                                    />
                                </div>
                                <div v-if="grantAccess(grant) === 'custom'" class="grid grid-cols-2 gap-x-3 gap-y-1 mt-1.5 mb-1 ms-1 ps-3 border-s border-gray-200 dark:border-gray-700">
                                    <label
                                        v-for="perm in getResourcePermissions(sec.type)"
                                        :key="perm"
                                        class="flex items-center gap-1.5 text-xs cursor-pointer text-gray-700 dark:text-gray-300"
                                    >
                                        <UCheckbox
                                            :model-value="grant.permissions.includes(perm)"
                                            size="xs"
                                            @update:model-value="toggleResourcePerm(grant, perm, $event)"
                                        />
                                        {{ formatPermission(perm) }}
                                    </label>
                                </div>
                            </div>
                        </template>
                        <div v-if="sec.count" class="h-1.5" />
                    </div>
                    <!-- Model access (enterprise: per-model LLM access control) -->
                    <div v-if="showModelAccess" class="border rounded-lg overflow-hidden">
                        <div class="px-3 py-2 bg-gray-50 dark:bg-gray-900 border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UIcon name="i-heroicons-cpu-chip" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
                                <span class="text-sm font-medium">{{ $t('rolesManager.modelAccess') }}</span>
                            </div>
                            <span class="text-xs text-gray-400">{{ $t('rolesManager.modelAccessHint') }}</span>
                        </div>
                        <div class="p-3">
                            <div v-if="restrictedModels.length === 0" class="text-xs text-gray-400 py-0.5">
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
                                    <span class="text-gray-700 dark:text-gray-300 truncate">{{ model.name }}</span>
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
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { deriveStage, stageMeta, STAGE_OPTIONS, type AgentStage } from '~/composables/useDataSourcePublishStatus'
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
const saving = ref(false)
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

// ── Access levels (agents + connections) ─────────────────────────────────
// Each resource type has named tiers plus Custom, which exposes the raw
// permission checkboxes (what the old "Advanced permissions" toggle showed).
// A grant whose permissions match no tier always renders as Custom.
//
// Agents mirror AgentSettingsPanel's "Add people" modal: query (empty grant)
// or manage (`manage` implies the other manage_* perms server-side).
// Connections: manage_data_sources implies create_data_sources server-side
// (RESOURCE_PERM_IMPLIES), so each tier stores only its highest permission.

type ResourceType = 'data_source' | 'connection'
type AccessLevel = string

const ACCESS_TIERS: Record<ResourceType, { key: string; perms: string[] }[]> = {
    data_source: [
        { key: 'query', perms: [] },
        { key: 'manage', perms: ['manage'] },
    ],
    connection: [
        { key: 'createAgents', perms: ['create_data_sources'] },
        { key: 'manageAgents', perms: ['manage_data_sources'] },
        { key: 'full', perms: ['manage_connection', 'manage_data_sources'] },
    ],
}

const sameSet = (a: string[], b: string[]) =>
    a.length === b.length && a.every((x) => b.includes(x))

function grantTier(grant: ResourceGrantForm): string | null {
    const tiers = ACCESS_TIERS[grant.resource_type as ResourceType] || []
    return tiers.find((tier) => sameSet(tier.perms, grant.permissions))?.key ?? null
}

const tierKeyPrefix = (type: ResourceType) =>
    type === 'connection' ? 'rolesManager.connTiers' : 'rolesManager.tiers'

function accessOptions(type: ResourceType) {
    return [
        ...ACCESS_TIERS[type].map((tier) => ({ value: tier.key, label: t(`${tierKeyPrefix(type)}.${tier.key}`) })),
        { value: 'custom', label: t('rolesManager.tiers.custom') },
    ]
}

function accessHint(type: ResourceType, level: AccessLevel) {
    return level === 'custom'
        ? t('rolesManager.tiers.customHint')
        : t(`${tierKeyPrefix(type)}.${level}Hint`)
}

function grantAccess(grant: ResourceGrantForm): AccessLevel {
    if (grant.showAdvanced) return 'custom'
    return grantTier(grant) ?? 'custom'
}

function setGrantAccess(grant: ResourceGrantForm, level: AccessLevel) {
    if (level === 'custom') {
        grant.showAdvanced = true
        return
    }
    grant.showAdvanced = false
    const tiers = ACCESS_TIERS[grant.resource_type as ResourceType] || []
    grant.permissions = [...(tiers.find((tier) => tier.key === level)?.perms || [])]
}

// ── Available resources for the picker ───────────────────────────────────

interface AvailableResource {
    label: string
    value: string
    type: string
    id: string
    name: string
    subtitle?: string
    stage?: AgentStage
    is_public?: boolean
}

const availableResources = ref<AvailableResource[]>([])

async function loadResources() {
    try {
        // show_all: a role editor must be able to grant on private agents it is
        // not itself a member of. The backend honors it only for org-wide
        // data-source governance (full_admin_access / manage_connections) and
        // ignores it for everyone else, so it never widens what a caller sees.
        // include_unconnected keeps user-auth agents the caller hasn't signed
        // in to in the list.
        const dsResult = await useMyFetch(`/data_sources/active`, {
            query: { show_all: true, include_unconnected: true },
        })
        const resources: AvailableResource[] = []
        if (dsResult.data.value) {
            for (const ds of dsResult.data.value as any[]) {
                resources.push({
                    label: `Agent: ${ds.name}`,
                    value: `data_source:${ds.id}`,
                    type: 'data_source',
                    id: ds.id,
                    name: ds.name,
                    stage: deriveStage(ds.publish_status, ds.reliability_status),
                    is_public: ds.is_public,
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
                    name: c.name,
                    subtitle: c.type,
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

// ── Inline add panel ─────────────────────────────────────────────────────
// Stages one row per selected resource, all at the same access level. It is
// purely a shortcut for repeated single adds: the grants saved are the same
// per-resource rows, so an agent that later changes stage keeps its grant.

const picker = reactive({
    open: false,
    type: 'data_source' as ResourceType,
    search: '',
    stage: 'all' as AgentStage | 'all',
    selected: new Set<string>(),
    tier: 'query' as AccessLevel,
    customPerms: [] as string[],
})

function isInRole(r: AvailableResource) {
    return form.resourceGrants.some((g) => g.resource_type === r.type && g.resource_id === r.id)
}

const resourcesOf = (type: ResourceType) => availableResources.value.filter((r) => r.type === type)

const pickerSearchMatches = computed(() => {
    const q = picker.search.trim().toLowerCase()
    const all = resourcesOf(picker.type)
    return q
        ? all.filter((r) => r.name.toLowerCase().includes(q) || (r.subtitle || '').toLowerCase().includes(q))
        : all
})

const pickerStageFilters = computed(() => {
    const matches = pickerSearchMatches.value
    return [
        { value: 'all' as const, label: `${t('rolesManager.bulk.allStages')} (${matches.length})` },
        ...STAGE_OPTIONS.map((o) => ({
            value: o.value,
            label: `${t(`agentsPage.stage.${o.value}`)} (${matches.filter((r) => r.stage === o.value).length})`,
        })),
    ]
})

const pickerVisible = computed(() =>
    picker.stage === 'all'
        ? pickerSearchMatches.value
        : pickerSearchMatches.value.filter((r) => r.stage === picker.stage)
)
const pickerSelectableVisible = computed(() => pickerVisible.value.filter((r) => !isInRole(r)))
const pickerAllVisibleSelected = computed(() =>
    pickerSelectableVisible.value.length > 0
    && pickerSelectableVisible.value.every((r) => picker.selected.has(r.id))
)

function openPicker(type: ResourceType) {
    picker.type = type
    picker.search = ''
    picker.stage = 'all'
    picker.selected = new Set()
    picker.tier = ACCESS_TIERS[type][0].key
    picker.customPerms = []
    picker.open = true
}

function closePicker() {
    picker.open = false
}

function togglePickerSelected(id: string, checked: boolean) {
    const next = new Set(picker.selected)
    if (checked) next.add(id)
    else next.delete(id)
    picker.selected = next
}

// Acts on the visible rows only; selections hidden by the current filter stay.
function toggleAllVisible(checked: boolean) {
    const next = new Set(picker.selected)
    for (const r of pickerSelectableVisible.value) {
        if (checked) next.add(r.id)
        else next.delete(r.id)
    }
    picker.selected = next
}

function togglePickerCustomPerm(perm: string, checked: boolean) {
    picker.customPerms = checked
        ? [...new Set([...picker.customPerms, perm])]
        : picker.customPerms.filter((p) => p !== perm)
}

function addPickerSelected() {
    const custom = picker.tier === 'custom'
    const perms = custom
        ? picker.customPerms
        : ACCESS_TIERS[picker.type].find((tier) => tier.key === picker.tier)?.perms || []
    for (const r of resourcesOf(picker.type)) {
        if (!picker.selected.has(r.id) || isInRole(r)) continue
        form.resourceGrants.push({
            resource_type: r.type,
            resource_id: r.id,
            resource_name: r.name,
            permissions: [...perms],
            showAdvanced: custom,
        })
    }
    picker.open = false
}

// ── Section model ────────────────────────────────────────────────────────
// `idx` is the row's index in form.resourceGrants so removal keeps working.
// Agents group by lifecycle stage; connections are a single ungrouped list.

type IndexedGrant = { grant: ResourceGrantForm; idx: number }

function resourceSubtitle(grant: ResourceGrantForm) {
    return availableResources.value.find(
        (r) => r.type === grant.resource_type && r.id === grant.resource_id
    )?.subtitle
}

const accessSections = computed(() => {
    const stageById = new Map(resourcesOf('data_source').map((r) => [r.id, r.stage]))
    const agentsByStage = new Map<AgentStage, IndexedGrant[]>()
    const connections: IndexedGrant[] = []
    form.resourceGrants.forEach((grant, idx) => {
        if (grant.resource_type === 'connection') {
            connections.push({ grant, idx })
        } else if (grant.resource_type === 'data_source') {
            // An agent the list couldn't load (e.g. deleted) has no known stage;
            // it lands under Production, the default stage.
            const stage = stageById.get(grant.resource_id) || 'production'
            if (!agentsByStage.has(stage)) agentsByStage.set(stage, [])
            agentsByStage.get(stage)!.push({ grant, idx })
        }
    })
    const agentGroups = STAGE_OPTIONS
        .filter((o) => agentsByStage.has(o.value))
        .map((o) => ({ key: o.value as string, stage: o.value as AgentStage | null, items: agentsByStage.get(o.value)! }))
    return [
        {
            type: 'data_source' as ResourceType,
            icon: 'i-heroicons-cube',
            title: t('rolesManager.agentAccess'),
            addLabel: t('rolesManager.bulk.button'),
            searchPlaceholder: t('rolesManager.bulk.search'),
            noMatches: t('rolesManager.bulk.noMatches'),
            emptyText: t('rolesManager.noAgentAccess'),
            addCountKey: 'rolesManager.bulk.add',
            resources: resourcesOf('data_source'),
            count: agentGroups.reduce((n, g) => n + g.items.length, 0),
            groups: agentGroups,
        },
        {
            type: 'connection' as ResourceType,
            icon: 'i-heroicons-circle-stack',
            title: t('rolesManager.connectionAccess'),
            addLabel: t('rolesManager.bulk.buttonConnections'),
            searchPlaceholder: t('rolesManager.bulk.searchConnections'),
            noMatches: t('rolesManager.bulk.noConnectionMatches'),
            emptyText: t('rolesManager.noConnectionAccess'),
            addCountKey: 'rolesManager.bulk.addConnections',
            resources: resourcesOf('connection'),
            count: connections.length,
            groups: connections.length ? [{ key: 'connections', stage: null as AgentStage | null, items: connections }] : [],
        },
    ]
})

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
    picker.open = false
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
    picker.open = false
    showModal.value = true
    await loadResources()
    form.resourceGrants = (role.resource_grants || []).map((g) => {
        const found = availableResources.value.find(
            (r) => r.type === g.resource_type && r.id === g.resource_id
        )
        return {
            resource_type: g.resource_type,
            resource_id: g.resource_id,
            resource_name: found ? found.name : g.resource_id,
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

// Load on mount
onMounted(() => {
    loadPermissionsRegistry()
    loadRoles()
    loadUsagePolicies()
})

watch(showQuotaColumn, (enabled) => {
    if (enabled && usagePolicies.value.length === 0) {
        loadUsagePolicies()
    }
})
</script>
