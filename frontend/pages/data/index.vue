<template>
    <div class="flex justify-center pl-2 md:pl-4 text-sm h-full">
        <div class="w-full max-w-7xl px-4 pl-0 py-2 h-full">
            <!-- Full page loading spinner -->
            <div v-if="loading" class="flex flex-col items-center justify-center py-20">
                <Spinner class="h-4 w-4 text-gray-400" />
                <p class="text-sm text-gray-500 mt-2">Loading...</p>
            </div>

            <div v-else>
                <!-- Header -->
                <div class="mb-4">
                    <h1 class="text-lg font-semibold">
                        <GoBackChevron v-if="isExcel" />
                        Data
                    </h1>
                    <p class="mt-1 text-gray-500">
                        Manage your database connections and organize tables into domains.
                    </p>
                </div>

                <!-- Connection Filters -->
                <div class="mb-5">
                    <div class="flex flex-wrap items-center gap-2">
                        <!-- All filter -->
                        <button
                            @click="selectedConnectionId = null"
                            :class="[
                                'inline-flex items-center gap-2 px-3 py-1.5 text-xs rounded-full border transition-all',
                                selectedConnectionId === null
                                    ? 'bg-blue-50 text-blue-700 border-blue-200'
                                    : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50 hover:border-gray-300'
                            ]"
                        >
                            <span>All</span>
                        </button>

                        <!-- Connection filters with inline dropdown -->
                        <div 
                            v-for="conn in connections" 
                            :key="conn.id"
                            :class="[
                                'inline-flex items-center gap-1.5 pl-3 pr-1 py-1 text-xs rounded-full border transition-all',
                                selectedConnectionId === conn.id
                                    ? 'bg-blue-50 text-blue-700 border-blue-200'
                                    : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50 hover:border-gray-300'
                            ]"
                        >
                            <button 
                                @click="selectedConnectionId = conn.id"
                                class="inline-flex items-center gap-1.5"
                            >
                                <DataSourceIcon class="h-3.5" :type="conn.type" />
                                <span>{{ conn.name }}</span>
                                <span :class="['w-1.5 h-1.5 rounded-full', isConnectionHealthy(conn) ? 'bg-green-500' : 'bg-red-500']"></span>
                            </button>
                            
                            <!-- Ellipsis dropdown (for admins) -->
                            <UDropdown
                                v-if="canUpdateDataSource"
                                :items="getConnectionMenuItems(conn)"
                                :popper="{ placement: 'bottom-end' }"
                            >
                                <button 
                                    :class="[
                                        'p-1 rounded-full transition-colors',
                                        selectedConnectionId === conn.id
                                            ? 'hover:bg-blue-100 text-blue-600'
                                            : 'hover:bg-gray-100 text-gray-400'
                                    ]"
                                >
                                    <UIcon name="heroicons-ellipsis-vertical" class="w-3.5 h-3.5" />
                                </button>
                            </UDropdown>
                        </div>

                        <!-- New data button -->
                        <UButton 
                            v-if="canCreateDataSource"
                            @click="navigateTo('/data/new')"
                            color="blue"
                            size="xs"
                            class="ml-auto"
                        >
                            <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                            New data
                        </UButton>
                    </div>
                </div>

                <!-- Domains grid -->
                <div v-if="filteredDomains.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 mb-6">
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
                                <DataSourceIcon class="h-3.5" :type="getConnectionType(ds)" />
                                <span>{{ getConnectionName(ds) }}</span>
                                <span class="text-gray-300">·</span>
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

                <!-- Empty state for filtered view (connection selected but no domains) -->
                <div v-else-if="selectedConnectionId !== null && canCreateDataSource" class="py-12 text-center border border-dashed border-gray-200 rounded-lg mb-6">
                    <div class="text-gray-400 mb-2">
                        <UIcon name="heroicons-circle-stack" class="w-8 h-8 mx-auto opacity-50" />
                    </div>
                    <p class="text-sm text-gray-500 mb-1">No domains in this connection</p>
                    <p class="text-xs text-gray-400 mb-4">Create a domain to start organizing your data</p>
                    <UButton 
                        @click="navigateTo('/data/new')"
                        color="blue"
                        variant="soft"
                        size="xs"
                    >
                        <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                        Create Domain
                    </UButton>
                </div>

                <!-- Empty state: has connections but no domains at all -->
                <div v-else-if="connections.length > 0 && allDomains.length === 0 && canCreateDataSource" class="py-12 text-center border border-dashed border-gray-200 rounded-lg mb-6">
                    <div class="text-gray-400 mb-2">
                        <UIcon name="heroicons-circle-stack" class="w-8 h-8 mx-auto opacity-50" />
                    </div>
                    <p class="text-sm text-gray-500 mb-1">No domains yet</p>
                    <p class="text-xs text-gray-400 mb-4">Create your first domain to start organizing tables and instructions</p>
                    <UButton 
                        @click="navigateTo('/data/new')"
                        color="blue"
                        variant="soft"
                        size="xs"
                    >
                        <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                        Create Domain
                    </UButton>
                </div>

                <!-- Empty State: Show DataSourceGrid when no connections (admin only) -->
                <div v-if="allDomains.length === 0 && connections.length === 0 && canCreateDataSource">
                    <DataSourceGrid 
                        @select="handleDataSourceSelect"
                        @demo-installed="handleDemoInstalled"
                    />
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
        </div>
    </div>
</template>

<script lang="ts" setup>
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import ConnectionDetailModal from '~/components/ConnectionDetailModal.vue'
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

// Filter state
const selectedConnectionId = ref<string | null>(null)

// Connection menu items for dropdown (per connection)
function getConnectionMenuItems(conn: any) {
    return [[
        {
            label: 'Settings',
            icon: 'i-heroicons-cog-6-tooth',
            click: () => openConnectionDetail(conn)
        }
    ]]
}

// Permission checks
const canViewDataSource = computed(() => useCan('view_data_source'))
const canUpdateDataSource = computed(() => useCan('update_data_source'))

const loading = computed(() => loadingConnected.value || loadingDemos.value || loadingConnections.value)

// All domains
const allDomains = computed(() => connected_ds.value || [])

// Get domains for a specific connection
function getDomainsForConnection(connectionId: string): any[] {
    return allDomains.value.filter(ds => 
        ds.connection?.id === connectionId || ds.connection_id === connectionId
    )
}

// Filtered domains based on selected connection
const filteredDomains = computed(() => {
    if (selectedConnectionId.value === null) {
        return allDomains.value
    }
    return getDomainsForConnection(selectedConnectionId.value)
})

const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))

// Helper functions for domain display
function getConnectionType(ds: any): string {
    return ds.connection?.type || ds.type || 'unknown'
}

function getConnectionName(ds: any): string {
    return ds.connection?.name || ds.name || 'Connection'
}

function getTableCount(ds: any): number {
    return ds.connection?.table_count || ds.tables?.length || 0
}

// Check if domain requires user auth
function requiresUserAuth(ds: any): boolean {
    return ds.auth_policy === 'user_required' || ds.connection?.auth_policy === 'user_required'
}

// Check if user needs to connect (user_required but not connected yet)
function needsUserConnection(ds: any): boolean {
    if (!requiresUserAuth(ds)) return false
    const userStatus = ds.user_status?.connection || ds.connection?.user_status?.connection
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
            await refreshData()
            if (result.data_source_id) {
                navigateTo(`/data/${result.data_source_id}`)
            }
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

function handleDataSourceSelect(ds: any) {
    // Navigate to the new connection form with the selected type
    navigateTo(`/data/new?type=${ds.type}`)
}

async function handleDemoInstalled(result: any) {
    // Refresh data after demo is installed
    await refreshData()
}

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
