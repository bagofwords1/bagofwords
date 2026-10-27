<template>
    <UTooltip :text="buttonLabel">
        <button @click="openModal" data-testid="share-open"
            :class="[
                'items-center flex gap-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-400',
                compact ? 'p-1.5' : 'px-2 py-1 text-xs'
            ]">
            <div class="relative">
                <Icon :name="buttonIcon" :class="compact ? 'w-4 h-4' : 'w-3.5 h-3.5'" />
                <span v-if="isShared" class="absolute -top-0.5 -end-0.5 w-1.5 h-1.5 bg-green-500 rounded-full"></span>
            </div>
            <span v-if="!compact" class="text-xs whitespace-nowrap">{{ buttonLabel }}</span>
        </button>
    </UTooltip>

    <!-- Laid out like a Google Docs share dialog: add people, who has access,
         general access, then Copy link / Done. A dashboard's settings
         (schedule, Data tab, chat, link name) live behind its toolbar gear. -->
    <UModal v-model="modalOpen" :ui="{ width: 'sm:max-w-lg' }">
        <div class="flex flex-col" data-testid="share-modal">
            <!-- Header -->
            <div class="flex items-start justify-between gap-3 px-6 pt-5 pb-1">
                <h2 class="text-lg font-normal text-gray-900 dark:text-white min-w-0 truncate" data-testid="share-title">{{ heading }}</h2>
                <button @click="modalOpen = false" :aria-label="$t('common.close')"
                    class="p-1.5 -me-1.5 -mt-0.5 rounded-full text-ink-muted hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800 dark:hover:text-gray-300 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>
            <p class="px-6 text-xs text-ink-muted mb-3">{{ shareDescription }}</p>

            <!-- No inner scroll: the modal grows with its content (the page
                 scrolls when it is taller than the screen), so the menus in it
                 open in full instead of being clipped. -->
            <div class="px-6 pt-1">
                <!-- Add people and groups. A dashboard's list is always there
                     (collab01): its editors stand whatever the visibility, before
                     the first publish too. A conversation's, while restricted. -->
                <div v-if="showPeople && canShare" class="mb-1" data-testid="share-people">
                    <div class="flex items-start gap-2">
                        <div class="relative flex-1 min-w-0 flex flex-wrap items-center gap-1.5 border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 min-h-[44px] focus-within:ring-2 focus-within:ring-blue-600 focus-within:border-transparent bg-white dark:bg-gray-900">
                            <span v-for="(principal, idx) in pendingPrincipals" :key="principal.kind + ':' + (principal.id || principal.email)"
                                class="inline-flex items-center gap-1.5 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 text-gray-800 dark:text-gray-200 text-xs ps-1 pe-1.5 py-0.5 rounded-full whitespace-nowrap">
                                <span class="w-5 h-5 rounded-full flex items-center justify-center text-2xs font-medium text-white" :class="avatarColor(principal.id || principal.email || '')">
                                    <Icon v-if="principal.kind === 'group'" name="heroicons:user-group" class="w-3 h-3" />
                                    <template v-else>{{ initial(principal.name || principal.email) }}</template>
                                </span>
                                {{ principal.name || principal.email }}
                                <button @click="removePendingPrincipal(idx)" class="text-ink-muted hover:text-gray-700 dark:hover:text-gray-200 outline-none">
                                    <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
                                </button>
                            </span>
                            <input ref="inputRef" v-model="inputValue" type="text" data-testid="share-add-input"
                                class="flex-1 min-w-[140px] border-none outline-none text-sm bg-transparent p-0 placeholder:text-gray-500"
                                :placeholder="pendingPrincipals.length ? '' : (groups.length > 0 ? $t('dashboardCollab.share.addPeopleAndGroups') : $t('dashboardCollab.share.addPeople'))"
                                @keydown.enter.prevent="handleEnter"
                                @keydown.,.prevent="handleComma"
                                @keydown.backspace="handleBackspace"
                                @input="onInput"
                                @focus="showDropdown = true"
                                @blur="onBlur" />
                            <div v-if="showDropdown && filteredOptions.length > 0"
                                class="absolute start-0 end-0 top-full mt-1 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 max-h-56 overflow-y-auto py-1">
                                <button v-for="option in filteredOptions" :key="option.kind + ':' + option.id"
                                    class="w-full text-start px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-3"
                                    @mousedown.prevent="addPrincipal(option)">
                                    <div class="w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium text-white flex-shrink-0" :class="avatarColor(option.id)">
                                        <Icon v-if="option.kind === 'group'" name="heroicons:user-group" class="w-4 h-4" />
                                        <template v-else>{{ initial(option.name || option.email) }}</template>
                                    </div>
                                    <div class="flex flex-col min-w-0">
                                        <span class="text-gray-900 dark:text-white truncate">{{ option.name || option.email }}</span>
                                        <span v-if="option.kind === 'group'" class="text-xs text-ink-muted truncate">{{ $t('share.groupMembers', option.memberCount || 0) }}</span>
                                        <span v-else-if="option.name" class="text-xs text-ink-muted truncate">{{ option.email }}</span>
                                    </div>
                                </button>
                            </div>
                        </div>
                        <!-- The role the picks get (collab01): editors before the
                             first publish, viewers once it is out. -->
                        <RoleSelect v-if="dashboardMode && pendingPrincipals.length" v-model="newRole" :view-disabled-reason="viewDisabledReason"
                            :disabled="isSaving" boxed class="mt-1" data-testid="share-new-role" />
                    </div>
                    <!-- Why "Can view" is not offered right now (collab01). -->
                    <p v-if="dashboardMode && viewDisabledReason" class="text-xs text-ink-muted mt-1.5"
                        data-testid="share-viewers-hint">
                        {{ viewDisabledReason }}
                    </p>
                </div>

                <!-- People with access -->
                <section v-if="showPeople && (dashboardMode || sharedEntries.length)" class="mt-5" data-testid="share-access-list">
                    <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-1">{{ $t('dashboardCollab.share.peopleWithAccess') }}</h3>
                    <!-- Someone who may see but not change the list (collab01). -->
                    <p v-if="dashboardMode && !canShare" class="text-xs text-ink-muted mb-1 flex items-start gap-1.5"
                        data-testid="share-read-only">
                        <Icon name="heroicons:lock-closed" class="w-3.5 h-3.5 flex-shrink-0 mt-px" />
                        <span>{{ $t('dashboardCollab.share.readOnly') }}</span>
                    </p>

                    <div class="-mx-2">
                        <!-- The owner, at the top, never removable (collab01). -->
                        <div v-if="dashboardMode && ownerId" class="flex items-center justify-between gap-3 py-2 px-2 rounded-lg"
                            data-testid="share-owner-row">
                            <div class="flex items-center gap-3 min-w-0">
                                <div class="w-8 h-8 flex-shrink-0 rounded-full flex items-center justify-center text-sm font-medium text-white" :class="avatarColor(ownerId)">
                                    {{ initial(ownerMember?.name || ownerMember?.email) }}
                                </div>
                                <div class="flex flex-col min-w-0">
                                    <span class="text-sm text-gray-900 dark:text-gray-100 truncate">{{ ownerLabel }}</span>
                                    <span v-if="ownerMember?.email" class="text-xs text-ink-muted truncate">{{ ownerMember.email }}</span>
                                </div>
                            </div>
                            <span class="text-sm text-ink-muted flex-shrink-0 pe-2">{{ $t('dashboardCollab.share.owner') }}</span>
                        </div>

                        <!-- Everyone who can open the dashboard's project edits it. -->
                        <div v-if="dashboardMode && projectId" class="flex items-center justify-between gap-3 py-2 px-2" data-testid="share-project-line">
                            <div class="flex items-center gap-3 min-w-0">
                                <div class="w-8 h-8 flex-shrink-0 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center text-gray-600 dark:text-gray-300">
                                    <Icon name="heroicons:folder" class="w-4 h-4" />
                                </div>
                                <span class="text-sm text-gray-700 dark:text-gray-300 min-w-0">
                                    {{ projectName ? $t('dashboardCollab.share.projectCanEdit', { name: projectName }) : tm('dashboardCollab.share.projectCanEditUnnamed', artifactMode) }}
                                </span>
                            </div>
                        </div>

                        <!-- People and groups -->
                        <div v-for="entry in sharedEntries" :key="entry.id"
                            class="flex items-center justify-between gap-3 py-2 px-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/60 group"
                            :data-testid="dashboardMode ? `share-entry-${entry.role}` : 'share-entry'">
                            <div class="flex items-center gap-3 min-w-0">
                                <div class="w-8 h-8 flex-shrink-0 rounded-full flex items-center justify-center text-sm font-medium text-white" :class="avatarColor(principalId(entry))">
                                    <Icon v-if="entry.principal_type === 'group'" name="heroicons:user-group" class="w-4 h-4" />
                                    <template v-else>{{ initial(entry.user_name || entry.user_email) }}</template>
                                </div>
                                <div class="flex flex-col min-w-0">
                                    <span class="text-sm text-gray-900 dark:text-gray-100 truncate">{{ entryName(entry) }}</span>
                                    <span v-if="entry.principal_type === 'group'" class="text-xs text-ink-muted truncate">{{ $t('share.groupMembers', entry.member_count || 0) }}</span>
                                    <span v-else-if="entry.user_name && entry.user_email" class="text-xs text-ink-muted truncate">{{ entry.user_email }}</span>
                                </div>
                            </div>
                            <div class="flex items-center flex-shrink-0">
                                <!-- Role and "Remove access" in one menu, as in Google Docs. -->
                                <RoleSelect v-if="dashboardMode && canShare" :model-value="entry.role" :view-disabled-reason="viewDisabledReason"
                                    :disabled="isSaving" removable
                                    @update:model-value="(role) => setEntryRole(entry, role)" @remove="removeSharedEntry(entry)" />
                                <span v-else-if="dashboardMode" class="text-sm text-ink-muted pe-2">
                                    {{ entry.role === 'edit' ? $t('dashboardCollab.share.canEdit') : $t('dashboardCollab.share.canView') }}
                                </span>
                                <UTooltip v-else :text="$t('dashboardCollab.share.removeAccess')">
                                    <button @click="removeSharedEntry(entry)" :disabled="isSaving" :aria-label="$t('dashboardCollab.share.removeAccess')"
                                        data-testid="share-remove"
                                        class="p-1.5 rounded-full text-ink-muted hover:text-red-600 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40">
                                        <Icon name="heroicons:x-mark" class="w-4 h-4" />
                                    </button>
                                </UTooltip>
                            </div>
                        </div>
                    </div>
                </section>

                <!-- General access -->
                <section class="mt-5 mb-2" data-testid="share-general-access">
                    <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">{{ $t('dashboardCollab.share.generalAccess') }}</h3>
                    <div class="flex items-start gap-3">
                        <div class="w-8 h-8 flex-shrink-0 rounded-full flex items-center justify-center"
                            :class="generalAccess === 'restricted' ? 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300' : 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400'">
                            <Icon :name="generalOption.icon" class="w-4 h-4" />
                        </div>
                        <div class="min-w-0 flex-1">
                            <USelectMenu
                                :model-value="generalAccess"
                                :options="generalOptions"
                                value-attribute="value"
                                option-attribute="label"
                                variant="none"
                                size="sm"
                                :disabled="dashboardMode && (!dashboardPublished || !canShare)"
                                :popper="{ placement: 'bottom-start' }"
                                :ui="{ base: 'w-auto', padding: { sm: 'ps-1.5 pe-7 py-1' }, rounded: 'rounded-md' }"
                                :ui-menu="{ width: 'w-72' }"
                                class="-ms-1.5 inline-flex hover:bg-gray-100 dark:hover:bg-gray-800 rounded-md"
                                data-testid="share-general-select"
                                @update:model-value="onGeneralAccessChange"
                            >
                                <template #label>
                                    <span class="text-sm font-medium text-gray-900 dark:text-white">{{ generalOption.label }}</span>
                                </template>
                                <template #option="{ option }">
                                    <!-- Public without make_public (collab01): listed, off, and says why. -->
                                    <div class="flex items-start gap-2.5 py-0.5" :data-testid="option.disabledReason ? 'share-public-locked' : undefined">
                                        <Icon :name="option.icon" class="w-4 h-4 flex-shrink-0 mt-0.5 text-gray-500" />
                                        <div class="flex flex-col">
                                            <span class="text-sm">{{ option.label }}</span>
                                            <span class="text-xs text-gray-500 whitespace-normal">{{ option.disabledReason || option.description }}</span>
                                        </div>
                                    </div>
                                </template>
                            </USelectMenu>
                            <p class="text-xs text-ink-muted mt-0.5">{{ generalOption.description }}</p>
                            <!-- A dashboard is shared with viewers once published
                                 (phase 8): until then general access is off and
                                 says why. Editors can be added before (collab01). -->
                            <p v-if="dashboardMode && !dashboardPublished" class="text-xs text-amber-700 dark:text-amber-400 mt-1.5 flex items-start gap-1.5"
                                data-testid="share-requires-publish">
                                <Icon name="heroicons:information-circle" class="w-4 h-4 flex-shrink-0" />
                                <span>{{ tm('dashboardCollab.share.publishToShareViewers', artifactMode) }}</span>
                            </p>
                            <!-- Public link on a per-user-data dashboard: anyone may
                                 open it, but data resolves per signed-in viewer. -->
                            <p v-if="shareType === 'artifact' && currentVisibility === 'public' && (hasRls || hasUserScoped)"
                                class="text-xs text-ink-muted mt-1.5">
                                {{ tm('share.publicPerUserNote', artifactMode) }}
                            </p>
                        </div>
                    </div>
                </section>

                <!-- Email the link (optional), folded away until asked for. -->
                <div v-if="smtpEnabled && isShared && (!dashboardMode || canShare)" class="mb-2">
                    <button v-if="!showEmail" @click="showEmail = true" data-testid="share-email-toggle"
                        class="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-blue-700 dark:text-blue-400 hover:underline">
                        <Icon name="heroicons:envelope" class="w-3.5 h-3.5" />
                        {{ $t('dashboardCollab.share.emailLink') }}
                    </button>
                    <NotifyRecipientPicker v-else
                        :report-id="report.id"
                        :notification-type="shareType === 'artifact' ? 'share_dashboard' : 'share_conversation'"
                        :share-url="shareUrl"
                        :notify-path="dashboardMode ? `/artifacts/${dashboardId}/notify` : undefined" />
                </div>
            </div>

            <!-- Footer: Copy link / Done, or Cancel / Share while people are picked. -->
            <div class="flex items-center justify-between gap-2 px-6 py-4 mt-2">
                <template v-if="pendingPrincipals.length">
                    <span></span>
                    <div class="flex items-center gap-2">
                        <button @click="pendingPrincipals = []" :disabled="isSaving"
                            class="h-9 px-4 text-sm font-medium rounded-full text-blue-700 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950">
                            {{ $t('common.cancel') }}
                        </button>
                        <button @click="invitePrincipals" :disabled="isSaving" data-testid="share-submit"
                            class="inline-flex items-center gap-1.5 h-9 px-5 text-sm font-medium rounded-full text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40">
                            <Spinner v-if="isSaving" class="w-3.5 h-3.5" />
                            {{ $t('share.share') }}
                        </button>
                    </div>
                </template>
                <template v-else>
                    <button v-if="shareUrl" @click="copyLink" data-testid="share-copy-link"
                        class="inline-flex items-center gap-2 h-9 px-4 text-sm font-medium rounded-full border border-gray-300 dark:border-gray-600 text-blue-700 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950">
                        <Icon :name="copied ? 'heroicons:check' : 'heroicons:link'" class="w-4 h-4" />
                        {{ copied ? $t('share.copied') : $t('share.copyLink') }}
                    </button>
                    <span v-else></span>
                    <button @click="modalOpen = false" data-testid="share-done"
                        class="h-9 px-6 text-sm font-medium rounded-full text-white bg-blue-600 hover:bg-blue-700">
                        {{ $t('dashboardCollab.share.done') }}
                    </button>
                </template>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
import { ref, computed, watch } from 'vue'
import RoleSelect from '~/components/dashboard/share/RoleSelect.vue'

const props = withDefaults(defineProps<{
    report: any
    shareType: 'artifact' | 'conversation'
    title: string
    // What is shared, for the heading: Share "<name>".
    itemName?: string
    compact?: boolean
    // The dashboard open in the editor (phase 8): its own sharing and its /d
    // link. Without it, the conversation's, as before.
    dashboardId?: string
    // The artifact's mode (page / doc / slides): dashboard, document or
    // presentation wording.
    artifactMode?: string | null
}>(), {
    compact: false,
})

const toast = useToast()
const { t } = useI18n()
const { tm } = useArtifactWording()
const { getErrorMessage } = useErrorMessage()
const dashboardMode = computed(() => props.shareType === 'artifact' && !!props.dashboardId)
const dashboardPublished = ref(true)
const dashboardSlug = ref<string | null>(null)
// The visibility as last saved (the conversation's lives on the report).
const savedDashboardVisibility = ref('none')
// Roles (collab01): what the reader may do here (null: not known, the
// server still decides), the editors, the owner, the dashboard's project.
const capabilities = ref<string[] | null>(null)
const hasCap = (cap: string) => (capabilities.value === null ? true : capabilities.value.includes(cap))
const canShare = computed(() => !dashboardMode.value || hasCap('share'))
const canMakePublic = computed(() => !dashboardMode.value || hasCap('make_public'))
const editorGrants = ref<{ user_ids: string[]; group_ids: string[] }>({ user_ids: [], group_ids: [] })
const ownerId = ref<string | null>(null)
const projectId = ref<string | null>(null)
const projectName = ref<string | null>(null)
// The role new picks get: "Can edit" until viewers can be added.
const newRole = ref<'view' | 'edit'>('view')
const { data: authUser } = useAuth()
const currentUserId = computed(() => (authUser.value as any)?.id || null)
const { smtpEnabled } = useAppSettings()
const modalOpen = ref(false)
const isSaving = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)
const inputValue = ref('')
const showDropdown = ref(false)
const showEmail = ref(false)
// Pending picks not yet saved: individual users or whole groups
const pendingPrincipals = ref<{ kind: 'user' | 'group'; id?: string; name?: string; email?: string; memberCount?: number }[]>([])
const sharedEntries = ref<any[]>([])
const copied = ref(false)

const currentVisibility = ref('none')
const conversationShareToken = ref<string | null>(null)
// Per-user data (RLS or user-scoped sources): a public link still resolves
// data per signed-in viewer, which the general-access note says.
const hasRls = ref(false)
const hasUserScoped = ref(false)

const heading = computed(() => (props.itemName ? t('dashboardCollab.share.shareNamed', { name: props.itemName }) : props.title))

// Avatars: a stable color per person, the first letter of their name.
const AVATAR_COLORS = ['bg-blue-600', 'bg-emerald-600', 'bg-violet-600', 'bg-amber-600', 'bg-rose-600', 'bg-cyan-600', 'bg-indigo-600', 'bg-teal-600']
const avatarColor = (key: string | null | undefined) => {
    let h = 0
    for (const c of key || '') h = (h * 31 + c.charCodeAt(0)) >>> 0
    return AVATAR_COLORS[h % AVATAR_COLORS.length]
}
const initial = (name?: string | null) => (name || '?').charAt(0).toUpperCase()

// The trigger's label and icon: the visibility as saved.
const visibilityMeta = computed<Record<string, { label: string; icon: string }>>(() => ({
    none: { label: t('share.visibilityPrivate'), icon: 'heroicons:lock-closed' },
    shared: { label: t('share.visibilityShared'), icon: 'heroicons:user-group' },
    internal: { label: t('share.visibilityInternal'), icon: 'heroicons:building-office' },
    public: { label: t('share.visibilityPublic'), icon: 'heroicons:globe-alt' },
}))

// General access, as Google has it: Restricted covers private and "specific
// people" (whoever is in the list above), then the organization, then anyone
// with the link. Public is the owner's (collab01): listed but off for an
// editor, unless the dashboard already is public (they may leave it so).
const publicLocked = computed(() => dashboardMode.value && !canMakePublic.value && savedDashboardVisibility.value !== 'public')
const generalOptions = computed(() => [
    { value: 'restricted', label: t('dashboardCollab.share.restricted'), description: t('dashboardCollab.share.restrictedDesc'), icon: 'heroicons:lock-closed' },
    { value: 'internal', label: t('share.visibilityInternal'), description: t('dashboardCollab.share.organizationDesc'), icon: 'heroicons:building-office' },
    {
        value: 'public', label: t('share.visibilityPublic'), description: t('dashboardCollab.share.publicDesc'), icon: 'heroicons:globe-alt',
        disabled: publicLocked.value,
        disabledReason: publicLocked.value ? tm('dashboardCollab.share.publicOwnerOnly', props.artifactMode) : '',
    },
])
const generalAccess = computed(() => (['internal', 'public'].includes(currentVisibility.value) ? currentVisibility.value : 'restricted'))
const generalOption = computed(() => generalOptions.value.find(o => o.value === generalAccess.value) || generalOptions.value[0])
// The list shows for a dashboard always (its editors), for a conversation
// while it is restricted (organization and public need no list).
const showPeople = computed(() => dashboardMode.value || generalAccess.value === 'restricted')

// Viewers come from a list only while the dashboard is published and
// restricted (a viewer pick then makes it "specific people"); otherwise
// "Can view" is off, with the reason.
const viewDisabledReason = computed<string | null>(() => {
    if (!dashboardMode.value) return null
    if (!dashboardPublished.value) return t('dashboardCollab.share.viewersAfterPublish')
    if (savedDashboardVisibility.value === 'internal') return t('dashboardCollab.share.viewersOrgWide')
    if (savedDashboardVisibility.value === 'public') return t('dashboardCollab.share.viewersPublic')
    return null
})
watch(viewDisabledReason, (reason) => { if (reason) newRole.value = 'edit' }, { immediate: true })

const visibilityField = computed(() =>
    props.shareType === 'artifact' ? 'artifact_visibility' : 'conversation_visibility'
)

const isShared = computed(() => currentVisibility.value !== 'none')

const shareDescription = computed(() =>
    dashboardMode.value
        ? tm('dashboardCollab.share.description', props.artifactMode)
        : props.shareType === 'artifact'
            ? tm('share.shareDashboardDesc', props.artifactMode)
            : t('share.shareConversationDesc')
)

const buttonLabel = computed(() => {
    // A dashboard's button just says Share; its icon tells the general access.
    if (dashboardMode.value) return t('share.share')
    if (!isShared.value) return props.shareType === 'artifact' ? tm('share.shareDashboard', props.artifactMode) : t('share.share')
    return (visibilityMeta.value[currentVisibility.value] || visibilityMeta.value.none).label
})

const buttonIcon = computed(() => (visibilityMeta.value[currentVisibility.value] || visibilityMeta.value.none).icon)

const shareUrl = computed(() => {
    if (dashboardMode.value) {
        return `${window.location.origin}/d/${dashboardSlug.value || props.dashboardId}`
    }
    if (props.shareType === 'artifact') {
        return `${window.location.origin}/r/${props.report.id}`
    }
    const token = conversationShareToken.value || props.report?.conversation_share_token
    return token ? `${window.location.origin}/c/${token}` : ''
})

// Org members and groups for autocomplete
const members = ref<{ id: string; name: string; email: string }[]>([])
const groups = ref<{ id: string; name: string; memberCount: number }[]>([])
const fetchMembers = async () => {
    try {
        const res = await useMyFetch('/organization/members')
        if (res.data.value) {
            members.value = (res.data.value as any[]).map((u: any) => ({
                id: u.id,
                name: u.name || '',
                email: u.email,
            }))
        }
    } catch { /* silent */ }
}

const fetchGroups = async () => {
    try {
        const res = await useMyFetch('/organization/groups')
        if (res.data.value) {
            groups.value = (res.data.value as any[]).map((g: any) => ({
                id: g.id,
                name: g.name || '',
                memberCount: g.member_count || 0,
            }))
        }
    } catch { /* silent */ }
}

const ownerMember = computed(() => members.value.find(m => m.id === ownerId.value) || null)
const ownerLabel = computed(() => {
    const name = ownerMember.value?.name || ownerMember.value?.email || t('dashboardCollab.share.unknownPerson')
    return ownerId.value && ownerId.value === currentUserId.value ? t('dashboardCollab.share.ownerYou', { name }) : name
})
const entryName = (entry: any) => (entry.principal_type === 'group'
    ? (entry.group_name || (dashboardMode.value ? t('dashboardCollab.share.unknownGroup') : ''))
    : (entry.user_name || entry.user_email || (dashboardMode.value ? t('dashboardCollab.share.unknownPerson') : '')))

const filteredOptions = computed(() => {
    const q = inputValue.value.toLowerCase().trim()
    if (!q) return []
    const existingUserIds = new Set([
        ...sharedEntries.value.filter(e => e.principal_type !== 'group').map(e => e.user_id),
        ...pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id),
    ])
    const existingGroupIds = new Set([
        ...sharedEntries.value.filter(e => e.principal_type === 'group').map(e => e.group_id),
        ...pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id),
    ])
    const groupMatches = groups.value.filter(
        g => !existingGroupIds.has(g.id) && g.name.toLowerCase().includes(q)
    ).map(g => ({ kind: 'group' as const, id: g.id, name: g.name, email: '', memberCount: g.memberCount }))
    const userMatches = members.value.filter(
        m => !existingUserIds.has(m.id) &&
            m.id !== props.report?.user?.id &&
            m.id !== ownerId.value &&
            (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    ).map(m => ({ kind: 'user' as const, id: m.id, name: m.name, email: m.email, memberCount: 0 }))
    return [...groupMatches, ...userMatches].slice(0, 6)
})

const isValidEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)

const addPrincipal = (option: { kind: 'user' | 'group'; id: string; name: string; email?: string; memberCount?: number }) => {
    if (!pendingPrincipals.value.find(p => p.kind === option.kind && p.id === option.id)) {
        pendingPrincipals.value.push({ kind: option.kind, id: option.id, name: option.name, email: option.email, memberCount: option.memberCount })
    }
    inputValue.value = ''
    showDropdown.value = false
}

const addEmailAsPending = (email: string) => {
    const clean = email.trim().toLowerCase()
    if (!clean) return
    if (!isValidEmail(clean)) {
        // Not an email — maybe an exact group name was typed.
        const group = groups.value.find(g => g.name.toLowerCase() === clean)
        if (group) addPrincipal({ kind: 'group', ...group })
        return
    }
    const member = members.value.find(m => m.email.toLowerCase() === clean)
    if (member) {
        addPrincipal({ kind: 'user', ...member })
    } else {
        toast.add({ title: t('share.userNotFound'), color: 'orange' })
    }
}

const removePendingPrincipal = (idx: number) => pendingPrincipals.value.splice(idx, 1)

const handleEnter = () => {
    if (filteredOptions.value.length > 0) {
        addPrincipal(filteredOptions.value[0])
    } else {
        addEmailAsPending(inputValue.value)
    }
}

const handleComma = () => addEmailAsPending(inputValue.value)

const handleBackspace = () => {
    if (!inputValue.value && pendingPrincipals.value.length > 0) pendingPrincipals.value.pop()
}

const onInput = () => { showDropdown.value = true }

const onBlur = () => {
    setTimeout(() => {
        showDropdown.value = false
        if (inputValue.value && isValidEmail(inputValue.value)) addEmailAsPending(inputValue.value)
    }, 200)
}

// API calls
// The dashboard's own sharing (phase 8) and its link's name.
const fetchDashboard = async () => {
    const [sharing, settings] = await Promise.all([
        useMyFetch(`/artifacts/${props.dashboardId}/sharing`),
        useMyFetch(`/artifacts/${props.dashboardId}/settings`),
    ])
    const sh = (sharing.data.value || {}) as any
    applyDashboardSharing(sh)
    dashboardSlug.value = ((settings.data.value || {}) as any).slug || null
    return { artifact_visibility: sh.visibility || 'none' }
}
const dashboardGrants = ref<{ user_ids: string[]; group_ids: string[] }>({ user_ids: [], group_ids: [] })
// A sharing answer (GET or PUT) into the modal's state.
function applyDashboardSharing(sh: any) {
    if (!sh) return
    dashboardPublished.value = sh.published !== false
    savedDashboardVisibility.value = sh.visibility || 'none'
    dashboardGrants.value = { user_ids: sh.user_ids || [], group_ids: sh.group_ids || [] }
    editorGrants.value = { user_ids: sh.editor_user_ids || [], group_ids: sh.editor_group_ids || [] }
    ownerId.value = sh.owner_id || null
    if (projectId.value !== (sh.project_id || null)) {
        projectId.value = sh.project_id || null
        projectName.value = null
    }
}
// What the reader may do (collab01): the draft's meta answers editors; a
// dashboard with no draft for them answers on its published page.
const fetchCapabilities = async () => {
    const id = props.dashboardId
    try {
        let res = await useMyFetch(`/d/${id}/draft`)
        if (res.error.value || !res.data.value) res = await useMyFetch(`/d/${id}`)
        if (id !== props.dashboardId) return
        const caps = (res.data.value as any)?.capabilities
        capabilities.value = !res.error.value && Array.isArray(caps) ? caps : null
    } catch {
        capabilities.value = null
    }
}
// "Everyone in project <name> can edit": the name when the reader can open
// the project; else the line says "this dashboard's project".
const fetchProjectName = async () => {
    const pid = projectId.value
    if (!pid || projectName.value) return
    try {
        const res = await useMyFetch(`/projects/${pid}`)
        if (pid === projectId.value && !res.error.value) projectName.value = (res.data.value as any)?.name || null
    } catch { /* the unnamed line */ }
}

const fetchVisibility = async () => {
    try {
        const res = await useMyFetch(`/reports/${props.report.id}`, { method: 'GET' })
        if (res.data.value) {
            const data = dashboardMode.value
                ? { ...(res.data.value as any), ...(await fetchDashboard()) }
                : res.data.value as any
            currentVisibility.value = data[visibilityField.value] || 'none'
            hasRls.value = !!data.has_rls
            hasUserScoped.value = !!data.has_user_scoped
            if (data.conversation_share_token !== undefined) {
                conversationShareToken.value = data.conversation_share_token
                if (props.report) props.report.conversation_share_token = data.conversation_share_token
            }
        }
    } catch { /* silent */ }
}

const fetchShares = async () => {
    if (dashboardMode.value) {
        // The dashboard's grants, named from the organization's lists:
        // editors first, then viewers (someone in both is an editor).
        const user = (id: string, role: 'view' | 'edit') => {
            const m = members.value.find(x => x.id === id)
            return { id: `user:${id}`, role, principal_type: 'user', user_id: id, user_name: m?.name || '', user_email: m?.email || '' }
        }
        const group = (id: string, role: 'view' | 'edit') => {
            const g = groups.value.find(x => x.id === id)
            return { id: `group:${id}`, role, principal_type: 'group', group_id: id, group_name: g?.name || '', member_count: g?.memberCount || 0 }
        }
        const editorUsers = new Set(editorGrants.value.user_ids)
        const editorGroups = new Set(editorGrants.value.group_ids)
        sharedEntries.value = [
            ...editorGrants.value.user_ids.map(id => user(id, 'edit')),
            ...editorGrants.value.group_ids.map(id => group(id, 'edit')),
            ...dashboardGrants.value.user_ids.filter(id => !editorUsers.has(id)).map(id => user(id, 'view')),
            ...dashboardGrants.value.group_ids.filter(id => !editorGroups.has(id)).map(id => group(id, 'view')),
        ]
        return
    }
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/shares/${props.shareType}`)
        if (res.data.value) {
            sharedEntries.value = res.data.value as any[]
        }
    } catch { /* silent */ }
}

// Viewers only: a dashboard's editors (role 'edit') are a list of their own.
const sharedUserIds = () => sharedEntries.value
    .filter(e => e.principal_type !== 'group' && e.role !== 'edit')
    .map(e => e.user_id)
const sharedGroupIds = () => sharedEntries.value
    .filter(e => e.principal_type === 'group' && e.role !== 'edit')
    .map(e => e.group_id)

type DashboardSharingExtra = { editor_user_ids?: string[]; editor_group_ids?: string[] }
// One PUT of the dashboard's sharing; a list left out stays as it is.
const saveDashboardSharing = async (visibility: string, userIds?: string[], groupIds?: string[], extra: DashboardSharingExtra = {}) => {
    isSaving.value = true
    const before = savedDashboardVisibility.value
    try {
        const body: any = { visibility, ...extra }
        if (userIds) body.user_ids = userIds
        if (groupIds) body.group_ids = groupIds
        const res = await useMyFetch(`/artifacts/${props.dashboardId}/sharing`, { method: 'PUT', body })
        if (res.error.value) throw res.error.value
        const data = res.data.value as any
        if (data) applyDashboardSharing({ ...data, visibility: data.visibility || visibility })
        currentVisibility.value = savedDashboardVisibility.value
        await fetchShares()
        toast.add({
            title: visibility === 'none' && before !== 'none' ? t('share.sharingDisabled') : t('share.sharingUpdated'),
            color: 'green',
        })
        return true
    } catch (e) {
        currentVisibility.value = savedDashboardVisibility.value
        toast.add({ title: t('share.sharingFailed'), description: getErrorMessage(e), color: 'red' })
        return false
    } finally {
        isSaving.value = false
    }
}

// Roles on a dashboard (collab01). Someone in both lists is an editor, so a
// change of role moves the id from one list to the other in one PUT.
const principalId = (entry: any): string => (entry.principal_type === 'group' ? entry.group_id : entry.user_id)
const without = (ids: string[], id: string) => ids.filter(x => x !== id)

const inviteToDashboard = async () => {
    const users = pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id).filter(Boolean) as string[]
    const groupIds = pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id).filter(Boolean) as string[]
    let ok: boolean
    if (newRole.value === 'edit' || viewDisabledReason.value) {
        // Editors stand whatever the visibility, before the first publish too.
        ok = await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined, {
            editor_user_ids: [...editorGrants.value.user_ids, ...users],
            editor_group_ids: [...editorGrants.value.group_ids, ...groupIds],
        })
    } else {
        // A viewer pick on a private dashboard makes it "Specific people".
        currentVisibility.value = 'shared'
        ok = await saveDashboardSharing('shared', [...sharedUserIds(), ...users], [...sharedGroupIds(), ...groupIds])
    }
    if (ok) pendingPrincipals.value = []
}

const setEntryRole = async (entry: any, role: 'view' | 'edit') => {
    if (role === entry.role) return
    const id = principalId(entry)
    const isGroup = entry.principal_type === 'group'
    const editors = without(isGroup ? editorGrants.value.group_ids : editorGrants.value.user_ids, id)
    const viewers = without(isGroup ? sharedGroupIds() : sharedUserIds(), id)
    if (role === 'edit') editors.push(id)
    else viewers.push(id)
    const extra: DashboardSharingExtra = isGroup ? { editor_group_ids: editors } : { editor_user_ids: editors }
    if (role === 'edit') {
        await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined, extra)
        return
    }
    // A viewer counts only while the visibility is "Specific people".
    currentVisibility.value = 'shared'
    await saveDashboardSharing('shared', isGroup ? undefined : viewers, isGroup ? viewers : undefined, extra)
}

const removeFromDashboard = async (entry: any) => {
    const id = principalId(entry)
    const isGroup = entry.principal_type === 'group'
    if (entry.role === 'edit') {
        await saveDashboardSharing(savedDashboardVisibility.value, undefined, undefined,
            isGroup ? { editor_group_ids: without(editorGrants.value.group_ids, id) } : { editor_user_ids: without(editorGrants.value.user_ids, id) })
        return
    }
    const users = isGroup ? sharedUserIds() : without(sharedUserIds(), id)
    const groupIds = isGroup ? without(sharedGroupIds(), id) : sharedGroupIds()
    if (users.length === 0 && groupIds.length === 0 && savedDashboardVisibility.value === 'shared') {
        // The last viewer: private again, as for a conversation. Editors stay.
        currentVisibility.value = 'none'
        await saveDashboardSharing('none')
    } else {
        await saveDashboardSharing('shared', users, groupIds)
    }
}

const saveVisibility = async (visibility: string, userIds?: string[], groupIds?: string[]) => {
    if (dashboardMode.value) return saveDashboardSharing(visibility, userIds, groupIds)
    isSaving.value = true
    try {
        const body: any = { visibility }
        if (userIds) body.shared_user_ids = userIds
        if (groupIds) body.shared_group_ids = groupIds
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/${props.shareType}`, {
            method: 'PUT',
            body,
        })
        if (res.error.value) throw res.error.value

        if (props.report) {
            props.report[visibilityField.value] = visibility
        }
        // Surface the resulting report_shared / artifact_shared session-event strip.
        window.dispatchEvent(new CustomEvent('report:mutated', { detail: { reportId: props.report?.id, kind: 'share' } }))

        const data = res.data.value as any
        if (props.shareType === 'conversation' && data) {
            const token = data.conversation_share_token ?? null
            conversationShareToken.value = token
            if (props.report) props.report.conversation_share_token = token
        }

        toast.add({
            title: visibility === 'none' ? t('share.sharingDisabled') : t('share.sharingUpdated'),
            color: 'green',
        })
    } catch {
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

// General access: Restricted is private, or "specific people" while
// someone is in the list -- leaving the organization or the link clears
// the viewer list server-side, so it lands on private.
const onGeneralAccessChange = async (value: string) => {
    if (value === generalAccess.value) return
    const target = value === 'restricted' ? 'none' : value
    currentVisibility.value = target
    await saveVisibility(target)
    if (!dashboardMode.value) await fetchShares()
}

const invitePrincipals = async () => {
    if (pendingPrincipals.value.length === 0) return
    if (dashboardMode.value) return inviteToDashboard()

    if (currentVisibility.value === 'none') {
        currentVisibility.value = 'shared'
    }

    const allUserIds = [
        ...sharedUserIds(),
        ...pendingPrincipals.value.filter(p => p.kind === 'user').map(p => p.id).filter(Boolean),
    ]
    const allGroupIds = [
        ...sharedGroupIds(),
        ...pendingPrincipals.value.filter(p => p.kind === 'group').map(p => p.id).filter(Boolean),
    ]

    await saveVisibility(currentVisibility.value === 'shared' ? 'shared' : currentVisibility.value, allUserIds, allGroupIds)
    await fetchShares()
    pendingPrincipals.value = []
}

const removeSharedEntry = async (entry: any) => {
    if (dashboardMode.value) return removeFromDashboard(entry)
    const remainingUsers = sharedEntries.value
        .filter(e => e.id !== entry.id && e.principal_type !== 'group')
        .map(e => e.user_id)
    const remainingGroups = sharedEntries.value
        .filter(e => e.id !== entry.id && e.principal_type === 'group')
        .map(e => e.group_id)

    if (remainingUsers.length === 0 && remainingGroups.length === 0 && currentVisibility.value === 'shared') {
        currentVisibility.value = 'none'
        await saveVisibility('none')
    } else {
        await saveVisibility('shared', remainingUsers, remainingGroups)
    }

    await fetchShares()
}

const copyLink = async () => {
    try {
        await navigator.clipboard.writeText(shareUrl.value)
        copied.value = true
        setTimeout(() => { copied.value = false }, 2000)
    } catch {
        toast.add({ title: t('share.copyFailed'), color: 'red' })
    }
}

const openModal = async () => {
    modalOpen.value = true
    showEmail.value = false
    pendingPrincipals.value = []
    if (dashboardMode.value) {
        currentVisibility.value = savedDashboardVisibility.value
        // The grants are named from these lists, so they come first.
        await Promise.all([fetchMembers(), fetchGroups(), fetchCapabilities()])
        await fetchVisibility()
        currentVisibility.value = savedDashboardVisibility.value
        newRole.value = viewDisabledReason.value ? 'edit' : 'view'
        await Promise.all([fetchShares(), fetchProjectName()])
        return
    }
    currentVisibility.value = props.report?.[visibilityField.value] || 'none'
    conversationShareToken.value = props.report?.conversation_share_token ?? null
    await Promise.all([fetchMembers(), fetchGroups(), fetchVisibility(), fetchShares()])
}

// The button shows the dashboard's own sharing (phase 8): read it whenever
// another dashboard is opened in the editor. Silent for whoever may not
// manage it (the modal then shows what the server refuses).
watch(() => props.dashboardId, async (id) => {
    if (!id || props.shareType !== 'artifact') return
    // Another dashboard: its own capabilities, read again on open.
    capabilities.value = null
    const { data, error } = await useMyFetch(`/artifacts/${id}/sharing`)
    if (id !== props.dashboardId || error.value || !data.value) return
    applyDashboardSharing(data.value)
    if (!modalOpen.value) currentVisibility.value = savedDashboardVisibility.value
}, { immediate: true })

// Keep button in sync when report data loads/changes (e.g. after page reload)
watch(
    () => props.report?.[visibilityField.value],
    (val) => {
        if (val && !modalOpen.value && !dashboardMode.value) {
            currentVisibility.value = val
        }
    },
    { immediate: true }
)
</script>
