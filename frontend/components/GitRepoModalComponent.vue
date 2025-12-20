<template>
    <UModal v-model="gitModalOpen" :ui="{ width: 'sm:max-w-lg' }">
        <UCard :ui="{ body: { padding: 'p-0' }, header: { padding: 'px-3 py-2.5' }, footer: { padding: 'px-3 py-2' } }">
            <template #header>
                <div class="flex items-center justify-between">
                    <div>
                        <h3 class="text-base font-semibold text-gray-900">
                            {{ headerTitle }}
                        </h3>
                        <p class="text-sm text-gray-500">
                            {{ headerSubtitle }}
                        </p>
                    </div>
                    <UButton icon="i-heroicons-x-mark" color="gray" variant="ghost" size="xs" @click="gitModalOpen = false" />
                </div>
                
                <!-- Step Indicators (only for new connection, not data source selection) -->
                <div v-if="!connectedRepo && !showDataSourceList && activeDatasourceId" class="flex items-center gap-1.5 mt-2">
                    <div 
                        v-for="step in 3" 
                        :key="step"
                        class="flex items-center gap-1.5"
                    >
                        <div 
                            class="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-medium transition-colors"
                            :class="step <= currentStep ? 'bg-blue-500 text-white' : 'bg-gray-100 text-gray-400'"
                        >
                            <UIcon v-if="step < currentStep" name="i-heroicons-check" class="w-3 h-3" />
                            <span v-else>{{ step }}</span>
                        </div>
                        <div v-if="step < 3" class="w-6 h-0.5" :class="step < currentStep ? 'bg-blue-500' : 'bg-gray-200'" />
                    </div>
                </div>
            </template>

            <!-- Delete Confirmation Dialog -->
            <UModal v-model="showDeleteConfirmation" :ui="{ width: 'sm:max-w-sm' }">
                <UCard :ui="{ body: { padding: 'p-3' }, header: { padding: 'px-3 py-2' }, footer: { padding: 'px-3 py-2' } }">
                    <template #header>
                        <div class="flex items-center gap-1.5">
                            <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 text-red-500" />
                            <h3 class="text-sm font-semibold text-gray-900">Disconnect Repository</h3>
                        </div>
                    </template>
                    <div class="space-y-2">
                        <p class="text-xs text-gray-600">Are you sure you want to disconnect?</p>
                        <div v-if="linkedInstructionCount > 0" class="bg-red-50 border border-red-200 rounded p-2">
                            <div class="flex items-start gap-1.5">
                                <UIcon name="i-heroicons-exclamation-circle" class="w-3.5 h-3.5 text-red-500 flex-shrink-0 mt-0.5" />
                                <p class="text-xs text-red-800">
                                    {{ linkedInstructionCount }} instruction{{ linkedInstructionCount !== 1 ? 's' : '' }} will be deleted
                                </p>
                            </div>
                        </div>
                    </div>
                    <template #footer>
                        <div class="flex justify-end gap-2">
                            <UButton color="gray" variant="ghost" size="xs" @click="showDeleteConfirmation = false">Cancel</UButton>
                            <UButton color="red" size="xs" :loading="isLoading" @click="executeDelete">Disconnect</UButton>
                        </div>
                    </template>
                </UCard>
            </UModal>

            <!-- Body -->
            <div class="p-4">
                <!-- Data Source Selection View (when no datasourceId provided) -->
                <div v-if="showDataSourceList" class="space-y-1">
                    <div v-if="loadingDataSources" class="py-8 flex items-center justify-center">
                        <div class="text-center">
                            <UIcon name="i-heroicons-arrow-path" class="w-6 h-6 mx-auto mb-2 text-gray-400 animate-spin" />
                            <p class="text-sm text-gray-500">Loading data sources...</p>
                        </div>
                    </div>

                    <div v-else-if="dataSources.length === 0" class="py-8 text-center">
                        <UIcon name="i-heroicons-circle-stack" class="w-8 h-8 mx-auto mb-2 text-gray-300" />
                        <p class="text-sm text-gray-500">No data sources configured.</p>
                        <p class="text-xs text-gray-400 mt-1">Add a data source first to connect Git repositories.</p>
                    </div>

                    <div v-else class="divide-y divide-gray-100 -mx-4">
                        <div 
                            v-for="ds in dataSources" 
                            :key="ds.id"
                            class="px-4 py-3 hover:bg-gray-50 cursor-pointer transition-colors"
                            @click="selectDataSource(ds)"
                        >
                            <div class="flex items-center justify-between">
                                <!-- Left: Data source name + git status -->
                                <div class="flex items-center gap-3 min-w-0 flex-1">
                                    <DataSourceIcon :type="ds.type" class="w-6 h-6 flex-shrink-0" />
                                    <div class="min-w-0">
                                        <p class="text-sm font-medium text-gray-900 truncate">{{ ds.name }}</p>
                                        <template v-if="ds.git_repository">
                                            <p class="text-sm text-gray-500 flex items-center gap-1.5 mt-0.5">
                                                <UIcon :name="getProviderIcon(ds.git_repository.provider)" class="w-4 h-4" />
                                                {{ formatRepoName(ds.git_repository.repo_url) }}
                                            </p>
                                        </template>
                                        <p v-else class="text-sm text-gray-400 mt-0.5">No git repo connected</p>
                                    </div>
                                </div>

                                <!-- Right: Action button -->
                                <div class="flex items-center gap-2 flex-shrink-0">
                                    <UButton
                                        :icon="ds.git_repository ? 'i-heroicons-cog-6-tooth' : 'i-heroicons-plus'"
                                        :color="ds.git_repository ? 'gray' : 'blue'"
                                        :variant="ds.git_repository ? 'ghost' : 'soft'"
                                        size="sm"
                                        @click.stop="selectDataSource(ds)"
                                    >
                                        {{ ds.git_repository ? 'Settings' : 'Connect Git Repo' }}
                                    </UButton>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Connected Repository View -->
                <div v-else-if="connectedRepo" class="space-y-4">
                    <!-- Repo Info -->
                    <div class="bg-gray-50 rounded-lg p-3 border border-gray-200">
                        <div class="flex items-center justify-between mb-3">
                            <div class="flex items-center gap-2 min-w-0">
                                <UIcon :name="getProviderIcon(connectedRepo.provider)" class="w-5 h-5 flex-shrink-0" />
                                <span class="text-sm font-medium text-gray-700 truncate">{{ connectedRepo.repo_url }}</span>
                            </div>
                            <UButton icon="i-heroicons-trash" color="red" variant="ghost" size="xs" :loading="isLoadingCount" @click="confirmDelete" />
                        </div>
                        
                        <div class="grid grid-cols-2 gap-3 text-sm">
                            <div>
                                <p class="text-gray-400">Branch</p>
                                <p class="font-medium text-gray-700">{{ connectedRepo.branch }}</p>
                            </div>
                            <div>
                                <p class="text-gray-400">Status</p>
                                <p class="font-medium" :class="statusClass">{{ statusText }}</p>
                            </div>
                            <div v-if="metadata_resources?.completed_at">
                                <p class="text-gray-400">Last Indexed</p>
                                <p class="font-medium text-gray-700">{{ formatDate(metadata_resources.completed_at) }}</p>
                            </div>
                            <div v-if="resourceCount > 0">
                                <p class="text-gray-400">Files Found</p>
                                <p class="font-medium text-gray-700">{{ resourceCount }}</p>
                            </div>
                        </div>

                        <div v-if="metadata_resources?.error_message" class="mt-2 text-sm text-red-500">
                            {{ metadata_resources.error_message }}
                        </div>

                        <!-- Indexing Progress Bar -->
                        <div v-if="isReindexing" class="mt-3 space-y-1">
                            <div class="flex items-center justify-between text-xs text-gray-500">
                                <span>{{ indexingPhase || 'Indexing...' }}</span>
                                <span>{{ indexingProgress }}%</span>
                            </div>
                            <UProgress :value="indexingProgress" size="sm" color="blue" />
                        </div>
                    </div>

                    <!-- Settings -->
                    <div class="space-y-3">
                        <h4 class="text-xs font-medium text-gray-400 uppercase tracking-wider">Instruction Settings</h4>
                        
                        <div class="flex items-center justify-between py-2">
                            <div>
                                <p class="text-sm text-gray-700">Auto-publish</p>
                                <p class="text-xs text-gray-400">Publish automatically</p>
                            </div>
                            <UToggle color="blue" v-model="editSettings.autoPublish" size="sm" @change="updateSettings" />
                        </div>

                        <div>
                            <p class="text-sm text-gray-700 mb-1">Load Mode</p>
                            <USelectMenu
                                v-model="editSettings.defaultLoadMode"
                                :options="loadModeOptions"
                                value-attribute="value"
                                option-attribute="label"
                                size="sm"
                                class="w-full"
                                color="blue"
                                :ui="{ option: { base: 'text-sm', active: 'text-sm', inactive: 'text-sm' } }"
                                @change="updateSettings"
                            />
                        </div>
                    </div>
                </div>

                <!-- Step 1: Connection Details -->
                <div v-else-if="currentStep === 1" class="space-y-4">
                    <!-- Git Provider -->
                    <div>
                        <label class="text-xs font-medium text-gray-400 uppercase tracking-wider">Provider</label>
                        <div class="grid grid-cols-4 gap-2 mt-2">
                            <button 
                                v-for="provider in gitProviders" 
                                :key="provider.type"
                                @click="selectProvider(provider)" 
                                type="button"
                                class="p-3 rounded border text-sm flex flex-col items-center gap-1.5 transition-colors"
                                :class="selectedProvider === provider.type 
                                    ? 'border-blue-500 bg-blue-50 text-blue-700' 
                                    : 'border-gray-200 bg-white text-gray-500 hover:bg-gray-50'"
                            >
                                <UIcon :name="provider.icon" class="w-5 h-5" />
                                <span>{{ provider.name }}</span>
                            </button>
                        </div>
                    </div>

                    <div v-if="selectedProvider" class="space-y-4">
                        <!-- Custom Host -->
                        <div v-if="selectedProvider === 'custom'">
                            <label class="text-xs font-medium text-gray-400 uppercase tracking-wider">Custom Host</label>
                            <input 
                                v-model="formData.customHost"
                                type="text"
                                placeholder="git.customdomain.com"
                                class="mt-1.5 border border-gray-200 rounded px-3 py-2 w-full text-sm focus:outline-none focus:border-blue-500"
                            />
                        </div>

                        <!-- Repository URL -->
                        <div>
                            <label class="text-xs font-medium text-gray-400 uppercase tracking-wider">Repository URL</label>
                            <div class="flex gap-2 mt-1.5">
                                <input 
                                    v-model="formData.repoUrl"
                                    type="text"
                                    placeholder="git@github.com:user/repo.git"
                                    class="flex-1 border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                                />
                                <input 
                                    v-model="formData.branch"
                                    type="text"
                                    placeholder="main"
                                    class="w-24 border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                                />
                            </div>
                        </div>

                        <!-- SSH Key -->
                        <div>
                            <label class="text-xs font-medium text-gray-400 uppercase tracking-wider">SSH Private Key <span class="text-gray-300 font-normal">(optional for public repos)</span></label>
                            <UTextarea
                                v-model="formData.privateKey"
                                color="blue"
                                :rows="3"
                                size="sm"
                                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                                class="mt-1.5 w-full font-mono"
                                :ui="{ base: 'bg-gray-50 text-sm' }"
                            />
                        </div>
                    </div>

                    <!-- Connection Status -->
                    <div v-if="connectionStatus" class="rounded-lg p-3 text-sm" :class="connectionStatus.success ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'">
                        <div class="flex items-start gap-2">
                            <UIcon 
                                :name="connectionStatus.success ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" 
                                class="w-4 h-4 mt-0.5 flex-shrink-0" 
                                :class="connectionStatus.success ? 'text-green-500' : 'text-red-500'"
                            />
                            <span :class="connectionStatus.success ? 'text-green-700' : 'text-red-700'">
                                {{ connectionStatus.message }}
                            </span>
                        </div>
                    </div>
                </div>

                <!-- Step 2: Instruction Settings -->
                <div v-else-if="currentStep === 2" class="space-y-4">
                    <div class="text-center py-3">
                        <UIcon name="i-heroicons-check-circle" class="w-10 h-10 text-green-500 mx-auto" />
                        <p class="text-sm font-medium text-gray-900 mt-2">Connection Successful</p>
                        <p v-if="displayFileCount" class="text-sm text-gray-500">Found {{ displayFileCount }} files</p>
                    </div>

                    <div class="space-y-4">
                        <h4 class="text-xs font-medium text-gray-400 uppercase tracking-wider">Instruction Settings</h4>
                        
                        <div class="flex items-center justify-between py-2 border-b border-gray-100">
                            <div>
                                <p class="text-sm text-gray-700">Auto-publish instructions</p>
                            </div>
                            <UToggle color="blue" v-model="formData.autoPublish" size="sm" />
                        </div>

                        <div>
                            <p class="text-sm text-gray-700 mb-1.5">Default Load Mode</p>
                            <USelectMenu
                                v-model="formData.defaultLoadMode"
                                :options="loadModeOptions"
                                value-attribute="value"
                                option-attribute="label"
                                size="sm"
                                class="w-full"
                                color="blue"
                                :ui="{ option: { base: 'text-sm', active: 'text-sm', inactive: 'text-sm' } }"
                            />
                        </div>
                    </div>
                </div>

                <!-- Step 3: Indexing -->
                <div v-else-if="currentStep === 3" class="space-y-4">
                    <div class="text-center py-4">
                        <div v-if="isIndexing || isReindexing" class="space-y-3">
                            <UIcon name="i-heroicons-arrow-path" class="w-10 h-10 text-blue-500 mx-auto animate-spin" />
                            <p class="text-sm font-medium text-gray-900">Indexing Repository...</p>
                            <div class="px-8">
                                <div class="flex items-center justify-between text-xs text-gray-500 mb-1">
                                    <span>{{ indexingPhase || 'Processing...' }}</span>
                                    <span>{{ indexingProgress }}%</span>
                                </div>
                                <UProgress :value="indexingProgress" size="sm" color="blue" />
                            </div>
                        </div>
                        <div v-else class="space-y-2">
                            <UIcon name="i-heroicons-check-circle" class="w-10 h-10 text-green-500 mx-auto" />
                            <p class="text-sm font-medium text-gray-900">Repository Connected</p>
                            <p class="text-sm text-gray-500">Indexing complete</p>
                        </div>
                    </div>
                </div>
            </div>

            <template #footer>
                <div class="flex items-center justify-between">
                    <!-- Left side -->
                    <div>
                        <UButton 
                            v-if="showDataSourceList && selectedDsForConnection" 
                            color="gray" 
                            variant="ghost" 
                            size="sm"
                            @click="goBackToDataSourceList"
                        >
                            Back
                        </UButton>
                        <UButton 
                            v-else-if="!connectedRepo && !showDataSourceList && currentStep > 1" 
                            color="gray" 
                            variant="ghost" 
                            size="sm"
                            @click="currentStep--"
                        >
                            Back
                        </UButton>
                    </div>

                    <!-- Right side -->
                    <div class="flex gap-2">
                        <!-- Data source list actions -->
                        <template v-if="showDataSourceList && !selectedDsForConnection">
                            <UButton color="gray" variant="ghost" size="sm" @click="gitModalOpen = false">Close</UButton>
                        </template>

                        <!-- Connected repo actions -->
                        <template v-else-if="connectedRepo">
                            <UButton
                                color="blue"
                                variant="soft"
                                size="sm"
                                :loading="isReindexing"
                                :disabled="isIndexing"
                                @click="reindexRepository"
                            >
                                Sync Git
                            </UButton>
                            <UButton color="gray" variant="soft" size="sm" @click="gitModalOpen = false">Close</UButton>
                        </template>

                        <!-- Step 1 actions -->
                        <template v-else-if="currentStep === 1">
                            <UButton color="gray" variant="soft" size="sm" @click="gitModalOpen = false">Cancel</UButton>
                            <UButton 
                                color="blue" 
                                size="sm"
                                :loading="isLoading"
                                :disabled="!canTestConnection"
                                @click="testAndProceed"
                            >
                                {{ isLoading ? 'Testing...' : 'Test & Continue' }}
                            </UButton>
                        </template>

                        <!-- Step 2 actions -->
                        <template v-else-if="currentStep === 2">
                            <UButton color="gray" variant="soft" size="sm" @click="gitModalOpen = false">Cancel</UButton>
                            <UButton 
                                color="blue" 
                                size="sm"
                                :loading="isLoading"
                                @click="saveAndIndex"
                            >
                                Connect & Index
                            </UButton>
                        </template>

                        <!-- Step 3 actions -->
                        <template v-else-if="currentStep === 3">
                            <UButton color="blue" size="sm" @click="finishWizard">Done</UButton>
                        </template>
                    </div>
                </div>
            </template>
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface ConnectionStatus {
    success: boolean
    message: string
    fileCount?: number
}

interface GitRepository {
    id: string
    provider: string
    repo_url: string
    branch: string
    custom_host?: string
    last_indexed?: string
    last_commit?: string
    auto_publish?: boolean
    default_load_mode?: string
}

interface DataSourceWithGit {
    id: string
    name: string
    type: string
    git_repository?: GitRepository | null
    metadata_resources?: any
}

const props = defineProps<{
    modelValue: boolean
    datasourceId?: string  // Now optional - if not provided, show data source list
    gitRepository?: GitRepository
    metadataResources?: any
}>()

const emit = defineEmits(['update:modelValue', 'changed'])

const gitModalOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

const toast = useToast()

// Data source list state (when no datasourceId provided)
const loadingDataSources = ref(false)
const dataSources = ref<DataSourceWithGit[]>([])
const selectedDsForConnection = ref<DataSourceWithGit | null>(null)

// Computed: determine if we should show data source list
const showDataSourceList = computed(() => {
    // Show list if no datasourceId prop AND no data source selected yet
    return !props.datasourceId && !selectedDsForConnection.value
})

// Active datasource ID (from props or selected)
const activeDatasourceId = computed(() => {
    return props.datasourceId || selectedDsForConnection.value?.id || ''
})

// Active git repository (from props or selected data source)
const activeGitRepository = computed(() => {
    if (props.datasourceId) return props.gitRepository
    return selectedDsForConnection.value?.git_repository || undefined
})

// Active metadata resources
const activeMetadataResources = computed(() => {
    if (props.datasourceId) return props.metadataResources
    return selectedDsForConnection.value?.metadata_resources || undefined
})

// State
const currentStep = ref(1)
const isLoading = ref(false)
const isReindexing = ref(false)
const connectionStatus = ref<ConnectionStatus | null>(null)
const showDeleteConfirmation = ref(false)
const linkedInstructionCount = ref(0)
const isLoadingCount = ref(false)
const detectedFileCount = ref<number | null>(null)
const justSaved = ref(false) // Track if we just saved to preserve form settings

// Progress tracking for indexing
const indexingProgress = ref(0)
const indexingPhase = ref('')
let pollInterval: ReturnType<typeof setInterval> | null = null

// Providers
const gitProviders = [
    { type: 'github', name: 'GitHub', icon: 'logos:github-icon' },
    { type: 'gitlab', name: 'GitLab', icon: 'logos:gitlab' },
    { type: 'bitbucket', name: 'Bitbucket', icon: 'logos:bitbucket' },
    { type: 'custom', name: 'Custom', icon: 'i-heroicons-server' },
]

const selectedProvider = ref<string | null>(null)
const formData = ref({
    customHost: '',
    repoUrl: '',
    branch: 'main',
    privateKey: '',
    autoPublish: true,
    defaultLoadMode: 'auto',
})

const loadModeOptions = [
    { value: 'auto', label: 'Auto - Markdown always, others smart' },
    { value: 'intelligent', label: 'Smart - Load based on search relevance' },
    { value: 'always', label: 'Always - Always include in context' },
    { value: 'disabled', label: 'Disabled - Never include automatically' },
]

const stepDescriptions: Record<number, string> = {
    1: 'Enter your repository details',
    2: 'Configure instruction settings',
    3: 'Indexing your repository',
}

// Computed
const connectedRepo = computed(() => activeGitRepository.value)
const metadata_resources = computed(() => activeMetadataResources.value || {})

// Header text
const headerTitle = computed(() => {
    if (showDataSourceList.value) return 'Git Repositories'
    if (connectedRepo.value) return 'Git Repository'
    return 'Connect Git Repository'
})

const headerSubtitle = computed(() => {
    if (showDataSourceList.value) return 'Connect Git repositories to sync dbt models, documentation, and other metadata as instructions.'
    if (connectedRepo.value) return 'Manage your repository connection'
    return stepDescriptions[currentStep.value]
})

const canTestConnection = computed(() => {
    return selectedProvider.value && formData.value.repoUrl
})

const isIndexing = computed(() => {
    const status = metadata_resources.value?.status
    return ['pending', 'indexing', 'running'].includes(status)
})

const statusText = computed(() => {
    const status = metadata_resources.value?.status
    if (isIndexing.value) return 'Indexing...'
    if (status === 'completed') return 'Indexed'
    if (status === 'failed') return 'Failed'
    return 'Pending'
})

const statusClass = computed(() => {
    const status = metadata_resources.value?.status
    if (isIndexing.value) return 'text-blue-600'
    if (status === 'completed') return 'text-green-600'
    if (status === 'failed') return 'text-red-600'
    return 'text-gray-600'
})

const resourceCount = computed(() => {
    const resources = metadata_resources.value?.resources || []
    return resources.length
})

// Use detected file count or fall back to resource count if available
const displayFileCount = computed(() => {
    if (detectedFileCount.value) return detectedFileCount.value
    if (resourceCount.value > 0) return resourceCount.value
    return null
})

// Edit settings for connected repo
const editSettings = ref({
    autoPublish: false,
    defaultLoadMode: 'auto'
})

// Watch for connected repo changes
watch(connectedRepo, (repo) => {
    if (repo) {
        // If we just saved, use form values (they're more up-to-date than what backend might return)
        if (justSaved.value) {
            editSettings.value.autoPublish = formData.value.autoPublish
            editSettings.value.defaultLoadMode = formData.value.defaultLoadMode
            justSaved.value = false
        } else {
            editSettings.value.autoPublish = repo.auto_publish ?? false
            editSettings.value.defaultLoadMode = repo.default_load_mode ?? 'auto'
        }
    }
}, { immediate: true })

// Reset wizard when modal opens
watch(gitModalOpen, async (open) => {
    if (open) {
        // Reset selection state
        selectedDsForConnection.value = null
        
        // Reset indexing state
        isReindexing.value = false
        indexingProgress.value = 0
        indexingPhase.value = ''
        stopPolling()
        
        // If no datasourceId provided, fetch data sources
        if (!props.datasourceId) {
            await fetchDataSources()
        }
        
        // Reset wizard state
        if (!connectedRepo.value) {
            currentStep.value = 1
            connectionStatus.value = null
            selectedProvider.value = null
            formData.value = {
                customHost: '',
                repoUrl: '',
                branch: 'main',
                privateKey: '',
                autoPublish: true,
                defaultLoadMode: 'auto',
            }
        }
    } else {
        // Modal closing - stop polling
        stopPolling()
    }
})

// Fetch data sources when no specific datasourceId is provided
async function fetchDataSources() {
    loadingDataSources.value = true
    dataSources.value = []
    
    try {
        // Fetch active data sources
        const { data: sourcesData, error: sourcesError } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET' })
        
        if (sourcesError.value || !sourcesData.value) {
            console.error('Failed to fetch data sources:', sourcesError.value)
            return
        }
        
        const sources = sourcesData.value
        if (sources.length === 0) return

        // Fetch full data source details for each
        const enriched: DataSourceWithGit[] = []
        
        for (const ds of sources) {
            const dsWithGit: DataSourceWithGit = {
                id: ds.id,
                name: ds.name,
                type: ds.type,
                git_repository: null,
                metadata_resources: null
            }

            // Fetch full data source object which includes git_repository
            const { data: fullDs, error: dsError } = await useMyFetch(`/data_sources/${ds.id}`, { method: 'GET' })
            if (!dsError.value && fullDs.value) {
                const fullData = fullDs.value as any
                if (fullData.git_repository) {
                    dsWithGit.git_repository = fullData.git_repository as GitRepository
                }
            }

            // Fetch metadata info  
            const { data: metaData, error: metaError } = await useMyFetch(`/data_sources/${ds.id}/metadata_resources`, { method: 'GET' })
            if (!metaError.value && metaData.value) {
                dsWithGit.metadata_resources = metaData.value
            }

            enriched.push(dsWithGit)
        }

        dataSources.value = enriched
        
        // Auto-select if there's only one data source
        if (enriched.length === 1) {
            selectDataSource(enriched[0])
        }
    } catch (e) {
        console.error('Failed to fetch data sources:', e)
        toast.add({ title: 'Failed to load data sources', color: 'red' })
    } finally {
        loadingDataSources.value = false
    }
}

// Select a data source to configure
function selectDataSource(ds: DataSourceWithGit) {
    selectedDsForConnection.value = ds
    
    // Reset wizard state for new connection
    if (!ds.git_repository) {
        currentStep.value = 1
        connectionStatus.value = null
        selectedProvider.value = null
        formData.value = {
            customHost: '',
            repoUrl: '',
            branch: 'main',
            privateKey: '',
            autoPublish: true,
            defaultLoadMode: 'auto',
        }
    }
}

// Go back to data source list
function goBackToDataSourceList() {
    selectedDsForConnection.value = null
    currentStep.value = 1
    connectionStatus.value = null
}

// Format repo name from URL
function formatRepoName(url: string) {
    const tail = url.split('/').pop() || ''
    return tail.replace(/\.git$/, '') || 'Repository'
}

// Methods
function selectProvider(provider: { type: string }) {
    selectedProvider.value = provider.type
}

function getProviderIcon(provider: string) {
    const found = gitProviders.find(p => p.type === provider)
    return found?.icon || 'i-heroicons-server'
}

function formatDate(dateStr: string) {
    return new Date(dateStr).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    })
}

async function testAndProceed() {
    isLoading.value = true
    connectionStatus.value = null
    detectedFileCount.value = null

    try {
        const response = await useMyFetch(`/data_sources/${activeDatasourceId.value}/git_repository/test`, {
            method: 'POST',
            body: {
                provider: selectedProvider.value,
                custom_host: formData.value.customHost,
                repo_url: formData.value.repoUrl,
                branch: formData.value.branch,
                ssh_key: formData.value.privateKey,
            }
        })

        if (response.error.value) {
            const errorMessage = (response.error.value as any)?.data?.detail || 
                               (response.error.value as any)?.message || 
                               'Failed to connect to repository'
            connectionStatus.value = { success: false, message: errorMessage }
        } else {
            const data = response.data.value as any
            const success = data?.success ?? false
            connectionStatus.value = {
                success,
                message: success ? 'Connection successful!' : (data?.message || 'Connection failed'),
                fileCount: data?.file_count
            }
            
            // Store file count if returned
            if (data?.file_count) {
                detectedFileCount.value = data.file_count
            }
            
            if (success) {
                // Move to next step after short delay
                setTimeout(() => {
                    currentStep.value = 2
                }, 500)
            }
        }
    } catch (error) {
        connectionStatus.value = { success: false, message: 'Failed to connect to repository' }
    } finally {
        isLoading.value = false
    }
}

async function saveAndIndex() {
    isLoading.value = true
    isReindexing.value = true
    indexingProgress.value = 0
    indexingPhase.value = 'starting'
    
    try {
        const response = await useMyFetch(`/data_sources/${activeDatasourceId.value}/git_repository`, {
            method: 'POST',
            body: {
                provider: selectedProvider.value,
                custom_host: formData.value.customHost,
                repo_url: formData.value.repoUrl,
                branch: formData.value.branch,
                ssh_key: formData.value.privateKey,
                auto_publish: formData.value.autoPublish,
                default_load_mode: formData.value.defaultLoadMode,
            }
        })

        if ((response.status as any).value === 'success') {
            justSaved.value = true // Preserve form settings when connectedRepo updates
            currentStep.value = 3
            emit('changed')
            // Start polling for indexing progress
            setTimeout(() => startPolling(), 500) // Small delay to let backend start
        } else {
            toast.add({ title: 'Failed to save repository', color: 'red' })
            isReindexing.value = false
        }
    } catch (error) {
        toast.add({ title: 'Failed to save repository', color: 'red' })
        isReindexing.value = false
    } finally {
        isLoading.value = false
    }
}

function finishWizard() {
    gitModalOpen.value = false
    emit('changed')
}

async function updateSettings() {
    if (!connectedRepo.value?.id) return
    
    try {
        await useMyFetch(`/data_sources/${activeDatasourceId.value}/git_repository/${connectedRepo.value.id}`, {
            method: 'PUT',
            body: {
                auto_publish: editSettings.value.autoPublish,
                default_load_mode: editSettings.value.defaultLoadMode
            }
        })
        toast.add({ title: 'Settings updated', color: 'green' })
        emit('changed')
    } catch (error) {
        toast.add({ title: 'Failed to update settings', color: 'red' })
    }
}

async function confirmDelete() {
    if (!connectedRepo.value?.id) return
    
    isLoadingCount.value = true
    try {
        const { data, error } = await useMyFetch<{ instruction_count: number }>(
            `/data_sources/${activeDatasourceId.value}/git_repository/${connectedRepo.value.id}/linked_instructions_count`,
            { method: 'GET' }
        )
        
        if (error.value) {
            toast.add({ title: 'Failed to check linked instructions', color: 'red' })
            return
        }
        
        linkedInstructionCount.value = data.value?.instruction_count || 0
        showDeleteConfirmation.value = true
    } finally {
        isLoadingCount.value = false
    }
}

async function executeDelete() {
    if (!connectedRepo.value?.id) return
    
    isLoading.value = true
    try {
        const { error } = await useMyFetch(`/data_sources/${activeDatasourceId.value}/git_repository/${connectedRepo.value.id}`, {
            method: 'DELETE'
        })
        
        if (error.value) {
            const errorMessage = (error.value as any)?.data?.detail || 'Failed to disconnect'
            toast.add({ title: errorMessage, color: 'red' })
            return
        }
        
        toast.add({ title: 'Repository disconnected', color: 'green' })
        showDeleteConfirmation.value = false
        emit('changed')
        
        // If we're in data source list mode, refresh and go back to list
        if (!props.datasourceId && selectedDsForConnection.value) {
            await fetchDataSources()
            selectedDsForConnection.value = null
        } else {
            gitModalOpen.value = false
        }
    } finally {
        isLoading.value = false
    }
}

async function reindexRepository() {
    if (!connectedRepo.value?.id) return
    
    isReindexing.value = true
    indexingProgress.value = 0
    indexingPhase.value = 'starting'
    
    try {
        const response = await useMyFetch(`/data_sources/${activeDatasourceId.value}/git_repository/${connectedRepo.value.id}/index`, {
            method: 'POST'
        })
        
        if ((response.status as any).value === 'success') {
            toast.add({ title: 'Reindexing started', color: 'green' })
            startPolling()
        }
    } catch (error) {
        toast.add({ title: 'Failed to reindex', color: 'red' })
        isReindexing.value = false
    }
}

async function pollJobStatus() {
    if (!connectedRepo.value?.id || !activeDatasourceId.value) return
    
    try {
        const { data } = await useMyFetch<{
            status: string
            phase: string | null
            progress: number
            processed_files: number
            total_files: number
            error_message: string | null
        }>(`/data_sources/${activeDatasourceId.value}/git_repository/${connectedRepo.value.id}/job_status`, {
            key: `job-status-${Date.now()}` // Prevent caching
        })
        
        if (!data.value) return
        
        const jobData = data.value
        
        // Handle different phases
        const phase = jobData.phase || ''
        const status = jobData.status || ''
        
        if (phase === 'parsing' || (status === 'running' && !jobData.total_files)) {
            indexingPhase.value = 'Parsing files...'
            indexingProgress.value = 15 // Show some progress during parsing
        } else if (phase === 'syncing') {
            const processed = jobData.processed_files || 0
            const total = jobData.total_files || 0
            indexingPhase.value = total > 0 ? `Syncing ${processed}/${total}` : 'Syncing...'
            indexingProgress.value = jobData.progress || 0
        } else if (phase === 'completed' || status === 'completed') {
            indexingPhase.value = 'Completed'
            indexingProgress.value = 100
        } else if (status === 'running') {
            indexingPhase.value = phase || 'Processing...'
            indexingProgress.value = Math.max(jobData.progress || 0, 5) // At least show some progress
        } else {
            indexingPhase.value = phase || 'Starting...'
            indexingProgress.value = jobData.progress || 0
        }
        
        if (jobData.status === 'completed') {
            indexingProgress.value = 100
            indexingPhase.value = 'Completed'
            stopPolling()
            // Keep showing progress for a moment before hiding
            setTimeout(() => {
                isReindexing.value = false
                emit('changed')
            }, 1500)
            toast.add({ title: 'Indexing completed', color: 'green' })
        } else if (jobData.status === 'failed') {
            stopPolling()
            isReindexing.value = false
            toast.add({ title: jobData.error_message || 'Indexing failed', color: 'red' })
        }
    } catch (error) {
        console.error('Failed to poll job status:', error)
    }
}

function startPolling() {
    stopPolling() // Clear any existing interval
    // Poll immediately, then every 1 second
    pollJobStatus()
    pollInterval = setInterval(pollJobStatus, 1000)
}

function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval)
        pollInterval = null
    }
}

// Cleanup on unmount
onUnmounted(() => {
    stopPolling()
})
</script>
