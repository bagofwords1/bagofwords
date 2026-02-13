<template>
    <div class="flex justify-center pl-2 md:pl-4 text-sm h-full">
        <div class="w-full max-w-7xl px-4 pl-0 py-2 h-full">
            <!-- Full page loading spinner -->
            <div v-if="loading" class="flex flex-col items-center justify-center py-20">
                <Spinner class="h-4 w-4 text-gray-400" />
                <p class="text-sm text-gray-500 mt-2">Loading...</p>
            </div>

            <div v-else>
                <!-- Data Agents Section - only show if there are data agents -->
                <div v-if="allDomains.length > 0" class="mb-6">
                    <div>
                        <h1 class="text-lg font-semibold">
                            <GoBackChevron v-if="isExcel" />
                            Data Agents
                        </h1>
                        <p class="mt-2 text-gray-500">Organize tables and instructions into agents.</p>
                    </div>

                    <!-- Header with search -->
                    <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 my-4">
                        <div class="flex-1 max-w-md w-full">
                            <div class="relative">
                                <input
                                    v-model="searchQuery"
                                    type="text"
                                    placeholder="Search data agents..."
                                    class="w-full pl-10 pr-4 text-xs py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                                />
                                <UIcon
                                    name="i-heroicons-magnifying-glass"
                                    class="absolute left-3 top-2.5 h-4 w-4 text-gray-400"
                                />
                            </div>
                        </div>

                        <div class="flex items-center justify-end gap-2 w-full md:w-auto">
                            <UButton
                                v-if="canCreateDataSource && connections.length > 0"
                                color="blue"
                                variant="solid"
                                size="xs"
                                icon="i-heroicons-plus"
                                class="w-full md:w-auto"
                                @click="navigateTo('/data/new')"
                            >
                                Create Data Agent
                            </UButton>
                        </div>
                    </div>

                    <!-- Sample databases -->
                    <div v-if="uninstalledDemos.length > 0" class="mb-4">
                        <div class="text-xs text-gray-400 mb-2">Try a sample database:</div>
                        <div class="flex flex-wrap gap-2">
                            <button
                                v-for="demo in uninstalledDemos"
                                :key="`demo-${demo.id}`"
                                @click="installDemo(demo.id)"
                                :disabled="installingDemo === demo.id"
                                class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                <Spinner v-if="installingDemo === demo.id" class="h-3 w-3" />
                                <DataSourceIcon v-else class="h-4" :type="demo.type" />
                                {{ demo.name }}
                                <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
                            </button>
                        </div>
                    </div>

                    <!-- Data Agents grid -->
                    <div v-if="filteredDomains.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                        <div
                            v-for="ds in filteredDomains"
                            :key="ds.id"
                            class="block p-4 rounded-lg border border-gray-100 bg-white hover:border-gray-200 hover:shadow-md transition-all group"
                        >
                            <NuxtLink :to="`/data/${ds.id}`" class="block">
                                <!-- Card header -->
                                <div class="font-medium text-gray-900 text-sm leading-tight mb-1">{{ ds.name }}</div>

                                <!-- Metadata -->
                                <div class="flex items-center gap-1.5 text-[11px] text-gray-400 mb-2">
                                    <UTooltip v-for="conn in (ds.connections || [])" :key="conn.id" :text="conn.name">
                                        <DataSourceIcon class="h-3.5" :type="conn.type" />
                                    </UTooltip>
                                    <span>{{ getTableCount(ds) }} tables</span>
                                </div>

                                <!-- Description (2 lines max) -->
                                <p v-if="ds.description" class="text-xs text-gray-500 leading-relaxed line-clamp-2">
                                    {{ ds.description }}
                                </p>
                                <p v-else class="text-xs text-gray-300 italic">
                                    No description
                                </p>
                            </NuxtLink>

                            <!-- Connect button for user auth required but not connected -->
                            <button
                                v-if="needsUserConnection(ds)"
                                @click.stop="openCredentialsModal(ds)"
                                class="mt-3 w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
                            >
                                <UIcon name="heroicons-key" class="w-3.5 h-3.5" />
                                Connect
                            </button>
                        </div>
                    </div>

                    <!-- Empty state for search with no results -->
                    <div v-else-if="searchQuery.trim()" class="py-12 text-center border border-dashed border-gray-200 rounded-lg">
                        <div class="text-gray-400 mb-2">
                            <UIcon name="heroicons-magnifying-glass" class="w-8 h-8 mx-auto opacity-50" />
                        </div>
                        <p class="text-sm text-gray-500 mb-1">No data agents found</p>
                        <p class="text-xs text-gray-400">Try a different search term</p>
                    </div>
                </div>

                <!-- Connections Section -->
                <div class="mb-6">
                    <div class="flex items-center justify-between mb-1">
                        <h1 class="text-lg font-semibold">Connections</h1>
                        <UButton
                            v-if="canCreateDataSource"
                            @click="selectedDataSourceType = undefined; showAddConnectionModal = true"
                            color="blue"
                            size="xs"
                        >
                            <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                            Add Connection
                        </UButton>
                    </div>
                    <p class="text-gray-500 mb-3">Manage your database connections.</p>

                    <!-- Connection chips (when connections exist) -->
                    <div v-if="connections.length > 0" class="flex flex-wrap items-center gap-2">
                        <button
                            v-for="conn in connections"
                            :key="conn.id"
                            @click="openConnectionDetail(conn)"
                            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-full border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 hover:border-gray-300 transition-all"
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

// All domains
const allDomains = computed(() => connected_ds.value || [])

// Uninstalled demo data sources
const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))

// Filtered domains based on search query
const filteredDomains = computed(() => {
    if (!searchQuery.value.trim()) {
        return allDomains.value
    }

    const query = searchQuery.value.toLowerCase().trim()
    return allDomains.value.filter(ds =>
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

// Check if domain requires user auth (any connection)
function requiresUserAuth(ds: any): boolean {
    const connections = ds.connections || []
    return ds.auth_policy === 'user_required' ||
        connections.some((conn: any) => conn.auth_policy === 'user_required')
}

// Check if user needs to connect (user_required but not connected yet)
function needsUserConnection(ds: any): boolean {
    if (!requiresUserAuth(ds)) return false
    const connections = ds.connections || []
    // Check if any user_required connection is not connected
    for (const conn of connections) {
        if (conn.auth_policy === 'user_required' && conn.user_status?.connection !== 'success') {
            return true
        }
    }
    const userStatus = ds.user_status?.connection
    return userStatus !== 'success'
}

// Open credentials modal for a domain
function openCredentialsModal(ds: any) {
    selectedDs.value = ds
    showCredsModal.value = true
}

// Check if connection is healthy - uses domain data to derive status
function isConnectionHealthy(conn: any): boolean {
    // Check connection's own status fields
    if (conn.last_status === 'success' || conn.status === 'success') return true
    if (conn.last_status === 'error' || conn.status === 'error') return false
    
    // Check user_status if available
    const userStatus = conn.user_status?.connection
    if (userStatus === 'success') return true
    if (userStatus === 'error' || userStatus === 'offline') return false
    
    // Fallback: check if any domain using this connection is connected
    const domainsUsingConn = connected_ds.value.filter(ds => 
        ds.connection?.id === conn.id || ds.connection_id === conn.id
    )
    if (domainsUsingConn.length > 0) {
        // If we have domains, check their connection status
        const anyConnected = domainsUsingConn.some(ds => {
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
        title: 'Sample data added',
        description: 'Sample database has been added successfully.',
        icon: 'i-heroicons-check-circle',
        color: 'green'
    })
    refreshData()
}

async function getConnectedDataSources() {
    loadingConnected.value = true
    try {
        const response = await useMyFetch('/data_sources', { method: 'GET' })
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
            const demoName = demo_ds.value.find((d: any) => d.id === demoId)?.name || 'Sample data'
            toast.add({
                title: 'Sample data added',
                description: `${demoName} has been added successfully.`,
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
}

const canCreateDataSource = computed(() => useCan('create_data_source'))

onMounted(async () => {
    nextTick(async () => {
        await refreshData()
    })
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
