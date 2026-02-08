<template>
    <NuxtLayout name="default">
        <div class="flex justify-center pl-2 md:pl-4 text-sm">
            <div class="w-full max-w-7xl px-4 pl-0 py-2">
                <div>
                    <div class="flex items-start justify-between">
                        <div>
                            <h1 class="text-lg font-semibold">
                                {{ fetchError ? 'Data Source' : (integration?.name || 'Domain') }}
                            </h1>
                            <div v-if="!isLoading && integration && !fetchError" class="flex items-center gap-2 mt-1 text-xs text-gray-500">
                                <template v-if="integration.connections && integration.connections.length > 0">
                                    <template v-for="conn in integration.connections" :key="conn.id">
                                        <span :class="['w-2 h-2 rounded-full', getConnectionStatus(conn) === 'success' ? 'bg-green-500' : 'bg-red-500']"></span>
                                        <UTooltip :text="conn.name">
                                            <DataSourceIcon :type="conn.type" class="h-4" />
                                        </UTooltip>
                                    </template>
                                </template>
                                <template v-else>
                                    <span :class="['w-2 h-2 rounded-full', isConnected ? 'bg-green-500' : 'bg-red-500']"></span>
                                    <DataSourceIcon :type="connectionType" class="h-4" />
                                    <span>{{ connectionName }}</span>
                                </template>
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <span v-if="isLoading" class="px-2 py-0.5 rounded text-xs border bg-gray-50 text-gray-700 border-gray-200 flex items-center gap-1">
                                <Spinner />
                                Loading
                            </span>
                        </div>
                    </div>

                    <!-- Access denied state -->
                    <div v-if="!isLoading && fetchError === 403" class="mt-8">
                        <div class="bg-white border border-gray-200 rounded-lg p-8 md:p-10">
                            <div class="flex flex-col items-center justify-center py-12 text-center">
                                <Icon name="i-heroicons-lock-closed" class="w-12 h-12 text-gray-300 mb-4" />
                                <h2 class="text-lg font-medium text-gray-900">Access Restricted</h2>
                                <p class="mt-2 text-sm text-gray-500 max-w-sm">
                                    This data source is private. Contact the owner or an admin to request access.
                                </p>
                                <NuxtLink to="/data" class="mt-4 text-sm text-blue-600 hover:underline">
                                    ← Back to Data Sources
                                </NuxtLink>
                            </div>
                        </div>
                    </div>

                    <!-- Not found state -->
                    <div v-else-if="!isLoading && fetchError === 404" class="mt-8">
                        <div class="bg-white border border-gray-200 rounded-lg p-8 md:p-10">
                            <div class="flex flex-col items-center justify-center py-12 text-center">
                                <Icon name="i-heroicons-exclamation-circle" class="w-12 h-12 text-gray-300 mb-4" />
                                <h2 class="text-lg font-medium text-gray-900">Data Source Not Found</h2>
                                <p class="mt-2 text-sm text-gray-500 max-w-sm">
                                    The data source you're looking for doesn't exist or has been removed.
                                </p>
                                <NuxtLink to="/data" class="mt-4 text-sm text-blue-600 hover:underline">
                                    ← Back to Data Sources
                                </NuxtLink>
                            </div>
                        </div>
                    </div>

                    <!-- Normal content -->
                    <template v-else-if="!fetchError">
                        <!-- Tabs navigation -->
                        <div class="border-b border-gray-200 mt-6">
                            <nav class=" flex space-x-8">
                                <NuxtLink
                                    v-for="tab in tabs"
                                    :key="tab.name"
                                    :to="tabTo(tab.name)"
                                    :class="[
                                        isTabActive(tab.name)
                                            ? 'border-blue-500 text-blue-600'
                                            : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
                                        'whitespace-nowrap border-b-2 py-2 px-2 text-sm font-medium flex items-center space-x-2'
                                    ]"
                                >
                                    <Icon v-if="tab.icon" :name="tab.icon" class="w-4 mr-1" />
                                    <span>{{ tab.label }}</span>
                                </NuxtLink>
                            </nav>
                        </div>

                        <!-- Page content -->
                        <slot />
                    </template>
                </div>
            </div>
        </div>
    </NuxtLayout>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const route = useRoute()

const id = computed(() => String(route.params.id || ''))

const tabs = [
    { name: '', label: 'Overview', icon: 'i-heroicons-home' },
    { name: 'tables', label: 'Tables', icon: 'i-heroicons-table-cells' },
    { name: 'context', label: 'Context', icon: 'i-heroicons-light-bulb' },
    { name: 'connection', label: 'Connection', icon: 'i-heroicons-link' },
    { name: 'settings', label: 'Settings', icon: 'i-heroicons-cog-6-tooth' }
]

function tabTo(tabName: string) {
    if (!id.value) return '/data'
    if (tabName === '') return `/data/${id.value}`
    return `/data/${id.value}/${tabName}`
}

function isTabActive(tabName: string) {
    const path = route.path
    if (tabName === '') {
        return path === `/data/${id.value}` || path === `/data/${id.value}/`
    }
    return path === `/data/${id.value}/${tabName}`
}

const integration = ref<any>(null)
const isLoading = ref(true)
const fetchError = ref<number | null>(null)
const connection = computed(() => String(integration.value?.user_status?.connection || '').toLowerCase())

// Connection info for display
const connectionType = computed(() => integration.value?.connection?.type || integration.value?.type)
const connectionName = computed(() => integration.value?.connection?.name || integration.value?.name || 'No connection')
const isConnected = computed(() => {
    const c = connection.value
    return c === 'success'
})

function getConnectionStatus(conn: any): string {
    return conn?.user_status?.connection || conn?.status || 'unknown'
}

async function fetchIntegration() {
    if (!id.value) return
    isLoading.value = true
    fetchError.value = null
    
    try {
        const config = useRuntimeConfig()
        const { token } = useAuth()
        const { organization } = useOrganization()
        
        const data = await $fetch(`/data_sources/${id.value}`, {
            baseURL: config.public.baseURL,
            method: 'GET',
            headers: {
                Authorization: `${token.value}`,
                'X-Organization-Id': organization.value?.id || '',
            }
        })
        
        integration.value = data as any
    } catch (e: any) {
        console.error('Failed to fetch integration:', e)
        fetchError.value = e?.response?.status || e?.status || e?.statusCode || 500
    }
    
    isLoading.value = false
}

// Provide integration data to child pages
provide('integration', integration)
provide('fetchIntegration', fetchIntegration)
provide('isLoading', isLoading)
provide('fetchError', fetchError)

watch(id, () => {
    fetchIntegration()
})

onMounted(() => {
    fetchIntegration()
})
</script>


