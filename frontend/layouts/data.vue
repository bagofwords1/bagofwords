<template>
    <NuxtLayout name="default">
        <div class="flex justify-center ps-2 md:ps-4 text-sm">
            <div class="w-full max-w-7xl px-4 ps-0 py-4">

                <!-- Back link -->
                <NuxtLink to="/agents" class="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 transition-colors">
                    <UIcon name="heroicons-chevron-left" class="w-3.5 h-3.5" />
                    All agents
                </NuxtLink>

                <!-- Header -->
                <div class="flex items-start justify-between gap-4 mt-5">
                    <div class="min-w-0 flex-1">

                        <!-- Skeleton while loading -->
                        <template v-if="isLoading">
                            <div class="h-8 w-40 bg-gray-100 rounded-md animate-pulse" />
                            <div class="h-4 w-72 bg-gray-100 rounded mt-3 animate-pulse" />
                            <div class="flex items-center gap-2 mt-4">
                                <div class="h-3.5 w-3.5 rounded-full bg-gray-100 animate-pulse" />
                                <div class="h-3.5 w-24 bg-gray-100 rounded animate-pulse" />
                            </div>
                        </template>

                        <template v-else-if="!fetchError">
                            <!-- Agent name -->
                            <h1 class="text-2xl font-bold text-gray-900 leading-tight tracking-tight truncate">
                                {{ integration?.name || 'Agent' }}
                            </h1>

                            <!-- Description (inline-editable) -->
                            <div v-if="integration?.description || useCan('update_data_source')" class="mt-2 flex items-center gap-2 group max-w-2xl">
                                <template v-if="editingDesc">
                                    <input
                                        ref="descInputRef"
                                        v-model="descForm"
                                        type="text"
                                        class="flex-1 text-sm text-gray-600 border-b border-blue-400 bg-transparent outline-none py-0.5"
                                        @keydown.enter="saveDesc"
                                        @keydown.escape="cancelDesc"
                                        @blur="saveDesc"
                                    />
                                </template>
                                <template v-else>
                                    <p
                                        class="text-sm text-gray-500 truncate rounded px-1 -mx-1 transition-colors"
                                        :class="useCan('update_data_source') ? 'cursor-pointer hover:bg-gray-100' : ''"
                                        @click="useCan('update_data_source') && startEditDesc()"
                                    >{{ integration?.description || '' }}</p>
                                    <button
                                        v-if="useCan('update_data_source')"
                                        class="text-[10px] text-blue-600 hover:underline opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                                        @click="startEditDesc"
                                    >Edit</button>
                                </template>
                            </div>

                            <!-- Connections + stats row -->
                            <div class="flex items-center gap-3 mt-4 flex-wrap">
                                <template v-if="(integration?.connections || []).length > 0">
                                    <div
                                        v-for="conn in (integration.connections || []).slice(0, 4)"
                                        :key="conn.id"
                                        class="flex items-center gap-1.5"
                                    >
                                        <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', statusDotClass(getEffectiveStatus(conn))]" />
                                        <UTooltip :text="conn.name + (getEffectiveStatus(conn) === 'indexing' ? ' · ' + indexingSummary(conn.indexing) : '')">
                                            <div class="flex items-center gap-1">
                                                <DataSourceIcon :type="conn.type" class="h-3.5 opacity-70" />
                                                <span class="text-xs text-gray-500">{{ conn.name }}</span>
                                            </div>
                                        </UTooltip>
                                    </div>
                                    <span v-if="(integration.connections || []).length > 4" class="text-xs text-gray-400">
                                        +{{ integration.connections.length - 4 }}
                                    </span>
                                </template>
                                <span v-else class="text-xs text-gray-400 italic">No connections</span>

                                <template v-if="(catalog.shouldShow && catalog.count > 0) || connectionCount > 0">
                                    <template v-if="catalog.shouldShow">
                                        <span class="text-gray-300 select-none">·</span>
                                        <span class="text-xs text-gray-400">{{ catalog.label }}</span>
                                    </template>
                                    <span class="text-gray-300 select-none">·</span>
                                    <span class="text-xs text-gray-400">{{ connectionCount }} {{ connectionCount === 1 ? 'connection' : 'connections' }}</span>
                                </template>
                            </div>
                        </template>

                        <template v-else>
                            <h1 class="text-2xl font-bold text-gray-900">Agent</h1>
                        </template>
                    </div>

                    <!-- New Report CTA -->
                    <div v-if="!isLoading && !fetchError && integration" class="shrink-0 mt-1">
                        <UButton
                            color="blue"
                            size="sm"
                            :loading="startingChat"
                            @click="startChat"
                        >
                            New Report
                            <UIcon name="heroicons-arrow-right" class="w-3.5 h-3.5 ms-1" />
                        </UButton>
                    </div>
                </div>

                <!-- Error states -->
                <div v-if="!isLoading && fetchError === 403" class="mt-8">
                    <div class="bg-white border border-gray-200 rounded-xl p-10 text-center">
                        <Icon name="i-heroicons-lock-closed" class="w-10 h-10 text-gray-300 mx-auto mb-3" />
                        <h2 class="text-base font-medium text-gray-900">Access Restricted</h2>
                        <p class="mt-1.5 text-sm text-gray-500 max-w-sm mx-auto">
                            This agent is private. Contact the owner or an admin to request access.
                        </p>
                        <NuxtLink to="/agents" class="mt-4 inline-block text-sm text-blue-600 hover:underline">
                            ← Back to agents
                        </NuxtLink>
                    </div>
                </div>

                <div v-else-if="!isLoading && fetchError === 404" class="mt-8">
                    <div class="bg-white border border-gray-200 rounded-xl p-10 text-center">
                        <Icon name="i-heroicons-exclamation-circle" class="w-10 h-10 text-gray-300 mx-auto mb-3" />
                        <h2 class="text-base font-medium text-gray-900">Agent Not Found</h2>
                        <p class="mt-1.5 text-sm text-gray-500 max-w-sm mx-auto">
                            The agent you're looking for doesn't exist or has been removed.
                        </p>
                        <NuxtLink to="/agents" class="mt-4 inline-block text-sm text-blue-600 hover:underline">
                            ← Back to agents
                        </NuxtLink>
                    </div>
                </div>

                <!-- Tabs + content -->
                <template v-else-if="!fetchError">
                    <div class="mt-6">
                        <nav class="flex items-center gap-1">
                            <NuxtLink
                                v-for="tab in tabs"
                                :key="tab.name"
                                :to="tabTo(tab.name)"
                                :class="[
                                    isTabActive(tab.name)
                                        ? 'bg-gray-100 text-gray-900 font-medium'
                                        : 'text-gray-500 hover:bg-gray-50 hover:text-gray-800',
                                    'whitespace-nowrap rounded-full px-4 py-1.5 text-sm transition-all'
                                ]"
                            >
                                {{ tab.label }}
                            </NuxtLink>
                        </nav>
                    </div>

                    <slot />
                </template>

            </div>
        </div>

    </NuxtLayout>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import {
    getEffectiveStatus,
    hasAnyActiveIndexing,
    indexingSummary,
    statusDotClass,
} from '~/composables/useConnectionStatus'
import { useCan } from '~/composables/usePermissions'

const route = useRoute()
const router = useRouter()
const toast = useToast?.()

const id = computed(() => String(route.params.id || ''))
const { isMcpToolsEnabled } = useOrgSettings()

const canViewMonitoring = computed(() => useCan('full_admin_access'))
const canManageEvals = computed(() => useCan('manage_evals'))

const allTabs = computed(() => {
    // Label the Tables tab to match the agent's data_shape: "Files" for
    // file-shape connectors (OneDrive / SharePoint / Google Drive),
    // "Collections" for object-shape (MongoDB), default "Tables" for SQL.
    const tablesLabel = (() => {
        const s = catalog.value.noun.plural
        if (s === 'tables') return 'Tables'
        // Title-case the noun
        return s.charAt(0).toUpperCase() + s.slice(1)
    })()
    return [
        { name: '', label: 'Overview' },
        { name: 'tables', label: tablesLabel },
        { name: 'context', label: 'Instructions' },
        { name: 'queries', label: 'Queries' },
        { name: 'tools', label: 'Tools' },
        { name: 'monitoring', label: 'Monitoring', gate: canViewMonitoring },
        { name: 'evals', label: 'Evals', gate: canManageEvals },
        { name: 'settings', label: 'Settings' },
    ]
})

const tabs = computed(() =>
    allTabs.value.filter(tab => {
        if (tab.name === 'tools' && !isMcpToolsEnabled.value) return false
        if (tab.gate && !tab.gate.value) return false
        return true
    })
)

function tabTo(tabName: string) {
    if (!id.value) return '/agents'
    if (tabName === '') return `/agents/${id.value}`
    return `/agents/${id.value}/${tabName}`
}

function isTabActive(tabName: string) {
    const path = route.path
    if (tabName === '') {
        return path === `/agents/${id.value}` || path === `/agents/${id.value}/`
    }
    return path === `/agents/${id.value}/${tabName}`
}

const tableCount = computed(() =>
    (integration.value?.connections || []).reduce((sum: number, c: any) => sum + (c.table_count || 0), 0)
)
const connectionCount = computed(() => (integration.value?.connections || []).length)

// Shape-aware catalog count: respects each connection's data_shape (files
// vs tables vs objects) and hides the number entirely when any attached
// connection is user_required + the current user hasn't signed in (the "0"
// would lie — per-user catalog hasn't been fetched yet).
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
const catalog = computed(() => computeFromAgent(integration.value, registryByType.value))

const integration = ref<any>(null)
const isLoading = ref(true)
const fetchError = ref<number | null>(null)
const startingChat = ref(false)

const editingDesc = ref(false)
const descForm = ref('')
const descInputRef = ref<HTMLInputElement | null>(null)

function startEditDesc() {
    descForm.value = integration.value?.description || ''
    editingDesc.value = true
    nextTick(() => descInputRef.value?.focus())
}

function cancelDesc() {
    editingDesc.value = false
}

async function saveDesc() {
    if (!editingDesc.value) return
    editingDesc.value = false
    const newVal = (descForm.value || '').trim()
    if (newVal === (integration.value?.description || '')) return
    if (integration.value) integration.value.description = newVal
    const { error } = await useMyFetch(`/data_sources/${id.value}`, {
        method: 'PUT',
        body: { description: newVal },
    })
    if (error?.value) {
        if (integration.value) integration.value.description = descForm.value
        toast?.add?.({ title: 'Failed to save description', color: 'red' })
    } else {
        toast?.add?.({ title: 'Description updated' })
        await fetchIntegration()
    }
}

async function startChat() {
    if (startingChat.value || !integration.value?.id) return
    startingChat.value = true
    try {
        const response = await useMyFetch('/reports', {
            method: 'POST',
            body: JSON.stringify({
                title: 'untitled report',
                files: [],
                data_sources: [integration.value.id],
            }),
        })
        const data = (response.data as any)?.value
        if (data?.id) {
            await router.push(`/reports/${data.id}`)
        }
    } finally {
        startingChat.value = false
    }
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
    maybeStartPolling()
}

provide('integration', integration)
provide('fetchIntegration', fetchIntegration)
provide('isLoading', isLoading)
provide('fetchError', fetchError)

const POLL_INTERVAL_MS = 2000
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
    }
}

function maybeStartPolling() {
    const hasActive = hasAnyActiveIndexing(integration.value?.connections)
    if (hasActive && !pollTimer) {
        pollTimer = setInterval(() => {
            if (fetchError.value) {
                stopPolling()
                return
            }
            fetchIntegration().then(() => {
                if (!hasAnyActiveIndexing(integration.value?.connections)) {
                    stopPolling()
                }
            })
        }, POLL_INTERVAL_MS)
    } else if (!hasActive) {
        stopPolling()
    }
}

watch(id, () => {
    stopPolling()
    fetchIntegration()
})

onMounted(() => {
    fetchIntegration()
})

onBeforeUnmount(() => {
    stopPolling()
})
</script>
