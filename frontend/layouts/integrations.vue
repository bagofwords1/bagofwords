<template>
    <NuxtLayout name="default">
        <div class="flex justify-center pl-2 md:pl-4 text-sm">
            <div class="w-full max-w-7xl px-4 pl-0 py-2">
                <div>
                    <div class="flex items-start justify-between">
                        <h1 class="text-lg font-semibold flex items-center">
                            <DataSourceIcon v-if="integration" :type="integration?.type" class="h-6 mr-2" />
                            <span>{{ integration?.name || 'Integration' }}</span>
                        </h1>
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
    const response = await useMyFetch(`/data_sources/${id.value}`, { method: 'GET' })
    if ((response.status as any)?.value === 'success') {
        integration.value = (response.data as any)?.value
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


