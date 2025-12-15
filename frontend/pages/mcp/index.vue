<template>
    <div class="flex justify-center pl-2 md:pl-4 text-sm">
        <div class="w-full max-w-7xl px-4 pl-0 py-2">
            <div>
                <h1 class="text-lg font-semibold">MCP</h1>
                <p class="mt-2 text-gray-500">Connect your IDE to Bag of Words via Model Context Protocol</p>
            </div>

            <div class="mt-6 max-w-xl">
                <!-- API Key -->
                <div class="flex items-center justify-between mb-4">
                    <span class="text-sm font-medium text-gray-700">API Key</span>
                    <UButton v-if="apiKeys.length === 0" size="xs" color="blue" @click="createApiKey">
                        Create Key
                    </UButton>
                </div>
                <div v-if="apiKeys.length === 0" class="text-sm text-gray-400 mb-6">
                    Create an API key to get started
                </div>
                <div v-for="key in apiKeys" :key="key.id" class="flex items-center justify-between mb-6">
                    <code class="text-xs text-gray-600 bg-gray-100 px-2 py-1 rounded">{{ key.key_prefix }}...</code>
                    <UButton size="xs" color="red" variant="ghost" @click="deleteApiKey(key)">
                        <UIcon name="heroicons-trash" class="w-4 h-4" />
                    </UButton>
                </div>

                <!-- Cursor Config -->
                <div class="flex items-center justify-between mb-2">
                    <span class="text-sm font-medium text-gray-700">Cursor Configuration</span>
                    <UButton size="xs" color="gray" variant="ghost" @click="copyCursorConfig">
                        <UIcon name="heroicons-clipboard-document" class="w-4 h-4 mr-1" />
                        Copy
                    </UButton>
                </div>
                <pre class="bg-gray-50 border border-gray-200 rounded-lg p-3 text-xs font-mono text-gray-600 overflow-x-auto">{{ cursorConfig }}</pre>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({
    auth: true,
    layout: 'default'
})

const toast = useToast()
const config = useRuntimeConfig()

interface ApiKey {
    id: string
    name: string
    key_prefix: string
    key?: string
    created_at: string
}

const apiKeys = ref<ApiKey[]>([])

const cursorConfig = computed(() => {
    const baseUrl = config.public.baseURL || 'https://api.bagofwords.com'
    return JSON.stringify({
        "mcpServers": {
            "bagofwords": {
                "url": `${baseUrl}/mcp`,
                "apiKey": apiKeys.value[0]?.key || "<YOUR_API_KEY>"
            }
        }
    }, null, 2)
})

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
    try {
        const res = await useMyFetch('/api/api_keys', { 
            method: 'POST',
            body: { name: 'MCP Integration' }
        })
        if (res.data.value) {
            const newKey = res.data.value as ApiKey
            apiKeys.value = [newKey, ...apiKeys.value]
            if (newKey.key) {
                await navigator.clipboard.writeText(newKey.key)
                toast.add({ title: 'API key created and copied to clipboard', icon: 'i-heroicons-check-circle', color: 'green' })
            }
        }
    } catch (e) {
        toast.add({ title: 'Failed to create API key', icon: 'i-heroicons-x-circle', color: 'red' })
    }
}

async function deleteApiKey(key: ApiKey) {
    if (!confirm('Delete this API key? This action cannot be undone.')) return
    try {
        await useMyFetch(`/api/api_keys/${key.id}`, { method: 'DELETE' })
        apiKeys.value = apiKeys.value.filter(k => k.id !== key.id)
        toast.add({ title: 'API key deleted', icon: 'i-heroicons-check-circle', color: 'green' })
    } catch (e) {
        toast.add({ title: 'Failed to delete API key', icon: 'i-heroicons-x-circle', color: 'red' })
    }
}

async function copyCursorConfig() {
    await navigator.clipboard.writeText(cursorConfig.value)
    toast.add({ title: 'Configuration copied', icon: 'i-heroicons-check-circle', color: 'green' })
}

onMounted(() => {
    loadApiKeys()
})
</script>
