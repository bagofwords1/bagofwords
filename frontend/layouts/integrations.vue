<template>
    <NuxtLayout name="default">
        <div class="flex justify-center pl-2 md:pl-4 text-sm">
            <div class="w-full max-w-7xl px-4 pl-0 py-2">
                <div>
                    <div class="flex items-start justify-between">
                        <div>
                            <h1 class="text-lg font-semibold">
                                {{ integration?.name || 'Domain' }}
                            </h1>
                            <div v-if="!isLoading && integration" class="flex items-center gap-2 mt-1 text-xs text-gray-500">
                                <span :class="['w-2 h-2 rounded-full', isConnected ? 'bg-green-500' : 'bg-red-500']"></span>
                                <DataSourceIcon :type="connectionType" class="h-4 w-4" />
                                <span>{{ connectionName }}</span>
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <span v-if="isLoading" class="px-2 py-0.5 rounded text-xs border bg-gray-50 text-gray-700 border-gray-200 flex items-center gap-1">
                                <Spinner />
                                Loading
                            </span>
                            <span v-else-if="connectionLabel" :class="['px-2 py-0.5 rounded text-xs border flex items-center gap-1', connectionClass]">
                                <span>{{ connectionLabel }}</span>
                            </span>
                        </div>
                    </div>

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
                </div>
            </div>
        </div>
    </NuxtLayout>
</template>

<script setup lang="ts">
const route = useRoute()
const toast = useToast()

const id = computed(() => String(route.params.id || ''))

const tabs = [
    { name: '', label: 'Overview', icon: 'i-heroicons-home' },
    { name: 'tables', label: 'Tables', icon: 'i-heroicons-table-cells' },
    { name: 'context', label: 'Context', icon: 'i-heroicons-light-bulb' },
    { name: 'connection', label: 'Connection', icon: 'i-heroicons-link' },
    { name: 'settings', label: 'Settings', icon: 'i-heroicons-cog-6-tooth' }
]

function tabTo(tabName: string) {
    if (!id.value) return '/integrations'
    if (tabName === '') return `/integrations/${id.value}`
    return `/integrations/${id.value}/${tabName}`
}

function isTabActive(tabName: string) {
    const path = route.path
    if (tabName === '') {
        return path === `/integrations/${id.value}` || path === `/integrations/${id.value}/`
    }
    return path === `/integrations/${id.value}/${tabName}`
}

const integration = ref<any>(null)
const isDisconnecting = ref(false)
const isLoading = ref(true)
const connection = computed(() => String(integration.value?.user_status?.connection || '').toLowerCase())

// Connection info for display
const connectionType = computed(() => integration.value?.connection?.type || integration.value?.type)
const connectionName = computed(() => integration.value?.connection?.name || integration.value?.name || 'No connection')
const isConnected = computed(() => {
    const c = connection.value
    return c === 'success'
})
const connectionLabel = computed(() => {
    const c = connection.value
    if (c === 'success') return 'Connected'
    if (c === 'not_connected') return 'Not connected'
    if (c === 'offline') return 'Offline'
    if (c === 'unknown' || !c) return integration.value?.is_active ? 'Active' : 'Inactive'
    return 'Unknown'
})
const connectionClass = computed(() => {
    const c = connection.value
    if (c === 'success') return 'bg-green-50 text-green-700 border-green-200'
    if (c === 'not_connected' || c === 'offline') return 'bg-red-50 text-red-700 border-red-200'
    return 'bg-gray-50 text-gray-700 border-gray-200'
})
// Disconnect control removed per requirements

async function fetchIntegration() {
    if (!id.value) return
    isLoading.value = true
    
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
    } catch (e) {
        console.error('Failed to fetch integration:', e)
    }
    
    isLoading.value = false
}



watch(id, () => {
    fetchIntegration()
})

onMounted(() => {
    fetchIntegration()
})
</script>


