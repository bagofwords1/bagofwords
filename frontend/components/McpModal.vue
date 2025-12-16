<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-6">
            <div class="flex items-center gap-3 mb-2">
                <McpIcon class="w-6 h-6" />
                <h2 class="text-lg font-semibold text-gray-900">MCP Integration</h2>
            </div>
            <p class="text-sm text-gray-500 mb-6">
                Connect AI assistants like Cursor, Claude, or Windsurf to your data via the Model Context Protocol.
            </p>

            <div class="space-y-5">
                <!-- Server URL -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <label class="text-sm font-medium text-gray-700">Server URL</label>
                        <button @click="copy(mcpServerUrl)" class="text-gray-400 hover:text-gray-600">
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                        </button>
                    </div>
                    <div class="bg-gray-50 rounded-lg px-3 py-2 font-mono text-sm text-gray-600">
                        {{ mcpServerUrl }}
                    </div>
                </div>

                <!-- API Key -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <label class="text-sm font-medium text-gray-700">API Key</label>
                        <UButton v-if="apiKeys.length === 0" size="xs" color="blue" @click="createApiKey" :loading="creating">
                            Generate
                        </UButton>
                        <button v-else-if="apiKeys[0]?.key" @click="copy(apiKeys[0].key)" class="text-gray-400 hover:text-gray-600">
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                        </button>
                    </div>
                    <div v-if="apiKeys.length === 0" class="text-sm text-gray-400">
                        Generate an API key to authenticate requests
                    </div>
                    <div v-else class="flex items-center justify-between bg-gray-50 rounded-lg px-3 py-2">
                        <code class="font-mono text-sm text-gray-600">{{ apiKeys[0].key_prefix }}•••••••••</code>
                        <button @click="deleteApiKey(apiKeys[0])" class="text-gray-400 hover:text-red-500">
                            <UIcon name="heroicons-trash" class="w-4 h-4" />
                        </button>
                    </div>
                </div>

                <!-- Config -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <label class="text-sm font-medium text-gray-700">Configuration</label>
                        <button @click="copy(mcpConfig)" class="text-gray-400 hover:text-gray-600">
                            <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                        </button>
                    </div>
                    <pre class="bg-gray-50 rounded-lg px-3 py-2 font-mono text-xs text-gray-600 overflow-x-auto">{{ mcpConfig }}</pre>
                </div>
            </div>

            <div class="mt-6 flex justify-end">
                <UButton color="gray" variant="ghost" @click="isOpen = false">Done</UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import McpIcon from '~/components/icons/McpIcon.vue'

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
const config = useRuntimeConfig()

interface ApiKey {
    id: string
    name: string
    key_prefix: string
    key?: string
    created_at: string
}

const apiKeys = ref<ApiKey[]>([])
const creating = ref(false)

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

async function copy(text: string | undefined) {
    if (!text) return
    await navigator.clipboard.writeText(text)
    toast.add({ title: 'Copied', icon: 'i-heroicons-check-circle', color: 'green' })
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

watch(isOpen, (open) => {
    if (open) loadApiKeys()
})
</script>
