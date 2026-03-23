<template>
    <div class="mt-4">
        <!-- Header with search and actions -->
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div class="flex-1 max-w-md w-full">
                <div class="relative">
                    <input
                        v-model="searchQuery"
                        type="text"
                        placeholder="Search roles..."
                        class="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    />
                    <UIcon
                        name="i-heroicons-magnifying-glass"
                        class="absolute left-3 top-2.5 h-4 w-4 text-gray-400"
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
                    New Role
                </UButton>
            </div>
        </div>

        <!-- Role cards -->
        <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden divide-y divide-gray-200">
            <!-- Loading state -->
            <div v-if="isLoading" class="px-6 py-12 text-center">
                <div class="flex items-center justify-center text-gray-500">
                    <Spinner class="w-4 h-4 mr-2" />
                    <span class="text-sm">Loading...</span>
                </div>
            </div>
            <!-- Empty state -->
            <div v-else-if="filteredRoles.length === 0" class="px-6 py-12 text-center">
                <div class="flex flex-col items-center">
                    <Icon name="heroicons:shield-check" class="mx-auto h-12 w-12 text-gray-400" />
                    <h3 class="mt-2 text-sm font-medium text-gray-900">No roles found</h3>
                    <p class="mt-1 text-sm text-gray-500">Create a role to manage permissions.</p>
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
                        <UBadge v-if="role.is_system" size="xs" color="gray">system</UBadge>
                        <UBadge
                            v-if="role.permissions?.includes('full_admin_access')"
                            size="xs"
                            color="primary"
                        >
                            Full Admin
                        </UBadge>
                    </div>
                    <p class="text-sm text-gray-500 mt-1">
                        {{ role.description || `${role.permissions?.length || 0} permissions` }}
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
        <UModal v-model="showModal" :ui="{ width: 'sm:max-w-2xl' }">
            <div class="p-6">
                <h3 class="text-lg font-medium mb-4">
                    {{ editingRole ? 'Edit Role' : 'Create Role' }}
                </h3>

                <!-- Name -->
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-1">Name</label>
                    <UInput v-model="form.name" placeholder="e.g. Analyst" />
                </div>

                <!-- Description -->
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-1">Description</label>
                    <UInput v-model="form.description" placeholder="Optional description" />
                </div>

                <!-- Full Admin Toggle -->
                <div class="mb-4 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                    <div class="flex items-center justify-between">
                        <div>
                            <span class="font-medium text-sm">Full Admin Access</span>
                            <p class="text-xs text-gray-500">
                                Bypasses all permission checks. At least one user must have this.
                            </p>
                        </div>
                        <UToggle v-model="isFullAdmin" />
                    </div>
                </div>

                <!-- Org Permissions (disabled when full admin) -->
                <div class="mb-4" :class="{ 'opacity-50 pointer-events-none': isFullAdmin }">
                    <label class="block text-sm font-medium mb-2">
                        {{ isFullAdmin ? 'Org Permissions (all granted automatically)' : 'Org Permissions' }}
                    </label>
                    <div class="max-h-64 overflow-y-auto border rounded-lg p-3 space-y-3">
                        <div
                            v-for="(perms, category) in permissionCategories"
                            :key="category"
                        >
                            <p class="text-xs font-semibold text-gray-500 uppercase mb-1">
                                {{ category }}
                            </p>
                            <div class="grid grid-cols-2 gap-1">
                                <label
                                    v-for="perm in perms"
                                    :key="perm"
                                    class="flex items-center gap-1.5 text-sm cursor-pointer"
                                >
                                    <UCheckbox
                                        :model-value="form.permissions.includes(perm)"
                                        @update:model-value="togglePermission(perm, $event)"
                                    />
                                    {{ formatPermission(perm) }}
                                </label>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Resource Permissions -->
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Resource Permissions</label>
                    <div
                        v-if="form.resourceGrants.length"
                        class="space-y-2 mb-2"
                    >
                        <div
                            v-for="(grant, idx) in form.resourceGrants"
                            :key="idx"
                            class="flex items-center gap-2 border rounded p-2"
                        >
                            <UBadge size="xs" :color="grant.resource_type === 'data_source' ? 'blue' : 'green'">
                                {{ grant.resource_type === 'data_source' ? 'DS' : 'Conn' }}
                            </UBadge>
                            <span class="text-sm flex-1">{{ grant.resource_name }}</span>
                            <USelectMenu
                                v-model="grant.permissions"
                                :options="getResourcePermOptions(grant.resource_type)"
                                multiple
                                size="xs"
                                class="w-48"
                            />
                            <UButton
                                variant="ghost"
                                size="xs"
                                color="red"
                                icon="i-heroicons-x-mark"
                                @click="form.resourceGrants.splice(idx, 1)"
                            />
                        </div>
                    </div>
                    <USelectMenu
                        v-model="selectedResource"
                        :options="availableResources"
                        option-attribute="label"
                        value-attribute="value"
                        searchable
                        placeholder="+ Add resource..."
                        @update:model-value="addResource"
                    />
                </div>

                <!-- Actions -->
                <div class="flex justify-end gap-2 mt-6">
                    <UButton variant="ghost" @click="showModal = false">Cancel</UButton>
                    <UButton color="blue" @click="saveRole" :loading="saving">
                        {{ editingRole ? 'Save' : 'Create' }}
                    </UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'

interface RoleData {
    id: string
    name: string
    description?: string
    permissions: string[]
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

// Permission categories for the checkbox grid
const permissionCategories: Record<string, string[]> = {
    Reports: ['view_reports', 'create_reports', 'update_reports', 'delete_reports', 'publish_reports', 'rerun_report_steps'],
    'Data Sources': ['view_data_source', 'create_data_source', 'update_data_source', 'delete_data_source', 'manage_data_source_memberships'],
    Connections: ['manage_connections', 'view_connections'],
    Widgets: ['view_widgets', 'create_widgets', 'update_widgets', 'delete_widgets', 'export_widgets', 'create_text_widgets', 'update_text_widgets', 'delete_text_widgets', 'view_text_widgets'],
    Files: ['view_files', 'upload_files', 'delete_files'],
    Members: ['view_organization_members', 'add_organization_members', 'update_organization_members', 'remove_organization_members', 'manage_roles', 'manage_groups', 'manage_role_assignments', 'manage_resource_grants'],
    Instructions: ['view_instructions', 'create_instructions', 'update_instructions', 'delete_instructions', 'create_private_instructions', 'update_private_instructions', 'delete_private_instructions', 'view_global_instructions', 'view_private_instructions', 'view_hidden_instructions', 'suggest_instructions'],
    Entities: ['view_entities', 'create_entities', 'update_entities', 'delete_entities', 'refresh_entities', 'approve_entities', 'reject_entities', 'suggest_entities', 'withdraw_entities'],
    Builds: ['view_builds', 'create_builds'],
    Settings: ['view_settings', 'modify_settings', 'manage_organization_settings', 'view_organization_settings', 'manage_organization_external_platforms', 'manage_llm_settings', 'view_llm_settings', 'view_organizations', 'view_organization_overview', 'manage_tests', 'train_mode'],
    Enterprise: ['view_audit_logs', 'manage_scim'],
    Feedback: ['create_completion_feedback', 'view_all_completion_feedbacks'],
}

const dsPermOptions = ['query', 'view_schema', 'upload_files', 'manage', 'manage_members']
const connPermOptions = ['use', 'manage', 'manage_credentials']

function getResourcePermOptions(type: string) {
    return type === 'data_source' ? dsPermOptions : connPermOptions
}

// Available resources for the autocomplete
const availableResources = ref<{ label: string; value: string; type: string; id: string }[]>([])

async function loadResources() {
    try {
        const [dsResult, connResult] = await Promise.all([
            useMyFetch(`/data_sources/active`),
            useMyFetch(`/connections`),
        ])
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
        if (connResult.data.value) {
            for (const conn of connResult.data.value as any[]) {
                resources.push({
                    label: `Connection: ${conn.name}`,
                    value: `connection:${conn.id}`,
                    type: 'connection',
                    id: conn.id,
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
    // Don't add duplicates
    if (form.resourceGrants.some((g) => g.resource_type === resource.type && g.resource_id === resource.id)) {
        selectedResource.value = null
        return
    }
    form.resourceGrants.push({
        resource_type: resource.type,
        resource_id: resource.id,
        resource_name: resource.label.replace(/^(Data Source|Connection): /, ''),
        permissions: resource.type === 'data_source' ? ['query', 'view_schema'] : ['use'],
    })
    selectedResource.value = null
}

function togglePermission(perm: string, checked: boolean) {
    if (checked) {
        if (!form.permissions.includes(perm)) form.permissions.push(perm)
    } else {
        form.permissions = form.permissions.filter((p) => p !== perm)
    }
}

function formatPermission(perm: string) {
    return perm.replace(/_/g, ' ')
}

const filteredRoles = computed(() => {
    const query = searchQuery.value.toLowerCase()
    if (!query) return roles.value
    return roles.value.filter(r =>
        r.name.toLowerCase().includes(query) ||
        (r.description || '').toLowerCase().includes(query)
    )
})

// CRUD
async function loadRoles() {
    isLoading.value = true
    try {
        const { data } = await useMyFetch(`/organizations/${props.organization.id}/roles`)
        if (data.value) {
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
    showModal.value = true
    loadResources()
}

function openEditModal(role: RoleData) {
    editingRole.value = role
    form.name = role.name
    form.description = role.description || ''
    form.permissions = [...(role.permissions || [])]
    form.resourceGrants = []
    showModal.value = true
    loadResources()
    // Load resource grants for this role (TODO: need backend filter by role context)
}

async function saveRole() {
    saving.value = true
    try {
        const body = {
            name: form.name,
            description: form.description || null,
            permissions: isFullAdmin.value ? ['full_admin_access'] : form.permissions,
        }

        if (editingRole.value) {
            const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles/${editingRole.value.id}`, {
                method: 'PUT',
                body,
            })
            if (error.value) {
                const detail = error.value.data?.detail || 'Failed to update role'
                toast.add({ title: detail, color: 'red' })
                return
            }
            toast.add({ title: 'Role updated' })
        } else {
            const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles`, {
                method: 'POST',
                body,
            })
            if (error.value) {
                const detail = error.value.data?.detail || 'Failed to create role'
                toast.add({ title: detail, color: 'red' })
                return
            }
            toast.add({ title: 'Role created' })
        }

        showModal.value = false
        await loadRoles()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to save role'
        toast.add({ title: detail, color: 'red' })
    } finally {
        saving.value = false
    }
}

async function deleteRole(role: RoleData) {
    if (!confirm(`Delete role "${role.name}"?`)) return
    try {
        const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles/${role.id}`, {
            method: 'DELETE',
        })
        if (error.value) {
            const detail = error.value.data?.detail || 'Failed to delete role'
            toast.add({ title: detail, color: 'red' })
            return
        }
        toast.add({ title: 'Role deleted' })
        await loadRoles()
    } catch (e: any) {
        const detail = e?.data?.detail || e?.message || 'Failed to delete role'
        toast.add({ title: detail, color: 'red' })
    }
}

// Load on mount
onMounted(() => {
    loadRoles()
})
</script>
