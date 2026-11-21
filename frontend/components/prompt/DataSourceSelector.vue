<template>
    <div class="inline-block relative" ref="containerRef">
        <UPopover :popper="{ strategy: 'absolute', placement: 'bottom-start', offset: [0,8] }">
            <UTooltip :text="isCompactFinal ? dataTooltip : ''" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                <button
                    class="inline-flex items-center text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md p-2 text-xs"
                    :disabled="isLoading"
                    data-testid="agent-scope-trigger"
                    :data-auto="isAutoMode ? 'true' : 'false'"
                >
                    <span v-if="isLoading" class="flex items-center">
                        <Spinner class="w-4 h-4 text-ink-muted animate-spin" />
                    </span>
                    <!-- Auto with nothing to resolve to isn't Auto, it's an
                         empty workspace — fall through to the bare icon. -->
                    <span v-else-if="folderIds.length" class="flex gap-1 items-center"><UIcon name="i-heroicons-folder" />{{ folderNames.get(folderIds[0]) || $t('sharedFolders.title') }}<span v-if="folderIds.length + internalSelectedDataSources.length > 1">+{{ folderIds.length + internalSelectedDataSources.length - 1 }}</span></span>
                    <span v-else-if="isAutoMode && visibleDataSources.length > 0" class="flex items-center">
                        <Icon name="heroicons-bolt" class="h-4 w-4" />
                        <span v-if="!isCompactFinal" class="ms-1 text-xs">{{ $t('prompt.modelAuto') }}</span>
                    </span>
                    <span v-else-if="internalSelectedDataSources.length > 0" class="flex items-center">
                        <template v-if="isCompactFinal">
                            <!-- Compact: show only first icon -->
                            <DataSourceIcon :type="internalSelectedDataSources[0].type" :connector-key="internalSelectedDataSources[0].connector_key" :icon-token="internalSelectedDataSources[0].icon_token" :icon="internalSelectedDataSources[0].icon" class="h-4" />
                        </template>
                        <template v-else>
                            <!-- Non-compact: show stacked icons -->
                            <div class="flex -space-x-1">
                                <DataSourceIcon
                                    v-for="ds in internalSelectedDataSources.slice(0, 3)"
                                    :key="ds.id"
                                    :type="ds.type"
                                    :connector-key="ds.connector_key"
                                    :icon-token="ds.icon_token" :icon="ds.icon"
                                    class="h-4 ring-1 ring-white rounded flex-shrink-0"
                                />
                            </div>
                            <!-- One agent: an icon alone doesn't say which one, and
                                 this is the state every new report in a project
                                 opens in. Name it. -->
                            <span v-if="internalSelectedDataSources.length === 1 && internalSelectedDataSources[0].name" class="ms-1.5 text-xs max-w-[140px] truncate">
                                {{ internalSelectedDataSources[0].name }}
                            </span>
                            <span v-else-if="internalSelectedDataSources.length > 3" class="ms-1 text-2xs text-ink-muted">
                                +{{ internalSelectedDataSources.length - 3 }}
                            </span>
                        </template>
                    </span>
                    <span v-else class="flex items-center">
                        <AgentIcon class="h-4 w-4" />
                    </span>
                </button>
            </UTooltip>
            <template #panel>
                <div class="text-xs w-max min-w-[260px] max-w-[420px] rounded-xl" data-testid="agent-scope-panel">
                    <!-- Search — same affordance as the model picker. Mounts with
                         the panel, so focus on mount == focus on open; the query
                         is reset when the panel closes so it never carries over. -->
                    <div
                        v-if="!isLoading && (visibleDataSources.length > 0 || connectableDataSources.length > 0 || availableFolders.length > 0)"
                        class="p-2 border-b border-gray-100 dark:border-gray-800"
                    >
                        <div class="flex items-center gap-1.5 px-2 py-1.5 rounded-md bg-gray-50 dark:bg-gray-800/60">
                            <Icon name="heroicons-magnifying-glass" class="w-3.5 h-3.5 text-ink-muted flex-shrink-0" />
                            <input
                                v-model="agentQuery"
                                type="text"
                                data-testid="agent-scope-search"
                                class="flex-1 min-w-0 bg-transparent outline-none text-xs text-gray-800 dark:text-gray-100 placeholder-gray-400"
                                :placeholder="$t('prompt.searchAgentFolders')"
                                @vue:mounted="focusSearch"
                                @vue:unmounted="agentQuery = ''"
                            />
                        </div>
                    </div>
                    <div class="p-2 max-h-64 overflow-y-auto">
                    <button type="button"
                        class="w-full px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer flex items-center justify-between"
                        @click="toggleAutoMode"
                        data-testid="agent-scope-auto"
                        :data-selected="isAutoMode ? 'true' : 'false'"
                    >
                        <div class="flex items-center">
                            <Icon name="heroicons-bolt" class="h-4 w-4 text-gray-500 dark:text-gray-400 me-2" />
                            <div class="flex flex-col text-start">
                                <span class="text-sm">{{ $t('prompt.modelAuto') }}</span>
                                <span class="text-2xs text-ink-muted">
                                    {{ $t('prompt.autoAgentsHint') }}
                                </span>
                            </div>
                        </div>
                        <Icon v-if="isAutoMode" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                    </button>
                    <div class="my-1 border-t border-gray-100 dark:border-gray-800" />
                    <AgentFolderTree :folders="pickerFolders" :selected="checkedFolders" :search="agentQuery"
                        :member-folders="[...folderMembers.keys()]" @select="pickFolder">
                        <template #members="{folderId}">
                            <AgentScopeRow v-for="ds in folderMembers.get(folderId)" :key="ds.id" :ds="ds" :selected="!isAutoMode && isSelected(ds)" :stage="stageOf(ds)" :service-account="isServiceAccount(ds)"
                                    @select="toggleDataSource(ds)" @hover="onDataSourceHover(ds.id, $event)" @leave="onDataSourceHoverLeave" />
                        </template>
                    </AgentFolderTree>
                    <div v-if="isLoading" class="flex items-center justify-center py-6 text-gray-500 dark:text-gray-400 space-x-2">
                        <Spinner class="w-4 h-4 text-ink-muted animate-spin" />
                        <span>{{ $t('prompt.loadingDataSources') }}</span>
                    </div>
                    <template v-else>
                        <div v-if="visibleDataSources.length === 0 && connectableDataSources.length === 0 && !availableFolders.length" class="text-center text-gray-500 dark:text-gray-400 py-4">
                            {{ $t('prompt.noDataSources') }}
                        </div>
                        <div
                            v-else-if="hasQuery && !scopeFolders.length && filteredGroups.length === 0 && filteredConnectable.length === 0"
                            class="text-center text-gray-500 dark:text-gray-400 py-4"
                            data-testid="agent-scope-no-match"
                        >
                            {{ $t('prompt.noAgentsMatch') }}
                        </div>
                        <template v-else>
                            <template v-if="visibleDataSources.length > 0">
                                <template v-for="group in filteredGroups" :key="group.key">
                                <div v-if="group.divider" class="my-1 border-t border-gray-100 dark:border-gray-800" />
                                <div v-if="group.label" class="px-2 pt-1 pb-0.5 text-start text-2xs font-medium uppercase tracking-wide text-ink-muted">
                                    {{ group.label }}
                                </div>
                                <AgentScopeRow v-for="ds in group.items" :key="ds.id" :parent-path="hasQuery ? folderNames.get(ds.folder_id || '') : undefined" :ds="ds" :selected="!isAutoMode && isSelected(ds)" :stage="stageOf(ds)" :service-account="isServiceAccount(ds)"
                                    @select="toggleDataSource(ds)" @hover="onDataSourceHover(ds.id, $event)" @leave="onDataSourceHoverLeave" />
                                </template>
                            </template>

                            <!-- Not-yet-connected (user_required) data sources: grayed out
                                 with a Connect action. These are NOT selectable and are
                                 never persisted to the report until connected. -->
                            <template v-if="filteredConnectable.length > 0">
                                <div v-if="filteredGroups.length > 0" class="my-1 border-t border-gray-100 dark:border-gray-800" />
                                <div
                                    v-for="ds in filteredConnectable"
                                    :key="ds.id"
                                    class="px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer flex items-center justify-between gap-2"
                                    @click="openCredentialsModal(ds)"
                                >
                                    <div class="flex items-center min-w-0 opacity-50">
                                        <DataSourceIcon :type="ds.type" :connector-key="ds.connector_key" :icon-token="ds.icon_token" :icon="ds.icon" class="h-4 flex-shrink-0" />
                                        <span class="ms-2 text-sm truncate">{{ ds.name }}<small v-if="hasQuery" class="block truncate text-2xs">{{ folderNames.get(ds.folder_id || '') }}</small></span>
                                    </div>
                                    <button
                                        type="button"
                                        :disabled="connectingId === ds.id"
                                        class="flex-shrink-0 inline-flex items-center gap-1 px-2 py-0.5 text-2xs text-blue-600 dark:text-blue-300 bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-md hover:bg-blue-100 dark:hover:bg-blue-900/50 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                                        @click.stop="openCredentialsModal(ds)"
                                    >
                                        <Spinner v-if="connectingId === ds.id" class="w-3 h-3" />
                                        <Icon v-else name="heroicons-key" class="w-3 h-3" />
                                        {{ $t('data.connect') }}
                                    </button>
                                </div>
                            </template>
                        </template>
                    </template>
                    </div>
                </div>
            </template>
        </UPopover>

        <!-- Agent flyout component -->
        <AgentFlyout
            :agent-id="hoveredDataSourceId"
            :visible="flyout.visible"
            :position="flyout"
            @mouseenter="onFlyoutEnter"
            @mouseleave="onFlyoutLeave"
            @connect="openCredentialsModal"
        />

        <!-- User credentials / OAuth modal for connecting user_required sources -->
        <UserDataSourceCredentialsModal
            v-model="showCredsModal"
            :data-source="selectedConnectDs"
            @saved="onCredentialsSaved"
        />
    </div>

</template>

<script lang="ts" setup>
import AgentScopeRow from './AgentScopeRow.vue'
import { loadAgentFolderOptions } from '~/composables/useAgentFolderOptions'
import Spinner from '@/components/Spinner.vue'
import AgentFlyout from '~/components/AgentFlyout.vue'
import AgentIcon from '~/components/icons/AgentIcon.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { usePermissions, usePermissionsLoaded, useResourcePermissions } from '~/composables/usePermissions'
import { deriveStage } from '~/composables/useDataSourcePublishStatus'
import { splitRecent } from '~/utils/recentItems'

type DataSource = { id: string; folder_id?: string; name: string; type?: string; auth_policy?: string; publish_status?: string; reliability_status?: string; connections?: any[]; user_status?: { effective_auth?: string; has_user_credentials?: boolean; uses_fallback?: boolean } }

// Unified lifecycle stage (Development / Training / Production / Disabled),
// derived from the agent's publish_status + reliability_status — matches the
// agent detail page instead of surfacing the raw "Draft" publish_status.
function stageOf(ds: DataSource) {
    return deriveStage(ds.publish_status, ds.reliability_status)
}
const internalSelectedDataSources = ref<DataSource[]>([])
const dataSources = ref<DataSource[]>([])
// user_required data sources the user hasn't connected yet — shown grayed out
// with a Connect action. Kept separate so they never enter selection/auto-mode
// and are never persisted to the report.
const connectableDataSources = ref<DataSource[]>([])
const isLoading = ref(true)
const isOpen = ref(false)
const containerRef = ref<HTMLElement | null>(null)
const isCompact = ref(false)
const isCompactFinal = computed(() => isCompact.value)
// Auto is the absence of a pin, never a shape of one — see
// utils/agentSelection.ts. Selecting every agent, or the org's only agent, is
// a manual scope and renders as such.
const autoState = computed(() => resolveAgentAuto({
    selectedIds: internalSelectedDataSources.value.map((ds: any) => String(ds.id)),
}))
const isAutoMode = computed(() => !props.folderIds.length && autoState.value.isAuto)

// Hover flyout state
const hoveredDataSourceId = ref<string | null>(null)
const flyout = reactive<{
    visible: boolean
    top?: number
    bottom?: number
    left?: number
    right?: number
    maxHeight?: number
    maxWidth?: number
}>({ visible: false, left: 0, maxHeight: 0 })
let flyoutHideTimer: ReturnType<typeof setTimeout> | null = null

// On touch/coarse-pointer devices there is no hover. Tapping a row would
// otherwise fire `mouseenter` and pop the fixed flyout overlay on top of the
// list, swallowing the tap so the selection never registers. Detect touch and
// skip the flyout entirely so rows stay tappable on mobile.
const isTouchDevice = ref(false)
onMounted(() => {
    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
        isTouchDevice.value = window.matchMedia('(pointer: coarse)').matches
    }
})

// Connect (user credentials / OAuth) modal state
const showCredsModal = ref(false)
const selectedConnectDs = ref<DataSource | null>(null)
const signIn = useConnectionSignIn()
const { t } = useI18n()
const toast = useToast()
const { fetchActiveAgents } = useActiveAgents()

// Mirror the agents page: if the source's pending-sign-in connection has
// OAuth as its only user auth mode, redirect straight to the provider
// instead of showing an empty modal. Falls back to the modal otherwise.
function findPendingSignInConnection(ds: DataSource): any | null {
    for (const conn of (ds.connections || [])) {
        if (conn.auth_policy === 'user_required' && !conn.user_status?.has_user_credentials) {
            return conn
        }
    }
    return null
}

// Data source id whose Connect button is mid-sign-in (awaiting the authorize
// redirect). Stays set through a redirect so the spinner persists until the
// browser unloads the page.
const connectingId = ref<string | null>(null)

async function openCredentialsModal(ds: DataSource) {
    const pending = findPendingSignInConnection(ds)
    if (pending) {
        // Spin the clicked button while we fetch the authorize URL — for
        // SSO/Entra/OBO this round-trip hits Azure and is slow enough that the
        // button otherwise looks frozen before the browser navigates away.
        connectingId.value = ds.id
        const result = await signIn.triggerUserSignIn(pending)
        if (result.redirecting) return // keep spinning; the page is navigating to the provider
        connectingId.value = null
        if (result.error) {
            toast.add({ title: t('data.oauthStartFailed'), description: result.error, color: 'red' })
        }
    }
    selectedConnectDs.value = ds
    showCredsModal.value = true
}

async function onCredentialsSaved() {
    showCredsModal.value = false
    // Re-fetch so a freshly-connected source moves into the selectable list.
    // force: the shared list would otherwise still be inside its freshness
    // window and hand back the pre-connection state.
    await getDataSources({ force: true })
}

const showFlyoutAtEvent = (evt: MouseEvent) => {
    const el = evt.currentTarget as HTMLElement | null
    if (!el) return

    const panel = el.closest('.rounded-xl') as HTMLElement | null
    const rowRect = el.getBoundingClientRect()
    const panelRect = panel?.getBoundingClientRect() ?? rowRect
    const vw = window.innerWidth
    const vh = window.innerHeight
    const gap = 8
    const edge = 12
    // AgentFlyout's width range (min-w-[320px] / max-w-[440px]).
    const minWidth = 320
    const maxWidth = 440

    // Horizontal: sit beside the dropdown. Prefer the right side; otherwise
    // anchor the flyout's RIGHT edge to the dropdown's left edge, so it hugs
    // the dropdown whatever width the card ends up rendering at.
    const roomRight = vw - panelRect.right - gap - edge
    const roomLeft = panelRect.left - gap - edge
    flyout.left = undefined
    flyout.right = undefined
    if (roomRight >= minWidth || roomRight >= roomLeft) {
        flyout.left = panelRect.right + gap
        flyout.maxWidth = Math.min(maxWidth, Math.max(roomRight, minWidth))
    } else {
        flyout.right = vw - panelRect.left + gap
        flyout.maxWidth = Math.min(maxWidth, Math.max(roomLeft, minWidth))
    }

    // Vertical: line up with the hovered row. Near the bottom of the screen,
    // grow upward from the row instead so the card stays on screen.
    flyout.top = undefined
    flyout.bottom = undefined
    const roomBelow = vh - rowRect.top - edge
    if (roomBelow >= 240) {
        flyout.top = Math.max(edge, rowRect.top - gap)
        flyout.maxHeight = vh - flyout.top - edge
    } else {
        flyout.bottom = Math.max(edge, vh - rowRect.bottom - gap)
        flyout.maxHeight = vh - flyout.bottom - edge
    }
    flyout.visible = true
}

const onDataSourceHover = (dataSourceId: string, evt: MouseEvent) => {
    // No hover flyout on touch devices — keep rows tappable.
    if (isTouchDevice.value) return
    if (flyoutHideTimer) {
        clearTimeout(flyoutHideTimer)
        flyoutHideTimer = null
    }
    if (typeof window !== 'undefined') showFlyoutAtEvent(evt)
    hoveredDataSourceId.value = dataSourceId
}

const onDataSourceHoverLeave = () => {
    // Give the user time to move cursor from list → flyout
    if (flyoutHideTimer) clearTimeout(flyoutHideTimer)
    flyoutHideTimer = setTimeout(() => {
        flyout.visible = false
        hoveredDataSourceId.value = null
    }, 120)
}

const onFlyoutEnter = () => {
    if (flyoutHideTimer) {
        clearTimeout(flyoutHideTimer)
        flyoutHideTimer = null
    }
    flyout.visible = true
}

const onFlyoutLeave = () => {
    onDataSourceHoverLeave()
}

const props = defineProps({
    allowFolderScope: Boolean,
    expandFolderSelection: Boolean,
    folderIds: { type: Array as () => string[], default: () => [] },
    selectedDataSources: {
        type: Array,
        default: () => [],
    },
    reportId: {
        type: String,
        default: () => '',
    },
    // Whether an agent toggle writes the selection back to the report
    // (PUT /reports/{id}). True on a real report's composer; false when the
    // selector lives in a context that borrows the report's id for files and
    // mentions but must NOT rewrite the report's agents — e.g. the scheduled
    // task form, where the pick belongs to the task, not the report.
    persistSelection: {
        type: Boolean,
        default: true,
    },
    // When set, only show data sources the user has this permission on
    // (either org-wide or via a per-DS resource grant).
    permission: {
        type: String,
        default: '',
    },
    // Project context (report inside a project): the project's name and its
    // default agent ids. When present, "Auto" selects exactly these defaults
    // and the panel groups them under a "From {project}" header.
    projectName: {
        type: String,
        default: '',
    },
    projectDefaultIds: {
        type: Array,
        default: () => [],
    }
});


const emit = defineEmits(['update:folderIds', 'update:availableFolders', 'update:selectedDataSources', 'update:availableDataSources', 'update:autoMode']);

// Optionally restrict visible data sources to those the user has `permission`
// for. Uses the resource-grant tier directly (NOT the org-perm implication
// tier) so that org-wide perms don't auto-grant the action on every DS the
// user can access — the user must have an explicit per-DS grant. Org admins
// (full_admin_access) still see everything.
// Mirrors the backend gate exactly (ResolvedPermissions.has_resource_permission):
// full_admin → org-perm implication (org manage_evals ⇒ every agent) → grant
// implication (`manage` ⇒ manage_evals on that agent) → explicit grant.
//
// All four tiers matter. Dropping the grant-implication tier hides an agent
// from its own manager (whose grant is spelled `manage`); dropping the org tier
// hides every agent from an org-wide eval admin. Either way the picker ends up
// stricter than the API it feeds, which reads as "the button is missing" rather
// than "you may not do this".
const permsLoaded = usePermissionsLoaded()
const visibleDataSources = computed(() => {
    if (!props.permission) return dataSources.value
    if (!permsLoaded.value) return []
    return dataSources.value.filter(
        (ds: any) => useCan(props.permission, { type: 'data_source', id: String(ds.id) }),
    )
})

// Publish the selectable agent list upward (report page renders it as the
// blank-report agent picker) so nobody has to re-derive "which agents can this
// user actually pick" — or refetch the list — outside this component.
watch(visibleDataSources, (val) => {
    emit('update:availableDataSources', val)
}, { immediate: true })

// Project default agents, intersected with what this user can actually see
// (a default never widens access). Empty when there is no project context.
const projectDefaultSources = computed(() => {
    const ids = new Set(((props.projectDefaultIds as any[]) || []).map((x: any) => String(x)))
    if (!ids.size) return []
    return visibleDataSources.value.filter((ds: any) => ids.has(String(ds.id)))
})
const nonDefaultSources = computed(() => {
    if (!projectDefaultSources.value.length) return visibleDataSources.value
    const ids = new Set(projectDefaultSources.value.map((ds: any) => String(ds.id)))
    return visibleDataSources.value.filter((ds: any) => !ids.has(String(ds.id)))
})
// Panel row groups, top to bottom:
//   1. project defaults under "From {project}" (an admin's deliberate choice),
//   2. "Recent" — the few agents this user last chatted with (last_used_at
//      from /data_sources/active; see utils/recentItems.ts),
//   3. everything else, A–Z, so that part never moves with usage.
// A search drops the Recent head: matches list A–Z, each agent once.
const byName = (a: any, b: any) => String(a?.name || '').localeCompare(String(b?.name || ''))
const sourceGroups = computed(() => {
    const groups: any[] = []
    if (projectDefaultSources.value.length) {
        groups.push({
            key: 'project',
            label: t('projects.overview.fromProject', { name: props.projectName }),
            divider: false,
            items: projectDefaultSources.value,
        })
    }
    const { recent, rest } = hasQuery.value
        ? { recent: [], rest: nonDefaultSources.value }
        : splitRecent(nonDefaultSources.value)
    if (recent.length) {
        groups.push({ key: 'recent', label: t('prompt.recent'), divider: groups.length > 0, items: recent })
    }
    if (rest.length) {
        groups.push({ key: 'rest', label: '', divider: groups.length > 0, items: [...rest].sort(byName) })
    }
    return groups
})

// Search, mirroring ModelPickerPanel: matches on name (and type, so "duckdb"
// finds every DuckDB agent). Group headers survive only when they still have
// rows. Auto remains available above the results.
const agentQuery = ref('')
const hasQuery = computed(() => agentQuery.value.trim().length > 0)
function matchesQuery(ds: DataSource) {
    const q = agentQuery.value.trim().toLowerCase()
    if (!q) return true
    return [ds.name, ds.type, folderNames.value.get(ds.folder_id || '')].some((v) => String(v || '').toLowerCase().includes(q))
}
const filteredGroups = computed(() => {
    return sourceGroups.value
        .map(g => ({ ...g, items: g.items.filter((ds: DataSource) => hasQuery.value ? matchesQuery(ds) : !folderMembers.value.has(ds.folder_id || '')) }))
        .filter((g: any) => g.items.length > 0)
})
const filteredConnectable = computed(() => connectableDataSources.value.filter(matchesQuery))
function focusSearch(vnode: any) {
    nextTick(() => vnode?.el?.focus?.())
}

// Auto isn't a selection — it's the absence of one. An external picker has to
// know, or it would light up every agent the moment an Auto report opens.
watch(isAutoMode, (val) => {
    emit('update:autoMode', val)
}, { immediate: true })

async function getDataSources(opts: { force?: boolean } = {}) {
    try {
        // Shared with the mention menu — see composables/useActiveAgents.ts.
        // Connectors (the user's own accounts/tools) live in the connectors
        // menu, never in the agent picker.
        const allSources = (await fetchActiveAgents(opts)).filter((ds: any) => ds?.kind !== 'connector')
        // A source is usable (selectable) when it doesn't require per-user
        // credentials, or the user already has them / falls back to system auth.
        const isUsable = (ds: any) => {
            if (ds.auth_policy !== 'user_required') return true
            const status = ds.user_status
            if (status?.has_user_credentials) return true
            // effective_auth === 'system' means the user can run via system/service-
            // principal creds — including the admin fallback (uses_fallback).
            // Treat that as usable so admins aren't forced through "Connect" for a
            // source they can already query.
            return status?.effective_auth === 'system'
        }
        dataSources.value = allSources.filter(isUsable)
        // Everything else returned is a user_required source the user can connect.
        connectableDataSources.value = allSources.filter((ds: any) => !isUsable(ds))
        // Align the parent's selection to the objects just loaded. An empty
        // selection is Auto and stays empty: seeding it with the whole roster
        // (the old "landing page defaults to auto" branch) both froze Auto into
        // a manual pin and raced parents that set their selection after mount,
        // overwriting an explicitly chosen agent with "all of them".
        if ((props.selectedDataSources as any[])?.length) {
            internalSelectedDataSources.value = alignSelection(props.selectedDataSources as any[])
            handleSelectionChange()
        }
    } finally {
        isLoading.value = false
    }
}

function handleSelectionChange() {
    emit('update:selectedDataSources', internalSelectedDataSources.value);
}

function isSelected(option: any) {
    return internalSelectedDataSources.value.some((ds: any) => ds.id === option.id)
}

// The user can use this user_required source via the connection's system
// (service principal) credentials — admin/owner fallback, no personal sign-in.
function isServiceAccount(ds: any) {
    return ds?.auth_policy === 'user_required'
        && ds?.user_status?.effective_auth === 'system'
        && !ds?.user_status?.has_user_credentials
}

const availableFolders = ref<Awaited<ReturnType<typeof loadAgentFolderOptions>>>([])
const { getErrorMessage } = useErrorMessage()
const folderNames = computed(() => new Map(availableFolders.value.map(f => [f.id, f.path])))
const folderMembers = computed(() => {
    const members = new Map<string, DataSource[]>()
    for (const ds of visibleDataSources.value) {
        if (!ds.folder_id || !folderNames.value.has(ds.folder_id)) continue
        if (!members.has(ds.folder_id)) members.set(ds.folder_id, [])
        members.get(ds.folder_id)!.push(ds)
    }
    return members
})
const eligibleFolderMembers = computed(() => {
    const result = new Map<string, string[]>()
    for (const folder of availableFolders.value) {
        const ids = folderMembers.value.get(folder.id)?.map(agent => agent.id)
        if (!ids?.length) continue
        for (const ancestor of folder.ancestors) {
            if (!result.has(ancestor)) result.set(ancestor, [])
            result.get(ancestor)!.push(...ids)
        }
    }
    return result
})
const pickerFolders = computed(() => props.expandFolderSelection
    ? availableFolders.value.filter(f => eligibleFolderMembers.value.has(f.id))
    : availableFolders.value)
const checkedFolders = computed(() => props.expandFolderSelection
    ? pickerFolders.value.filter(f => eligibleFolderMembers.value.get(f.id)!.every(id => internalSelectedDataSources.value.some(ds => ds.id === id))).map(f => f.id)
    : props.folderIds)
function pickFolder(id: string) {
    if (props.expandFolderSelection) selectFolderMembers(eligibleFolderMembers.value.get(id) || [])
    else chooseFolder(id)
}
const scopeFolders = computed(() => pickerFolders.value.filter(f => f.path.toLowerCase().includes(agentQuery.value.toLowerCase())))
watch(availableFolders, items => emit('update:availableFolders', items), {immediate:true})
onMounted(async () => {
    if (!props.allowFolderScope && !props.expandFolderSelection) return
    try {
        availableFolders.value = await loadAgentFolderOptions()
    } catch (error) {
        toast.add({title: getErrorMessage(error), color: 'red'})
    }
})
function selectFolderMembers(ids: string[]) {
    const picked = new Map(internalSelectedDataSources.value.map(ds => [ds.id, ds]))
    for (const ds of visibleDataSources.value) if (ids.includes(ds.id)) picked.set(ds.id, ds)
    internalSelectedDataSources.value = [...picked.values()]
    handleSelectionChange()
    persistSelectionIfReport()
}
function chooseFolder(id: string) {
    if (!props.allowFolderScope || !availableFolders.value.some(f => f.id === id)) return
    const ids = props.folderIds.includes(id) ? props.folderIds.filter(f => f !== id) : [...props.folderIds, id]
    emit('update:folderIds', ids)
    persistSelectionIfReport(ids)
}
function toggleAutoMode() {
    // Auto is stored as "no agents pinned" and resolved per run by the backend.
    // Materializing it into today's roster would freeze it into a manual scope
    // that never picks up an agent added later.
    if (isAutoMode.value) return
    emit('update:folderIds', [])
    internalSelectedDataSources.value = []
    handleSelectionChange()
    persistSelectionIfReport([])
}

function toggleDataSource(ds: DataSource) {
    if (isAutoMode.value) {
        // Exit auto: start fresh with only this source selected
        internalSelectedDataSources.value = [ds]
    } else {
        const exists = internalSelectedDataSources.value.find((x) => x.id === ds.id)
        if (exists) {
            internalSelectedDataSources.value = internalSelectedDataSources.value.filter((x) => x.id !== ds.id)
        } else {
            internalSelectedDataSources.value = [...internalSelectedDataSources.value, ds]
        }
    }
    handleSelectionChange()
    // If we are in a report context, persist selection at report level immediately
    persistSelectionIfReport()
}

// Let a parent drive the same toggle the panel rows use, so an external picker
// (the blank-report agent list) shares the auto-mode semantics and the
// report-level persistence instead of reimplementing them.
defineExpose({ toggleDataSource, chooseFolder, toggleAutoMode, settleScope })

onMounted(() => {
    nextTick(async () => {
        const { organization, ensureOrganization } = useOrganization()

        try {
            // Wait for organization to be available before making API calls
            await ensureOrganization()

            if (organization.value?.id) {
                getDataSources()
            } else {
                console.warn('DataSourceSelectorComponentExcel: Organization not available, skipping API calls')
            }
        } catch (error) {
            console.error('DataSourceSelectorComponentExcel: Error during initialization:', error)
        }
        // Setup resize observer for compact mode
        // Look for the nearest parent container that's likely the prompt box
        const findPromptContainer = () => {
            let parent = containerRef.value?.parentElement
            while (parent && parent.clientWidth < 300) {
                parent = parent.parentElement
            }
            return parent || containerRef.value
        }

        const ro = new ResizeObserver(() => {
            const targetEl = findPromptContainer()
            const w = targetEl?.clientWidth || 0
            // Use a more reasonable threshold - compact if container is less than 420px
            isCompact.value = w > 0 && w < 420
        })

        // Observe the container initially, then try to find a better parent
        if (containerRef.value) {
            ro.observe(containerRef.value)
            // Also try to observe a parent container after a short delay
            setTimeout(() => {
                const betterTarget = findPromptContainer()
                if (betterTarget && betterTarget !== containerRef.value) {
                    ro.unobserve(containerRef.value!)
                    ro.observe(betterTarget)
                }
            }, 100)
        }
    })
})
// Give each selected entry its loaded object (name, type, icon) where we have
// one, and KEEP it as-is where we don't. Dropping an unrecognized entry would
// silently rewrite the user's scope just for opening the picker: an agent still
// loading, one parked in connectableDataSources pending sign-in, or the bare
// {id} a parent passes would vanish — and since an empty selection IS Auto, a
// deliberate pin would quietly become "every agent".
function alignSelection(entries: any[]) {
    const byId = new Map(dataSources.value.map((ds: any) => [ds.id, ds]))
    return entries.map((entry: any) => byId.get(entry.id) || entry)
}

// Keep internal selection in sync with parent-provided selectedDataSources
watch(() => props.selectedDataSources, (newVal: any[]) => {
    if (!Array.isArray(newVal)) return
    internalSelectedDataSources.value = alignSelection(newVal) as any
}, { immediate: true, deep: true })

let saveQueue = Promise.resolve()
let lastSave = 0
let scopeSaved = true
async function settleScope() {
    let pending
    do { pending = saveQueue; await pending } while (pending !== saveQueue)
    return scopeSaved
}
function persistSelectionIfReport(folders = props.folderIds) {
    const reportId = props.reportId
    if (!reportId || !props.persistSelection) return
    const ids = internalSelectedDataSources.value.map(ds => ds.id)
    const body = {data_sources: ids, ...(props.allowFolderScope ? {agent_folder_ids: [...folders]} : {})}
    const revision = ++lastSave
    saveQueue = saveQueue.then(async () => {
        const {error} = await useMyFetch(`/reports/${reportId}`, {method:'PUT', body})
        if (revision !== lastSave || props.reportId !== reportId) return
        scopeSaved = !error.value
        if (error.value) {
            toast.add({title: error.value.data?.detail || error.value.message, color:'red'})
            const {data} = await useMyFetch<any>(`/reports/${reportId}`)
            if (revision === lastSave && data.value && props.reportId === reportId) {
                internalSelectedDataSources.value = alignSelection(data.value.data_sources || [])
                emit('update:folderIds', data.value.agent_folder_ids || (data.value.agent_folder_id ? [data.value.agent_folder_id] : []))
                handleSelectionChange()
            }
            return
        }
        window.dispatchEvent(new CustomEvent('report:mutated', {detail:{reportId, kind:'data_sources', ids, folderIds: [...folders]}}))
    }).catch(error => { scopeSaved = false; toast.add({title: String(error), color:'red'}) })
}
const dataTooltip = computed<string>(() => {
    if (internalSelectedDataSources.value.length <= 1) return ''
    const rest = internalSelectedDataSources.value.slice(1).map(s => s.name).join(', ')
    return rest
})

</script>
