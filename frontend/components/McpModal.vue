<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-3xl' }">
        <UCard>
            <!-- Header -->
            <template #header>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <McpIcon class="w-6 h-6" />
                        <h3 class="text-lg font-semibold text-gray-900">MCP Integration</h3>
                    </div>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark-20-solid"
                        @click="isOpen = false"
                    />
                </div>
                <p class="text-sm text-gray-500 mt-2">
                    Connect AI assistants like Cursor, Claude, or Windsurf to your data via the Model Context Protocol.
                </p>
            </template>

            <!-- Content -->
            <div v-if="loading" class="py-12 flex items-center justify-center">
                <div class="text-center">
                    <Spinner class="w-8 h-8 mx-auto mb-4 text-gray-400" />
                    <p class="text-sm text-gray-500">Loading...</p>
                </div>
            </div>

            <div v-else class="space-y-6">
                <!-- Server URL -->
                <div>
                    <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-3">MCP Server</div>
                    <div class="flex items-center justify-between bg-white rounded-lg px-4 py-3 border border-gray-200">
                        <div class="flex items-center gap-3 min-w-0">
                            <div class="w-2 h-2 rounded-full bg-green-500 flex-shrink-0"></div>
                            <div class="min-w-0">
                                <div class="text-sm font-medium text-gray-900 mb-0.5">Server Active</div>
                                <div class="text-xs text-gray-500 font-mono truncate">{{ mcpServerUrl }}</div>
                            </div>
                        </div>
                        <button 
                            @click="copy(mcpServerUrl)" 
                            class="text-gray-400 hover:text-gray-600 p-1.5 rounded hover:bg-gray-50 flex-shrink-0"
                            title="Copy URL"
                        >
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                        </button>
                    </div>
                </div>

                <!-- Configuration -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <div class="text-[11px] uppercase tracking-wide text-gray-500">Configuration</div>
                        <button 
                            @click="copy(mcpConfig)" 
                            class="text-gray-400 hover:text-gray-600 p-1"
                        >
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                        </button>
                    </div>
                    <pre class="bg-gray-50 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-700 overflow-x-auto border border-gray-200">{{ mcpConfig }}</pre>
                </div>

                <!-- MCP Access Token -->
                <div>
                    <div class="flex items-center justify-between mb-3">
                        <div class="text-[11px] uppercase tracking-wide text-gray-500">MCP Access Token</div>
                        <UButton 
                            size="xs" 
                            color="blue"
                            @click="createApiKey"
                        >
                            <Spinner v-if="creating" class="w-3 h-3 mr-1" />
                            <UIcon v-else name="heroicons-plus" class="w-3 h-3 mr-1" />
                            Generate
                        </UButton>
                    </div>
                    
                    <div v-if="apiKeys.length === 0" class="text-xs text-gray-400">
                        No access tokens available.
                        <button 
                            @click="createApiKey" 
                            class="text-blue-500 hover:text-blue-600 underline ml-1"
                        >
                            Generate one
                        </button>
                    </div>
                    
                    <div v-else class="border border-gray-200 rounded-lg divide-y divide-gray-200">
                        <div 
                            v-for="key in apiKeys" 
                            :key="key.id" 
                            class="flex items-center justify-between px-3 py-2 hover:bg-gray-50 transition-colors group"
                        >
                            <code class="font-mono text-xs text-gray-700">{{ key.key_prefix }}•••••••••</code>
                            <div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                <button 
                                    v-if="key.key" 
                                    @click="copy(key.key)" 
                                    class="text-gray-400 hover:text-gray-600 p-1"
                                    title="Copy token"
                                >
                                    <UIcon name="heroicons-clipboard-document" class="w-3.5 h-3.5" />
                                </button>
                                <button 
                                    @click="deleteApiKey(key)" 
                                    class="text-gray-400 hover:text-red-500 p-1"
                                    title="Delete token"
                                >
                                    <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
import McpIcon from '~/components/icons/McpIcon.vue'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
    modelValue: boolean
}>()

const emit = defineEmits<{
    'update:modelValue': [value: boolean]
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const toast = useToast()

interface ApiKey {
    id: string
    name: string
    key_prefix: string
    key?: string
    created_at: string
}

const apiKeys = ref<ApiKey[]>([])
const creating = ref(false)
const loading = ref(false)
const baseUrl = ref('')

const mcpServerUrl = computed(() => {
    const base = baseUrl.value || window.location.origin
    return `${base}/mcp`
})

const mcpConfig = computed(() => {
    const apiKey = apiKeys.value[0]?.key || "<YOUR_API_KEY>"
    return JSON.stringify({
        "mcpServers": {
            "bagofwords": {
                "url": mcpServerUrl.value,
                "headers": {
                    "Authorization": `Bearer ${apiKey}`
                }
            }
        }
    }, null, 2)
})

async function copy(text: string | undefined) {
    if (!text) return
    await navigator.clipboard.writeText(text)
    toast.add({ title: 'Copied', icon: 'i-heroicons-check-circle', color: 'green' })
}

async function loadSettings() {
    try {
        const res = await useMyFetch('/settings')
        if (res.data.value) {
            baseUrl.value = (res.data.value as any).base_url || ''
        }
    } catch (e) {
        // Use window.location.origin as fallback
    }
}

async function loadApiKeys() {
    try {
        const res = await useMyFetch('/api/api_keys')
        if (res.data.value) {
            apiKeys.value = res.data.value as ApiKey[]
        }
    } catch (e) {
        // API might not exist yet
    }
}

async function createApiKey() {
    creating.value = true
    try {
        const res = await useMyFetch('/api/api_keys', { 
            method: 'POST',
            body: { name: 'MCP' }
        })
        if (res.data.value) {
            const newKey = res.data.value as ApiKey
            apiKeys.value = [newKey, ...apiKeys.value]
            if (newKey.key) {
                await navigator.clipboard.writeText(newKey.key)
                toast.add({ title: 'API key created and copied', icon: 'i-heroicons-check-circle', color: 'green' })
            }
        }
    } catch (e) {
        toast.add({ title: 'Failed to create API key', icon: 'i-heroicons-x-circle', color: 'red' })
    } finally {
        creating.value = false
    }
}

async function deleteApiKey(key: ApiKey) {
    if (!confirm('Delete this API key?')) return
    try {
        await useMyFetch(`/api/api_keys/${key.id}`, { method: 'DELETE' })
        apiKeys.value = apiKeys.value.filter(k => k.id !== key.id)
        toast.add({ title: 'API key deleted', icon: 'i-heroicons-check-circle', color: 'green' })
    } catch (e) {
        toast.add({ title: 'Failed to delete API key', icon: 'i-heroicons-x-circle', color: 'red' })
    }
}

watch(isOpen, async (open) => {
    if (open) {
        loading.value = true
        await Promise.all([
            loadSettings(),
            loadApiKeys()
        ])
        loading.value = false
    }
})
</script>
