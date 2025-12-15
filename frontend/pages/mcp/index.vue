<template>
    <div class="flex justify-center pl-2 md:pl-4 text-sm">
        <div class="w-full max-w-7xl px-4 pl-0 py-2">
            <div>
                <h1 class="text-lg font-semibold">MCP</h1>
                <p class="mt-2 text-gray-500">Connect AI assistants to Bag of Words via Model Context Protocol</p>
            </div>

            <div class="mt-8 max-w-2xl space-y-6">
                <!-- Server URL -->
                <div class="bg-white border border-gray-200 rounded-xl p-5">
                    <div class="flex items-center justify-between mb-3">
                        <div class="flex items-center gap-2">
                            <div class="w-2 h-2 rounded-full bg-green-500"></div>
                            <span class="text-sm font-medium text-gray-900">MCP Server</span>
                        </div>
                        <UButton size="xs" variant="ghost" color="gray" @click="copyUrl(mcpServerUrl)">
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                        </UButton>
                    </div>
                    <code class="text-sm text-gray-600 font-mono">{{ mcpServerUrl }}</code>
                </div>

                <!-- API Key -->
                <div class="bg-white border border-gray-200 rounded-xl p-5">
                    <div class="flex items-center justify-between mb-3">
                        <span class="text-sm font-medium text-gray-900">API Key</span>
                        <UButton v-if="apiKeys.length === 0" size="xs" color="blue" @click="createApiKey">
                            Generate
                        </UButton>
                    </div>
                    <div v-if="apiKeys.length === 0" class="text-sm text-gray-400">
                        Generate an API key to authenticate MCP requests
                    </div>
                    <div v-for="key in apiKeys" :key="key.id" class="flex items-center justify-between">
                        <code class="text-sm text-gray-600 font-mono">{{ key.key_prefix }}•••••••••</code>
                        <div class="flex items-center gap-1">
                            <UButton size="xs" variant="ghost" color="gray" @click="copyUrl(key.key)" :disabled="!key.key">
                                <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                            </UButton>
                            <UButton size="xs" variant="ghost" color="red" @click="deleteApiKey(key)">
                                <UIcon name="heroicons-trash" class="w-4 h-4" />
                            </UButton>
                        </div>
                    </div>
                </div>

                <!-- Configuration Example -->
                <div class="bg-gray-50 border border-gray-200 rounded-xl p-5">
                    <div class="flex items-center justify-between mb-3">
                        <span class="text-sm font-medium text-gray-900">Configuration</span>
                        <UButton size="xs" variant="ghost" color="gray" @click="copyUrl(mcpConfig)">
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4 mr-1" />
                            Copy
                        </UButton>
                    </div>
                    <pre class="text-xs font-mono text-gray-600 overflow-x-auto whitespace-pre">{{ mcpConfig }}</pre>
                </div>
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

const mcpServerUrl = computed(() => {
    const baseUrl = config.public.baseURL || window.location.origin
    return `${baseUrl}/api/mcp`
})

const mcpConfig = computed(() => {
    return JSON.stringify({
        "mcpServers": {
            "bagofwords": {
                "url": mcpServerUrl.value,
                "apiKey": apiKeys.value[0]?.key || "<YOUR_API_KEY>"
            }
        }
    }, null, 2)
})

async function copyUrl(text: string | undefined) {
    if (!text) return
    await navigator.clipboard.writeText(text)
    toast.add({ title: 'Copied to clipboard', icon: 'i-heroicons-check-circle', color: 'green' })
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

onMounted(() => {
    loadApiKeys()
})
</script>
