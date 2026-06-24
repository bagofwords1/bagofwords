<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm h-full">
        <div class="w-full max-w-7xl px-4 ps-0 py-2 h-full">
            <!-- Full page loading spinner -->
            <div v-if="loading" class="flex flex-col items-center justify-center py-20">
                <Spinner class="h-4 w-4 text-gray-400 dark:text-gray-500" />
                <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">{{ $t('common.loading') }}</p>
            </div>

            <div v-else>
                <!-- Data Agents Section - show if there are data agents or connections -->
                <div v-if="allAgents.length > 0 || connections.length > 0" class="mb-6">
                    <div>
                        <h1 class="text-lg font-semibold">
                            <GoBackChevron v-if="isExcel" />
                            {{ $t('data.agentsTitle') }}
                        </h1>
                        <p class="mt-2 text-gray-500 dark:text-gray-400">{{ $t('data.agentsSubtitle') }}</p>
                    </div>

                    <!-- Header with search -->
                    <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 my-4">
                        <div class="flex-1 max-w-md w-full">
                            <div class="relative">
                                <input
                                    v-model="searchQuery"
                                    type="text"
                                    :placeholder="$t('data.searchAgents')"
                                    class="w-full ps-10 pe-4 text-xs py-2 border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                                />
                                <UIcon
                                    name="i-heroicons-magnifying-glass"
                                    class="absolute start-3 top-2.5 h-4 w-4 text-gray-400 dark:text-gray-500"
                                />
                            </div>
                        </div>

                        <div class="flex items-center justify-end gap-3 w-full md:w-auto">
                            <UTooltip
                                v-if="canViewAllAgents"
                                :text="$t('data.showAllAgentsHint')"
                            >
                                <label class="inline-flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer select-none">
                                    <UToggle v-model="showAllAgents" size="2xs" />
                                    {{ $t('data.showAllAgents') }}
                                </label>
                            </UTooltip>
                            <UButton
                                v-if="canCreateDataSource && connections.length > 0"
                                color="blue"
                                variant="solid"
                                size="xs"
                                icon="i-heroicons-plus"
                                class="w-full md:w-auto"
                                @click="navigateTo('/agents/new')"
                            >
                                {{ $t('data.createAgent') }}
                            </UButton>
                        </div>
                    </div>

                    <!-- Sample databases -->
                    <div v-if="uninstalledDemos.length > 0 && allAgents.length === 0" class="mb-4">
                        <div class="text-xs text-gray-400 dark:text-gray-500 mb-2">{{ $t('data.trySample') }}</div>
                        <div class="flex flex-wrap gap-2">
                            <button
                                v-for="demo in uninstalledDemos"
                                :key="`demo-${demo.id}`"
                                @click="installDemo(demo.id)"
                                :disabled="installingDemo === demo.id"
                                class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 dark:text-gray-400 rounded-full border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800/50 hover:border-gray-300 dark:hover:border-gray-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                <Spinner v-if="installingDemo === demo.id" class="h-3 w-3" />
                                <DataSourceIcon v-else class="h-4" :type="demo.type" />
                                {{ demo.name }}
                                <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 dark:text-purple-400 bg-purple-100 dark:bg-purple-500/10 px-1.5 py-0.5 rounded">{{ $t('data.sampleTag') }}</span>
                            </button>
                        </div>
                    </div>

                    <!-- Data Agents grid -->
                    <div v-if="filteredAgents.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                        <div
                            v-for="ds in filteredAgents"
                            :key="ds.id"
                            class="block p-4 rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 transition-all group"
                            :class="userHasAccess(ds) ? 'hover:border-gray-200 dark:hover:border-gray-700 hover:shadow-md' : 'opacity-75'"
                        >
                            <component :is="userHasAccess(ds) ? NuxtLink : 'div'" :to="userHasAccess(ds) ? `/old_agents/${ds.id}` : undefined" class="block">
                                <!-- Card header -->
                                <div class="flex items-center gap-1.5 mb-1">
                                    <span class="font-medium text-gray-900 dark:text-white text-sm leading-tight">{{ ds.name }}</span>
                                    <UTooltip v-if="ds.admin_only" :text="$t('data.adminOnlyHint')">
                                        <span class="text-[9px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400 bg-amber-100 dark:bg-amber-500/10 px-1.5 py-0.5 rounded">{{ $t('data.adminOnlyTag') }}</span>
                                    </UTooltip>
                                    <!-- Publishing lifecycle badge (only when not the default published) -->
                                    <UTooltip v-if="ds.publish_status && ds.publish_status !== 'published'" :text="publishStatusDescription(ds.publish_status)">
                                        <span :class="['text-[9px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded border', publishStatusBadgeClass(ds.publish_status)]">{{ publishStatusLabel(ds.publish_status) }}</span>
                                    </UTooltip>
                                </div>

                                <!-- Metadata -->
                                <div class="flex items-center gap-1.5 text-[11px] text-gray-400 dark:text-gray-500 mb-2">
                                    <UTooltip v-for="conn in (ds.connections || []).slice(0, 3)" :key="conn.id" :text="conn.name">
                                        <DataSourceIcon class="h-3.5" :type="conn.type" />
                                    </UTooltip>
                                    <span v-if="(ds.connections || []).length > 3" class="text-gray-400 dark:text-gray-500">+{{ (ds.connections || []).length - 3 }}</span>
                                    <span v-if="userHasAccess(ds) && catalogFor(ds).shouldShow">{{ catalogFor(ds).label }}</span>
                                </div>

                                <!-- Description (2 lines max) -->
                                <p v-if="ds.description" class="text-xs text-gray-500 dark:text-gray-400 leading-relaxed line-clamp-2">
                                    {{ ds.description }}
                                </p>
                                <p v-else class="text-xs text-gray-300 dark:text-gray-600 italic">
                                    {{ $t('data.noDescription') }}
                                </p>
                            </component>

                            <!-- Connect button for user auth required but not connected -->
                            <button
                                v-if="needsUserConnection(ds)"
                                @click.stop="openCredentialsModal(ds)"
                                :disabled="connectingId === ds.id"
                                class="mt-3 w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-500/10 border border-blue-200 dark:border-blue-500/30 rounded-lg hover:bg-blue-100 dark:hover:bg-blue-500/20 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                            >
                                <Spinner v-if="connectingId === ds.id" class="w-3.5 h-3.5" />
                                <UIcon v-else name="heroicons-key" class="w-3.5 h-3.5" />
                                {{ $t('data.connect') }}
                            </button>
                            <!-- Admin/owner runs via the connection's system (service
                                 principal) credentials — no personal sign-in needed. -->
                            <div
                                v-else-if="usesServiceAccount(ds)"
                                class="mt-3 w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800/40 border border-gray-200 dark:border-gray-800 rounded-lg"
                            >
                                <UIcon name="heroicons-shield-check" class="w-3.5 h-3.5" />
                                Service account
                            </div>
                        </div>
                    </div>

                    <!-- Empty state for search with no results -->
                    <div v-else-if="searchQuery.trim()" class="py-12 text-center border border-dashed border-gray-200 dark:border-gray-800 rounded-lg">
                        <div class="text-gray-400 dark:text-gray-500 mb-2">
                            <UIcon name="heroicons-magnifying-glass" class="w-8 h-8 mx-auto opacity-50" />
                        </div>
                        <p class="text-sm text-gray-500 dark:text-gray-400 mb-1">{{ $t('data.noAgentsFound') }}</p>
                        <p class="text-xs text-gray-400 dark:text-gray-500">{{ $t('data.noAgentsHint') }}</p>
                    </div>
                </div>

                <!-- Connections Section -->
                <div class="mb-6">
                    <div class="flex items-center justify-between mb-1">
                        <h1 class="text-lg font-semibold">{{ $t('data.connectionsTitle') }}</h1>
                        <UButton
                            v-if="canCreateDataSource"
                            @click="selectedDataSourceType = undefined; showAddConnectionModal = true"
                            color="blue"
                            size="xs"
                        >
                            <UIcon name="heroicons-plus" class="w-3 h-3 me-1" />
                            {{ $t('data.addConnection') }}
                        </UButton>
                    </div>
                    <p class="text-gray-500 dark:text-gray-400 mb-3">{{ $t('data.subtitle') }}</p>

                    <!-- Connection chips (when connections exist) -->
                    <div v-if="connections.length > 0" class="flex flex-wrap items-center gap-2">
                        <button
                            v-for="conn in connections"
                            :key="conn.id"
                            @click="openConnectionDetail(conn)"
                            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-full border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50 hover:border-gray-300 dark:hover:border-gray-700 transition-all"
                        >
                            <DataSourceIcon class="h-3.5" :type="conn.type" />
                            <span>{{ conn.name }}</span>
                            <span :class="['w-1.5 h-1.5 rounded-full', isConnectionHealthy(conn) ? 'bg-green-500' : 'bg-red-500']"></span>
                        </button>
                    </div>

                    <!-- Empty state when no connections - show data source grid -->
                    <div v-else-if="canCreateDataSource">
                        <DataSourceGrid
                            :show-demos="true"
                            :navigate-on-demo="false"
                            @select="handleDataSourceSelect"
                            @demo-installed="handleDemoInstalled"
                        />
                    </div>
                </div>
            </div>

            <!-- Connection Detail Modal -->
            <ConnectionDetailModal 
                v-model="showConnectionModal" 
                :connection="selectedConnection" 
                @updated="refreshData"
            />

            <!-- User Credentials Modal (for per-user auth) -->
            <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="selectedDs" @saved="refreshData" />

            <!-- Add Connection Modal -->
            <AddConnectionModal
                v-model="showAddConnectionModal"
                :initial-selected-type="selectedDataSourceType"
                @created="handleConnectionCreated"
            />
        </div>
    </div>
</template>

<script lang="ts" setup>
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import ConnectionDetailModal from '~/components/ConnectionDetailModal.vue'
import AddConnectionModal from '~/components/AddConnectionModal.vue'
import DataSourceGrid from '~/components/datasources/DataSourceGrid.vue'
import Spinner from '~/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import {
    publishStatusBadgeClass,
    publishStatusLabel,
    publishStatusDescription,
} from '~/composables/useDataSourcePublishStatus'
import { resolveComponent } from 'vue'

const NuxtLink = resolveComponent('NuxtLink')

const { t } = useI18n()
const { organization } = useOrganization()
const { isExcel } = useExcel()

definePageMeta({ auth: true })

const connected_ds = ref<any[]>([])
const connections = ref<any[]>([])
const demo_ds = ref<any[]>([])
const loadingConnected = ref(true)
const loadingConnections = ref(true)
const loadingDemos = ref(true)
const installingDemo = ref<string | null>(null)

const showConnectionModal = ref(false)
const selectedConnection = ref<any>(null)
const showCredsModal = ref(false)
const selectedDs = ref<any>(null)
const showAddConnectionModal = ref(false)
const selectedDataSourceType = ref<string | undefined>(undefined)

// Filter state
const searchQuery = ref('')

const loading = computed(() => loadingConnected.value || loadingDemos.value || loadingConnections.value)

// All agents
const allAgents = computed(() => connected_ds.value || [])

// Uninstalled demo data sources
const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))

// Filtered agents based on search query
const filteredAgents = computed(() => {
    if (!searchQuery.value.trim()) {
        return allAgents.value
    }

    const query = searchQuery.value.toLowerCase().trim()
    return allAgents.value.filter(ds =>
        ds.name?.toLowerCase().includes(query) ||
        ds.description?.toLowerCase().includes(query)
    )
})

function getTableCount(ds: any): number {
    // Sum table counts from all connections
    const connections = ds.connections || []
    if (connections.length > 0) {
        return connections.reduce((sum: number, conn: any) => sum + (conn.table_count || 0), 0)
    }
    return ds.tables?.length || 0
}

// Shape-aware count + sign-in-aware suppression, shared with the agent
// header in layouts/data.vue. See composables/useCatalogCount.ts.
const registryByType = ref<Record<string, any>>({})
onMounted(async () => {
    try {
        const { data } = await useMyFetch('/available_data_sources', { method: 'GET' })
        for (const entry of (data.value as any[]) || []) {
            registryByType.value[entry.type] = entry
        }
    } catch {}
})
const { computeFromAgent } = useCatalogCount()
function catalogFor(ds: any) {
    return computeFromAgent(ds, registryByType.value)
}

// Check if agent requires user auth (any connection)
function requiresUserAuth(ds: any): boolean {
    const connections = ds.connections || []
    return ds.auth_policy === 'user_required' ||
        connections.some((conn: any) => conn.auth_policy === 'user_required')
}

// Check if user needs to connect (user_required but no credentials yet)
function needsUserConnection(ds: any): boolean {
    if (!requiresUserAuth(ds)) return false
    const connections = ds.connections || []
    // Needs a personal connection only when the user has neither their own creds
    // NOR a system fallback (admin/owner). effective_auth === 'system' means the
    // service-principal fallback covers them, so no sign-in prompt.
    for (const conn of connections) {
        if (conn.auth_policy === 'user_required'
            && !conn.user_status?.has_user_credentials
            && conn.user_status?.effective_auth !== 'system') {
            return true
        }
    }
    return ds.user_status?.has_user_credentials !== true && ds.user_status?.effective_auth !== 'system'
}

// True when the user can use this source via the connection's system (service
// principal) credentials — i.e. admin/owner fallback, no personal sign-in.
function usesServiceAccount(ds: any): boolean {
    if (!requiresUserAuth(ds)) return false
    const connections = ds.connections || []
    if (connections.length > 0) {
        return connections.some((conn: any) =>
            conn.auth_policy === 'user_required'
            && !conn.user_status?.has_user_credentials
            && conn.user_status?.effective_auth === 'system'
        )
    }
    return ds.user_status?.has_user_credentials !== true && ds.user_status?.effective_auth === 'system'
}

// Check if user has access to this data source (for clickability / table count)
function userHasAccess(ds: any): boolean {
    if (!requiresUserAuth(ds)) return true
    const connections = ds.connections || []
    if (connections.length > 0) {
        return connections.every((conn: any) =>
            conn.auth_policy !== 'user_required' || conn.user_status?.has_user_credentials || conn.user_status?.effective_auth === 'system'
        )
    }
    return ds.user_status?.has_user_credentials === true || ds.user_status?.effective_auth === 'system'
}

// Open credentials modal for an agent
async function openCredentialsModal(ds: any) {
    // Direct-redirect path: if the agent's pending-sign-in connection has
    // OAuth as its only user auth mode, skip the modal and jump straight to
    // the provider — there's nothing to type or pick.
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
    selectedDs.value = ds
    showCredsModal.value = true
}

// Data source id whose Connect button is mid-sign-in (awaiting the authorize
// redirect). Stays set through a redirect so the spinner persists until the
// browser unloads the page.
const connectingId = ref<string | null>(null)

// Locate the first attached connection that's user_required without
// credentials — that's what the sign-in flow should target.
function findPendingSignInConnection(ds: any): any | null {
    for (const conn of (ds.connections || [])) {
        if (conn.auth_policy === 'user_required' && !conn.user_status?.has_user_credentials) {
            return conn
        }
    }
    return null
}

const signIn = useConnectionSignIn()

// Check if connection is healthy - uses agent data to derive status
function isConnectionHealthy(conn: any): boolean {
    // Check connection's own status fields
    if (conn.last_status === 'success' || conn.status === 'success') return true
    if (conn.last_status === 'error' || conn.status === 'error') return false
    
    // Check user_status if available
    const userStatus = conn.user_status?.connection
    if (userStatus === 'success') return true
    if (userStatus === 'error' || userStatus === 'offline') return false
    
    // Fallback: check if any agent using this connection is connected
    const agentsUsingConn = connected_ds.value.filter(ds =>
        ds.connection?.id === conn.id || ds.connection_id === conn.id
    )
    if (agentsUsingConn.length > 0) {
        // If we have agents, check their connection status
        const anyConnected = agentsUsingConn.some(ds => {
            const status = ds.user_status?.connection || ds.connection?.user_status?.connection
            return status === 'success'
        })
        if (anyConnected) return true
    }
    
    // Default: assume healthy if we have the connection in the list
    return true
}

function openConnectionDetail(conn: any) {
    selectedConnection.value = conn
    showConnectionModal.value = true
}

function handleDataSourceSelect(ds: any) {
    selectedDataSourceType.value = ds.type
    showAddConnectionModal.value = true
}

function handleConnectionCreated() {
    selectedDataSourceType.value = undefined
    refreshData()
}

const toast = useToast()

function handleDemoInstalled(result: any) {
    toast.add({
        title: t('data.sampleAdded'),
        description: t('data.sampleAddedDesc'),
        icon: 'i-heroicons-check-circle',
        color: 'green'
    })
    refreshData()
}

async function getConnectedDataSources() {
    loadingConnected.value = true
    try {
        // Admins (full_admin_access / manage_connections) can toggle a
        // governance "show all" view that reveals private data sources they
        // aren't a member of. The backend ignores show_all for everyone else.
        const url = showAllAgents.value ? '/data_sources?show_all=true' : '/data_sources'
        const response = await useMyFetch(url, { method: 'GET' })
        if (response.data.value) {
            connected_ds.value = response.data.value as any[]
        }
    } finally {
        loadingConnected.value = false
    }
}

async function getConnections() {
    loadingConnections.value = true
    try {
        const response = await useMyFetch('/connections', { method: 'GET' })
        if (response.data.value) {
            connections.value = response.data.value as any[]
        }
    } finally {
        loadingConnections.value = false
    }
}

async function getDemoDataSources() {
    loadingDemos.value = true
    try {
        const response = await useMyFetch('/data_sources/demos', { method: 'GET' })
        if (response.data.value) {
            demo_ds.value = response.data.value as any[]
        }
    } finally {
        loadingDemos.value = false
    }
}

async function installDemo(demoId: string) {
    installingDemo.value = demoId
    try {
        const response = await useMyFetch(`/data_sources/demos/${demoId}`, { method: 'POST' })
        const result = response.data.value as any
        if (result?.success) {
            const demoName = demo_ds.value.find((d: any) => d.id === demoId)?.name || t('data.sampleDataFallback')
            toast.add({
                title: t('data.sampleAdded'),
                description: t('data.sampleAddedNamed', { name: demoName }),
                icon: 'i-heroicons-check-circle',
                color: 'green'
            })
            await refreshData()
        }
    } finally {
        installingDemo.value = null
    }
}

async function refreshData() {
    await Promise.all([
        getConnectedDataSources(),
        getConnections(),
        getDemoDataSources(),
    ])
    maybeStartPolling()
}

// Poll while any connection (standalone or under a data source) is currently
// indexing. The `/data_sources` and `/connections` endpoints both inline the
// latest `indexing` row, so a single re-fetch updates badges everywhere.
const POLL_INTERVAL_MS = 5000000
let pollTimer: ReturnType<typeof setInterval> | null = null

function anyIndexingActive(): boolean {
    const isActive = (idx: any) =>
        idx && (idx.status === 'pending' || idx.status === 'running')
    if ((connections.value || []).some((c: any) => isActive(c?.indexing))) return true
    for (const ds of (connected_ds.value || [])) {
        if ((ds.connections || []).some((c: any) => isActive(c?.indexing))) return true
    }
    return false
}

function maybeStartPolling() {
    if (anyIndexingActive() && !pollTimer) {
        pollTimer = setInterval(async () => {
            await Promise.all([getConnectedDataSources(), getConnections()])
            if (!anyIndexingActive()) stopPolling()
        }, POLL_INTERVAL_MS)
    } else if (!anyIndexingActive()) {
        stopPolling()
    }
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
    }
}

onBeforeUnmount(() => stopPolling())

const canCreateDataSource = computed(() => useCan('create_data_source'))

// Org-wide data-source governance: full admins and connection admins. Gates
// the "show all" toggle. useCan already treats full_admin_access as a bypass,
// so this is true for both. Per-DS `manage` does NOT grant it.
const canViewAllAgents = computed(() => useCan('manage_connections'))

// Admin "show all" toggle state — when on, the list includes private data
// sources the admin isn't a member of (flagged with an Admin badge).
const showAllAgents = ref(false)
watch(showAllAgents, () => {
    getConnectedDataSources()
})

onMounted(async () => {
    nextTick(async () => {
        await refreshData()
    })

    // Handle OAuth callback redirect
    const route = useRoute()
    if (route.query.oauth === 'success') {
        const toast = useToast()
        toast.add({ title: t('data.connectedSuccess'), color: 'green', icon: 'i-heroicons-check-circle' })
        // Clean up query params
        navigateTo('/old_agents', { replace: true })
    } else if (route.query.oauth === 'error') {
        const toast = useToast()
        toast.add({ title: t('data.connectionFailed'), description: route.query.message as string || '', color: 'red', icon: 'i-heroicons-x-circle' })
        navigateTo('/old_agents', { replace: true })
    }
})
</script>

<style scoped>
.line-clamp-2 {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}
</style>
