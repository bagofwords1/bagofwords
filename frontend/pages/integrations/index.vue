<template>
    <div class="flex flex-col min-h-screen">
        <!-- Full page loading spinner -->
        <div v-if="loading" class="flex flex-col items-center justify-center flex-grow py-20">
            <Spinner class="h-4 w-4 text-gray-400" />
            <p class="text-sm text-gray-500 mt-2">Loading...</p>
        </div>

        <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10" v-else>
            <div class="w-full px-4 pl-0 py-4">
                <div>
                    <h1 class="text-lg font-semibold text-center">
                        <GoBackChevron v-if="isExcel" />
                        Integrations
                    </h1>
                    <p class="mt-4 text-gray-500 text-center">Connect and manage your data sources</p>
                </div>

                <!-- My Domains Section -->
                <div class="mt-6 mb-4" v-if="allDomains.length > 0">
                    <div class="font-medium mb-2">My Domains</div>
                    <NuxtLink 
                        :to="`/integrations/${ds.id}`" 
                        class="border-b border-gray-100 w-full py-3 rounded-lg flex hover:bg-gray-50 items-center"
                        v-for="ds in allDomains" 
                        :key="ds.id"
                    >
                        <div class="flex-1">
                            <div class="font-medium text-gray-900">{{ ds.name }}</div>
                            <div class="text-xs text-gray-400 flex items-center gap-1 mt-0.5">
                                <DataSourceIcon class="h-3.5 w-3.5" :type="getConnectionType(ds)" />
                                <span>{{ getConnectionName(ds) }}</span>
                                <span class="text-gray-300">|</span>
                                <span>{{ getTableCount(ds) }} tables</span>
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <UTooltip :text="getStatusTooltip(ds)">
                                <span v-if="isConnected(ds)" class="text-green-800 bg-green-100 px-1.5 py-0.5 rounded-md text-[10px] flex items-center">
                                    <UIcon name="heroicons-check" class="mr-0.5" /> Connected
                                </span>
                                <span v-else class="text-amber-800 bg-amber-100 px-1.5 py-0.5 rounded-md text-[10px] flex items-center">
                                    <UIcon name="heroicons-exclamation-circle" class="mr-0.5" /> Setup Required
                                </span>
                            </UTooltip>
                            <UIcon name="heroicons-chevron-right" class="text-gray-400" />
                        </div>
                    </NuxtLink>
                </div>

                <!-- Empty state -->
                <div v-else class="mt-6 mb-4 text-center py-8 text-gray-500">
                    <p>No domains yet. Add a connection to get started.</p>
                </div>

                <!-- Action Buttons -->
                <div class="mt-6 flex gap-3" v-if="canCreateDataSource()">
                    <UButton 
                        color="primary" 
                        variant="solid"
                        @click="navigateTo('/integrations/new')"
                        class="flex-1"
                    >
                        <UIcon name="heroicons-plus" class="mr-1" />
                        Add Connection
                    </UButton>
                    <UButton 
                        color="gray" 
                        variant="outline"
                        @click="navigateTo('/domains/new')"
                        v-if="allDomains.length > 0"
                    >
                        <UIcon name="heroicons-plus" class="mr-1" />
                        Add Domain
                    </UButton>
                    <UButton 
                        color="gray" 
                        variant="outline"
                        @click="showConnectionsModal = true"
                    >
                        <UIcon name="heroicons-cog-6-tooth" class="mr-1" />
                        Manage Connections
                    </UButton>
                </div>

                <!-- Sample Databases -->
                <div v-if="uninstalledDemos.length > 0 && canCreateDataSource()" class="mt-6">
                    <div class="text-xs text-gray-400 mb-2">Or try a sample database:</div>
                    <div class="flex flex-wrap gap-2">
                        <button 
                            v-for="demo in uninstalledDemos" 
                            :key="`chip-${demo.id}`"
                            @click="installDemo(demo.id)"
                            :disabled="installingDemo === demo.id"
                            class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <Spinner v-if="installingDemo === demo.id" class="h-3" />
                            <DataSourceIcon v-else class="h-4" :type="demo.type" />
                            {{ demo.name }}
                            <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
                        </button>
                    </div>
                </div>

            </div>
        </div>

        <!-- Modals -->
        <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="selectedDs" @saved="getConnectedDataSources" />
        <ManageConnectionsModal v-model="showConnectionsModal" @updated="getConnectedDataSources" />
    </div>
</template>

<script lang="ts" setup>
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import ManageConnectionsModal from '~/components/ManageConnectionsModal.vue'
import Spinner from '~/components/Spinner.vue'

const { organization } = useOrganization()
const { isExcel } = useExcel()

definePageMeta({ auth: true })

const connected_ds = ref<any[]>([])
const demo_ds = ref<any[]>([])
const loadingConnected = ref(true)
const loadingDemos = ref(true)
const installingDemo = ref<string | null>(null)
const showConnectionsModal = ref(false)

const loading = computed(() => loadingConnected.value || loadingDemos.value)

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
    // Try to get from connection, or count from tables array
    return ds.connection?.table_count || ds.tables?.length || 0
}

function isConnected(ds: any): boolean {
    const userStatus = ds.connection?.user_status || ds.user_status
    const authPolicy = ds.connection?.auth_policy || ds.auth_policy
    return (
        userStatus?.has_user_credentials || 
        authPolicy === 'system_only' || 
        userStatus?.effective_auth === 'system'
    )
}

function getStatusTooltip(ds: any): string {
    const userStatus = ds.connection?.user_status || ds.user_status
    const authPolicy = ds.connection?.auth_policy || ds.auth_policy
    if (userStatus?.effective_auth === 'user') return 'Connected via user credentials'
    if (userStatus?.effective_auth === 'system' || authPolicy === 'system_only') return 'Connected via system credentials'
    return 'User credentials required'
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
            await Promise.all([
                getConnectedDataSources(),
                getDemoDataSources(),
            ])
            if (result.data_source_id) {
                navigateTo(`/integrations/${result.data_source_id}`)
            }
        }
    } finally {
        installingDemo.value = null
    }
}

const showCredsModal = ref(false)
const selectedDs = ref<any>(null)

function canCreateDataSource(): boolean {
    try {
        return typeof useCan === 'function' ? useCan('create_data_source') : true
    } catch (e) {
        return true
    }
}

onMounted(async () => {
    nextTick(async () => {
        getConnectedDataSources()
        getDemoDataSources()
    })
})
</script>
