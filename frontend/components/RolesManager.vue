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
                        class="w-full ps-10 pe-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
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
        <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden divide-y divide-gray-200">
            <div v-if="isLoading" class="px-6 py-12 text-center">
                <div class="flex items-center justify-center text-gray-500">
                    <Spinner class="w-4 h-4 me-2" />
                    <span class="text-sm">{{ $t('rolesManager.loading') }}</span>
                </div>
            </div>
            <div v-else-if="filteredRoles.length === 0" class="px-6 py-12 text-center">
                <div class="flex flex-col items-center">
                    <Icon name="heroicons:shield-check" class="mx-auto h-12 w-12 text-gray-400" />
                    <h3 class="mt-2 text-sm font-medium text-gray-900">{{ $t('rolesManager.noRolesFound') }}</h3>
                    <p class="mt-1 text-sm text-gray-500">{{ $t('rolesManager.noRolesHint') }}</p>
                </div>
            </div>
            <div
                v-else
                v-for="role in filteredRoles"
                :key="role.id"
                class="p-4 flex items-center justify-between hover:bg-gray-50"
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
                    <p class="text-sm text-gray-500 mt-1">
                        {{ role.description || $t('rolesManager.permissionsCount', { n: role.permissions?.length || 0 }) }}
                    </p>
                </div>
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

        <!-- Create/Edit Modal -->
        <UModal v-model="showModal" :ui="{ width: 'sm:max-w-xl' }">
            <div class="p-6">
                <h3 class="text-lg font-medium mb-4">
                    {{ editingRole ? $t('rolesManager.editRole') : $t('rolesManager.createRole') }}
                </h3>

                <!-- Name + Description -->
                <div class="grid grid-cols-2 gap-3 mb-4">
                    <div>
                        <label class="block text-xs font-medium text-gray-500 mb-1">{{ $t('rolesManager.nameLabel') }}</label>
                        <UInput v-model="form.name" :placeholder="$t('rolesManager.namePlaceholder')" size="sm" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-gray-500 mb-1">{{ $t('rolesManager.descriptionLabel') }}</label>
                        <UInput v-model="form.description" :placeholder="$t('rolesManager.descriptionPlaceholder')" size="sm" />
                    </div>
                </div>

                <!-- Full Admin Toggle -->
                <div class="mb-5 px-3 py-2.5 bg-gray-50 rounded-lg flex items-center justify-between">
                    <div>
                        <span class="text-sm font-medium">{{ $t('rolesManager.fullAdminAccess') }}</span>
                        <p class="text-xs text-gray-500">{{ $t('rolesManager.fullAdminBypass') }}</p>
                    </div>
                    <UToggle v-model="isFullAdmin" />
                </div>

                <!-- Permission cards (disabled when full admin) -->
                <div :class="{ 'opacity-40 pointer-events-none': isFullAdmin }" class="space-y-3">

                    <!-- Org-wide card -->
                    <div class="border rounded-lg overflow-hidden">
                        <div class="px-3 py-2 bg-gray-50 border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UIcon name="i-heroicons-globe-alt" class="w-4 h-4 text-gray-500" />
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
                                    <span class="text-gray-700">{{ formatPermission(perm) }}</span>
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
                        <div class="px-3 py-2 bg-gray-50 border-b flex items-center justify-between">
                            <div class="flex items-center gap-2">
                                <UBadge size="xs" color="blue">{{ $t('rolesManager.ds') }}</UBadge>
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
                            <!-- Flat checkbox UI for all resource permissions -->
                            <div class="grid grid-cols-2 gap-x-3 gap-y-1">
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
                                    <span class="text-gray-700">{{ formatPermission(perm) }}</span>
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
                        :placeholder="$t('rolesManager.addDataSource')"
                        @update:model-value="addResource"
                        size="sm"
                    />
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
import { useI18n } from 'vue-i18n'

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
}

const props = defineProps<{
    organization: { id: string; name: string }
}>()

const toast = useToast()

// State
const roles = ref<RoleData[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showModal = ref(false)
const editingRole = ref<RoleData | null>(null)
const saving = ref(false)
const selectedResource = ref(null)
const showOrgDetails = ref(false)

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

// ── Available resources for the picker ───────────────────────────────────

const availableResources = ref<{ label: string; value: string; type: string; id: string }[]>([])

async function loadResources() {
    try {
        const dsResult = await useMyFetch(`/data_sources/active`)
        const resources: any[] = []
        if (dsResult.data.value) {
            for (const ds of dsResult.data.value as any[]) {
                resources.push({
                    label: `Data Source: ${ds.name}`,
                    value: `data_source:${ds.id}`,
                    type: 'data_source',
                    id: ds.id,
                })
            }
        }
        availableResources.value = resources
    } catch (e) {
        console.error('Failed to load resources', e)
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
        resource_name: resource.label.replace(/^(Data Source|Connection): /, ''),
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
    'create_data_source',
    'manage_connections',
    'manage_instructions',
    'manage_entities',
    'manage_evals',
    'view_members',
    'manage_members',
    'manage_settings',
    'manage_llm',
    'view_audit_logs',
    'manage_identity_providers',
    'view',
    'view_schema',
    'create_entities',
    'manage',
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

function openCreateModal() {
    editingRole.value = null
    form.name = ''
    form.description = ''
    form.permissions = []
    form.resourceGrants = []
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
            resource_name: found ? found.label.replace(/^(Data Source|Connection): /, '') : g.resource_id,
            permissions: [...(g.permissions || [])],
        }
    })
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
})
</script>
