<template>
    <div class="flex flex-col min-h-screen">
        <!-- Full page loading spinner -->
        <div v-if="loading" class="flex flex-col items-center justify-center flex-grow py-20">
            <Spinner class="h-4 w-4 text-gray-400" />
            <p class="text-sm text-gray-500 mt-2">Loading...</p>
        </div>

        <div class="flex pl-2 md:pl-4 text-sm mx-auto w-full max-w-4xl md:pt-10" v-else>
            <div class="w-full px-4 py-4">
                <!-- Header -->
                <div class="mb-8">
                    <h1 class="text-lg font-semibold text-center">
                        <GoBackChevron v-if="isExcel" />
                        Data
                    </h1>
                    <p class="mt-2 text-gray-400 text-center text-xs">Manage your connections and domains</p>
                </div>

                <!-- My Domains Section (show if has domains OR user can create) -->
                <div v-if="allDomains.length > 0 || (connections.length > 0 && canCreateDataSource)" class="mb-8">
                    <div class="flex items-center justify-between mb-1">
                        <h2 class="text-sm font-medium text-gray-700">Domains</h2>
                        <UButton 
                            v-if="canCreateDataSource"
                            @click="navigateTo('/data/new')"
                            color="blue"
                            size="xs"
                        >
                            <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                            New Domain
                        </UButton>
                    </div>
                    <p class="text-xs text-gray-400 mb-4">
                        Domains group related tables and instructions together to make data easier to manage and use.
                    </p>

                    <!-- Domains grid - 3 columns -->
                    <div v-if="allDomains.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                        <div 
                            v-for="ds in allDomains" 
                            :key="ds.id"
                            class="block p-4 rounded-xl border border-gray-100 bg-white hover:border-gray-200 hover:shadow-sm transition-all group"
                        >
                            <NuxtLink :to="`/data/${ds.id}`" class="block">
                                <!-- Card header -->
                                <div class="font-medium text-gray-900 text-sm leading-tight mb-1">{{ ds.name }}</div>
                                
                                <!-- Metadata with icon -->
                                <div class="flex items-center gap-1.5 text-[11px] text-gray-400 mb-2">
                                    <DataSourceIcon class="h-3.5 w-3.5" :type="getConnectionType(ds)" />
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

                    <!-- Empty domains state (has connections but no domains) -->
                    <div v-else class="py-8 text-center border border-dashed border-gray-200 rounded-xl">
                        <p class="text-xs text-gray-400 mb-3">No domains yet</p>
                        <UButton 
                            v-if="canCreateDataSource"
                            @click="navigateTo('/data/new')"
                            color="blue"
                            variant="soft"
                            size="xs"
                        >
                            <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                            Create Domain
                        </UButton>
                    </div>
                </div>

                <!-- Connections Section (only show if has connections AND user can update) -->
                <div v-if="connections.length > 0 && canUpdateDataSource" class="mb-8">
                    <div class="flex items-center justify-between mb-1">
                        <h2 class="text-sm font-medium text-gray-700">Connections</h2>
                        <UButton 
                            v-if="canCreateDataSource"
                            @click="navigateTo('/data/new?mode=new_connection')"
                            color="blue"
                            size="xs"
                        >
                            <UIcon name="heroicons-plus" class="w-3 h-3 mr-1" />
                            New Connection
                        </UButton>
                    </div>
                    <p class="text-xs text-gray-400 mb-3">
                        Manage your database and warehouse connections here
                    </p>

                    <!-- Connections chips -->
                    <div class="flex flex-wrap gap-2">
                        <button 
                            v-for="conn in connections" 
                            :key="conn.id"
                            @click="openConnectionDetail(conn)"
                            class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors"
                        >
                            <DataSourceIcon class="h-4 w-4" :type="conn.type" />
                            <span>{{ conn.name }}</span>
                            <span :class="['w-1.5 h-1.5 rounded-full', isConnectionHealthy(conn) ? 'bg-green-500' : 'bg-red-500']"></span>
                        </button>
                    </div>

                    <!-- Sample Databases (only when has connections) -->
                    <div v-if="uninstalledDemos.length > 0 && canCreateDataSource" class="mt-6">
                        <h2 class="text-sm font-medium text-gray-700 mb-1">Try a sample</h2>
                        <p class="text-xs text-gray-400 mb-3">Explore with pre-loaded demo databases</p>
                        <div class="flex flex-wrap gap-2">
                            <button 
                                v-for="demo in uninstalledDemos" 
                                :key="`chip-${demo.id}`"
                                @click="installDemo(demo.id)"
                                :disabled="installingDemo === demo.id"
                                class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                <Spinner v-if="installingDemo === demo.id" class="h-3 w-3" />
                                <DataSourceIcon v-else class="h-4 w-4" :type="demo.type" />
                                {{ demo.name }}
                                <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Empty State: Show DataSourceGrid when no connections (admin) -->
                <div v-else-if="canCreateDataSource" class="mb-8">
                    <DataSourceGrid 
                        @select="handleDataSourceSelect"
                        @demo-installed="handleDemoInstalled"
                    />
                </div>

                <!-- Empty State for view-only users -->
                <div v-else-if="canViewDataSource" class="mb-8 py-12 text-center">
                    <UIcon name="heroicons-circle-stack" class="w-12 h-12 mx-auto text-gray-300 mb-4" />
                    <h3 class="text-sm font-medium text-gray-700 mb-2">No data sources available</h3>
                    <p class="text-xs text-gray-400">Contact your administrator to get access to data sources.</p>
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

// Permission checks
const canViewDataSource = computed(() => useCan('view_data_source'))
const canUpdateDataSource = computed(() => useCan('update_data_source'))

const loading = computed(() => loadingConnected.value || loadingDemos.value || loadingConnections.value)

// All domains (both connected and those needing setup)
const allDomains = computed(() => connected_ds.value || [])

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
