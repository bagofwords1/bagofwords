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
                    Connect AI assistants like Claude, Cursor, or others to your data via the Model Context Protocol.
                </p>
            </template>

            <!-- Content -->
            <div v-if="loading" class="py-12 flex items-center justify-center">
                <div class="text-center">
                    <Spinner class="w-8 h-8 mx-auto mb-4 text-gray-400" />
                    <p class="text-sm text-gray-500">Loading...</p>
                </div>
            </div>

            <div v-else class="space-y-5">
                <!-- Top bar: Server status left, Generate/Regenerate right -->
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-2 text-xs text-gray-500">
                        <div class="w-1.5 h-1.5 rounded-full bg-green-500"></div>
                        <code class="font-mono text-gray-700">{{ mcpServerUrl }}</code>
                    </div>
                    <UButton 
                        size="xs" 
                        color="blue"
                        @click="regenerateToken"
                        :loading="creating"
                    >
                        <UIcon :name="apiKeys.length === 0 ? 'heroicons-plus' : 'heroicons-arrow-path'" class="w-3.5 h-3.5 mr-1" />
                        {{ apiKeys.length === 0 ? 'Generate Token' : 'Regenerate Token' }}
                    </UButton>
                </div>

                <!-- Token display -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <div class="text-[11px] uppercase tracking-wide text-gray-500">Access Token</div>
                        <UTooltip 
                            :text="currentToken ? '' : (apiKeys.length === 0 ? 'Generate token to copy' : 'Regenerate token to copy')" 
                            :popper="{ placement: 'top' }"
                        >
                            <button 
                                @click="currentToken && copy(currentToken)" 
                                :class="[
                                    'p-1 rounded',
                                    currentToken 
                                        ? 'text-gray-400 hover:text-gray-600' 
                                        : 'text-gray-300 cursor-not-allowed'
                                ]"
                                :disabled="!currentToken"
                            >
                                <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                            </button>
                        </UTooltip>
                    </div>
                    <div class="px-3 py-2 bg-gray-50 rounded-lg border border-gray-200">
                        <code class="font-mono text-xs text-gray-700">{{ currentToken || '••••••••••••••••••••••••••••••••' }}</code>
                    </div>
                </div>

                <!-- Configuration -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <div class="text-[11px] uppercase tracking-wide text-gray-500">Configuration</div>
                        <UTooltip 
                            :text="currentToken ? '' : (apiKeys.length === 0 ? 'Generate token to copy' : 'Regenerate token to copy')" 
                            :popper="{ placement: 'top' }"
                        >
                            <button 
                                @click="currentToken && copy(mcpConfig)" 
                                :class="[
                                    'p-1 rounded',
                                    currentToken 
                                        ? 'text-gray-400 hover:text-gray-600' 
                                        : 'text-gray-300 cursor-not-allowed'
                                ]"
                                :disabled="!currentToken"
                            >
                                <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
                            </button>
                        </UTooltip>
                    </div>
                    <pre class="bg-gray-50 rounded-lg px-3 py-2.5 font-mono text-xs text-gray-700 overflow-x-auto border border-gray-200">{{ mcpConfig }}</pre>
                </div>

                <!-- Manage tokens (collapsed) -->
                <div v-if="apiKeys.length > 0" class="pt-2 border-t border-gray-100">
                    <button 
                        @click="showTokens = !showTokens"
                        class="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                    >
                        <UIcon 
                            :name="showTokens ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" 
                            class="w-3 h-3"
                        />
                        Manage tokens ({{ apiKeys.length }})
                    </button>
                    
                    <div v-if="showTokens" class="mt-3 border border-gray-200 rounded-lg divide-y divide-gray-200">
                        <div 
                            v-for="key in apiKeys" 
                            :key="key.id" 
                            class="flex items-center justify-between px-3 py-2 hover:bg-gray-50 transition-colors group"
                        >
                            <div class="flex items-center gap-3 min-w-0">
                                <code class="font-mono text-xs text-gray-700">{{ key.key_prefix }}•••••••••</code>
                                <span class="text-[10px] text-gray-400">{{ formatDate(key.created_at) }}</span>
                            </div>
                            <button 
                                @click="deleteApiKey(key)" 
                                class="text-gray-400 hover:text-red-500 p-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0"
                                title="Delete token"
                            >
                                <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
                            </button>
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
const currentToken = ref<string | null>(null)
const showTokens = ref(false)

const mcpServerUrl = computed(() => {
    const base = baseUrl.value || window.location.origin
    return `${base}/mcp`
})

const mcpConfig = computed(() => {
    const token = currentToken.value || "<YOUR_API_KEY>"
    return JSON.stringify({
        "mcpServers": {
            "bagofwords": {
                "url": mcpServerUrl.value,
                "headers": {
                    "Authorization": `Bearer ${token}`
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

function formatDate(dateStr: string) {
    return new Date(dateStr).toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    })
}

async function regenerateToken() {
    await createApiKey()
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
                currentToken.value = newKey.key
                toast.add({ title: 'Token generated', icon: 'i-heroicons-check-circle', color: 'green' })
            }
        }
    } catch (e) {
        toast.add({ title: 'Failed to create token', icon: 'i-heroicons-x-circle', color: 'red' })
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
        currentToken.value = null
        showTokens.value = false
        await Promise.all([
            loadSettings(),
            loadApiKeys()
        ])
        loading.value = false
    }
})
</script>
