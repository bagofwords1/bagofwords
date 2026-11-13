<template>
    <div>
        <!-- Primary action lives in the page header (settings shell). -->
        <Teleport to="#settings-page-actions" defer>
            <UButton v-if="canManageRoles" color="blue" icon="i-heroicons-plus" @click="openCreateModal()">
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

        <!-- Roles: a list rather than a table; each row is a role with its
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
                v-for="role in filteredRoles"
                v-else
                :key="role.id"
                class="px-5 py-4 flex items-center justify-between gap-4 hover:bg-ink/[0.03] transition-colors"
                :class="{ 'cursor-pointer': canEdit(role) }"
                :data-testid="`role-row-${role.name}`"
                @click="canEdit(role) && openEditModal(role)"
            >
                <div class="min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="text-sm font-medium text-ink" v-text-dir>{{ role.name }}</span>
                        <UBadge v-if="isOrgDefaultRole(role)" size="xs" color="gray" variant="subtle">{{ $t('rolesManager.default') }}</UBadge>
                        <UBadge v-if="isFullAdminRole(role)" size="xs" color="blue" variant="subtle">{{ $t('rolesManager.fullAdmin') }}</UBadge>
                    </div>
                    <p class="text-xs text-ink-muted mt-0.5 truncate">
                        <span v-if="role.description" v-text-dir>{{ role.description }} · </span>{{ roleMeta(role) }}
                    </p>
                </div>
                <div class="flex items-center gap-2 shrink-0" @click.stop>
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
                    <UDropdown v-if="rowActions(role).length" :items="rowActions(role)" :popper="{ placement: 'bottom-end' }">
                        <UButton
                            variant="ghost"
                            color="gray"
                            size="xs"
                            icon="i-heroicons-ellipsis-horizontal"
                            :aria-label="$t('rolesManager.moreActions')"
                            :data-testid="`role-actions-${role.name}`"
                        />
                    </UDropdown>
                </div>
            </div>
        </div>

        <!-- Create / edit -->
        <UModal v-model="showModal" :prevent-close="saving" :ui="{ width: 'sm:max-w-2xl' }">
            <div class="flex flex-col max-h-[90vh]" data-testid="role-modal">
                <div class="px-6 pt-5">
                    <div class="flex items-center justify-between">
                        <h3 class="text-base font-semibold text-ink">
                            {{ editingRole ? $t('rolesManager.editRole') : $t('rolesManager.createRole') }}
                        </h3>
                        <UButton
                            variant="ghost"
                            color="gray"
                            size="xs"
                            icon="i-heroicons-x-mark"
                            :aria-label="$t('common.close')"
                            :disabled="saving"
                            @click="showModal = false"
                        />
                    </div>
                    <p v-if="editingBuiltinRole" class="text-xs text-ink-muted mt-1">{{ $t('rolesManager.defaultRoleHint') }}</p>

                    <div class="grid sm:grid-cols-2 gap-3 mt-4">
                        <div>
                            <label for="role-name" class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.nameLabel') }}</label>
                            <UInput id="role-name" v-model="form.name" v-text-dir :placeholder="$t('rolesManager.namePlaceholder')" :disabled="editingBuiltinRole" />
                        </div>
                        <div>
                            <label for="role-description" class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.descriptionLabel') }}</label>
                            <UInput id="role-description" v-model="form.description" v-text-dir :placeholder="$t('rolesManager.descriptionPlaceholder')" />
                        </div>
                    </div>

                    <!-- Tabs -->
                    <div class="flex gap-5 mt-5 border-b border-line" role="tablist">
                        <button
                            v-for="tab in tabs"
                            :key="tab.key"
                            type="button"
                            role="tab"
                            :aria-selected="activeTab === tab.key"
                            class="-mb-px pb-2 text-sm border-b-2 transition-colors"
                            :class="activeTab === tab.key ? 'border-blue-500 text-ink font-medium' : 'border-transparent text-ink-muted hover:text-ink'"
                            :data-testid="`role-tab-${tab.key}`"
                            @click="activeTab = tab.key"
                        >
                            {{ tab.label }}
                            <span v-if="tab.count" class="ms-1 text-xs text-ink-muted">{{ tab.count }}</span>
                        </button>
                    </div>
                </div>

                <!-- Body: fixed height so switching tabs or finishing a load never jumps -->
                <div class="h-[26rem] overflow-y-auto px-6" :class="{ 'opacity-60 pointer-events-none': saving }">
                    <div v-if="loadState === 'loading'" class="h-full flex items-center justify-center text-ink-muted" data-testid="role-loading">
                        <Spinner class="w-4 h-4 me-2" />
                        <span class="text-sm">{{ $t('rolesManager.loadingRole') }}</span>
                    </div>

                    <div v-else-if="loadState === 'error'" class="h-full flex flex-col items-center justify-center gap-3 text-center" data-testid="role-load-error">
                        <p class="text-sm text-ink-2">{{ $t('rolesManager.loadFailed') }}</p>
                        <UButton size="xs" color="gray" variant="solid" @click="retryLoad">{{ $t('rolesManager.retry') }}</UButton>
                    </div>

                    <!-- Permissions -->
                    <div v-else-if="activeTab === 'permissions'" class="py-4" data-testid="role-permissions">
                        <label class="flex items-start gap-3 py-2 cursor-pointer">
                            <UCheckbox v-model="isFullAdmin" class="mt-0.5" />
                            <span>
                                <span class="block text-sm text-ink">{{ $t('rolesManager.fullAdminAccess') }}</span>
                                <span class="block text-xs text-ink-muted">{{ $t('rolesManager.fullAdminHint') }}</span>
                            </span>
                        </label>

                        <template v-if="!isFullAdmin">
                            <div v-for="group in permissionGroups" :key="group.key" class="mt-4">
                                <div class="text-xs font-medium text-ink-muted mb-1">{{ $t(`rolesManager.groups.${group.key}`) }}</div>
                                <label
                                    v-for="perm in group.perms"
                                    :key="perm"
                                    class="flex items-start gap-3 py-1.5"
                                    :class="impliedBy(perm) ? 'cursor-default' : 'cursor-pointer'"
                                >
                                    <UCheckbox
                                        :model-value="form.permissions.includes(perm) || !!impliedBy(perm)"
                                        :disabled="!!impliedBy(perm)"
                                        class="mt-0.5"
                                        @update:model-value="togglePermission(perm, $event)"
                                    />
                                    <span>
                                        <span class="block text-sm text-ink">{{ formatPermission(perm) }}</span>
                                        <span class="block text-xs text-ink-muted">
                                            {{ impliedBy(perm) ? $t('rolesManager.includedWith', { name: formatPermission(impliedBy(perm)!) }) : permissionHint(perm) }}
                                        </span>
                                    </span>
                                </label>
                            </div>
                        </template>
                    </div>

                    <!-- Access -->
                    <div v-else class="py-4" data-testid="role-access">
                        <p v-if="isFullAdmin" class="text-sm text-ink-muted py-2">{{ $t('rolesManager.fullAdminAccessTab') }}</p>
                        <template v-else>
                            <USelectMenu
                                v-model="selectedResource"
                                :options="addableResources"
                                option-attribute="label"
                                value-attribute="value"
                                searchable
                                :searchable-placeholder="$t('rolesManager.searchResources')"
                                :placeholder="$t('rolesManager.addAccess')"
                                data-testid="role-add-access"
                                @update:model-value="addResource"
                            >
                                <template #option="{ option }">
                                    <span class="flex items-center gap-2 min-w-0">
                                        <UIcon :name="TYPE_ICON[option.type]" class="w-4 h-4 text-ink-muted shrink-0" />
                                        <span class="truncate">{{ option.label }}</span>
                                        <span class="text-xs text-ink-muted ms-auto">{{ $t(`rolesManager.types.${option.type}`) }}</span>
                                    </span>
                                </template>
                            </USelectMenu>

                            <ul v-if="visibleGrants.length" class="mt-3 divide-y divide-line border-y border-line">
                                <li v-for="grant in visibleGrants" :key="`${grant.resource_type}:${grant.resource_id}`" class="py-2">
                                    <div class="flex items-center gap-3">
                                        <UIcon :name="TYPE_ICON[grant.resource_type]" class="w-4 h-4 text-ink-muted shrink-0" />
                                        <div class="flex-1 min-w-0">
                                            <div class="text-sm text-ink truncate" v-text-dir>{{ grant.resource_name }}</div>
                                            <div class="text-xs text-ink-muted">{{ $t(`rolesManager.types.${grant.resource_type}`) }}</div>
                                        </div>
                                        <USelect
                                            v-if="LEVELS[grant.resource_type]"
                                            :model-value="grantLevel(grant)"
                                            :options="levelOptions(grant)"
                                            option-attribute="label"
                                            value-attribute="value"
                                            size="sm"
                                            class="w-36 sm:w-44 shrink-0"
                                            :aria-label="grant.resource_name"
                                            @update:model-value="setGrantLevel(grant, $event)"
                                        />
                                        <span v-else class="w-36 sm:w-44 shrink-0 text-sm text-ink-2 px-2.5">{{ $t('rolesManager.levels.use') }}</span>
                                        <UButton
                                            variant="ghost"
                                            color="gray"
                                            size="xs"
                                            icon="i-heroicons-x-mark"
                                            :aria-label="$t('rolesManager.removeAccess')"
                                            @click="removeGrant(grant)"
                                        />
                                    </div>
                                    <!-- Custom: the grant's individual permissions -->
                                    <div
                                        v-if="grantLevel(grant) === 'custom'"
                                        class="mt-2 ms-7 grid sm:grid-cols-2 gap-x-4 gap-y-1"
                                        :data-testid="`role-custom-${grant.resource_id}`"
                                    >
                                        <label
                                            v-for="perm in customPermissions(grant.resource_type)"
                                            :key="perm"
                                            class="flex items-center gap-2 text-xs"
                                            :class="resourceImpliedBy(grant, perm) ? 'cursor-default' : 'cursor-pointer'"
                                        >
                                            <UCheckbox
                                                :model-value="grant.permissions.includes(perm) || !!resourceImpliedBy(grant, perm)"
                                                :disabled="!!resourceImpliedBy(grant, perm)"
                                                @update:model-value="toggleGrantPermission(grant, perm, $event)"
                                            />
                                            <span class="text-ink-2">{{ resourcePermissionLabel(grant.resource_type, perm) }}</span>
                                        </label>
                                    </div>
                                </li>
                            </ul>
                            <p v-else class="mt-6 text-sm text-ink-muted text-center">{{ $t('rolesManager.accessEmpty') }}</p>

                            <p v-if="publicAgentCount > 0" class="mt-3 text-xs text-ink-muted">
                                {{ $t('rolesManager.publicAgents', { n: publicAgentCount }) }}
                            </p>
                        </template>
                    </div>
                </div>

                <div class="px-6 py-3 border-t border-line flex justify-end gap-2">
                    <UButton variant="ghost" color="gray" :disabled="saving" @click="showModal = false">{{ $t('rolesManager.cancel') }}</UButton>
                    <UButton color="blue" :disabled="!canSave" :loading="saving" data-testid="role-save" @click="saveRole">
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
import Spinner from '@/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useI18n } from 'vue-i18n'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { t } = useI18n()
const toast = useToast()
const { hasFeature } = useEnterprise()

const FULL_ADMIN = 'full_admin_access'

interface Grant {
    resource_type: string
    resource_id: string
    permissions: string[]
}

interface RoleData {
    id: string
    name: string
    description?: string
    permissions: string[]
    resource_grants?: Grant[]
    is_system: boolean
    organization_id?: string
    overrides_system_role?: string | null
    is_editable?: boolean
    is_deletable?: boolean
}

interface GrantForm extends Grant {
    resource_name: string
    // The admin chose "Custom": show the individual permissions even when
    // they happen to match a standard level.
    custom?: boolean
}

interface UsagePolicyAssignment {
    principal_type: 'user' | 'group' | 'role'
    principal_id: string
}

interface UsagePolicySummary {
    id: string
    name: string
    enabled: boolean
    assignments: UsagePolicyAssignment[]
}

interface RestrictedModel {
    model_id: string
    name: string
    provider_name: string
}

interface ResourceOption {
    label: string
    value: string
    type: 'data_source' | 'connection' | 'llm_model'
    id: string
}

const props = defineProps<{
    organization: { id: string; name: string }
}>()

// ── Role helpers ─────────────────────────────────────────────────────────

const defaultMemberRole = ref('member')
const isOrgDefaultRole = (role: RoleData) => role.name === defaultMemberRole.value
const isFullAdminRole = (role: RoleData) => !!role.permissions?.includes(FULL_ADMIN)
// The built-in `member` role (or an org's override of it): editable, but its
// name is fixed and it can't be deleted.
const isBuiltinRole = (role: RoleData) =>
    !!role.overrides_system_role || (role.is_system && role.is_editable === true)

const canManageRoles = computed(() => useCan('manage_roles'))
const canEdit = (role: RoleData) => !!role.is_editable && canManageRoles.value

// ── State ────────────────────────────────────────────────────────────────

const roles = ref<RoleData[]>([])
const usagePolicies = ref<UsagePolicySummary[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showModal = ref(false)
const editingRole = ref<RoleData | null>(null)
const editingBuiltinRole = computed(() => !!editingRole.value && isBuiltinRole(editingRole.value))
const saving = ref(false)
const selectedResource = ref<string | null>(null)
const activeTab = ref<'permissions' | 'access'>('permissions')
const showQuotaColumn = computed(() => hasFeature('usage_limits') && useCan('manage_settings'))

// The editor is either loading, failed, or ready — never half loaded. (It
// used to show the form before grants arrived, with Save enabled, so an
// early Save replaced the role's grants with an empty list.)
const loadState = ref<'loading' | 'error' | 'ready'>('loading')
let pendingLoad: (() => Promise<void>) | null = null

const form = reactive({
    name: '',
    description: '',
    permissions: [] as string[],
    grants: [] as GrantForm[],
})

const isFullAdmin = computed({
    get: () => form.permissions.includes(FULL_ADMIN),
    set: (on: boolean) => {
        form.permissions = form.permissions.filter((p) => p !== FULL_ADMIN)
        if (on) form.permissions.push(FULL_ADMIN)
    },
})

// ── Editor data (cached, prefetched when the tab opens) ──────────────────

const allCategories = ref<Record<string, string[]>>({})
const resourcePermissions = ref<Record<string, string[]>>({})
const availableResources = ref<ResourceOption[]>([])
const publicAgentCount = ref(0)
// Restricted-model access is a role grant like any other (`llm_model`,
// `['use']`) and is saved by the same PUT as the rest of the role. The role
// PUT replaces ALL of the role's grants, so writing models through a separate
// endpoint (as before) let a later Save silently drop them.
const restrictedModels = ref<RestrictedModel[]>([])
const canManageModels = computed(() => hasFeature('llm_access_control') && useCan('manage_llm'))

function cached(fn: () => Promise<void>): () => Promise<void> {
    let p: Promise<void> | null = null
    return () => {
        if (!p) p = fn().catch((e) => { p = null; throw e })
        return p
    }
}

const ensureRegistry = cached(async () => {
    const { data, error } = await useMyFetch('/permissions/registry')
    if (error.value || !data.value) throw error.value || new Error('registry')
    allCategories.value = (data.value as any).categories || {}
    resourcePermissions.value = (data.value as any).resource_permissions || {}
})

const ensureResources = cached(async () => {
    // show_all: org admins see every agent, not only those they're a member of.
    const [ds, conns] = await Promise.all([
        useMyFetch('/data_sources/active?show_all=true'),
        useMyFetch('/connections'),
    ])
    if (ds.error.value) throw ds.error.value
    if (conns.error.value) throw conns.error.value
    const agents = (ds.data.value || []) as any[]
    publicAgentCount.value = agents.filter((a) => a.is_public).length
    availableResources.value = [
        ...agents.map((a) => ({ label: a.name, value: `data_source:${a.id}`, type: 'data_source' as const, id: a.id })),
        ...((conns.data.value || []) as any[]).map((c) => ({ label: c.name, value: `connection:${c.id}`, type: 'connection' as const, id: c.id })),
    ]
})

const ensureModels = cached(async () => {
    if (!canManageModels.value) return
    // Keyed by principal; only the list of restricted models is used here.
    const { data, error } = await useMyFetch('/llm/model-access/by-principal?principal_type=role&principal_id=__role_editor__')
    if (error.value) throw error.value
    restrictedModels.value = (data.value || []) as RestrictedModel[]
})

const ensureEditorData = () => Promise.all([ensureRegistry(), ensureResources(), ensureModels()])

// ── Permissions tab ──────────────────────────────────────────────────────

// Display groups. Anything the server registers that isn't listed here shows
// under "Other", so a new permission is never hidden from the editor.
const GROUPS: { key: string; perms: string[] }[] = [
    { key: 'agents', perms: ['create_data_source', 'create_connections', 'manage_connections'] },
    { key: 'knowledge', perms: ['manage_instructions', 'manage_entities', 'manage_evals'] },
    { key: 'code', perms: ['view_code', 'run_custom_code'] },
    { key: 'admin', perms: ['manage_members', 'manage_service_accounts', 'manage_settings', 'manage_llm', 'view_audit_logs', 'manage_identity_providers'] },
]

const registeredPerms = computed(() => Object.values(allCategories.value).flat())

const permissionGroups = computed(() => {
    const known = new Set(registeredPerms.value)
    const grouped = new Set(GROUPS.flatMap((g) => g.perms))
    const out = GROUPS
        .map((g) => ({ key: g.key, perms: g.perms.filter((p) => known.has(p)) }))
        .filter((g) => g.perms.length)
    const other = registeredPerms.value.filter((p) => !grouped.has(p))
    if (other.length) out.push({ key: 'other', perms: other })
    return out
})

// Mirrors permission_resolver.ORG_PERM_IMPLIES_ORG.
const ORG_IMPLIED_BY: Record<string, string> = {
    view_code: 'run_custom_code',
    create_connections: 'manage_connections',
}

function impliedBy(perm: string): string | null {
    const by = ORG_IMPLIED_BY[perm]
    return by && form.permissions.includes(by) ? by : null
}

function togglePermission(perm: string, on: boolean) {
    form.permissions = form.permissions.filter((p) => p !== perm)
    if (on) form.permissions.push(perm)
}

const KNOWN_PERMISSIONS = new Set([
    'manage_files', 'view_code', 'run_custom_code', 'create_data_source', 'create_connections',
    'manage_connections', 'manage_instructions', 'manage_entities', 'manage_evals', 'view_members',
    'manage_members', 'manage_service_accounts', 'manage_settings', 'manage_llm', 'view_audit_logs',
    'manage_identity_providers', 'view', 'view_schema', 'create_entities', 'manage',
    'manage_connection', 'create_data_sources', 'manage_data_sources',
])

function formatPermission(perm: string) {
    if (KNOWN_PERMISSIONS.has(perm)) return t(`rolesManager.permissions.${perm}`)
    return perm.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

const HINTED = new Set(GROUPS.flatMap((g) => g.perms))
const permissionHint = (perm: string) => (HINTED.has(perm) ? t(`rolesManager.permissionHints.${perm}`) : '')

// ── Access tab ───────────────────────────────────────────────────────────

const TYPE_ICON: Record<string, string> = {
    data_source: 'i-heroicons-cube',
    connection: 'i-heroicons-circle-stack',
    llm_model: 'i-heroicons-cpu-chip',
}

// One access level per agent / connection, like Retool's Use / Edit / Own.
// A grant matching no level (set elsewhere or by the old editor) shows as
// "Custom" and is saved unchanged unless the admin picks a level.
const LEVELS: Record<string, { key: string; perms: string[] }[]> = {
    data_source: [
        { key: 'use', perms: [] },
        { key: 'manage', perms: ['manage'] },
    ],
    connection: [
        { key: 'build', perms: ['create_data_sources'] },
        { key: 'manage', perms: ['manage_connection', 'create_data_sources'] },
    ],
}
const MODEL_USE = ['use']

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x))

function grantLevel(grant: GrantForm): string {
    if (grant.custom) return 'custom'
    return LEVELS[grant.resource_type]?.find((l) => sameSet(l.perms, grant.permissions))?.key ?? 'custom'
}

function levelOptions(grant: GrantForm) {
    return [
        ...(LEVELS[grant.resource_type] || []).map((l) => ({ value: l.key, label: t(`rolesManager.levels.${l.key}`) })),
        { value: 'custom', label: t('rolesManager.levels.custom') },
    ]
}

function setGrantLevel(grant: GrantForm, key: string) {
    if (key === 'custom') {
        grant.custom = true
        return
    }
    const level = LEVELS[grant.resource_type]?.find((l) => l.key === key)
    if (level) {
        grant.custom = false
        grant.permissions = [...level.perms]
    }
}

// Mirrors permission_resolver.RESOURCE_PERM_IMPLIES: a permission held via a
// broader one shows checked and locked.
const RESOURCE_IMPLIED_BY: Record<string, Record<string, string>> = {
    data_source: {
        manage_instructions: 'manage',
        create_entities: 'manage',
        manage_evals: 'manage',
        manage_members: 'manage',
    },
    connection: {
        create_data_sources: 'manage_data_sources',
    },
}

function resourceImpliedBy(grant: Grant, perm: string): string | null {
    const by = RESOURCE_IMPLIED_BY[grant.resource_type]?.[perm]
    return by && grant.permissions.includes(by) ? by : null
}

function toggleGrantPermission(grant: GrantForm, perm: string, on: boolean) {
    grant.permissions = grant.permissions.filter((p) => p !== perm)
    if (on) grant.permissions.push(perm)
}

const RESOURCE_PERMISSION_KEYS: Record<string, Set<string>> = {
    data_source: new Set(['manage', 'manage_instructions', 'create_entities', 'manage_evals', 'manage_members']),
    connection: new Set(['manage_connection', 'create_data_sources', 'manage_data_sources']),
}

// The registry's per-type list, with the permission that includes the others
// first (it locks them when checked).
function customPermissions(type: string): string[] {
    const perms = resourcePermissions.value[type] || []
    const broad = new Set(Object.values(RESOURCE_IMPLIED_BY[type] || {}))
    return [...perms.filter((p) => broad.has(p)), ...perms.filter((p) => !broad.has(p))]
}

function resourcePermissionLabel(type: string, perm: string): string {
    if (RESOURCE_PERMISSION_KEYS[type]?.has(perm)) return t(`rolesManager.resourcePermissions.${type}.${perm}`)
    return formatPermission(perm)
}

function resourceName(type: string, id: string): string {
    if (type === 'llm_model') return restrictedModels.value.find((m) => m.model_id === id)?.name || id
    return availableResources.value.find((r) => r.type === type && r.id === id)?.label || id
}

// Model grants are only shown to admins who can manage models; for anyone
// else they stay in the form untouched and are saved back as they were.
const visibleGrants = computed(() =>
    form.grants.filter((g) => g.resource_type !== 'llm_model' || canManageModels.value))

const addableResources = computed<ResourceOption[]>(() => {
    const granted = new Set(form.grants.map((g) => `${g.resource_type}:${g.resource_id}`))
    const models = restrictedModels.value.map((m) => ({
        label: m.name, value: `llm_model:${m.model_id}`, type: 'llm_model' as const, id: m.model_id,
    }))
    return [...availableResources.value, ...models].filter((r) => !granted.has(r.value))
})

function addResource(value: string | null) {
    selectedResource.value = null
    if (!value) return
    const i = value.indexOf(':')
    const type = value.slice(0, i)
    const id = value.slice(i + 1)
    if (form.grants.some((g) => g.resource_type === type && g.resource_id === id)) return
    form.grants.push({
        resource_type: type,
        resource_id: id,
        resource_name: resourceName(type, id),
        permissions: type === 'llm_model' ? [...MODEL_USE] : [...(LEVELS[type]?.[0]?.perms || [])],
    })
}

function removeGrant(grant: GrantForm) {
    form.grants = form.grants.filter((g) => g !== grant)
}

const tabs = computed(() => [
    { key: 'permissions' as const, label: t('rolesManager.tabs.permissions'), count: 0 },
    { key: 'access' as const, label: t('rolesManager.tabs.access'), count: isFullAdmin.value ? 0 : visibleGrants.value.length },
])

// ── Dirty tracking / save gating ─────────────────────────────────────────

function snapshot(): string {
    return JSON.stringify([
        form.name, form.description, [...form.permissions].sort(),
        form.grants.map((g) => [g.resource_type, g.resource_id, [...g.permissions].sort()]),
    ])
}
const baseline = ref('')
const canSave = computed(() =>
    loadState.value === 'ready' && !saving.value && !!form.name.trim() &&
    (!editingRole.value || snapshot() !== baseline.value))

// ── Open / load ──────────────────────────────────────────────────────────

const toForm = (grants?: Grant[]): GrantForm[] =>
    (grants || []).map((g) => ({
        resource_type: g.resource_type,
        resource_id: g.resource_id,
        resource_name: resourceName(g.resource_type, g.resource_id),
        permissions: [...(g.permissions || [])],
    }))

async function runLoad(loader: () => Promise<void>) {
    pendingLoad = loader
    loadState.value = 'loading'
    try {
        await loader()
        baseline.value = snapshot()
        loadState.value = 'ready'
    } catch (e) {
        console.error('Failed to load role editor data', e)
        loadState.value = 'error'
    }
}

const retryLoad = () => pendingLoad && runLoad(pendingLoad)

// Mirrors permissions_registry.DEFAULT_ON_PERMISSIONS.
const DEFAULT_ON_PERMISSIONS = ['view_code', 'run_custom_code']

function openModal(role: RoleData | null, source: RoleData | null) {
    editingRole.value = role
    activeTab.value = 'permissions'
    selectedResource.value = null
    form.name = role ? role.name : source ? t('rolesManager.copyOf', { name: source.name }) : ''
    form.description = role?.description || source?.description || ''
    const from = role || source
    form.permissions = from ? [...(from.permissions || [])] : [...DEFAULT_ON_PERMISSIONS]
    form.grants = []
    showModal.value = true
    runLoad(async () => {
        await ensureEditorData()
        form.grants = toForm(from?.resource_grants)
    })
}

const openCreateModal = (source?: RoleData) => openModal(null, source || null)
const openEditModal = (role: RoleData) => openModal(role, null)

// ── Save ─────────────────────────────────────────────────────────────────

const errorDetail = (e: any, fallback: string) => e?.data?.detail || e?.message || fallback

async function saveRole() {
    if (!canSave.value) return
    saving.value = true
    const editing = editingRole.value
    try {
        const body = {
            name: form.name.trim(),
            description: form.description || null,
            permissions: isFullAdmin.value ? [FULL_ADMIN] : form.permissions,
            resource_grants: form.grants.map(({ resource_type, resource_id, permissions }) => ({ resource_type, resource_id, permissions })),
        }
        const base = `/organizations/${props.organization.id}/roles`
        const { data, error } = await useMyFetch(editing ? `${base}/${editing.id}` : base, {
            method: editing ? 'PUT' : 'POST',
            body,
        })
        if (error.value) {
            toast.add({ title: errorDetail(error.value, t(editing ? 'rolesManager.failedToUpdate' : 'rolesManager.failedToCreate')), color: 'red' })
            return
        }
        const name = (data.value as RoleData)?.name || body.name
        toast.add({ title: t(editing ? 'rolesManager.toastUpdatedNamed' : 'rolesManager.toastCreatedNamed', { name }), color: 'green' })
        showModal.value = false
        await loadRoles()
    } catch (e: any) {
        toast.add({ title: errorDetail(e, t('rolesManager.failedToSave')), color: 'red' })
    } finally {
        saving.value = false
    }
}

// ── List ─────────────────────────────────────────────────────────────────

const filteredRoles = computed(() => {
    const q = searchQuery.value.toLowerCase()
    if (!q) return roles.value
    return roles.value.filter((r) => r.name.toLowerCase().includes(q) || (r.description || '').toLowerCase().includes(q))
})

function countLabel(n: number, key: string) {
    return t(`rolesManager.meta.${key}${n === 1 ? 'One' : 'Many'}`, { n })
}

// "2 permissions · 3 agents · 1 model" — counts what the editor shows (hidden
// baseline permissions such as reports are not counted).
function roleMeta(role: RoleData): string {
    if (isFullAdminRole(role)) return t('rolesManager.meta.everything')
    const known = registeredPerms.value.length ? new Set(registeredPerms.value) : KNOWN_PERMISSIONS
    const grants = role.resource_grants || []
    const parts = [countLabel((role.permissions || []).filter((p) => known.has(p)).length, 'permissions')]
    const byType = (type: string) => grants.filter((g) => g.resource_type === type).length
    if (byType('data_source')) parts.push(countLabel(byType('data_source'), 'agents'))
    if (byType('connection')) parts.push(countLabel(byType('connection'), 'connections'))
    if (byType('llm_model')) parts.push(countLabel(byType('llm_model'), 'models'))
    return parts.join(' · ')
}

function rowActions(role: RoleData) {
    if (!canManageRoles.value) return []
    const main: any[] = []
    if (canEdit(role)) main.push({ label: t('rolesManager.edit'), icon: 'i-heroicons-pencil-square', click: () => openEditModal(role) })
    main.push({ label: t('rolesManager.duplicate'), icon: 'i-heroicons-document-duplicate', click: () => openCreateModal(role) })
    const items = [main]
    if (role.is_deletable) items.push([{ label: t('rolesManager.delete'), icon: 'i-heroicons-trash', click: () => deleteRole(role) }])
    return items
}

async function loadRoles() {
    try {
        const { data, error } = await useMyFetch(`/organizations/${props.organization.id}/roles`)
        if (error.value) toast.add({ title: (error.value as any)?.data?.detail || t('rolesManager.failedToLoad'), color: 'red' })
        else if (data.value) roles.value = data.value as RoleData[]
    } finally {
        isLoading.value = false
    }
}

async function deleteRole(role: RoleData) {
    if (!confirm(t('rolesManager.confirmDelete', { name: role.name }))) return
    try {
        const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles/${role.id}`, { method: 'DELETE' })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('rolesManager.failedToDelete'), color: 'red' })
            return
        }
        toast.add({ title: t('rolesManager.toastDeleted') })
        await loadRoles()
    } catch (e: any) {
        toast.add({ title: errorDetail(e, t('rolesManager.failedToDelete')), color: 'red' })
    }
}

async function loadDefaultMemberRole() {
    try {
        const { data } = await useMyFetch('/organization/default-member-role')
        const role = (data.value as any)?.role
        if (role) defaultMemberRole.value = role
    } catch {
        // keep the "member" fallback
    }
}

// ── Usage quotas (unchanged) ─────────────────────────────────────────────

const quotaSelectOptions = computed(() => [
    { value: null, label: t('quotaPolicies.noDirectQuota') },
    ...usagePolicies.value.filter((p) => p.enabled).map((p) => ({ value: p.id, label: p.name })),
])

function getPrincipalQuotaPolicies(principalType: UsagePolicyAssignment['principal_type'], principalId: string) {
    return usagePolicies.value.filter((policy) =>
        policy.enabled && policy.assignments?.some((a) => a.principal_type === principalType && a.principal_id === principalId))
}

const getRoleQuotaPolicies = (role: RoleData) => (showQuotaColumn.value ? getPrincipalQuotaPolicies('role', role.id) : [])
const getDirectQuotaId = (type: UsagePolicyAssignment['principal_type'], id: string) => getPrincipalQuotaPolicies(type, id)[0]?.id || null

async function updatePrincipalQuota(principalType: UsagePolicyAssignment['principal_type'], principalId: string, policyId: string | null) {
    try {
        const { error } = await useMyFetch(`/organizations/${props.organization.id}/usage-policy-assignments/principal`, {
            method: 'PUT',
            body: { principal_type: principalType, principal_id: principalId, policy_id: policyId },
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToSave'), color: 'red' })
            return
        }
        usagePolicies.value = usagePolicies.value.map((policy) => {
            const assignments = (policy.assignments || []).filter((a) => a.principal_type !== principalType || a.principal_id !== principalId)
            if (policyId && policy.id === policyId) assignments.push({ principal_type: principalType, principal_id: principalId })
            return { ...policy, assignments }
        })
        toast.add({ title: t('quotaPolicies.toastAssignmentUpdated'), color: 'green' })
    } catch (e: any) {
        toast.add({ title: errorDetail(e, t('quotaPolicies.failedToSave')), color: 'red' })
    }
}

async function loadUsagePolicies() {
    if (!showQuotaColumn.value) return
    try {
        const { data } = await useMyFetch(`/organizations/${props.organization.id}/usage-policies`)
        usagePolicies.value = (data.value || []) as UsagePolicySummary[]
    } catch {
        usagePolicies.value = []
    }
}

watch(showQuotaColumn, (on) => {
    if (on && usagePolicies.value.length === 0) loadUsagePolicies()
})

onMounted(() => {
    loadRoles()
    loadUsagePolicies()
    loadDefaultMemberRole()
    ensureRegistry().catch(() => {})
    // Prefetch so most editors open already loaded; a failure is retried
    // (and shown) when the editor opens.
    if (canManageRoles.value) ensureEditorData().catch(() => {})
})
</script>
