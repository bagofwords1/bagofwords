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
                        class="absolute start-3 top-2.5 h-4 w-4 text-ink-muted"
                    />
                </div>
            </div>
            <div class="flex items-center justify-end gap-2 w-full md:w-auto">
                <UButton
                    v-if="canManageRoles"
                    color="blue"
                    variant="solid"
                    size="xs"
                    icon="i-heroicons-plus"
                    @click="openCreateModal()"
                >
                    {{ $t('rolesManager.newRole') }}
                </UButton>
            </div>
        </div>

        <!-- Role rows -->
        <div class="bg-surface shadow-sm border border-line rounded-lg overflow-hidden divide-y divide-line">
            <div v-if="isLoading" class="px-6 py-12 text-center">
                <div class="flex items-center justify-center text-ink-muted">
                    <Spinner class="w-4 h-4 me-2" />
                    <span class="text-sm">{{ $t('rolesManager.loading') }}</span>
                </div>
            </div>
            <div v-else-if="filteredRoles.length === 0" class="px-6 py-12 text-center">
                <div class="flex flex-col items-center">
                    <Icon name="heroicons:shield-check" class="mx-auto h-12 w-12 text-ink-muted" />
                    <h3 class="mt-2 text-sm font-medium text-ink">{{ $t('rolesManager.noRolesFound') }}</h3>
                    <p class="mt-1 text-sm text-ink-muted">{{ $t('rolesManager.noRolesHint') }}</p>
                </div>
            </div>
            <div
                v-else
                v-for="role in filteredRoles"
                :key="role.id"
                class="p-4 flex items-center justify-between gap-4 hover:bg-surface-subtle"
                :class="{ 'cursor-pointer': canEdit(role) }"
                :data-testid="`role-row-${role.name}`"
                @click="canEdit(role) && openEditModal(role)"
            >
                <div class="min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-medium text-ink" v-text-dir>{{ role.name }}</span>
                        <UBadge v-if="isOrgDefaultRole(role)" size="xs" color="gray">{{ $t('rolesManager.default') }}</UBadge>
                        <UBadge v-else-if="role.is_system" size="xs" color="gray">{{ $t('rolesManager.system') }}</UBadge>
                        <UBadge
                            v-if="role.permissions?.includes(FULL_ADMIN)"
                            size="xs"
                            color="blue"
                        >
                            {{ $t('rolesManager.fullAdmin') }}
                        </UBadge>
                    </div>
                    <p v-if="role.description" class="text-sm text-ink-muted mt-0.5 truncate" v-text-dir>{{ role.description }}</p>
                    <p class="text-xs text-ink-muted mt-0.5" data-testid="role-row-summary">{{ roleListSummary(role) }}</p>
                </div>
                <div class="flex items-center gap-3 shrink-0" @click.stop>
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
                                <span v-if="getRoleQuotaPolicies(role).length === 0" class="text-ink-muted text-sm italic">{{ $t('quotaPolicies.unlimited') }}</span>
                            </span>
                        </template>
                        <template #option="{ option }">
                            <span class="text-sm">{{ option.label }}</span>
                        </template>
                    </USelectMenu>
                    <div class="flex gap-1">
                        <UTooltip v-if="canEdit(role)" :text="$t('rolesManager.edit')">
                            <UButton
                                variant="ghost"
                                size="xs"
                                icon="i-heroicons-pencil"
                                :aria-label="$t('rolesManager.edit')"
                                @click="openEditModal(role)"
                            />
                        </UTooltip>
                        <UTooltip v-if="canManageRoles" :text="$t('rolesManager.duplicate')">
                            <UButton
                                variant="ghost"
                                size="xs"
                                icon="i-heroicons-document-duplicate"
                                :aria-label="$t('rolesManager.duplicate')"
                                @click="openCreateModal(role)"
                            />
                        </UTooltip>
                        <UButton
                            v-if="role.is_deletable && canManageRoles"
                            variant="ghost"
                            size="xs"
                            color="red"
                            icon="i-heroicons-trash"
                            :aria-label="$t('rolesManager.delete')"
                            @click="deleteRole(role)"
                        />
                    </div>
                </div>
            </div>
        </div>

        <!-- Create/Edit Modal -->
        <UModal
            v-model="showModal"
            :prevent-close="saving"
            :ui="{ width: 'sm:max-w-4xl', rounded: 'rounded-xl' }"
        >
            <div class="flex flex-col max-h-[88vh]" data-testid="role-modal">
                <!-- Header: identity + plain-language summary -->
                <div class="px-6 pt-5 pb-4 border-b border-line">
                    <div class="flex items-center justify-between gap-3">
                        <h3 class="text-lg font-medium text-ink">
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
                    <p v-if="editingDefaultRole" class="text-xs text-ink-muted mt-1">
                        {{ $t('rolesManager.defaultRoleHint') }}
                    </p>

                    <div class="grid gap-3 mt-3" :class="editingRole ? 'sm:grid-cols-2' : 'sm:grid-cols-3'">
                        <div>
                            <label for="role-name" class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.nameLabel') }}</label>
                            <UInput id="role-name" v-model="form.name" v-text-dir :placeholder="$t('rolesManager.namePlaceholder')" size="sm" :disabled="editingDefaultRole" />
                        </div>
                        <div>
                            <label for="role-description" class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.descriptionLabel') }}</label>
                            <UInput id="role-description" v-model="form.description" v-text-dir :placeholder="$t('rolesManager.descriptionPlaceholder')" size="sm" />
                        </div>
                        <div v-if="!editingRole">
                            <label class="block text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.startFrom') }}</label>
                            <USelectMenu
                                v-model="startFrom"
                                :options="startFromOptions"
                                value-attribute="value"
                                option-attribute="label"
                                size="sm"
                                :disabled="loadState !== 'ready'"
                                data-testid="role-start-from"
                                @update:model-value="applyStartFrom"
                            />
                        </div>
                    </div>

                    <p
                        v-if="loadState === 'ready'"
                        class="mt-3 px-3 py-2 rounded-lg bg-blue-50 dark:bg-blue-950/40 text-sm text-ink-2"
                        data-testid="role-summary"
                    >
                        {{ formSummary }}
                    </p>
                </div>

                <!-- Body -->
                <div class="flex-1 min-h-0 overflow-y-auto">
                    <!-- Loading: one state, final height, nothing jumps in later -->
                    <div
                        v-if="loadState === 'loading'"
                        class="min-h-[22rem] flex items-center justify-center text-ink-muted"
                        data-testid="role-loading"
                    >
                        <Spinner class="w-4 h-4 me-2" />
                        <span class="text-sm">{{ $t('rolesManager.loadingRole') }}</span>
                    </div>

                    <!-- Load failed: never show a role with grants silently missing -->
                    <div
                        v-else-if="loadState === 'error'"
                        class="min-h-[22rem] flex flex-col items-center justify-center gap-3 px-6 text-center"
                        data-testid="role-load-error"
                    >
                        <p class="text-sm text-red-600 dark:text-red-400">{{ $t('rolesManager.loadFailed') }}</p>
                        <UButton size="xs" color="gray" variant="solid" icon="i-heroicons-arrow-path" @click="retryLoad">
                            {{ $t('rolesManager.retry') }}
                        </UButton>
                    </div>

                    <!-- Full admin: the rest doesn't apply -->
                    <div
                        v-else-if="isFullAdmin"
                        class="min-h-[14rem] flex flex-col items-center justify-center gap-3 px-6 text-center"
                    >
                        <UIcon name="i-heroicons-shield-check" class="w-8 h-8 text-blue-500" />
                        <p class="text-sm text-ink-2 max-w-md">{{ $t('rolesManager.fullAdminOn') }}</p>
                        <UButton size="xs" color="gray" variant="solid" @click="isFullAdmin = false">
                            {{ $t('rolesManager.removeFullAdmin') }}
                        </UButton>
                    </div>

                    <div v-else class="grid md:grid-cols-2" :class="{ 'opacity-60 pointer-events-none': saving }">
                        <!-- ── Can reach ─────────────────────────────────── -->
                        <section class="px-6 py-4 min-w-0" data-testid="role-can-reach">
                            <h4 class="text-2xs font-semibold uppercase tracking-wide text-ink-muted mb-2">{{ $t('rolesManager.canReach') }}</h4>
                            <USelectMenu
                                v-model="selectedResource"
                                :options="addableResources"
                                option-attribute="label"
                                value-attribute="value"
                                searchable
                                :placeholder="$t('rolesManager.addAccess')"
                                size="sm"
                                class="mb-3"
                                data-testid="role-add-access"
                                @update:model-value="addResource"
                            >
                                <template #option="{ option }">
                                    <span class="flex items-center gap-2 min-w-0">
                                        <UIcon :name="TYPE_ICON[option.type]" class="w-3.5 h-3.5 text-ink-muted shrink-0" />
                                        <span class="truncate">{{ option.label }}</span>
                                    </span>
                                </template>
                            </USelectMenu>

                            <p v-if="publicAgentCount > 0" class="text-xs text-ink-muted mb-3 flex items-center gap-1.5">
                                <UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5" />
                                {{ $t('rolesManager.publicAgents', { n: publicAgentCount }) }}
                            </p>

                            <p v-if="!hasAnyReach" class="text-xs text-ink-muted border border-dashed border-line rounded-lg px-3 py-4 text-center">
                                {{ $t('rolesManager.reachEmpty') }}
                            </p>

                            <!-- Agents -->
                            <div v-if="agentGrants.length" class="mb-3">
                                <div class="text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.agents') }}</div>
                                <div class="border border-line rounded-lg divide-y divide-line">
                                    <div v-for="grant in agentGrants" :key="grant.resource_id" class="px-3 py-2">
                                        <div class="flex items-center gap-2">
                                            <span class="flex-1 min-w-0 truncate text-sm text-ink" v-text-dir>{{ grant.resource_name }}</span>
                                            <UBadge v-if="grantLevel(grant) === null" size="xs" color="gray" variant="subtle">{{ $t('rolesManager.levels.custom') }}</UBadge>
                                            <RoleLevelPicker
                                                :model-value="grantLevel(grant)"
                                                :options="agentLevelOptions"
                                                :label="grant.resource_name"
                                                @update:model-value="setGrantLevel(grant, $event)"
                                            />
                                            <UButton variant="ghost" color="gray" size="2xs" icon="i-heroicons-x-mark" :aria-label="$t('rolesManager.removeAccess')" @click="removeGrant(grant)" />
                                        </div>
                                        <div v-if="grantLevel(grant) === null" class="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
                                            <label v-for="perm in getResourcePermissions('data_source')" :key="perm" class="flex items-center gap-1.5 text-xs cursor-pointer">
                                                <UCheckbox :model-value="grant.permissions.includes(perm)" size="xs" @update:model-value="toggleResourcePerm(grant, perm, $event)" />
                                                <span class="text-ink-2">{{ formatPermission(perm) }}</span>
                                            </label>
                                        </div>
                                    </div>
                                </div>
                                <p class="text-xs text-ink-muted mt-1">{{ $t('rolesManager.agentLevelsHint') }}</p>
                            </div>

                            <!-- Connections -->
                            <div v-if="connectionGrants.length" class="mb-3">
                                <div class="text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.connections') }}</div>
                                <div class="border border-line rounded-lg divide-y divide-line">
                                    <div v-for="grant in connectionGrants" :key="grant.resource_id" class="px-3 py-2">
                                        <div class="flex items-center gap-2">
                                            <span class="flex-1 min-w-0 truncate text-sm text-ink" v-text-dir>{{ grant.resource_name }}</span>
                                            <UBadge v-if="grantLevel(grant) === null" size="xs" color="gray" variant="subtle">{{ $t('rolesManager.levels.custom') }}</UBadge>
                                            <RoleLevelPicker
                                                :model-value="grantLevel(grant)"
                                                :options="connectionLevelOptions"
                                                :label="grant.resource_name"
                                                @update:model-value="setGrantLevel(grant, $event)"
                                            />
                                            <UButton variant="ghost" color="gray" size="2xs" icon="i-heroicons-x-mark" :aria-label="$t('rolesManager.removeAccess')" @click="removeGrant(grant)" />
                                        </div>
                                        <div v-if="grantLevel(grant) === null" class="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
                                            <label v-for="perm in getResourcePermissions('connection')" :key="perm" class="flex items-center gap-1.5 text-xs cursor-pointer">
                                                <UCheckbox :model-value="grant.permissions.includes(perm)" size="xs" @update:model-value="toggleResourcePerm(grant, perm, $event)" />
                                                <span class="text-ink-2">{{ formatPermission(perm) }}</span>
                                            </label>
                                        </div>
                                    </div>
                                </div>
                                <p class="text-xs text-ink-muted mt-1">{{ $t('rolesManager.connectionLevelsHint') }}</p>
                            </div>

                            <!-- Restricted models (enterprise) -->
                            <div v-if="grantedModels.length && canManageModels" class="mb-3">
                                <div class="text-xs font-medium text-ink-muted mb-1">{{ $t('rolesManager.models') }}</div>
                                <div class="border border-line rounded-lg divide-y divide-line">
                                    <div v-for="grant in grantedModels" :key="grant.resource_id" class="px-3 py-2 flex items-center gap-2">
                                        <span class="flex-1 min-w-0 truncate text-sm text-ink">{{ grant.resource_name }}</span>
                                        <span class="text-xs text-ink-muted">{{ modelProvider(grant.resource_id) }}</span>
                                        <UBadge size="xs" color="blue" variant="subtle">{{ $t('rolesManager.levels.use') }}</UBadge>
                                        <UButton variant="ghost" color="gray" size="2xs" icon="i-heroicons-x-mark" :aria-label="$t('rolesManager.removeAccess')" @click="removeGrant(grant)" />
                                    </div>
                                </div>
                            </div>
                        </section>

                        <!-- ── Can also ──────────────────────────────────── -->
                        <section class="px-6 py-4 min-w-0 bg-surface-subtle border-t md:border-t-0 md:border-s border-line" data-testid="role-can-also">
                            <div class="flex items-baseline justify-between mb-2">
                                <h4 class="text-2xs font-semibold uppercase tracking-wide text-ink-muted">{{ $t('rolesManager.canAlso') }}</h4>
                                <button type="button" class="text-xs text-blue-600 dark:text-blue-400 hover:underline" data-testid="role-customize-toggle" @click="showCustomize = !showCustomize">
                                    {{ showCustomize ? $t('rolesManager.hideCustomize') : $t('rolesManager.customize') }}
                                </button>
                            </div>

                            <div v-if="!showCustomize" class="border border-line rounded-lg bg-surface divide-y divide-line">
                                <!-- Code visibility: three levels -->
                                <div class="px-3 py-2.5 flex items-center gap-3">
                                    <div class="flex-1 min-w-0">
                                        <div class="text-sm text-ink">{{ $t('rolesManager.code.label') }}</div>
                                        <div class="text-xs text-ink-muted">{{ $t('rolesManager.code.hint') }}</div>
                                    </div>
                                    <RoleLevelPicker
                                        :model-value="codeLevel"
                                        :options="codeLevelOptions"
                                        :label="$t('rolesManager.code.label')"
                                        @update:model-value="setCodeLevel"
                                    />
                                </div>
                                <!-- Bundled switches -->
                                <div v-for="bundle in visibleBundles" :key="bundle.key" class="px-3 py-2.5 flex items-center gap-3" :data-testid="`role-bundle-${bundle.key}`">
                                    <div class="flex-1 min-w-0">
                                        <div class="text-sm text-ink flex items-center gap-1.5 flex-wrap">
                                            {{ $t(`rolesManager.bundles.${bundle.key}.label`) }}
                                            <UBadge v-if="bundle.sensitive" size="xs" color="amber" variant="subtle">{{ $t('rolesManager.sensitive') }}</UBadge>
                                            <UBadge v-if="bundleState(bundle) === 'partial'" size="xs" color="gray" variant="subtle">{{ $t('rolesManager.partial') }}</UBadge>
                                        </div>
                                        <div class="text-xs text-ink-muted">{{ $t(`rolesManager.bundles.${bundle.key}.hint`) }}</div>
                                    </div>
                                    <UToggle
                                        :model-value="bundleState(bundle) === 'all'"
                                        :aria-label="$t(`rolesManager.bundles.${bundle.key}.label`)"
                                        @update:model-value="setBundle(bundle, $event)"
                                    />
                                </div>
                            </div>

                            <!-- Customize: every permission, grouped, with what includes what -->
                            <div v-else class="space-y-3" data-testid="role-customize">
                                <div v-for="(perms, cat) in allCategories" :key="cat" class="border border-line rounded-lg bg-surface">
                                    <div class="px-3 py-1.5 border-b border-line text-2xs font-semibold uppercase tracking-wide text-ink-muted">{{ categoryLabel(String(cat)) }}</div>
                                    <div class="px-3 py-2 space-y-1">
                                        <label v-for="perm in perms" :key="perm" class="flex items-center gap-2 text-sm cursor-pointer">
                                            <UCheckbox
                                                :model-value="form.permissions.includes(perm) || !!impliedBy(perm)"
                                                :disabled="!!impliedBy(perm)"
                                                size="xs"
                                                @update:model-value="togglePermission(perm, $event)"
                                            />
                                            <span class="text-ink-2">{{ formatPermission(perm) }}</span>
                                            <span v-if="impliedBy(perm)" class="text-xs text-ink-muted">{{ $t('rolesManager.includedBy', { name: formatPermission(impliedBy(perm)!) }) }}</span>
                                        </label>
                                    </div>
                                </div>
                                <div class="border border-line rounded-lg bg-surface px-3 py-2.5 flex items-center gap-3">
                                    <div class="flex-1 min-w-0">
                                        <div class="text-sm text-ink flex items-center gap-1.5">
                                            {{ $t('rolesManager.fullAdminAccess') }}
                                            <UBadge size="xs" color="amber" variant="subtle">{{ $t('rolesManager.sensitive') }}</UBadge>
                                        </div>
                                        <div class="text-xs text-ink-muted">{{ $t('rolesManager.fullAdminBypass') }}</div>
                                    </div>
                                    <UToggle v-model="isFullAdmin" :aria-label="$t('rolesManager.fullAdminAccess')" />
                                </div>
                            </div>
                        </section>
                    </div>
                </div>

                <!-- Footer -->
                <div class="px-6 py-3 border-t border-line flex items-center justify-between gap-3">
                    <span class="text-xs text-ink-muted">
                        <template v-if="loadState === 'ready' && isDirty">{{ $t('rolesManager.unsavedChanges') }}</template>
                    </span>
                    <div class="flex items-center gap-2">
                        <UButton variant="ghost" :disabled="saving" @click="showModal = false">{{ $t('rolesManager.cancel') }}</UButton>
                        <UButton
                            color="blue"
                            :disabled="!canSave"
                            data-testid="role-save"
                            @click="saveRole"
                        >
                            <Spinner v-if="saving" class="w-4 h-4" />
                            {{ saving ? $t('rolesManager.saving') : (editingRole ? $t('rolesManager.save') : $t('rolesManager.create')) }}
                        </UButton>
                    </div>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import RoleLevelPicker from '@/components/RoleLevelPicker.vue'
import { useCan } from '~/composables/usePermissions'
import { useI18n } from 'vue-i18n'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const { t, locale } = useI18n()

const FULL_ADMIN = 'full_admin_access'

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

interface ResourceGrantForm {
    resource_type: string
    resource_id: string
    resource_name: string
    permissions: string[]
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

// A restricted model as listed by /llm/model-access/by-principal.
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

const toast = useToast()
const { hasFeature } = useEnterprise()

// The org-wide default role for new members (settings.default_member_role).
const defaultMemberRole = ref('member')

function isOrgDefaultRole(role: RoleData): boolean {
    return role.name === defaultMemberRole.value
}

// A built-in `member` role (or an org's editable override of it): editable but
// not deletable, and its name is fixed.
function isBuiltinRole(role: RoleData): boolean {
    return !!role.overrides_system_role || (role.is_system && role.is_editable === true)
}

const canManageRoles = computed(() => useCan('manage_roles'))
const canEdit = (role: RoleData) => !!role.is_editable && canManageRoles.value

// ── State ────────────────────────────────────────────────────────────────

const roles = ref<RoleData[]>([])
const usagePolicies = ref<UsagePolicySummary[]>([])
const isLoading = ref(true)
const searchQuery = ref('')
const showModal = ref(false)
const editingRole = ref<RoleData | null>(null)
const editingDefaultRole = computed(() => !!editingRole.value && isBuiltinRole(editingRole.value))
const saving = ref(false)
const selectedResource = ref<string | null>(null)
const showCustomize = ref(false)
const showQuotaColumn = computed(() => hasFeature('usage_limits') && useCan('manage_settings'))

// The modal is either loading everything it needs, failed to, or ready. It is
// never "half loaded": grants and models used to pop in after the modal
// opened, and Save worked before they arrived.
const loadState = ref<'loading' | 'error' | 'ready'>('loading')
// What the last open asked for, so "Try again" can repeat it.
let pendingLoad: (() => Promise<void>) | null = null

const form = reactive({
    name: '',
    description: '',
    permissions: [] as string[],
    resourceGrants: [] as ResourceGrantForm[],
})

// Restricted-model access is a role grant like any other (resource_type
// `llm_model`, permission `use`), so it is part of form.resourceGrants and
// saved by the same PUT. It used to be written on every click through a
// separate endpoint: Cancel didn't undo it, and the role's own Save — which
// replaces ALL of the role's grants — could silently drop a model just added.
const canManageModels = computed(() => hasFeature('llm_access_control') && useCan('manage_llm'))
const restrictedModels = ref<RestrictedModel[]>([])
const MODEL_USE = ['use']

const isFullAdmin = computed({
    get: () => form.permissions.includes(FULL_ADMIN),
    set: (val: boolean) => {
        form.permissions = form.permissions.filter((p) => p !== FULL_ADMIN)
        if (val) form.permissions.push(FULL_ADMIN)
    },
})

// ── Registry (cached) ────────────────────────────────────────────────────

const allCategories = ref<Record<string, string[]>>({})
const resourcePermissions = ref<Record<string, string[]>>({})
let registryPromise: Promise<void> | null = null

function ensureRegistry(): Promise<void> {
    if (!registryPromise) {
        registryPromise = (async () => {
            const { data, error } = await useMyFetch('/permissions/registry')
            if (error.value || !data.value) throw error.value || new Error('registry')
            const registry = data.value as {
                categories: Record<string, string[]>
                resource_permissions: Record<string, string[]>
            }
            allCategories.value = registry.categories
            resourcePermissions.value = registry.resource_permissions
        })().catch((e) => {
            registryPromise = null
            throw e
        })
    }
    return registryPromise
}

function getResourcePermissions(resourceType: string): string[] {
    return resourcePermissions.value[resourceType] || []
}

const flatOrgPermissions = computed(() => Object.values(allCategories.value).flat())

// ── Agents & connections (cached, prefetched when the tab opens) ─────────

const availableResources = ref<ResourceOption[]>([])
const publicAgentCount = ref(0)
let resourcesPromise: Promise<void> | null = null

function ensureResources(): Promise<void> {
    if (!resourcesPromise) {
        resourcesPromise = (async () => {
            const [dsResult, connResult] = await Promise.all([
                useMyFetch('/data_sources/active?show_all=true'),
                useMyFetch('/connections'),
            ])
            if (dsResult.error.value) throw dsResult.error.value
            if (connResult.error.value) throw connResult.error.value
            const resources: ResourceOption[] = []
            const agents = (dsResult.data.value || []) as any[]
            for (const ds of agents) {
                resources.push({ label: ds.name, value: `data_source:${ds.id}`, type: 'data_source', id: ds.id })
            }
            publicAgentCount.value = agents.filter((ds) => ds.is_public).length
            for (const c of (connResult.data.value || []) as any[]) {
                resources.push({ label: c.name, value: `connection:${c.id}`, type: 'connection', id: c.id })
            }
            availableResources.value = resources
        })().catch((e) => {
            resourcesPromise = null
            throw e
        })
    }
    return resourcesPromise
}

// The org's restricted models (names for the picker). The endpoint is keyed
// by principal; only the model list is used here, the role's own model grants
// come from its resource_grants.
let modelsPromise: Promise<void> | null = null
function ensureModels(): Promise<void> {
    if (!canManageModels.value) return Promise.resolve()
    if (!modelsPromise) {
        modelsPromise = (async () => {
            const { data, error } = await useMyFetch(
                '/llm/model-access/by-principal?principal_type=role&principal_id=__role_editor__'
            )
            if (error.value) throw error.value
            restrictedModels.value = (data.value || []) as RestrictedModel[]
        })().catch((e) => {
            modelsPromise = null
            throw e
        })
    }
    return modelsPromise
}

function modelProvider(modelId: string): string {
    return restrictedModels.value.find((m) => m.model_id === modelId)?.provider_name || ''
}

const ensureEditorData = () => Promise.all([ensureRegistry(), ensureResources(), ensureModels()])

const TYPE_ICON: Record<string, string> = {
    data_source: 'i-heroicons-cube',
    connection: 'i-heroicons-circle-stack',
    llm_model: 'i-heroicons-cpu-chip',
}

// Everything the picker can still add: agents and connections without a
// grant, and restricted models the role can't use yet.
const addableResources = computed<ResourceOption[]>(() => {
    const granted = new Set(form.resourceGrants.map((g) => `${g.resource_type}:${g.resource_id}`))
    const out = availableResources.value.filter((r) => !granted.has(r.value))
    for (const m of restrictedModels.value) {
        if (!granted.has(`llm_model:${m.model_id}`)) {
            out.push({ label: m.name, value: `llm_model:${m.model_id}`, type: 'llm_model', id: m.model_id })
        }
    }
    return out
})

function resourceName(type: string, id: string): string {
    if (type === 'llm_model') return restrictedModels.value.find((m) => m.model_id === id)?.name || id
    return availableResources.value.find((r) => r.type === type && r.id === id)?.label || id
}

function addResource(value: string | null) {
    selectedResource.value = null
    if (!value) return
    const [type, ...rest] = value.split(':')
    const id = rest.join(':')
    if (form.resourceGrants.some((g) => g.resource_type === type && g.resource_id === id)) return
    form.resourceGrants.push({
        resource_type: type,
        resource_id: id,
        resource_name: resourceName(type, id),
        permissions: type === 'llm_model' ? [...MODEL_USE] : [...(LEVELS[type]?.[0]?.perms || [])],
    })
}

function removeGrant(grant: ResourceGrantForm) {
    form.resourceGrants = form.resourceGrants.filter((g) => g !== grant)
}

const agentGrants = computed(() => form.resourceGrants.filter((g) => g.resource_type === 'data_source'))
const connectionGrants = computed(() => form.resourceGrants.filter((g) => g.resource_type === 'connection'))
const grantedModels = computed(() => form.resourceGrants.filter((g) => g.resource_type === 'llm_model'))
const hasAnyReach = computed(() =>
    agentGrants.value.length + connectionGrants.value.length + (canManageModels.value ? grantedModels.value.length : 0) > 0)

// ── Resource levels ──────────────────────────────────────────────────────
// Agents: an EMPTY grant is the query tier (any grant row on a data source
// implies `view`), `manage` is the owner/manager superset — same two tiers as
// the agent panel's "Add people" modal.
// Connections: "Build agents" = create agents on it; "Manage" = also edit,
// test, reindex and delete it. Any other combination is "Custom" and shows
// the raw checkboxes.

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

const sameSet = (a: string[], b: string[]) =>
    a.length === b.length && a.every((x) => b.includes(x))

function grantLevel(grant: ResourceGrantForm): string | null {
    return LEVELS[grant.resource_type]?.find((l) => sameSet(l.perms, grant.permissions))?.key ?? null
}

function setGrantLevel(grant: ResourceGrantForm, key: string) {
    const level = LEVELS[grant.resource_type]?.find((l) => l.key === key)
    if (level) grant.permissions = [...level.perms]
}

function toggleResourcePerm(grant: ResourceGrantForm, perm: string, checked: boolean) {
    grant.permissions = grant.permissions.filter((p) => p !== perm)
    if (checked) grant.permissions.push(perm)
}

const agentLevelOptions = computed(() => [
    { value: 'use', label: t('rolesManager.levels.use') },
    { value: 'manage', label: t('rolesManager.levels.manage') },
])
const connectionLevelOptions = computed(() => [
    { value: 'build', label: t('rolesManager.levels.buildAgents') },
    { value: 'manage', label: t('rolesManager.levels.manage') },
])

// ── Org capabilities: code level + bundles ───────────────────────────────

const codeLevelOptions = computed(() => [
    { value: 'hidden', label: t('rolesManager.code.hidden') },
    { value: 'view', label: t('rolesManager.code.view') },
    { value: 'run', label: t('rolesManager.code.run') },
])

const codeLevel = computed(() => {
    if (form.permissions.includes('run_custom_code')) return 'run'
    if (form.permissions.includes('view_code')) return 'view'
    return 'hidden'
})

function setCodeLevel(level: string) {
    form.permissions = form.permissions.filter((p) => p !== 'view_code' && p !== 'run_custom_code')
    if (level === 'view') form.permissions.push('view_code')
    // run_custom_code implies view_code server-side; keep both, like every
    // seeded role, so the stored row is self-describing.
    if (level === 'run') form.permissions.push('view_code', 'run_custom_code')
}

interface Bundle { key: string; perms: string[]; sensitive?: boolean }

// Outcome-named groups of org permissions. The raw list stays under
// Customize; a role holding part of a bundle shows "Partial" rather than
// being silently changed when it is opened.
const BUNDLES: Bundle[] = [
    { key: 'build', perms: ['create_data_source', 'create_connections'] },
    { key: 'knowledge', perms: ['manage_instructions', 'manage_entities', 'manage_evals'] },
    { key: 'connections', perms: ['manage_connections'] },
    { key: 'people', perms: ['manage_members', 'manage_service_accounts'], sensitive: true },
    { key: 'admin', perms: ['manage_settings', 'manage_llm', 'manage_identity_providers', 'view_audit_logs'], sensitive: true },
]

// Only the permissions this server actually registers (e.g. no audit-log
// permission on a build without it).
function bundlePerms(bundle: Bundle): string[] {
    const known = new Set(flatOrgPermissions.value)
    return bundle.perms.filter((p) => known.has(p))
}

const visibleBundles = computed(() => BUNDLES.filter((b) => bundlePerms(b).length > 0))

function bundleStateOf(perms: string[], bundle: Bundle): 'all' | 'partial' | 'none' {
    const ps = bundlePerms(bundle).length ? bundlePerms(bundle) : bundle.perms
    const held = ps.filter((p) => perms.includes(p)).length
    if (held === 0) return 'none'
    return held === ps.length ? 'all' : 'partial'
}

const bundleState = (bundle: Bundle) => bundleStateOf(form.permissions, bundle)

function setBundle(bundle: Bundle, on: boolean) {
    const ps = bundlePerms(bundle)
    form.permissions = form.permissions.filter((p) => !ps.includes(p))
    if (on) form.permissions.push(...ps)
}

// Mirrors permission_resolver.ORG_PERM_IMPLIES_ORG: these are granted by
// another permission, so Customize shows them checked and locked.
const ORG_IMPLIED_BY: Record<string, string> = {
    view_code: 'run_custom_code',
    create_connections: 'manage_connections',
}

function impliedBy(perm: string): string | null {
    const by = ORG_IMPLIED_BY[perm]
    return by && form.permissions.includes(by) ? by : null
}

function togglePermission(perm: string, checked: boolean) {
    form.permissions = form.permissions.filter((p) => p !== perm)
    if (checked) form.permissions.push(perm)
}

const CATEGORY_KEYS: Record<string, string> = {
    'Data & Connections': 'data',
    Instructions: 'instructions',
    Entities: 'entities',
    Evals: 'evals',
    Code: 'code',
    Members: 'members',
    Settings: 'settings',
    Enterprise: 'enterprise',
}

function categoryLabel(cat: string): string {
    const key = CATEGORY_KEYS[cat]
    return key ? t(`rolesManager.categories.${key}`) : cat
}

const KNOWN_PERMISSION_KEYS = new Set([
    'manage_files', 'view_code', 'run_custom_code', 'create_data_source', 'create_connections',
    'manage_connections', 'manage_instructions', 'manage_entities', 'manage_evals', 'view_members',
    'manage_members', 'manage_service_accounts', 'manage_settings', 'manage_llm', 'view_audit_logs',
    'manage_identity_providers', 'view', 'view_schema', 'create_entities', 'manage',
    'manage_connection', 'create_data_sources', 'manage_data_sources',
])

function formatPermission(perm: string) {
    if (KNOWN_PERMISSION_KEYS.has(perm)) return t(`rolesManager.permissions.${perm}`)
    return perm.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

// ── Plain-language summary ───────────────────────────────────────────────
// One function for the modal sentence and the list row, so they can't drift.

function countPhrase(n: number, oneKey: string, manyKey: string): string {
    return n === 1 ? t(oneKey) : t(manyKey, { n })
}

function summaryParts(
    permissions: string[],
    grants: { resource_type: string; permissions: string[] }[],
): string[] {
    const parts: string[] = []
    const levelCount = (type: string, key: string) =>
        grants.filter((g) => g.resource_type === type &&
            LEVELS[type]?.find((l) => sameSet(l.perms, g.permissions))?.key === key).length
    const customCount = grants.filter((g) => LEVELS[g.resource_type] &&
        !LEVELS[g.resource_type].some((l) => sameSet(l.perms, g.permissions))).length
    const modelCount = grants.filter((g) => g.resource_type === 'llm_model').length

    const useAgents = levelCount('data_source', 'use')
    const manageAgents = levelCount('data_source', 'manage')
    const buildConns = levelCount('connection', 'build')
    const manageConns = levelCount('connection', 'manage')
    if (useAgents) parts.push(countPhrase(useAgents, 'rolesManager.summary.useAgentsOne', 'rolesManager.summary.useAgentsMany'))
    if (manageAgents) parts.push(countPhrase(manageAgents, 'rolesManager.summary.manageAgentsOne', 'rolesManager.summary.manageAgentsMany'))
    if (buildConns) parts.push(countPhrase(buildConns, 'rolesManager.summary.buildOnOne', 'rolesManager.summary.buildOnMany'))
    if (manageConns) parts.push(countPhrase(manageConns, 'rolesManager.summary.manageConnectionsOne', 'rolesManager.summary.manageConnectionsMany'))
    if (customCount) parts.push(countPhrase(customCount, 'rolesManager.summary.customOne', 'rolesManager.summary.customMany'))
    if (modelCount) parts.push(countPhrase(modelCount, 'rolesManager.summary.modelsOne', 'rolesManager.summary.modelsMany'))

    if (permissions.includes('run_custom_code')) parts.push(t('rolesManager.summary.runCode'))
    else if (permissions.includes('view_code')) parts.push(t('rolesManager.summary.viewCode'))

    for (const bundle of BUNDLES) {
        const state = bundleStateOf(permissions, bundle)
        if (state === 'all') parts.push(t(`rolesManager.summary.${bundle.key}`))
        // Part of a bundle: name the permissions actually held.
        else if (state === 'partial') {
            for (const p of bundlePerms(bundle).filter((x) => permissions.includes(x))) {
                parts.push(lowerFirst(formatPermission(p)))
            }
        }
    }
    return parts
}

function lowerFirst(text: string): string {
    return text.charAt(0).toLocaleLowerCase(locale.value) + text.slice(1)
}

function joinList(parts: string[]): string {
    try {
        return new Intl.ListFormat(locale.value, { style: 'long', type: 'conjunction' }).format(parts)
    } catch {
        return parts.join(', ')
    }
}

const formSummary = computed(() => {
    if (isFullAdmin.value) return t('rolesManager.summary.fullAdmin')
    const parts = summaryParts(form.permissions, form.resourceGrants)
    if (!parts.length) return t('rolesManager.summary.none')
    return t('rolesManager.summary.can', { list: joinList(parts) })
})

function roleListSummary(role: RoleData): string {
    if (role.permissions?.includes(FULL_ADMIN)) return t('rolesManager.summary.listFullAdmin')
    const parts = summaryParts(role.permissions || [], role.resource_grants || [])
    if (!parts.length) return t('rolesManager.summary.listNone')
    const text = joinList(parts)
    return text.charAt(0).toLocaleUpperCase(locale.value) + text.slice(1)
}

// ── Dirty tracking ───────────────────────────────────────────────────────

function snapshot(): string {
    return JSON.stringify({
        n: form.name,
        d: form.description,
        p: [...form.permissions].sort(),
        g: form.resourceGrants.map((g) => [g.resource_type, g.resource_id, [...g.permissions].sort()]),
    })
}
const baseline = ref('')
const isDirty = computed(() => loadState.value === 'ready' && snapshot() !== baseline.value)

const canSave = computed(() =>
    loadState.value === 'ready' && !saving.value && !!form.name.trim() && (!editingRole.value || isDirty.value)
)

// ── Open / load ──────────────────────────────────────────────────────────

function grantsToForm(grants: RoleData['resource_grants']): ResourceGrantForm[] {
    return (grants || []).map((g) => ({
        resource_type: g.resource_type,
        resource_id: g.resource_id,
        resource_name: resourceName(g.resource_type, g.resource_id),
        permissions: [...(g.permissions || [])],
    }))
}

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

function retryLoad() {
    if (pendingLoad) runLoad(pendingLoad)
}

// Starting points for a new role: the product defaults, or a copy of any
// existing role (its permissions and grants, model access included).
const DEFAULT_START = '__default__'
const startFrom = ref<string>(DEFAULT_START)
const startFromOptions = computed(() => [
    { value: DEFAULT_START, label: t('rolesManager.startFromDefault') },
    ...roles.value.map((r) => ({ value: r.id, label: r.name })),
])

// Mirrors permissions_registry.DEFAULT_ON_PERMISSIONS: every existing role
// carries them, so a new role starts with them too.
const DEFAULT_ON_PERMISSIONS = ['view_code', 'run_custom_code']

async function loadStartingPoint(sourceId: string) {
    await ensureEditorData()
    const source = roles.value.find((r) => r.id === sourceId)
    if (source) {
        form.permissions = [...(source.permissions || [])]
        form.resourceGrants = grantsToForm(source.resource_grants)
    } else {
        form.permissions = [...DEFAULT_ON_PERMISSIONS]
        form.resourceGrants = []
    }
}

function resetModal() {
    form.name = ''
    form.description = ''
    form.permissions = []
    form.resourceGrants = []
    selectedResource.value = null
    showCustomize.value = false
}

function openCreateModal(from?: RoleData) {
    resetModal()
    editingRole.value = null
    startFrom.value = from ? from.id : DEFAULT_START
    if (from) form.name = t('rolesManager.copyOf', { name: from.name })
    showModal.value = true
    runLoad(() => loadStartingPoint(startFrom.value))
}

function applyStartFrom(value: string) {
    // Keep what the admin typed; replace only the permissions.
    const name = form.name
    const description = form.description
    runLoad(async () => {
        await loadStartingPoint(value)
        form.name = name
        form.description = description
    })
}

function openEditModal(role: RoleData) {
    resetModal()
    editingRole.value = role
    form.name = role.name
    form.description = role.description || ''
    form.permissions = [...(role.permissions || [])]
    showModal.value = true
    runLoad(async () => {
        await ensureEditorData()
        form.resourceGrants = grantsToForm(role.resource_grants)
    })
}

// ── Save ─────────────────────────────────────────────────────────────────

function errorDetail(e: any, fallback: string): string {
    return e?.data?.detail || e?.message || fallback
}

async function saveRole() {
    if (!canSave.value) return
    saving.value = true
    try {
        const body = {
            name: form.name.trim(),
            description: form.description || null,
            permissions: isFullAdmin.value ? [FULL_ADMIN] : form.permissions,
            resource_grants: form.resourceGrants.map((g) => ({
                resource_type: g.resource_type,
                resource_id: g.resource_id,
                permissions: g.permissions,
            })),
        }

        const url = editingRole.value
            ? `/organizations/${props.organization.id}/roles/${editingRole.value.id}`
            : `/organizations/${props.organization.id}/roles`
        const { data, error } = await useMyFetch(url, { method: editingRole.value ? 'PUT' : 'POST', body })
        if (error.value) {
            const fallback = editingRole.value ? t('rolesManager.failedToUpdate') : t('rolesManager.failedToCreate')
            toast.add({ title: errorDetail(error.value, fallback), color: 'red' })
            return
        }
        const saved = data.value as RoleData

        await loadRoles()
        toast.add({
            title: t(editingRole.value ? 'rolesManager.toastUpdatedNamed' : 'rolesManager.toastCreatedNamed', { name: saved.name }),
            color: 'green',
        })
        showModal.value = false
    } catch (e: any) {
        toast.add({ title: errorDetail(e, t('rolesManager.failedToSave')), color: 'red' })
    } finally {
        saving.value = false
    }
}

// ── List: search, quota, CRUD ────────────────────────────────────────────

const filteredRoles = computed(() => {
    const query = searchQuery.value.toLowerCase()
    if (!query) return roles.value
    return roles.value.filter((r) =>
        r.name.toLowerCase().includes(query) ||
        (r.description || '').toLowerCase().includes(query)
    )
})

const quotaSelectOptions = computed(() => [
    { value: null, label: t('quotaPolicies.noDirectQuota') },
    ...usagePolicies.value
        .filter((policy) => policy.enabled)
        .map((policy) => ({ value: policy.id, label: policy.name })),
])

function getRoleQuotaPolicies(role: RoleData): UsagePolicySummary[] {
    if (!showQuotaColumn.value) return []
    return getPrincipalQuotaPolicies('role', role.id)
}

function getPrincipalQuotaPolicies(principalType: UsagePolicyAssignment['principal_type'], principalId: string): UsagePolicySummary[] {
    return usagePolicies.value.filter((policy) =>
        policy.enabled &&
        policy.assignments?.some((a) => a.principal_type === principalType && a.principal_id === principalId)
    )
}

function getDirectQuotaId(principalType: UsagePolicyAssignment['principal_type'], principalId: string): string | null {
    return getPrincipalQuotaPolicies(principalType, principalId)[0]?.id || null
}

function applyLocalQuotaAssignment(principalType: UsagePolicyAssignment['principal_type'], principalId: string, policyId: string | null) {
    usagePolicies.value = usagePolicies.value.map((policy) => {
        const assignments = (policy.assignments || []).filter(
            (a) => a.principal_type !== principalType || a.principal_id !== principalId
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
            body: { principal_type: principalType, principal_id: principalId, policy_id: policyId },
        })
        if (error.value) {
            toast.add({ title: error.value.data?.detail || t('quotaPolicies.failedToSave'), color: 'red' })
            return
        }
        applyLocalQuotaAssignment(principalType, principalId, policyId)
        toast.add({ title: t('quotaPolicies.toastAssignmentUpdated'), color: 'green' })
    } catch (e: any) {
        toast.add({ title: errorDetail(e, t('quotaPolicies.failedToSave')), color: 'red' })
    }
}

async function loadRoles() {
    try {
        const { data, error } = await useMyFetch(`/organizations/${props.organization.id}/roles`)
        if (error.value) {
            toast.add({ title: (error.value as any)?.data?.detail || t('rolesManager.failedToLoad'), color: 'red' })
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

async function deleteRole(role: RoleData) {
    if (!confirm(t('rolesManager.confirmDelete', { name: role.name }))) return
    try {
        const { error } = await useMyFetch(`/organizations/${props.organization.id}/roles/${role.id}`, {
            method: 'DELETE',
        })
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
    } catch (e) {
        // keep the "member" fallback
    }
}

onMounted(() => {
    loadRoles()
    loadUsagePolicies()
    loadDefaultMemberRole()
    // Prefetch what the editor needs so most modals open already loaded. A
    // failure here is retried (and reported) when the modal opens.
    ensureRegistry().catch(() => {})
    if (canManageRoles.value) ensureEditorData().catch(() => {})
})

watch(showQuotaColumn, (enabled) => {
    if (enabled && usagePolicies.value.length === 0) loadUsagePolicies()
})
</script>
